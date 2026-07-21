"""PDF text extractor using Docling for layout-aware document understanding.

Docling runs in persistent isolated subprocesses so native-library hangs can be
terminated without leaking resources into the pipeline process. PyMuPDF is the
bounded fallback for document-specific conversion failures.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal

from eurlex_builder.errors import DoclingStartupError
from eurlex_builder.extractors.splitter import (
    _find_quoted_regions,
    _is_in_quoted_region,
    split_article,
)
from eurlex_builder.utils import strip_boilerplate

logger = logging.getLogger("eurlex_builder")

# Timeout for a single PDF conversion (seconds).
_PDF_TIMEOUT = 120
_WORKER_START_TIMEOUT = 180
_DOCLING_SIZE_LIMIT = 50 * 1024 * 1024
_WORKER_JOIN_TIMEOUT = 5
_CONTROL_MESSAGE_LIMIT = 1024 * 1024
_FORCED_OUTER_ARTICLE = "\ue000outer-article:"

_FailureReason = Literal[
    "timeout",
    "partial",
    "conversion",
    "crash",
    "protocol",
    "startup",
    "oversize",
    "empty",
]


@dataclass(frozen=True)
class _DoclingResult:
    markdown: str | None = None
    failure_reason: _FailureReason | None = None
    error: str | None = None


def _conversion_result_error(result) -> str | None:
    errors = [error.error_message for error in result.errors]
    status = getattr(result.status, "value", str(result.status))
    if status == "success" and not errors:
        return None
    return "; ".join(errors) or status


class _DoclingWorkerClient:
    """Parent-side lifecycle manager for one persistent Docling process."""

    def __init__(
        self,
        *,
        worker_module: str = "eurlex_builder.extractors.docling_process",
        startup_timeout: float = _WORKER_START_TIMEOUT,
        conversion_timeout: float = _PDF_TIMEOUT,
    ) -> None:
        self._worker_module = worker_module
        self._startup_timeout = startup_timeout
        self._conversion_timeout = conversion_timeout
        self._process: subprocess.Popen[str] | None = None
        self._diagnostics_stream: IO[str] | None = None
        self._diagnostics_path: str | None = None
        self._request_id = 0
        self._control_buffer = bytearray()
        self._lifecycle_lock = threading.RLock()
        self.closed = False

    def _diagnostic_tail(self) -> str:
        diagnostics_path = self._diagnostics_path
        if not diagnostics_path:
            return ""
        try:
            with open(diagnostics_path, "rb") as diagnostics:
                diagnostics.seek(0, os.SEEK_END)
                size = diagnostics.tell()
                diagnostics.seek(max(0, size - 8192))
                return diagnostics.read().decode("utf-8", errors="replace").strip()
        except OSError:
            return ""

    def _clear_diagnostics(self) -> None:
        with self._lifecycle_lock:
            diagnostics_stream = self._diagnostics_stream
            diagnostics_path = self._diagnostics_path
            if diagnostics_stream is not None:
                with contextlib.suppress(OSError, ValueError):
                    diagnostics_stream.flush()
            if diagnostics_path:
                with contextlib.suppress(OSError):
                    os.truncate(diagnostics_path, 0)

    def _wait_for_message(
        self,
        process: subprocess.Popen[str],
        timeout: float,
    ) -> list:
        stdout = process.stdout
        if stdout is None:
            raise OSError("Docling worker stdout is unavailable")
        deadline = time.monotonic() + timeout
        selector = selectors.DefaultSelector()
        try:
            try:
                selector.register(stdout, selectors.EVENT_READ)
            except (OSError, ValueError) as exc:
                raise OSError("Docling worker control pipe was closed") from exc
            while True:
                newline = self._control_buffer.find(b"\n")
                if newline >= 0:
                    raw_line = bytes(self._control_buffer[:newline])
                    del self._control_buffer[:newline + 1]
                    try:
                        message = json.loads(raw_line)
                    except json.JSONDecodeError as exc:
                        raise OSError(
                            f"invalid Docling worker response: {raw_line!r}"
                        ) from exc
                    if not isinstance(message, list):
                        raise OSError(
                            f"invalid Docling worker response: {message!r}"
                        )
                    return message
                with self._lifecycle_lock:
                    if self.closed or self._process is not process:
                        raise OSError("Docling worker was closed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Docling worker deadline exceeded")
                try:
                    ready = selector.select(min(0.25, remaining))
                except (OSError, ValueError) as exc:
                    raise OSError("Docling worker control pipe was closed") from exc
                if not ready:
                    continue
                try:
                    chunk = os.read(stdout.fileno(), 65536)
                except (OSError, ValueError) as exc:
                    raise OSError("Docling worker control pipe was closed") from exc
                if not chunk:
                    raise EOFError("Docling worker closed its control pipe")
                self._control_buffer.extend(chunk)
                if len(self._control_buffer) > _CONTROL_MESSAGE_LIMIT:
                    raise OSError("Docling worker control message exceeded 1 MiB")
        finally:
            selector.close()

    def _stop(self, *, graceful: bool = False) -> None:
        with self._lifecycle_lock:
            process = self._process
            self._process = None
            self._control_buffer.clear()

            if process is not None:
                if graceful and process.poll() is None and process.stdin is not None:
                    with contextlib.suppress(BrokenPipeError, OSError, ValueError):
                        process.stdin.write('["shutdown"]\n')
                        process.stdin.flush()
                if process.stdin is not None:
                    with contextlib.suppress(BrokenPipeError, OSError, ValueError):
                        process.stdin.close()
                try:
                    process.wait(timeout=2 if graceful else 0)
                except subprocess.TimeoutExpired:
                    pass
                if process.poll() is None:
                    self._signal_process(process, signal.SIGTERM)
                    try:
                        process.wait(timeout=_WORKER_JOIN_TIMEOUT)
                    except subprocess.TimeoutExpired:
                        pass
                if process.poll() is None:
                    self._signal_process(process, signal.SIGKILL)
                    try:
                        process.wait(timeout=_WORKER_JOIN_TIMEOUT)
                    except subprocess.TimeoutExpired:
                        logger.critical(
                            "Docling worker %s could not be reaped after SIGKILL",
                            process.pid,
                        )
                if process.stdout is not None:
                    with contextlib.suppress(OSError, ValueError):
                        process.stdout.close()

            if self._diagnostics_stream is not None:
                self._diagnostics_stream.close()
                self._diagnostics_stream = None

            if self._diagnostics_path:
                Path(self._diagnostics_path).unlink(missing_ok=True)
                self._diagnostics_path = None

    @staticmethod
    def _signal_process(process: subprocess.Popen[str], sig: signal.Signals) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, sig)
            elif sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        except ProcessLookupError:
            pass

    def _start(self) -> None:
        with self._lifecycle_lock:
            self._stop()
            fd, diagnostics_path = tempfile.mkstemp(
                prefix="eurlex-docling-", suffix=".log"
            )
            os.close(fd)
            self._diagnostics_path = diagnostics_path
            diagnostics_stream = open(
                diagnostics_path, "a", encoding="utf-8", buffering=1,
            )
            self._diagnostics_stream = diagnostics_stream
            environment = os.environ.copy()
            environment.update({
                "OMP_NUM_THREADS": "2",
                "MKL_NUM_THREADS": "2",
                "VECLIB_MAXIMUM_THREADS": "2",
                "TOKENIZERS_PARALLELISM": "false",
            })
            try:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        self._worker_module,
                        diagnostics_path,
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=diagnostics_stream,
                    text=True,
                    bufsize=1,
                    env=environment,
                    start_new_session=True,
                )
            except Exception:
                diagnostics_stream.close()
                self._diagnostics_stream = None
                self._diagnostics_path = None
                Path(diagnostics_path).unlink(missing_ok=True)
                raise
            self._process = process
            self._control_buffer.clear()

        try:
            message = self._wait_for_message(process, self._startup_timeout)
        except (EOFError, OSError, TimeoutError) as exc:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=0.5)
            exitcode = process.poll()
            detail = self._diagnostic_tail()
            self._stop()
            raise RuntimeError(
                f"Docling worker exited during initialization ({exitcode=}): "
                f"{detail or exc}"
            ) from exc
        if not message or message[0] != "ready":
            detail = message[1] if message and len(message) > 1 else "invalid response"
            diagnostics = self._diagnostic_tail()
            self._stop()
            raise RuntimeError(
                f"Docling worker initialization failed: {detail}"
                + (f"; diagnostics: {diagnostics}" if diagnostics else "")
            )
        _record_startup_success()

    def convert(self, pdf_path: str) -> _DoclingResult:
        if self.closed:
            raise RuntimeError("Docling worker client is closed")
        process = self._process
        if process is None or process.poll() is not None:
            try:
                self._start()
            except Exception as exc:
                return _DoclingResult(
                    failure_reason="startup", error=f"{type(exc).__name__}: {exc}"
                )
            process = self._process
            if process is None:
                return _DoclingResult(
                    failure_reason="startup",
                    error="Docling worker did not become available",
                )
        self._request_id += 1
        request_id = self._request_id
        fd, output_path = tempfile.mkstemp(prefix="eurlex-docling-", suffix=".md")
        os.close(fd)
        try:
            self._clear_diagnostics()
            try:
                if process.stdin is None:
                    raise BrokenPipeError("worker stdin is unavailable")
                process.stdin.write(json.dumps([
                    "convert", request_id, pdf_path, output_path,
                ]) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                exitcode = process.poll()
                diagnostics = self._diagnostic_tail()
                self._stop()
                return _DoclingResult(
                    failure_reason="crash",
                    error=f"worker send failed ({exitcode=}): {diagnostics or exc}",
                )

            try:
                message = self._wait_for_message(process, self._conversion_timeout)
            except TimeoutError:
                diagnostics = self._diagnostic_tail()
                self._stop()
                return _DoclingResult(
                    failure_reason="timeout",
                    error=diagnostics or "conversion deadline exceeded",
                )
            except (EOFError, OSError) as exc:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=0.5)
                exitcode = process.poll()
                diagnostics = self._diagnostic_tail()
                self._stop()
                return _DoclingResult(
                    failure_reason="crash",
                    error=f"worker pipe closed ({exitcode=}): {diagnostics or exc}",
                )
            if (
                not message
                or len(message) != 4
                or message[0] != "result"
                or message[1] != request_id
            ):
                self._stop()
                return _DoclingResult(
                    failure_reason="protocol", error=f"invalid worker response: {message!r}"
                )

            _, _, status, detail = message
            if status == "ok":
                try:
                    markdown = Path(output_path).read_text(encoding="utf-8")
                except OSError as exc:
                    self._stop()
                    return _DoclingResult(
                        failure_reason="protocol",
                        error=f"worker output unavailable: {exc}",
                    )
                return _DoclingResult(markdown=markdown)

            reason: _FailureReason = (
                "partial" if status == "partial" else "conversion"
            )
            # A native page failure may leave converter state unsafe even when
            # the child can still answer, so any non-success recycles it.
            self._stop()
            return _DoclingResult(failure_reason=reason, error=detail)
        finally:
            Path(output_path).unlink(missing_ok=True)

    def close(self) -> None:
        with self._lifecycle_lock:
            if self.closed:
                return
            self.closed = True
            self._stop(graceful=True)

    def abort(self) -> None:
        with self._lifecycle_lock:
            if self.closed:
                return
            self.closed = True
            self._stop(graceful=False)


_worker_local = threading.local()
_worker_registry: set[_DoclingWorkerClient] = set()
_worker_registry_lock = threading.Lock()
_worker_shutdown = threading.Event()
_startup_failure_lock = threading.Lock()
_startup_failures = 0
_startup_circuit_error: str | None = None


def _record_startup_failure(error: str) -> bool:
    global _startup_failures, _startup_circuit_error
    with _startup_failure_lock:
        _startup_failures += 1
        if _startup_failures >= 3:
            _startup_circuit_error = error
        return _startup_circuit_error is not None


def _record_startup_success() -> None:
    global _startup_failures, _startup_circuit_error
    with _startup_failure_lock:
        _startup_failures = 0
        _startup_circuit_error = None


def _get_startup_circuit_error() -> str | None:
    with _startup_failure_lock:
        return _startup_circuit_error


def _get_docling_worker() -> _DoclingWorkerClient:
    with _worker_registry_lock:
        if _worker_shutdown.is_set():
            raise RuntimeError("Docling workers are shutting down")
        startup_error = _get_startup_circuit_error()
        if startup_error is not None:
            raise DoclingStartupError(
                f"Docling startup circuit is open: {startup_error}",
                fatal=True,
            )
        worker = getattr(_worker_local, "docling_worker", None)
        if worker is None or worker.closed:
            worker = _DoclingWorkerClient()
            _worker_registry.add(worker)
            _worker_local.docling_worker = worker
    return worker


def enable_docling_workers() -> None:
    _worker_shutdown.clear()
    _record_startup_success()


def close_all_docling_workers(*, force: bool = False) -> None:
    if force:
        _worker_shutdown.set()
    with _worker_registry_lock:
        workers = list(_worker_registry)
        _worker_registry.clear()
    for worker in workers:
        worker.abort() if force else worker.close()


def _pymupdf_text_from_path(pdf_path: str, celex_id: str) -> str | None:
    try:
        import pymupdf
    except ImportError:
        logger.warning("pymupdf not installed — cannot extract %s", celex_id)
        return None

    try:
        doc = pymupdf.open(pdf_path)
        parts: list[str] = []
        try:
            for page_number in range(doc.page_count):
                text = doc.load_page(page_number).get_text().strip()
                if text:
                    parts.append(text)
        finally:
            doc.close()
    except Exception as exc:
        logger.error("pymupdf extraction failed for %s: %s", celex_id, exc)
        return None
    return "\n\n".join(parts) if parts else None


def extract_pdf_markdown(
    celex_id: str,
    raw_content: bytes,
    *,
    out_metadata: dict | None = None,
) -> str | None:
    """Extract PDF text through the isolated Docling worker or bounded fallback."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as pdf_file:
        pdf_file.write(raw_content)
        pdf_path = pdf_file.name

    failure_reason: _FailureReason | None = None
    failure_error: str | None = None
    try:
        if len(raw_content) > _DOCLING_SIZE_LIMIT:
            failure_reason = "oversize"
            logger.info(
                "PDF for %s is %.0f MB — skipping Docling, using pymupdf",
                celex_id, len(raw_content) / 1024 / 1024,
            )
        else:
            result = _get_docling_worker().convert(pdf_path)
            if result.markdown:
                if out_metadata is not None:
                    out_metadata.update({
                        "markdown": result.markdown,
                        "pdf_backend": "docling",
                    })
                logger.debug(
                    "Docling extracted %d chars from PDF for %s",
                    len(result.markdown), celex_id,
                )
                return result.markdown
            failure_reason = result.failure_reason or "empty"
            failure_error = result.error

        if failure_reason == "startup":
            error = failure_error or "unknown startup error"
            raise DoclingStartupError(
                f"Docling worker unavailable: {error}",
                fatal=_record_startup_failure(error),
            )

        logger.warning(
            "Docling %s for %s%s; using pymupdf",
            failure_reason,
            celex_id,
            f": {failure_error}" if failure_error else "",
        )
        text = _pymupdf_text_from_path(pdf_path, celex_id)
        if text:
            logger.info(
                "pymupdf fallback extracted %d chars for %s", len(text), celex_id
            )
        if out_metadata is not None:
            out_metadata.update({
                "markdown": text,
                "pdf_backend": "pymupdf",
                "pdf_fallback_reason": failure_reason,
            })
            if failure_error:
                out_metadata["pdf_fallback_error"] = failure_error[-2000:]
        return text
    finally:
        Path(pdf_path).unlink(missing_ok=True)


def _distinct_article_numbers(units: list[dict]) -> list[str]:
    numbers: list[str] = []
    for unit in units:
        if unit.get("type") != "article":
            continue
        number = str(unit.get("number") or "").lower()
        if number and number not in numbers:
            numbers.append(number)
    return numbers


def _article_sequence_is_complete(units: list[dict]) -> bool:
    numbers = _distinct_article_numbers(units)
    if numbers == ["sole"]:
        return True
    if not numbers or any(not re.fullmatch(r"\d+[a-z]*", number) for number in numbers):
        return False
    bases: list[int] = []
    for number in numbers:
        match = re.match(r"\d+", number)
        if match is None:
            return False
        base = int(match.group())
        if base not in bases:
            bases.append(base)
    return bases == list(range(1, bases[-1] + 1))


def _normalize_cyclic_numeric_article_numbers(numbers: list[str]) -> list[str]:
    """Normalize a two-column text-layer sequence only when it is one rotation."""
    if len(numbers) < 2 or any(not number.isdigit() for number in numbers):
        return numbers
    expected = [str(number) for number in range(1, len(numbers) + 1)]
    if set(numbers) != set(expected) or "1" not in numbers:
        return numbers
    pivot = numbers.index("1")
    rotated = numbers[pivot:] + numbers[:pivot]
    return expected if rotated == expected else numbers


def _article_group_is_substantive(units: list[dict]) -> bool:
    content = " ".join(
        f"{unit.get('title') or ''} "
        f"{strip_boilerplate(_clean_pdf_artifacts(unit.get('text') or ''))}"
        for unit in units
    )
    return sum(character.isalnum() for character in content) >= 3


_RECOVERED_PREAMBLE_RE = re.compile(
    r"\bTHE\s+(?:(?:EUROPEAN\s+)?COMMISSION|COUNCIL|"
    r"EUROPEAN\s+PARLIAMENT(?:\s+AND\s+THE\s+COUNCIL)?)"
    r"(?:\s+OF\s+THE\s+EUROPEAN\s+(?:COMMUNITIES|COMMUNITY|UNION))?\s*,"
    r"(?=\s+Having\s+regard)"
    r"|\bHaving\s+regard\s+to\b"
    r"|\bWhereas\b",
)
_RECOVERED_PAGE_HEADER_RE = re.compile(
    r"\s+(?:(?:No\s+[LC]\s*\d+\s*/?\s*\d*)|(?:\d{1,2}\.\s*\d{2,4}))"
    r"\s+(?:EN\s+)?Official\s+Journal\s+of\s+the\s+European\s+Communities\b.*$",
    re.DOTALL,
)
_RECOVERED_LEADING_PAGE_HEADER_RE = re.compile(
    r"^(?:(?:(?:No\s+[LC]\s*\d+\s*/?\s*\d*)|"
    r"(?:\d{1,2}\.\s*\d{2,4}))\s+(?:EN\s+)?|EN\s+)?"
    r"Official\s+Journal\s+of\s+the\s+European\s+Communities\b(?P<tail>.*)$",
    re.DOTALL,
)
_RECOVERED_OJ_REFERENCE_RE = re.compile(
    r"^[^A-Za-z]*(?:[fO0]\d*\)?\s+)?OJ\s+(?:No\s+)?[LC]\s*\d",
    re.IGNORECASE,
)
_RECOVERED_INLINE_OJ_REFERENCE_RE = re.compile(
    r"\s+(?:\([^)]{0,4}?\)?|\[[^\]]{0,4}?\]?|[fO0]\d*\)?)"
    r"\s*OJ\s+(?:No\s+)?[LC]\s*\d",
    re.IGNORECASE,
)
_RECOVERED_DATE_FRAGMENT_RE = re.compile(
    r"^(?:(?:\d{1,2}\s*\.\s*)?\d{2,4}\s*,?\s*=*\s*p\.?(?:\s*\d+)?|"
    r"\d{1,2}\s*\.\s*\d{2,4}|\d{4}\s*\.)$",
    re.IGNORECASE,
)


def _sanitize_recovered_article_group(units: list[dict]) -> list[dict]:
    sanitized: list[dict] = []
    for unit in units:
        text = _clean_pdf_artifacts(unit.get("text") or "")
        preamble = _RECOVERED_PREAMBLE_RE.search(text)
        if preamble:
            text = text[:preamble.start()].rstrip()
        leading_header = _RECOVERED_LEADING_PAGE_HEADER_RE.match(text)
        if leading_header:
            text = re.sub(
                r"^(?:No\s+[LC]\s*\d+\s*/?\s*\d*|[LC]\s*\d+\s*/\s*\d+)\s*",
                "",
                leading_header.group("tail").strip(),
            )
        text = _RECOVERED_PAGE_HEADER_RE.sub("", text).strip()
        inline_oj = _RECOVERED_INLINE_OJ_REFERENCE_RE.search(text)
        if (
            inline_oj
            and sum(character.isalnum() for character in text[:inline_oj.start()]) >= 3
        ):
            text = text[:inline_oj.start()].rstrip()
        text = re.sub(
            r"(?<=\.)\s+No\s+[LC]\s*\d+(?:/\d+)?\s*$", "", text,
        ).strip()
        if (
            len(text) <= 160
            and _RECOVERED_OJ_REFERENCE_RE.match(text)
        ) or _RECOVERED_DATE_FRAGMENT_RE.fullmatch(text):
            text = ""
        if not text and not unit.get("title"):
            continue
        cleaned = dict(unit)
        cleaned["text"] = text
        sanitized.append(cleaned)
    return sanitized


def _merge_complete_pymupdf_articles(
    docling_units: list[dict],
    pymupdf_units: list[dict],
) -> tuple[list[dict], bool]:
    """Add a strict, complete text-layer superset while retaining Docling units."""
    raw_pymupdf_numbers = _distinct_article_numbers(pymupdf_units)
    pymupdf_groups = {
        number: _sanitize_recovered_article_group([
            unit
            for unit in pymupdf_units
            if unit.get("type") == "article"
            and str(unit.get("number") or "").lower() == number
        ])
        for number in raw_pymupdf_numbers
    }
    raw_docling_numbers = _distinct_article_numbers(docling_units)
    docling_groups = {
        number: [
            unit for unit in docling_units
            if unit.get("type") == "article"
            and str(unit.get("number") or "").lower() == number
        ]
        for number in raw_docling_numbers
    }
    docling_numbers = [
        number for number in raw_docling_numbers
        if _article_group_is_substantive(docling_groups[number])
    ]
    pymupdf_numbers = _normalize_cyclic_numeric_article_numbers(
        [
            number for number in raw_pymupdf_numbers
            if (
                _article_group_is_substantive(pymupdf_groups[number])
                or number in docling_numbers
            )
        ],
    )
    pymupdf_sequence = [
        {"type": "article", "number": number}
        for number in pymupdf_numbers
    ]
    if not _article_sequence_is_complete(pymupdf_sequence):
        return docling_units, False
    if len(pymupdf_numbers) <= len(docling_numbers):
        return docling_units, False
    if not set(docling_numbers).issubset(pymupdf_numbers):
        return docling_units, False

    new_numbers = set(pymupdf_numbers) - set(docling_numbers)
    if any(
        not _article_group_is_substantive(pymupdf_groups[number])
        for number in new_numbers
    ):
        return docling_units, False

    recitals = [unit for unit in docling_units if unit.get("type") == "recital"]
    articles = [
        unit
        for number in pymupdf_numbers
        for unit in (
            docling_groups[number]
            if number in docling_numbers
            else pymupdf_groups[number]
        )
    ]
    other = [
        unit
        for unit in docling_units
        if unit.get("type") not in {"recital", "article", "annex"}
    ]
    annexes = [unit for unit in docling_units if unit.get("type") == "annex"]
    return [*recitals, *articles, *other, *annexes], True


class PdfExtractor:
    """Extracts text from PDF documents using Docling."""

    def can_handle(self, raw_content: bytes) -> bool:
        """Return True if content is a PDF."""
        return raw_content[:5] == b"%PDF-"

    def extract(
        self,
        celex_id: str,
        raw_content: bytes,
        *,
        include_recitals: bool = True,
        include_articles: bool = True,
        include_annexes: bool = True,
        article_granularity: str = "article",
        language: str = "eng",
        out_metadata: dict | None = None,
    ) -> list[dict]:
        """Extract text from PDF content via Docling, then parse structure.

        When `out_metadata` is provided, the Docling markdown intermediate is
        stored under `out_metadata["markdown"]` so the Pipeline can decide
        whether to run the translate-before-extract fallback. The fallback
        decision lives at Pipeline level because it needs document type and
        the ability to update `works.content_source` with provenance.
        """
        markdown = extract_pdf_markdown(
            celex_id, raw_content, out_metadata=out_metadata,
        )
        if not markdown:
            logger.warning("PDF extraction returned empty text for %s", celex_id)
            return []

        # Parse the markdown into structured text units.
        units = _parse_legislative_markdown(
            markdown,
            include_recitals=include_recitals,
            include_articles=include_articles,
            include_annexes=include_annexes,
            article_granularity=article_granularity,
        )

        if (
            include_articles
            and out_metadata is not None
            and out_metadata.get("pdf_backend") == "docling"
        ):
            pymupdf_text = (
                out_metadata.get("pdf_text_layer")
                if "pdf_text_layer" in out_metadata
                else extract_pdf_full_text(raw_content)
            )
            if pymupdf_text:
                pymupdf_units = _parse_legislative_markdown(
                    pymupdf_text,
                    include_recitals=include_recitals,
                    include_articles=include_articles,
                    include_annexes=include_annexes,
                    article_granularity=article_granularity,
                )
                units, repaired = _merge_complete_pymupdf_articles(
                    units, pymupdf_units,
                )
                if repaired:
                    out_metadata["pdf_representation_repair"] = "pymupdf_articles"
                    logger.info(
                        "Recovered a complete article sequence for %s from "
                        "the PDF text layer",
                        celex_id,
                    )

        # Clean up: trim OJ references, signatures, and page headers from units.
        for unit in units:
            unit["text"] = _clean_pdf_artifacts(unit["text"])

        # If no structured units found, return the full text as body.
        if not units and include_articles:
            units = [{
                "type": "body",
                "number": None,
                "title": None,
                "text": markdown.strip(),
            }]

        logger.info("Extracted %d text units from PDF for %s", len(units), celex_id)
        return units


def extract_pdf_full_text(raw_content: bytes) -> str | None:
    """Extract raw text from PDF using pymupdf. Used for full_text column."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(raw_content)
        tmp_path = f.name

    try:
        return _pymupdf_text_from_path(tmp_path, "full_text")
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# Inline cross-ref / footnote patterns — same semantics as the HTML side.
# Must NOT use re.IGNORECASE: legitimate recitals start with capital letters,
# so matching lowercase-initial continuations is our key signal for fragments.
_PDF_INLINE_REF_RE = re.compile(
    r"^-?\s*\(\d+\)\s+(?:of |to |the |thereof|is |shall |and |in |for |or |which |[a-z])",
)
_PDF_LEADING_REFERENCE_FRAGMENT_RE = re.compile(
    r"^-?\s*\(\d+\)\s+(?:of|to|thereof)\b",
)
_PDF_FOOTNOTE_REF_RE = re.compile(
    r"^-?\s*\(\d+\)\s+(?:OJ\s+(?:No\s+)?[LC]?\s*\d|"
    r"\[\d{4}\]\s*ECR|Ibidem|Ibid\.|Cf\.\s)",
)
_PDF_RECITAL_TAIL_OJ_RE = re.compile(
    r"\s*\(\s*\d+\s*\)\s*OJ\s+(?:No\s+)?[LC]?\s*\d+.*$",
    re.DOTALL,
)

# OJ footnote line ("( 1 ) OJ L 169, 12.7.1993, p. 1.") — trailing noise
# after the last annex, not annex content. Tolerates Docling's "( 1 )" spacing.
_OJ_FOOTNOTE_LINE_RE = re.compile(r"^-?\s*\(\s*\d+\s*\)\s*OJ\b")


def _pdf_classify_recital(text: str) -> str | None:
    """Short recital → subtype='subheading' (same rule as html extractor)."""
    if len(text.strip()) <= 30:
        return "subheading"
    return None


def _pdf_strip_recital_tail(text: str) -> str:
    return _PDF_RECITAL_TAIL_OJ_RE.sub("", text).rstrip()


def _repair_interleaved_article_markers(lines: list[str]) -> list[str]:
    """Move outer article markers that Docling interleaved into a long table."""
    marker_re = re.compile(
        r"^(?P<prefix>.+?)\s+Article\s+(?P<first>\d+[a-z]*)\s+"
        r"(?P<body>This\s+(?P<kind>Decision|Regulation|Directive)\s+shall\b.+?)\s+"
        r"Article\s+(?P<second>\d+[a-z]*)\s*$",
        re.IGNORECASE,
    )
    for index, line in enumerate(lines):
        match = marker_re.match(line.strip())
        if not match:
            continue
        body_start_re = re.compile(
            rf"^This\s+{re.escape(match.group('kind'))}\s+shall\s+be\s+published\b",
            re.IGNORECASE,
        )
        body_index = next(
            (
                later_index
                for later_index, later in enumerate(lines[index + 1 :], index + 1)
                if body_start_re.match(later.strip())
            ),
            None,
        )
        if body_index is None:
            continue
        repaired = list(lines)
        repaired[index] = match.group("prefix")
        repaired[body_index:body_index] = [
            f"{_FORCED_OUTER_ARTICLE}Article {match.group('first')}",
            match.group("body"),
            f"{_FORCED_OUTER_ARTICLE}Article {match.group('second')}",
        ]
        return repaired
    return lines


def _repair_embedded_operative_markers(lines: list[str]) -> list[str]:
    """Split enacting formulae and operative headings flattened onto one line."""
    joined = "\n".join(lines)
    quoted_regions = _find_quoted_regions(joined)
    line_offsets: list[int] = []
    offset = 0
    for line in lines:
        line_offsets.append(offset)
        offset += len(line) + 1

    formula_re = re.compile(
        r"\b(?P<formula>(?:HAS|HAVE)\s+(?:ADOPTED|DECIDED|AGREED)"
        r"(?:\s+THIS\s+\w+|\s+AS\s+FOLLOWS)?\s*:?)",
        re.IGNORECASE,
    )
    article_re = re.compile(
        r"(?<!\w)((?i:(?:Sole\s+)?Article)\s+\d+[a-zA-Z]*)"
        r"(?=\s+(?:[-–—一]\s*)?[A-ZÀ-Þ'‘\"“])",
    )
    trailing_article_re = re.compile(
        r"(?<!\w)(?P<heading>(?i:(?:Sole\s+)?Article)\s+"
        r"(?P<number>\d+)[a-zA-Z]*)\s*$",
    )
    final_article_body_re = re.compile(
        r"^This\s+(?:Regulation|Decision|Directive)\s+shall\b",
        re.IGNORECASE,
    )
    operative_boundary_re = re.compile(
        r"^(?:#{0,3}\s*ANNEX\b|Done\s+at\b|For\s+(?:the\s+)?"
        r"(?:Commission|Council)\b|The\s+President\b)",
        re.IGNORECASE,
    )
    repaired: list[str] = []
    seen_formula = False
    pending_initial_article_tail: str | None = None

    def _split_articles(
        value: str,
        *,
        value_offset: int,
        include_trailing: bool = False,
    ) -> list[tuple[str, bool]]:
        matches = [
            match for match in article_re.finditer(value)
            if (
                not value[:match.start()].rstrip()
                or value[:match.start()].rstrip()[-1] in ":.;|!?"
            )
            if not _is_in_quoted_region(
                value_offset + match.start(), quoted_regions,
            )
        ]
        if include_trailing:
            trailing = trailing_article_re.search(value)
            if (
                trailing
                and not _is_in_quoted_region(
                    value_offset + trailing.start(), quoted_regions,
                )
                and not any(
                    trailing.start() < match.end() and match.start() < trailing.end()
                    for match in matches
                )
            ):
                matches.append(trailing)
                matches.sort(key=lambda match: match.start())
        if not matches:
            return [(value, False)]
        parts: list[tuple[str, bool]] = []
        prefix = value[:matches[0].start()].strip()
        if prefix:
            parts.append((prefix, False))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
            part = value[match.start():end].strip()
            if part:
                parts.append((part, True))
        return parts

    def _append_parts(parts: list[tuple[str, bool]]) -> None:
        nonlocal pending_initial_article_tail
        for part, starts_article in parts:
            if starts_article and pending_initial_article_tail:
                repaired.append(pending_initial_article_tail)
                pending_initial_article_tail = None
            repaired.append(part)

    for line_index, line in enumerate(lines):
        if line.startswith(_FORCED_OUTER_ARTICLE):
            repaired.append(line)
            continue
        next_text = next(
            (
                candidate.strip()
                for candidate in lines[line_index + 1 :]
                if candidate.strip()
            ),
            "",
        )
        formula = next(
            (
                match for match in formula_re.finditer(line)
                if not _is_in_quoted_region(
                    line_offsets[line_index] + match.start(), quoted_regions,
                )
            ),
            None,
        )
        if formula:
            if pending_initial_article_tail:
                repaired.append(pending_initial_article_tail)
                pending_initial_article_tail = None
            prefix = line[:formula.start()].strip()
            if prefix:
                repaired.append(prefix)
            repaired.append(formula.group("formula").strip())
            raw_suffix = line[formula.end():]
            leading = len(raw_suffix) - len(raw_suffix.lstrip())
            suffix = raw_suffix.strip()
            suffix_offset = line_offsets[line_index] + formula.end() + leading
            if suffix:
                trailing = trailing_article_re.search(suffix)
                if (
                    trailing
                    and trailing.group("number") == "1"
                    and not _is_in_quoted_region(
                        suffix_offset + trailing.start(), quoted_regions,
                    )
                ):
                    displaced_tail = suffix[:trailing.start()].strip()
                    repaired.append(trailing.group("heading"))
                    pending_initial_article_tail = displaced_tail or None
                else:
                    _append_parts(_split_articles(
                        suffix,
                        value_offset=suffix_offset,
                        include_trailing=bool(final_article_body_re.match(next_text)),
                    ))
            seen_formula = True
        elif seen_formula:
            if (
                pending_initial_article_tail
                and operative_boundary_re.match(line.strip())
            ):
                repaired.append(pending_initial_article_tail)
                pending_initial_article_tail = None
            leading = len(line) - len(line.lstrip())
            value = line.strip()
            _append_parts(_split_articles(
                value,
                value_offset=line_offsets[line_index] + leading,
                include_trailing=bool(final_article_body_re.match(next_text)),
            ))
        else:
            repaired.append(line)
    if pending_initial_article_tail:
        repaired.append(pending_initial_article_tail)
    return repaired


def _repair_displaced_operative_block(lines: list[str]) -> list[str]:
    """Move an operative block that Docling placed after the signature."""
    signature_re = re.compile(r"^Done at \w+[,\s]+\d", re.IGNORECASE)
    formula_re = re.compile(
        r"^(?:#{1,3}\s+)?(?:HAS|HAVE)\s+(?:ADOPTED|DECIDED|AGREED)\b",
        re.IGNORECASE,
    )
    article_heading_re = re.compile(
        r"^(?:#{1,3}\s+)?(?:Sole\s+)?Article(?!s)(?:\s+\d+[a-z]*)?\s*$",
        re.IGNORECASE,
    )
    annex_re = re.compile(r"^#{1,3}\s+ANNEX\b", re.IGNORECASE)
    signatory_re = re.compile(
        r"^(?:For\s+(?:the\s+)?(?:Commission|Council)|The\s+President)\b",
        re.IGNORECASE,
    )
    signature_index = next(
        (
            index
            for index, line in enumerate(lines)
            if signature_re.match(line.strip())
            and any(
                formula_re.match(later.strip())
                or article_heading_re.match(later.strip())
                for later in lines[index + 1 :]
            )
        ),
        None,
    )
    if signature_index is None:
        return lines

    formula_index = next(
        (
            index
            for index, line in enumerate(lines[signature_index + 1 :], signature_index + 1)
            if formula_re.match(line.strip())
        ),
        None,
    )
    instrument_re = re.compile(
        r"\b(?:AGREEMENT|PROTOCOL|CONVENTION|RESOLUTION|ARRANGEMENT|"
        r"EXCHANGE\s+OF\s+LETTERS|MEMORANDUM(?:\s+OF\s+UNDERSTANDING)?)\b",
        re.IGNORECASE,
    )

    def _looks_like_attachment_boundary(line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return False
        if re.match(r"^#{1,3}\s+\S", stripped):
            return True
        if instrument_re.search(stripped):
            return True
        letters = [character for character in stripped if character.isalpha()]
        if (
            len(stripped) <= 240
            and len(letters) >= 4
            and sum(character.isupper() for character in letters) / len(letters) >= 0.8
        ):
            return True
        words = re.findall(r"[^\W\d_]+", stripped, re.UNICODE)
        connectors = {
            "and", "between", "by", "concerning", "for", "in", "of",
            "on", "the", "to", "with",
        }
        return (
            len(stripped) <= 240
            and 2 <= len(words) <= 20
            and stripped[-1] not in ".,;:"
            and all(
                word.casefold() in connectors
                or word.isupper()
                or word[:1].isupper()
                for word in words
            )
        )

    if formula_index is not None and any(
        _looks_like_attachment_boundary(line)
        for line in lines[signature_index + 1 : formula_index]
    ):
        return lines
    if formula_index is None:
        operative_end = next(
            (
                index
                for index, line in enumerate(lines[signature_index + 1 :], signature_index + 1)
                if annex_re.match(line.strip()) or signatory_re.match(line.strip())
            ),
            len(lines),
        )
        first_article = next(
            (
                index
                for index, line in enumerate(lines[signature_index + 1 : operative_end], signature_index + 1)
                if article_heading_re.match(line.strip())
            ),
            None,
        )
        preceding_nonempty = (
            sum(bool(line.strip()) for line in lines[signature_index + 1 : first_article])
            if first_article is not None
            else 99
        )
        if (
            first_article is None
            or preceding_nonempty > 5
            or any(
                _looks_like_attachment_boundary(line)
                for line in lines[signature_index + 1 : first_article]
            )
        ):
            return lines
        move_start = next(
            index
            for index in range(signature_index + 1, first_article + 1)
            if lines[index].strip()
        )
        binding_re = re.compile(
            r"^This\s+(?:Regulation|Decision|Directive)\s+shall\s+be\s+binding\b",
            re.IGNORECASE,
        )
        fallback_insertion_index = next(
            (
                index
                for index, line in enumerate(lines[:signature_index])
                if binding_re.match(line.strip())
            ),
            signature_index,
        )
        block = lines[move_start:operative_end]
        remaining = lines[:move_start] + lines[operative_end:]
        return (
            remaining[:fallback_insertion_index]
            + block
            + remaining[fallback_insertion_index:]
        )

    repaired: list[str] = []
    trailing_article_re = re.compile(
        r"^(?P<prefix>.+\S)\s+(?P<heading>(?:Sole\s+)?Article(?!s)\s+\d+[a-z]*)\s*$",
        re.IGNORECASE,
    )
    for index, line in enumerate(lines):
        match = trailing_article_re.match(line.strip())
        next_text = next(
            (candidate.strip() for candidate in lines[index + 1 :] if candidate.strip()),
            "",
        )
        if (
            index < signature_index
            and match
            and match.group("prefix").lstrip().lower().startswith("whereas")
            and re.match(
                r"^This\s+(?:Regulation|Decision|Directive)\s+shall\b",
                next_text,
                re.IGNORECASE,
            )
        ):
            repaired.extend([match.group("prefix"), match.group("heading")])
        else:
            repaired.append(line)
    lines = repaired

    signature_index = next(
        index for index, line in enumerate(lines) if signature_re.match(line.strip())
    )
    formula_index = next(
        index
        for index, line in enumerate(lines[signature_index + 1 :], signature_index + 1)
        if formula_re.match(line.strip())
    )
    operative_end = next(
        (
            index
            for index, line in enumerate(lines[formula_index + 1 :], formula_index + 1)
            if annex_re.match(line.strip()) or signatory_re.match(line.strip())
        ),
        len(lines),
    )

    move_start = formula_index
    footnote_indexes = [
        index
        for index, line in enumerate(lines[signature_index + 1 : formula_index], signature_index + 1)
        if re.match(r"^[('O0\[]", line.strip())
        and re.search(r"\bOJ\s+(?:No\s+)?[LC]?\s*\d", line, re.IGNORECASE)
    ]
    if footnote_indexes:
        candidate = next(
            (
                index
                for index in range(footnote_indexes[-1] + 1, formula_index)
                if lines[index].strip()
            ),
            formula_index,
        )
        move_start = candidate

    insertion_index: int | None = next(
        (
            index
            for index, line in enumerate(lines[:signature_index])
            if article_heading_re.match(line.strip())
        ),
        None,
    )
    if insertion_index is None:
        binding_re = re.compile(
            r"^This\s+(?:Regulation|Decision|Directive)\s+shall\s+be\s+binding\b",
            re.IGNORECASE,
        )
        insertion_index = next(
            (
                index
                for index, line in enumerate(lines[:signature_index])
                if binding_re.match(line.strip())
            ),
            signature_index,
        )

    block = lines[move_start:operative_end]
    remaining = lines[:move_start] + lines[operative_end:]
    if insertion_index > move_start:
        insertion_index -= len(block)
    return remaining[:insertion_index] + block + remaining[insertion_index:]


def _parse_legislative_markdown(
    text: str,
    *,
    include_recitals: bool,
    include_articles: bool,
    include_annexes: bool,
    article_granularity: str = "article",
) -> list[dict]:
    """Parse Docling markdown output into legislative text units.

    Handles the same patterns as the text-only HTML extractor but adapted
    for markdown output (headings marked with ##, paragraphs separated by
    blank lines).
    """
    units: list[dict] = []
    signature_re = re.compile(r"^Done at \w+[,\s]+\d", re.IGNORECASE)
    # Pre-process: join lines where OCR splits "Article\n1" or "ANNEX\nI" across lines.
    text = re.sub(r"\b(Article)\s*\n\s*(\d+)\b", r"\1 \2", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(ANNEX)\s*\n\s*([IVXLCDMivxlcdm0-9]+)\b", r"\1 \2", text, flags=re.IGNORECASE)
    lines = text.split("\n")
    lines = _repair_interleaved_article_markers(lines)
    lines = _repair_embedded_operative_markers(lines)
    tail_annex_re = re.compile(
        r"^#{1,3}\s+ANNEX(?:\s+[IVXLCDMivxlcdm0-9]+)?\s*$", re.IGNORECASE,
    )
    for heading_index, line in enumerate(lines):
        if not tail_annex_re.match(line.strip()):
            continue
        if not all(
            not candidate.strip() or tail_annex_re.match(candidate.strip())
            for candidate in lines[heading_index:]
        ):
            continue
        table_start: int | None = None
        for index in range(heading_index - 1, -1, -1):
            stripped = lines[index].strip()
            if stripped.startswith("|"):
                table_start = index
            elif not stripped and table_start is not None:
                continue
            elif table_start is not None:
                break
        if table_start is not None:
            lines = (
                lines[:table_start]
                + lines[heading_index:]
                + lines[table_start:heading_index]
            )
        break
    nonempty = [(index, line.strip()) for index, line in enumerate(lines) if line.strip()]
    if nonempty and signature_re.match(nonempty[0][1]):
        title_re = re.compile(
            r"^#{1,3}\s+(?:COMMISSION|COUNCIL|EUROPEAN\s+PARLIAMENT)\s+"
            r"(?:REGULATION|DIRECTIVE|DECISION)\b",
            re.IGNORECASE,
        )
        title_index = next(
            (index for index, line in nonempty[1:] if title_re.match(line)), None,
        )
        if title_index is not None and any(
            re.match(r"^(?:HAS|HAVE)\s+(?:ADOPTED|DECIDED)\b", line, re.IGNORECASE)
            for index, line in nonempty
            if index >= title_index
        ):
            lines = lines[title_index:]
    lines = _repair_displaced_operative_block(lines)
    repaired_text = "\n".join(lines)
    quoted_regions = _find_quoted_regions(repaired_text)
    line_offsets: list[int] = []
    offset = 0
    for line in lines:
        line_offsets.append(offset)
        offset += len(line) + 1

    recital_counter = 0
    in_recital_zone = False
    current_article: dict | None = None
    current_annex: dict | None = None
    current_recital: dict | None = None
    seen_enacting_formula = False

    # "Done at <city>, <date>" signals end of legislative content.
    past_signature = False

    # Match articles/annexes as markdown headings (## Article 1) or as
    # standalone short lines (Article 1) — Docling doesn't always add headings.
    # Bare "Article N" in long body text must NOT match.
    art_heading_re = re.compile(r"^#{1,3}\s+(?:Sole\s+)?Article(?!s)(?:\s+(\d+[a-z]*))?", re.IGNORECASE)
    art_bare_re = re.compile(
        r"^(?:Sole\s+)?Article(?!s)(?:\s+(\d+[a-z]*))?", re.IGNORECASE,
    )
    annex_heading_re = re.compile(
        r"^#{1,3}\s+ANNEX\s*(?P<number>[IVXLCDMivxlcdm0-9]*)(?P<title>.*)",
        re.IGNORECASE,
    )
    annex_bare_re = re.compile(
        r"^ANNEX\s*(?P<number>[IVXLCDMivxlcdm0-9]*)\s*$", re.IGNORECASE,
    )
    annex_titled_re = re.compile(
        r"^ANNEX(?:\s+(?P<number>[IVXLCDM0-9]+))?\s+(?P<title>.+)$",
    )
    multilingual_annex_re = re.compile(
        r"^#{1,3}\s+(?=[^\n]*\bANNEX\b)"
        r"(?:ANNEXE|ANHANG|ALLEGATO|BIJLAGE|BILAG|ΠΑΡΑΡΤΗΜΑ)\b[^\n]*$",
        re.IGNORECASE,
    )
    # Match "(1) text" and also "- (1) text" (Docling bullet format).
    numbered_recital_re = re.compile(r"^-?\s*\((\d+)\)\s*(.*)")
    # Recital-zone trigger. "Whereas:" is the native English marker; Opus-MT
    # commonly renders the French "considérant ce qui suit :" and the German
    # "in Erwägung nachstehender Gründe:" as "Considering the following:" or
    # "Having regard to the following:". Accept those too so translated PDFs
    # still trigger the recital zone.
    whereas_marker_re = re.compile(
        r"^(?:#{1,3}\s+)?"
        r"(?:Whereas|Considering(?:\s+the\s+following)?|Having\s+regard\s+to\s+the\s+following)"
        r"\s*:?\s*$",
        re.IGNORECASE,
    )
    # Old-style recitals start mid-paragraph with "Whereas the …" or, when
    # translated from non-English, "Considering …" / "Considering that …".
    whereas_line_re = re.compile(
        r"^(?:Whereas|Considering(?:\s+that)?)[\s,;]",
        re.IGNORECASE,
    )
    # Enacting formula marks the end of recitals and start of operative provisions.
    # Examples: "HAS ADOPTED THIS REGULATION:", "HAVE ADOPTED THIS DECISION",
    # "HAS DECIDED AS FOLLOWS:". Without this marker, the line falls through to
    # the recital handler and gets glued onto the last recital's text.
    enacting_formula_re = re.compile(
        r"^(?:#{1,3}\s+)?(?:HAS|HAVE)\s+(?:ADOPTED|DECIDED|AGREED)\b",
        re.IGNORECASE,
    )

    def _flush_recital():
        nonlocal current_recital
        _flush(current_recital, units, include_recitals)
        current_recital = None

    def _flush_article(art: dict | None) -> None:
        """Flush an article: apply sub-article splitter at the configured granularity."""
        if art is None or not include_articles:
            return
        body = art.get("_body", [])
        title = art.get("title")
        number = art.get("number")
        if body or title or number:
            units.extend(split_article(
                body, number=number, title=title, granularity=article_granularity,
            ))

    def _start_annex(match: re.Match) -> dict:
        groups = match.groupdict()
        number = (groups.get("number") or "").strip() or None
        title = (groups.get("title") or "").strip() or None
        return {
            "type": "annex",
            "number": number,
            "title": title,
            "text": "",
            "_body": [],
        }

    def _match_annex(stripped: str) -> re.Match | None:
        multilingual = multilingual_annex_re.match(stripped)
        if multilingual:
            return multilingual
        # "ANNEXES"/"Annexe(s)" are cover headings or references, not annex
        # starts — without this guard "## ANNEXES" becomes an annex titled "ES".
        if re.match(r"^#{0,3}\s*ANNEXE", stripped, re.IGNORECASE):
            return None
        return (
            annex_heading_re.match(stripped)
            or annex_bare_re.match(stripped)
            or annex_titled_re.match(stripped)
        )

    for line_index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        forced_outer_article = stripped.startswith(_FORCED_OUTER_ARTICLE)
        if forced_outer_article:
            stripped = stripped.removeprefix(_FORCED_OUTER_ARTICLE)

        # "Done at <city>, <date>" signals the end of the enacting terms.
        # Annexes follow the signature in the OJ layout, so keep collecting
        # them; everything else after it (signatory names, footnote lists,
        # archival references) is noise and gets discarded.
        if past_signature:
            annex_match = _match_annex(stripped)
            if annex_match:
                _flush_recital()
                _flush_article(current_article)
                current_article = None
                _flush(current_annex, units, include_annexes)
                current_annex = _start_annex(annex_match)
            elif current_annex is not None and not _OJ_FOOTNOTE_LINE_RE.match(stripped):
                current_annex["_body"].append(stripped)
            continue
        if signature_re.match(stripped):
            past_signature = True
            if current_article is not None:
                current_article["_body"].append(stripped)
            elif current_annex is not None:
                current_annex["_body"].append(stripped)
            continue

        # Enacting formula: end of recitals, no doc-unit emitted for it.
        # Covers both modern-style (in_recital_zone) and old-style (continuing
        # recital built from "Whereas the …" line that doesn't set the zone).
        if enacting_formula_re.match(stripped):
            seen_enacting_formula = True
            if in_recital_zone or current_recital is not None:
                in_recital_zone = False
                _flush_recital()
            continue

        art_match = art_heading_re.match(stripped) or art_bare_re.match(stripped)
        if (
            art_match
            and not forced_outer_article
            and _is_in_quoted_region(line_offsets[line_index], quoted_regions)
        ):
            art_match = None
        if art_match:
            after = stripped[art_match.end():].strip()
            if (
                art_match.group(1) is None
                and not re.match(
                    r"^#{0,3}\s*Sole\s+Article", stripped, re.IGNORECASE,
                )
            ):
                art_match = None
            elif after and re.match(r"^[.,;:\-–—\|]+$", after):
                after = ""
            if art_match and after and re.match(
                r"(?:\([a-z0-9]+\)|of |to |the |is |shall |and |in |for |or |"
                r"which |has |was |provides |referred |,)",
                after,
                re.IGNORECASE,
            ):
                if not (
                    seen_enacting_formula
                    and after[0].isupper()
                ):
                    art_match = None
            elif art_match and not after:
                next_text = next(
                    (
                        candidate.strip()
                        for candidate in lines[line_index + 1:]
                        if candidate.strip()
                    ),
                    "",
                )
                if next_text and (
                    next_text[0].islower()
                    or re.match(r"^\(\d+\)\s*(?:of\b|thereof)", next_text)
                    or next_text.startswith("thereof")
                ):
                    art_match = None
        annex_match = _match_annex(stripped)

        if art_match:
            in_recital_zone = False
            _flush_recital()
            _flush_article(current_article)
            _flush(current_annex, units, include_annexes)
            current_annex = None

            current_article = {
                "type": "article",
                "number": art_match.group(1) or "sole",
                "title": None,
                "text": "",
                "_body": [],
            }
            remainder = stripped[art_match.end():].strip()
            if remainder:
                current_article["_body"].append(remainder)

        elif annex_match:
            in_recital_zone = False
            _flush_recital()
            _flush_article(current_article)
            current_article = None
            _flush(current_annex, units, include_annexes)

            current_annex = _start_annex(annex_match)

        elif current_annex is not None:
            current_annex["_body"].append(stripped)

        elif current_article is not None:
            current_article["_body"].append(stripped)

        elif whereas_marker_re.match(stripped):
            _flush_recital()
            in_recital_zone = True

        elif in_recital_zone:
            num_match = numbered_recital_re.match(stripped)
            if num_match:
                if _PDF_FOOTNOTE_REF_RE.match(stripped):
                    continue
                number = int(num_match.group(1))
                current_number = None
                if current_recital is not None:
                    current_number = int(current_recital["number"])
                inline_reference = bool(_PDF_INLINE_REF_RE.match(stripped))
                if (
                    current_recital is not None
                    and current_number is not None
                    and number <= current_number
                    and inline_reference
                ):
                    current_recital["_body"].append(stripped)
                elif current_number is None and inline_reference and number != 1:
                    continue
                else:
                    _flush_recital()
                    current_recital = {
                        "type": "recital",
                        "number": num_match.group(1),
                        "title": None,
                        "text": "",
                        "_body": [stripped],
                    }
            elif current_recital is not None:
                # Continuation line for the current recital.
                current_recital["_body"].append(stripped)

        elif (
            (num_match := numbered_recital_re.match(stripped))
            and (
                re.match(
                    r"^-?\s*\(\d+\)\s*(?:Whereas|Considering)\b",
                    stripped,
                    re.IGNORECASE,
                )
                or (
                    current_recital is None
                    and re.search(
                        r"\b(?:whereas|considering)\b",
                        stripped,
                        re.IGNORECASE,
                    )
                    and not _PDF_LEADING_REFERENCE_FRAGMENT_RE.match(stripped)
                )
            )
        ):
            # Numbered recital with "Whereas" or "Considering" (translated FR/DE)
            # but no prior zone-marker line. E.g. "(1) Whereas...", "(1) Considering that..."
            _flush_recital()
            in_recital_zone = True
            current_recital = {
                "type": "recital",
                "number": num_match.group(1),
                "title": None,
                "text": "",
                "_body": [stripped],
            }

        elif whereas_line_re.match(stripped):
            # Old-style "Whereas ..." recital — flush previous, start new.
            _flush_recital()
            recital_counter += 1
            current_recital = {
                "type": "recital",
                "number": str(recital_counter),
                "title": None,
                "text": "",
                "_body": [stripped],
            }

        elif current_recital is not None:
            # Continuation line in old-style recital zone (no explicit marker).
            current_recital["_body"].append(stripped)

    # Flush trailing.
    _flush_recital()
    _flush_article(current_article)
    _flush(current_annex, units, include_annexes)

    return units


def _flush(item: dict | None, units: list[dict], include: bool) -> None:
    """Flush an accumulated article/annex/recital into the units list."""
    if item is None or not include:
        return
    item["text"] = " ".join(item["_body"])
    del item["_body"]
    # For recitals: strip trailing OJ footnote anchors and classify short ones.
    if item.get("type") == "recital" and item["text"]:
        item["text"] = _pdf_strip_recital_tail(item["text"])
        item["subtype"] = _pdf_classify_recital(item["text"])
        if sum(character.isalpha() for character in item["text"]) < 3:
            return
    if item["text"] or item.get("title") or item.get("number"):
        units.append(item)


def _clean_pdf_artifacts(text: str) -> str:
    """Remove common PDF extraction artifacts from text.

    Strips Official Journal headers/references, signature blocks, and
    page number artifacts that Docling includes in the text flow.
    """
    # Strip Docling image placeholders (also pymupdf (...PICT...) markers).
    text = re.sub(r"<!--\s*image\s*-->", "", text)
    text = re.sub(r"\(\s*\.{2,}\s*PICT\s*\.{2,}\s*\)", "", text)
    # Collapse the whitespace that stripping leaves behind.
    text = re.sub(r"[ \t]{2,}", " ", text).strip()

    # Trim signature block from end of text ("Done at Brussels..." onwards).
    done_match = re.search(r"\bDone at \w+,\s+\d+", text)
    if done_match:
        text = text[:done_match.start()].rstrip()

    # Trim "For the Commission..." signature if it appears at the end.
    commission_match = re.search(r"\bFor the (?:Commission|Council)\b.*$", text, re.DOTALL)
    if commission_match and len(text) - commission_match.start() < 200:
        text = text[:commission_match.start()].rstrip()

    # Remove OJ footnote references at end: "( 1 ) OJ L 181, ..."
    text = re.sub(r"\(\s*\d+\s*\)\s*OJ\s+(?:No\s+)?L\s+\d+.*$", "", text, flags=re.DOTALL).rstrip()

    # Remove "EN Official Journal..." page headers that leak into text.
    text = re.sub(r"\b\d+\.\s*\d+\.\s*\d+\s+EN\s+Official Journal\b.*?\d+/\d+", "", text).strip()

    # Remove standalone page markers.
    text = re.sub(r"^\s*L\s+\d+/\d+\s*$", "", text, flags=re.MULTILINE).strip()

    return text
