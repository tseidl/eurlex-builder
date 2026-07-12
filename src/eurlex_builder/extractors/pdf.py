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
from eurlex_builder.extractors.splitter import split_article

logger = logging.getLogger("eurlex_builder")

# Timeout for a single PDF conversion (seconds).
_PDF_TIMEOUT = 120
_WORKER_START_TIMEOUT = 180
_DOCLING_SIZE_LIMIT = 50 * 1024 * 1024
_WORKER_JOIN_TIMEOUT = 5
_CONTROL_MESSAGE_LIMIT = 1024 * 1024

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
    # Pre-process: join lines where OCR splits "Article\n1" or "ANNEX\nI" across lines.
    text = re.sub(r"\b(Article)\s*\n\s*(\d+)\b", r"\1 \2", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(ANNEX)\s*\n\s*([IVXLCDMivxlcdm0-9]+)\b", r"\1 \2", text, flags=re.IGNORECASE)
    lines = text.split("\n")

    recital_counter = 0
    in_recital_zone = False
    current_article: dict | None = None
    current_annex: dict | None = None
    current_recital: dict | None = None

    # "Done at <city>, <date>" signals end of legislative content.
    signature_re = re.compile(r"^Done at \w+[,\s]+\d", re.IGNORECASE)
    past_signature = False

    # Match articles/annexes as markdown headings (## Article 1) or as
    # standalone short lines (Article 1) — Docling doesn't always add headings.
    # Bare "Article N" in long body text must NOT match.
    art_heading_re = re.compile(r"^#{1,3}\s+(?:Sole\s+)?Article(?!s)(?:\s+(\d+[a-z]*))?", re.IGNORECASE)
    art_bare_re = re.compile(r"^(?:Sole\s+)?Article(?!s)(?:\s+(\d+[a-z]*))?\s*$", re.IGNORECASE)
    annex_heading_re = re.compile(r"^#{1,3}\s+ANNEX\s*([IVXLCDMivxlcdm0-9]*)(.*)", re.IGNORECASE)
    annex_bare_re = re.compile(r"^ANNEX\s*([IVXLCDMivxlcdm0-9]*)\s*$", re.IGNORECASE)
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
        # group(2) exists for the heading regex but not the bare regex.
        try:
            title = match.group(2).strip() or None
        except IndexError:
            title = None
        return {
            "type": "annex",
            "number": match.group(1).strip() or None,
            "title": title,
            "text": "",
            "_body": [],
        }

    def _match_annex(stripped: str) -> re.Match | None:
        # "ANNEXES"/"Annexe(s)" are cover headings or references, not annex
        # starts — without this guard "## ANNEXES" becomes an annex titled "ES".
        if re.match(r"^#{0,3}\s*ANNEXE", stripped, re.IGNORECASE):
            return None
        return annex_heading_re.match(stripped) or annex_bare_re.match(stripped)

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

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
        if (in_recital_zone or current_recital is not None) and enacting_formula_re.match(stripped):
            in_recital_zone = False
            _flush_recital()
            continue

        art_match = art_heading_re.match(stripped) or art_bare_re.match(stripped)
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
                # Skip footnote refs / inline cross-refs disguised as recitals.
                if _PDF_FOOTNOTE_REF_RE.match(stripped) or _PDF_INLINE_REF_RE.match(stripped):
                    continue
                # New numbered recital — flush previous, start new.
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
            and ("WHEREAS" in stripped.upper() or "CONSIDERING" in stripped.upper())
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
