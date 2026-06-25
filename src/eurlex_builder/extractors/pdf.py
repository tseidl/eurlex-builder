"""PDF text extractor using Docling for layout-aware document understanding.

Falls back to pymupdf for lightweight text extraction when Docling times out.
"""

from __future__ import annotations

import logging
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path

from eurlex_builder.extractors.splitter import split_article

logger = logging.getLogger("eurlex_builder")

# Timeout for a single PDF conversion (seconds).
_PDF_TIMEOUT = 120

# Lazy-loaded converter to avoid heavy import at module load.
import threading

_converter = None
_converter_lock = threading.Lock()


def _get_converter():
    """Lazily initialize the Docling DocumentConverter."""
    global _converter
    if _converter is None:
        with _converter_lock:
            if _converter is None:
                from docling.document_converter import DocumentConverter
                _converter = DocumentConverter()
                logger.debug("Docling DocumentConverter initialized")
    return _converter


class PdfExtractor:
    """Extracts text from PDF documents using Docling."""

    def can_handle(self, raw_content: bytes) -> bool:
        """Return True if content is a PDF."""
        return raw_content[:5] == b"%PDF-"

    # Skip Docling for PDFs larger than 50 MB — they cause segfaults in native code.
    _DOCLING_SIZE_LIMIT = 50 * 1024 * 1024

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
        # Docling needs a file path, so write to a temp file.
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(raw_content)
            tmp_path = f.name

        # Skip Docling for very large PDFs to avoid segfaults in native code.
        if len(raw_content) > self._DOCLING_SIZE_LIMIT:
            logger.info(
                "PDF for %s is %.0f MB — skipping Docling, using pymupdf directly",
                celex_id, len(raw_content) / 1024 / 1024,
            )
            return self._pymupdf_fallback(
                tmp_path, celex_id,
                include_recitals=include_recitals,
                include_articles=include_articles,
                include_annexes=include_annexes,
                article_granularity=article_granularity,
            )

        try:
            converter = _get_converter()
            # Run conversion with a timeout to avoid hanging on problematic PDFs.
            # Avoid returning from inside the `with` block — its __exit__ calls
            # shutdown(wait=True), which would block until Docling finishes and
            # defeat the timeout.
            timed_out = False
            pool = ThreadPoolExecutor(max_workers=1)
            future = pool.submit(converter.convert, tmp_path)
            try:
                result = future.result(timeout=_PDF_TIMEOUT)
            except FuturesTimeoutError:
                logger.error(
                    "Docling conversion timed out after %ds for %s",
                    _PDF_TIMEOUT, celex_id,
                )
                future.cancel()
                pool.shutdown(wait=False, cancel_futures=True)
                timed_out = True
            else:
                pool.shutdown(wait=False)

            if timed_out:
                return self._pymupdf_fallback(
                    tmp_path, celex_id,
                    include_recitals=include_recitals,
                    include_articles=include_articles,
                    include_annexes=include_annexes,
                    article_granularity=article_granularity,
                )
            markdown = result.document.export_to_markdown()
        except Exception as exc:
            logger.error("Docling conversion failed for %s: %s", celex_id, exc)
            return self._pymupdf_fallback(
                tmp_path, celex_id,
                include_recitals=include_recitals,
                include_articles=include_articles,
                include_annexes=include_annexes,
                article_granularity=article_granularity,
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        if not markdown:
            logger.warning("Docling returned empty text for %s", celex_id)
            return []

        logger.debug(
            "Docling extracted %d chars from PDF for %s", len(markdown), celex_id
        )

        # Expose the Docling markdown so Pipeline can decide whether to run
        # the translate-before-extract fallback for non-English legislative
        # PDFs. Doing the decision here would be at the wrong layer — it
        # needs document type and the ability to update works.content_source.
        if out_metadata is not None:
            out_metadata["markdown"] = markdown

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
        if not units:
            units = [{
                "type": "body",
                "number": None,
                "title": None,
                "text": markdown.strip(),
            }]

        logger.info("Extracted %d text units from PDF for %s", len(units), celex_id)
        return units


    @staticmethod
    def _pymupdf_fallback(
        tmp_path: str,
        celex_id: str,
        *,
        include_recitals: bool = True,
        include_articles: bool = True,
        include_annexes: bool = True,
        article_granularity: str = "article",
    ) -> list[dict]:
        """Lightweight PDF text extraction via pymupdf when Docling fails."""
        try:
            import pymupdf
        except ImportError:
            logger.warning("pymupdf not installed — cannot fall back for %s", celex_id)
            return []

        try:
            doc = pymupdf.open(tmp_path)
            parts: list[str] = []
            for page in doc:
                text = page.get_text()
                if text.strip():
                    parts.append(text.strip())
            doc.close()

            full_text = "\n\n".join(parts)
            if not full_text:
                logger.warning("pymupdf returned empty text for %s", celex_id)
                return []

            logger.info(
                "pymupdf fallback extracted %d chars for %s", len(full_text), celex_id
            )

            # Try structural parsing on the raw text.
            units = _parse_legislative_markdown(
                full_text,
                include_recitals=include_recitals,
                include_articles=include_articles,
                include_annexes=include_annexes,
                article_granularity=article_granularity,
            )
            if units:
                for unit in units:
                    unit["text"] = _clean_pdf_artifacts(unit["text"])
                return units

            return [{
                "type": "body",
                "number": None,
                "title": None,
                "text": full_text,
            }]
        except Exception as exc:
            logger.error("pymupdf fallback failed for %s: %s", celex_id, exc)
            return []
        finally:
            Path(tmp_path).unlink(missing_ok=True)


def extract_pdf_full_text(raw_content: bytes) -> str | None:
    """Extract raw text from PDF using pymupdf. Used for full_text column."""
    try:
        import pymupdf
    except ImportError:
        return None

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(raw_content)
        tmp_path = f.name

    try:
        doc = pymupdf.open(tmp_path)
        parts = [page.get_text().strip() for page in doc if page.get_text().strip()]
        doc.close()
        return "\n\n".join(parts) if parts else None
    except Exception as exc:
        logger.warning("PDF full-text extraction failed: %s", exc)
        return None
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

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # "Done at <city>, <date>" signals the end of legislative content.
        # Append the signature line itself once so strip_boilerplate can anchor
        # on it, then discard everything that follows (annex tables, footnote
        # lists, archival references) to avoid contaminating article bodies.
        if past_signature:
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
        annex_match = annex_heading_re.match(stripped) or annex_bare_re.match(stripped)

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

            annex_number = annex_match.group(1).strip() or None
            # group(2) exists for heading regex but not bare regex.
            try:
                annex_title = annex_match.group(2).strip() or None
            except IndexError:
                annex_title = None
            current_annex = {
                "type": "annex",
                "number": annex_number,
                "title": annex_title,
                "text": "",
                "_body": [],
            }

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

        elif numbered_recital_re.match(stripped) and (
            "WHEREAS" in stripped.upper() or "CONSIDERING" in stripped.upper()
        ):
            # Numbered recital with "Whereas" or "Considering" (translated FR/DE)
            # but no prior zone-marker line. E.g. "(1) Whereas...", "(1) Considering that..."
            _flush_recital()
            in_recital_zone = True
            num_match = numbered_recital_re.match(stripped)
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
