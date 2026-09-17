"""Corroborated repair of a single delayed Docling article heading."""

from __future__ import annotations

import re

from eurlex_builder.extractors.pdf_languages import LEGISLATIVE_MARKERS
from eurlex_builder.extractors.splitter import (
    _find_quoted_regions,
    _is_in_quoted_region,
)

_JOURNALS = {
    "eng": ("EN", "Official Journal of the European Union"),
    "fra": ("FR", "Journal officiel de l'Union européenne"),
    "deu": ("DE", "Amtsblatt der Europäischen Union"),
    "ita": ("IT", "Gazzetta ufficiale dell'Unione europea"),
    "nld": ("NL", "Publicatieblad van de Europese Unie"),
    "spa": ("ES", "Diario Oficial de la Unión Europea"),
}


def _headings(text: str, language: str) -> list[tuple[int, int, int]]:
    """Locate standalone operative headings, excluding quotations and annexes."""
    native = LEGISLATIVE_MARKERS.get(language)
    article = native.compile(native.article + r"\s*$") if native else re.compile(
        r"^(?:#{1,3}\s+)?Article\s+(?P<number>\d+)\s*$",
    )
    formula = native.compile(native.enacting) if native else re.compile(
        r"^(?:#{1,3}\s+)?(?:HAS|HAVE)\s+(?:ADOPTED|DECIDED|AGREED)\b",
    )
    boundary = native.compile(native.signature + "|" + native.annex_word + r"\b") if native else re.compile(
        r"^(?:#{1,3}\s+)?(?:Done at\b|ANNEX\b)",
    )
    quotes = _find_quoted_regions(text)
    active = False
    offset = 0
    found: list[tuple[int, int, int]] = []
    for line in text.splitlines(keepends=True):
        value = line.strip()
        if not _is_in_quoted_region(offset, quotes):
            if formula.match(value) and value.lstrip("# ").isupper():
                active = True
            if active and boundary.match(value):
                break
            match = article.fullmatch(value)
            if active and match and value.lstrip("# ")[0].isupper():
                number = match.groupdict().get("number") or (
                    "1" if match.groupdict().get("first") else ""
                )
                if not number.isdigit():
                    return []
                found.append((int(number), offset, offset + len(line)))
        offset += len(line)
    return found


def _normalize(text: str) -> str:
    """Ignore formatting only when comparing a candidate with its reference."""
    text = re.sub(r"<!--\s*image\s*-->", "", text)
    text = re.sub(r"(?m)^[ \t]*(?:#{1,3}[ \t]+|-[ \t]+)", "", text)
    return re.sub(r"\s+", "", text.replace("\xad", ""))


def _same_body(candidate: str, reference: str, language: str) -> bool:
    """Require full body agreement, allowing a corroborated OJ page header."""
    code, journal = _JOURNALS[language]
    header = re.compile(
        r"(?m)^[ \t]*\d{1,2}\.[ \t]*\d{1,2}\.[ \t]*\d{4}[ \t]*\n"
        r"[ \t]*L[ \t]*\d+/\d+[ \t]*\n[ \t]*"
        + re.escape(journal) + r"[ \t]*\n[ \t]*" + code + r"[ \t]*(?=\n|$)",
    )
    reference, headers = header.subn("", reference)
    # Docling sometimes retains just the language label of the same header.
    # Without a full reference header, that label remains ordinary body text.
    if headers:
        candidate = header.sub("", candidate)
        candidate, labels = re.subn(r"(?m)^[ \t]*" + code + r"[ \t]*$", "", candidate)
        if labels > headers:
            return False
    return _normalize(candidate) == _normalize(reference)


def repair_docling_heading(
    markdown: str, text_layer: str, language: str,
) -> tuple[str, dict]:
    """Move one delayed heading only when every touched article matches the PDF."""
    if language not in _JOURNALS:
        return markdown, {}
    source = _headings(text_layer, language)
    converted = _headings(markdown, language)
    expected = list(range(1, len(source) + 1))
    numbers = [number for number, _, _ in converted]
    if all(a <= b for a, b in zip(numbers, numbers[1:])):
        return markdown, {}

    def decline(reason: str) -> tuple[str, dict]:
        return markdown, {"status": "declined", "reason": reason}

    if [number for number, _, _ in source] != expected or sorted(numbers) != expected:
        return decline("incomplete_or_conflicting_reference")
    delayed = [
        i for i, number in enumerate(numbers)
        if number > 1 and i > number - 1 and i < len(numbers) - 1
        and numbers[:i] + numbers[i + 1:] == [n for n in expected if n != number]
    ]
    if len(delayed) != 1:
        return decline("not_one_delayed_heading")
    index = delayed[0]
    number, start, end = converted[index]
    if not markdown[start:end].lstrip().startswith("#"):
        return decline("unmarked_heading")
    title = re.match(r"[ \t\n]*(#{1,3}[ \t]+[^\n]+)\n[ \t\n]*", markdown[end:])
    ref_title = re.match(r"[ \t\n]*([^\n]+)\n", text_layer[source[number - 1][2]:])
    if not title or not ref_title or _normalize(title[1]) != _normalize(ref_title[1]):
        return decline("title_mismatch")
    body = text_layer[source[number - 1][2] + ref_title.end():source[number][1]]
    anchor = _normalize(body)[:100]
    if sum(c.isalpha() for c in anchor) < 40:
        return decline("short_body_anchor")
    targets = [
        match.start() for match in re.finditer(r"(?m)^.+$", markdown)
        if _normalize(match.group()) and _normalize(markdown[match.start():match.start() + 500]).startswith(anchor)
    ]
    if len(targets) != 1:
        return decline("ambiguous_body_anchor")
    target = targets[0]
    previous = next(h for h in converted if h[0] == number - 1)
    following = next(h for h in converted if h[0] == number + 1)
    move_end = end + title.end()
    if not previous[2] < target < following[1] < start:
        return decline("body_anchor_outside_neighbours")
    if any(a < move_end and b > target for a, b in _find_quoted_regions(markdown)):
        return decline("quoted_region")
    candidate = markdown[:target] + markdown[start:move_end] + markdown[target:start] + markdown[move_end:]
    repaired = _headings(candidate, language)
    if [n for n, _, _ in repaired] != expected:
        return decline("sequence_not_restored")
    for n in range(number - 1, numbers[index - 1] + 1):
        if not _same_body(
            candidate[repaired[n - 1][2]:repaired[n][1]],
            text_layer[source[n - 1][2]:source[n][1]], language,
        ):
            return decline("article_body_mismatch")
    return candidate, {"status": "repaired", "number": str(number)}
