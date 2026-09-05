"""Optional extraction of official language versions into a separate dataset."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import duckdb
from lxml import etree

from eurlex_builder.extractors.html import _iter_visible_text, _walk_article_body
from eurlex_builder.html_parser import parse_html
from eurlex_builder.sources.cellar import (
    CELLAR_RESOURCE_BASE,
    HTML_ACCEPT,
    CellarSource,
)
from eurlex_builder.storage.export import export_tables
from eurlex_builder.utils import is_valid_celex, normalize_string

logger = logging.getLogger("eurlex_builder")

LANGUAGES = {
    "bul": "bg",
    "ces": "cs",
    "dan": "da",
    "deu": "de",
    "ell": "el",
    "eng": "en",
    "est": "et",
    "fin": "fi",
    "fra": "fr",
    "gle": "ga",
    "hrv": "hr",
    "hun": "hu",
    "ita": "it",
    "lav": "lv",
    "lit": "lt",
    "mlt": "mt",
    "nld": "nl",
    "pol": "pl",
    "por": "pt",
    "ron": "ro",
    "slk": "sk",
    "slv": "sl",
    "spa": "es",
    "swe": "sv",
}
_LANGUAGE_CODES = {
    **{value: key for key, value in LANGUAGES.items()},
    **{key: key for key in LANGUAGES},
}
_ARTICLE_ID = re.compile(r"art(?:_(\d+[a-z]*))?\Z")
_PARAGRAPH_ID = re.compile(r"(\d+[a-z]*)\.(\d+[a-z]*)\Z")
_CONSOLIDATED_REFERENCE = re.compile(
    r"(0\d{4}[A-Z]\d{4})\s*[—–-]\s*([A-Z]{2})\s*[—–-]\s*(\d{2}\.\d{2}\.\d{4})"
)
_MAX_BYTES = 50 * 1024 * 1024
EXTRACTION_VERSION = 1
EXPORT_TABLES = (
    "multilingual_documents",
    "text_units_multilingual",
    "multilingual_coverage",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS _multilingual_languages (language VARCHAR PRIMARY KEY);
CREATE TABLE IF NOT EXISTS multilingual_documents (
    celex_id VARCHAR,
    language VARCHAR,
    status VARCHAR,
    source_url VARCHAR,
    source_sha256 VARCHAR,
    retrieved_at_utc TIMESTAMP,
    http_status INTEGER,
    http_content_language VARCHAR,
    extraction_version INTEGER,
    article_count INTEGER,
    paragraph_count INTEGER,
    detail VARCHAR,
    PRIMARY KEY (celex_id, language)
);
CREATE TABLE IF NOT EXISTS _multilingual_sources (
    celex_id VARCHAR,
    language VARCHAR,
    raw_html BLOB,
    PRIMARY KEY (celex_id, language)
);
CREATE TABLE IF NOT EXISTS text_units_multilingual (
    celex_id VARCHAR,
    language VARCHAR,
    unit_key VARCHAR,
    type VARCHAR,
    source_id VARCHAR,
    article_num VARCHAR,
    paragraph_num VARCHAR,
    title VARCHAR,
    text VARCHAR,
    unit_order INTEGER,
    has_unanchored_text BOOLEAN,
    PRIMARY KEY (celex_id, language, source_id)
);
CREATE OR REPLACE VIEW multilingual_coverage AS
SELECT u.celex_id, u.unit_key, u.type, u.source_id, u.article_num, u.paragraph_num,
       string_agg(u.language, ',' ORDER BY u.language) AS languages_present,
       count(*) AS language_count,
       (SELECT count(*) FROM _multilingual_languages) AS requested_language_count,
       (SELECT string_agg(l.language, ',' ORDER BY l.language)
        FROM _multilingual_languages l
        WHERE NOT EXISTS (
            SELECT 1 FROM text_units_multilingual t
            WHERE t.celex_id = u.celex_id AND t.unit_key = u.unit_key
                  AND t.language = l.language
        )) AS languages_missing
FROM text_units_multilingual u
GROUP BY u.celex_id, u.unit_key, u.type, u.source_id, u.article_num, u.paragraph_num;
"""


class UnsupportedHtml(ValueError):
    """The HTML cannot support extraction with verified source identifiers."""


@dataclass(frozen=True)
class MultilingualResult:
    output_directory: Path
    extracted: int = 0
    skipped: int = 0
    unavailable: int = 0
    unsupported: int = 0
    failed: int = 0


def normalize_languages(languages: Sequence[str]) -> list[str]:
    """Validate official EU language codes and retain their requested order."""
    result = []
    for value in languages:
        code = _LANGUAGE_CODES.get(value.strip().lower())
        if code is None:
            raise ValueError(
                f"Unsupported language {value!r}; use: {', '.join(LANGUAGES)}"
            )
        if code not in result:
            result.append(code)
    if not result:
        raise ValueError("Request at least one language.")
    return result


def _verify_language(tree, celex_id: str, language: str, content_language: str) -> None:
    """Reject absent or conflicting language evidence instead of labelling a fallback."""
    evidence = [
        tree.get("lang", ""),
        tree.get("{http://www.w3.org/XML/1998/namespace}lang", ""),
    ]
    evidence.extend(content_language.split(","))
    evidence.extend(
        tree.xpath(
            ".//*[local-name()='meta' and "
            "translate(@http-equiv, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
            "='content-language']/@content"
        )
    )
    for element in tree.iter():
        if not isinstance(element.tag, str):
            continue
        classes = set(element.get("class", "").split())
        if {"oj-hd-lg", "hd-lg"}.intersection(classes):
            evidence.append("".join(element.itertext()))
        if "reference" in classes:
            reference = _CONSOLIDATED_REFERENCE.match(
                "".join(element.itertext()).strip()
            )
            if reference:
                date = datetime.strptime(reference[3], "%d.%m.%Y").strftime("%Y%m%d")
                if f"{reference[1]}-{date}" != celex_id:
                    raise ValueError(
                        "Consolidated source reference disagrees with the requested CELEX/date."
                    )
                evidence.append(reference[2])
    codes = {
        _LANGUAGE_CODES[value.strip().lower().split("-")[0]]
        for value in evidence
        if value.strip().lower().split("-")[0] in _LANGUAGE_CODES
    }
    if not codes:
        raise UnsupportedHtml(
            "No recognised language declaration or OJ language header."
        )
    if codes != {language}:
        raise ValueError(
            f"Requested {language}, but HTML/HTTP language evidence is {sorted(codes)}."
        )


def _number(value: str) -> str:
    """Normalise numerical padding without discarding amendment suffixes."""
    return value.lstrip("0") or "0"


def _remove_with_tail(element) -> None:
    """Remove an anchored paragraph while retaining following unanchored text."""
    parent = element.getparent()
    previous = element.getprevious()
    tail = element.tail or ""
    parent.remove(element)
    if previous is None:
        parent.text = (parent.text or "") + tail
    else:
        previous.tail = (previous.tail or "") + tail


def extract_official_units(
    raw: bytes,
    celex_id: str,
    language: str,
    *,
    content_language: str = "",
) -> list[dict]:
    """Retain whole articles and anchored numbered paragraphs without inferred alignment."""
    if not is_valid_celex(celex_id):
        raise ValueError(f"Invalid CELEX ID: {celex_id}")
    language = normalize_languages([language])[0]
    if len(raw) > _MAX_BYTES:
        raise UnsupportedHtml("HTML exceeds the 50 MB limit; no text was truncated.")
    try:
        tree = parse_html(raw)
    except (etree.Error, ValueError) as exc:
        raise UnsupportedHtml("Content could not be parsed as HTML.") from exc
    _verify_language(tree, celex_id, language, content_language)
    articles = [
        element
        for element in tree.iter()
        if isinstance(element.tag, str)
        and etree.QName(element).localname == "div"
        and _ARTICLE_ID.fullmatch(element.get("id", ""))
    ]
    if not articles:
        raise UnsupportedHtml(
            "No supported article identifiers; legacy HTML and PDFs are outside this mode."
        )

    units: list[dict] = []
    seen: set[str] = set()
    for article in articles:
        identifier = article.get("id")
        number = (
            _number(identifier.removeprefix("art_")) if identifier != "art" else "sole"
        )
        if any(parent in articles for parent in article.iterancestors()):
            # Replacement law remains inside the enclosing amending article.
            continue
        titles = [
            child
            for child in article
            if isinstance(child.tag, str)
            and "eli-title" in child.get("class", "").split()
        ]
        title = (
            normalize_string(" ".join(_iter_visible_text(titles[0])))
            if titles
            else None
        )
        paragraphs = [
            child
            for child in article
            if isinstance(child.tag, str)
            and etree.QName(child).localname == "div"
            and _PARAGRAPH_ID.fullmatch(child.get("id", ""))
        ]
        remainder = deepcopy(article)
        paragraph_ids = {paragraph.get("id") for paragraph in paragraphs}
        for element in list(remainder):
            if element.get("id") in paragraph_ids:
                _remove_with_tail(element)
        article_text = normalize_string(" ".join(_walk_article_body(article)))
        if not article_text:
            raise UnsupportedHtml(f"Article {identifier} has no extractable body text.")
        candidates: list[tuple[etree._Element, str, str | None, str, bool]] = [
            (
                article,
                "article",
                None,
                article_text,
                bool(_walk_article_body(remainder)),
            )
        ]
        for paragraph in paragraphs:
            match = _PARAGRAPH_ID.fullmatch(paragraph.get("id"))
            assert match is not None
            if _number(match[1]) != ("1" if number == "sole" else number):
                raise UnsupportedHtml(
                    f"Paragraph {paragraph.get('id')} disagrees with its parent {identifier}."
                )
            text = normalize_string(" ".join(_walk_article_body(paragraph)))
            if not text:
                raise UnsupportedHtml(
                    f"Paragraph {paragraph.get('id')} has no extractable text."
                )
            candidates.append((paragraph, "paragraph", _number(match[2]), text, False))
        for element, unit_type, paragraph_num, text, unanchored in candidates:
            source_id = element.get("id")
            if source_id in seen:
                raise UnsupportedHtml(f"Duplicate source identifier: {source_id}")
            seen.add(source_id)
            units.append(
                {
                    "unit_key": f"{celex_id}:{source_id}",
                    "type": unit_type,
                    "source_id": source_id,
                    "article_num": number,
                    "paragraph_num": paragraph_num,
                    "title": title,
                    "text": text,
                    "unit_order": len(units) + 1,
                    "has_unanchored_text": unanchored,
                }
            )
    return units


def _record_expression(
    conn,
    celex_id: str,
    language: str,
    status: str,
    metadata: dict,
    raw: bytes | None,
    units: list[dict],
    detail: str,
) -> None:
    """Save one expression, its source bytes and units in a single transaction."""
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(
            "DELETE FROM text_units_multilingual WHERE celex_id = ? AND language = ?",
            [celex_id, language],
        )
        conn.execute(
            "DELETE FROM _multilingual_sources WHERE celex_id = ? AND language = ?",
            [celex_id, language],
        )
        if raw is not None:
            conn.execute(
                "INSERT INTO _multilingual_sources VALUES (?, ?, ?)",
                [celex_id, language, raw],
            )
        conn.execute(
            "INSERT OR REPLACE INTO multilingual_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                celex_id,
                language,
                status,
                metadata.get("source_url"),
                hashlib.sha256(raw).hexdigest() if raw is not None else None,
                datetime.now(timezone.utc).replace(tzinfo=None),
                metadata.get("http_status"),
                metadata.get("content_language"),
                EXTRACTION_VERSION,
                sum(unit["type"] == "article" for unit in units),
                sum(unit["type"] == "paragraph" for unit in units),
                detail or None,
            ],
        )
        if units:
            conn.executemany(
                "INSERT INTO text_units_multilingual VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    [
                        celex_id,
                        language,
                        unit["unit_key"],
                        unit["type"],
                        unit["source_id"],
                        unit["article_num"],
                        unit["paragraph_num"],
                        unit["title"],
                        unit["text"],
                        unit["unit_order"],
                        unit["has_unanchored_text"],
                    ]
                    for unit in units
                ],
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def extract_multilingual_database(
    db_path: str | Path,
    *,
    languages: Sequence[str],
    output_directory: str | Path | None = None,
    formats: Sequence[str] = ("parquet",),
    limit: int | None = None,
    retry_unavailable: bool = False,
) -> MultilingualResult:
    """Read source CELEX IDs without writes and build a resumable, separate language dataset."""
    language_codes = normalize_languages(languages)
    if not formats or set(formats) - {"parquet", "csv"}:
        raise ValueError("Choose parquet and/or csv output.")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive.")
    source_path = Path(db_path).resolve()
    if not source_path.is_file():
        raise ValueError(f"Database file not found: {source_path}")
    out = (
        Path(output_directory)
        if output_directory is not None
        else source_path.parent / f"{source_path.stem}-multilingual"
    ).resolve()
    destination = out / "multilingual.duckdb"
    output_paths = [
        destination,
        *[out / f"{table}.{fmt}" for table in EXPORT_TABLES for fmt in formats],
    ]
    if any(
        path == source_path or (path.exists() and path.samefile(source_path))
        for path in output_paths
    ):
        raise ValueError(
            "Multilingual output must be separate from the source database."
        )
    with duckdb.connect(str(source_path), read_only=True) as original:
        celex_ids = [
            row[0]
            for row in original.execute(
                "SELECT celex_id FROM works ORDER BY celex_id"
            ).fetchall()
        ]
    if any(
        not isinstance(value, str) or not is_valid_celex(value) for value in celex_ids
    ):
        raise ValueError("Source works table contains invalid CELEX identifiers.")

    if destination.exists():
        with duckdb.connect(str(destination), read_only=True) as existing:
            tables = {
                row[0]
                for row in existing.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
                ).fetchall()
            }
        if not {
            "_multilingual_languages",
            "multilingual_documents",
            "text_units_multilingual",
            "_multilingual_sources",
        }.issubset(tables):
            raise ValueError(
                "Output database already exists and is not a multilingual dataset; choose a new output directory."
            )

    out.mkdir(parents=True, exist_ok=True)
    counts = dict.fromkeys(
        ("extracted", "skipped", "unavailable", "unsupported", "failed"), 0
    )
    source = CellarSource()
    try:
        with duckdb.connect(str(destination)) as conn:
            conn.execute(_SCHEMA)
            versions = conn.execute(
                "SELECT DISTINCT extraction_version FROM multilingual_documents"
            ).fetchall()
            if any(version != EXTRACTION_VERSION for (version,) in versions):
                raise ValueError(
                    "Existing output uses a different multilingual extractor version; choose a new output directory."
                )
            conn.executemany(
                "INSERT OR IGNORE INTO _multilingual_languages VALUES (?)",
                [[language] for language in language_codes],
            )
            processed_documents = 0
            for celex_id in celex_ids:
                completed = dict(
                    conn.execute(
                        "SELECT language, status FROM multilingual_documents "
                        "WHERE celex_id = ?",
                        [celex_id],
                    ).fetchall()
                )
                pending = [
                    language
                    for language in language_codes
                    if completed.get(language)
                    not in {"extracted", "unavailable", "unsupported"}
                    or (
                        retry_unavailable
                        and completed.get(language) in {"unavailable", "unsupported"}
                    )
                ]
                counts["skipped"] += len(language_codes) - len(pending)
                if not pending:
                    continue
                if limit is not None and processed_documents >= limit:
                    break
                processed_documents += 1
                for language in pending:
                    metadata = {
                        "source_url": f"{CELLAR_RESOURCE_BASE}{quote(celex_id, safe='')}"
                    }
                    raw: bytes | None = None
                    units: list[dict] = []
                    detail = ""
                    try:
                        raw = source._fetch_with_300_handling(
                            metadata["source_url"],
                            {"Accept": HTML_ACCEPT, "Accept-Language": language},
                            celex_id,
                            out_metadata=metadata,
                        )
                        if raw is None:
                            if metadata.get("http_status") not in (404, 406):
                                raise ValueError(
                                    f"No usable HTML response (HTTP {metadata.get('http_status')})."
                                )
                            status = "unavailable"
                            detail = "Requested HTML was unavailable; this does not establish absence of an official PDF."
                        else:
                            units = extract_official_units(
                                raw,
                                celex_id,
                                language,
                                content_language=metadata.get("content_language", ""),
                            )
                            status = "extracted"
                    except UnsupportedHtml as exc:
                        status, detail = "unsupported", str(exc)
                    except Exception as exc:
                        status, detail = "failed", str(exc)
                        logger.warning(
                            "Multilingual extraction failed for %s/%s: %s",
                            celex_id,
                            language,
                            exc,
                        )
                    _record_expression(
                        conn, celex_id, language, status, metadata, raw, units, detail
                    )
                    counts[status] += 1
                    logger.info(
                        "%s/%s: %s (%d articles, %d paragraphs)%s",
                        celex_id,
                        language,
                        status,
                        sum(unit["type"] == "article" for unit in units),
                        sum(unit["type"] == "paragraph" for unit in units),
                        f" — {detail}" if detail else "",
                    )
            export_tables(conn, str(out), formats, tables=EXPORT_TABLES)
    finally:
        source.session.close()
    return MultilingualResult(output_directory=out, **counts)
