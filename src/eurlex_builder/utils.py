"""Shared utilities: CELEX validation, string normalization."""

from __future__ import annotations

import re
import unicodedata


# Matches standard CELEX IDs like 32016R0679, 52005DC0229, 02016R0679-20210101
CELEX_PATTERN = re.compile(r"^[0-9][0-9A-Za-z]+$")


def is_valid_celex(celex_id: str) -> bool:
    """Check if a string looks like a valid CELEX ID."""
    return bool(CELEX_PATTERN.match(celex_id)) and len(celex_id) >= 8


def is_consolidated_celex(celex_id: str) -> bool:
    """Consolidated texts start with sector 0."""
    return celex_id.startswith("0")


def convert_consolidated_to_original(celex_id: str) -> str:
    """Convert a consolidated CELEX ID to the original act's CELEX ID.

    Example: 02016R0679-20210101 -> 32016R0679
    """
    if not is_consolidated_celex(celex_id):
        return celex_id
    base = celex_id.split("-")[0]
    return "3" + base[1:]


def normalize_string(text: str) -> str:
    """Normalize whitespace and unicode in a string."""
    text = unicodedata.normalize("NFKC", text)
    # Remove soft hyphens (appear at line breaks in PDF-extracted text).
    text = text.replace("\u00ad", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_type_code(celex_id: str) -> str:
    """Extract the document type code from a CELEX ID (e.g. 'R', 'L', 'D', 'DC')."""
    try:
        return celex_id[5:7].rstrip("0123456789")
    except IndexError:
        return ""


# Maps CELEX type codes to human-readable document type names
DOC_TYPE_MAP: dict[str, str] = {
    "L": "directive",
    "R": "regulation",
    "D": "decision",
    "PC": "proposal",
    "DC": "communication",
    "SC": "staff working document",
    "IP": "institutional proposal",
}

# Reverse map: human-readable name → (type_code, sector)
# Sector 3 = adopted legislation, sector 5 = preparatory documents
_REVERSE_DOC_TYPE_MAP: dict[str, tuple[str, str]] = {
    "directive": ("L", "3"),
    "regulation": ("R", "3"),
    "decision": ("D", "3"),
    "proposal": ("PC", "5"),
    "communication": ("DC", "5"),
    "staff working document": ("SC", "5"),
    "institutional proposal": ("IP", "5"),
}


def resolve_doc_type(name: str) -> tuple[str, str] | None:
    """Map a human-readable document type to (celex_type_code, sector) or None."""
    return _REVERSE_DOC_TYPE_MAP.get(name.lower())


def get_document_type(celex_id: str) -> str:
    """Derive the document type name from a CELEX ID."""
    code = extract_type_code(celex_id)
    return DOC_TYPE_MAP.get(code, "unknown")


# Document types that have legislative structure (recitals, articles, annexes).
STRUCTURAL_DOC_TYPES = frozenset({"regulation", "directive", "decision"})


# ---------------------------------------------------------------------------
# Boilerplate removal
# ---------------------------------------------------------------------------

# Matches the "This Regulation/Directive/Decision shall be binding..." closing formula.
# Covers both "in all Member States." and "in the Member States in accordance with...".
_BINDING_RE = re.compile(
    r"\s*This (?:Regulation|Decision|Directive) shall be binding in its entirety"
    r" and directly applicable in (?:all )?(?:the )?Member States\b.*",
    re.DOTALL,
)

# Matches "Done at <City>..." signature blocks and everything after.
# Tolerant of OCR artifacts between city and date (e.g. "Done at Brussels^ 11 December").
_DONE_AT_RE = re.compile(r"\s*Done at \w+\W?\s+.*$", re.DOTALL)

# Matches "For the Commission/Council/European Parliament..." signature lines.
_SIGNATURE_RE = re.compile(
    r"\s*For the (?:Commission|Council|European Parliament)\b.*$", re.DOTALL
)

# Matches trailing OJ references like "(1) OJ L 337, ...".
_OJ_REFS_RE = re.compile(
    r"\s*(?:\(\s*\d+\s*\)|(?:\[\s*\d+\s*\]))\s*OJ\s+(?:No\s+)?[LC]\s+\d+.*$",
    re.DOTALL,
)

# Trailing separator lines.
_SEPARATOR_RE = re.compile(r"\s*-{3,}\s*$")


def strip_boilerplate(text: str) -> str:
    """Remove signature blocks, closing formulas, and OJ refs from text."""
    if not text:
        return text
    text = _BINDING_RE.sub("", text)
    text = _DONE_AT_RE.sub("", text)
    text = _SIGNATURE_RE.sub("", text)
    text = _OJ_REFS_RE.sub("", text)
    text = _SEPARATOR_RE.sub("", text)
    return text.rstrip()
