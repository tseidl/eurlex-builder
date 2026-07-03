"""Sub-article splitting at paragraph and point granularity.

EU drafting convention (Joint Practical Guide):
  Article > Paragraph (numbered "1.", "2.", ...) > Point (lettered "(a)", "(b)", ...)
  Citation form: Article 5(2)(a) = Article 5, paragraph 2, point (a).

This module takes the body_parts produced by an HTML/PDF extractor (one string
per source element, in document order) and returns one or more text_unit dicts
according to the requested granularity:

  - "article":   one unit per article (current behavior, byte-identical)
  - "paragraph": one unit per numbered paragraph; preamble (text before "1.")
                 gets paragraph_num="0"
  - "point":     one unit per lettered point when present, otherwise falls
                 back to paragraph

Amending-act articles (Article 1 of an amending regulation is a list of edits
to another act) are detected heuristically. Mechanical edit paragraphs get
subtype="amendment_item"; substantive replacement paragraphs (introduce
quoted replacement text) keep subtype=None and stay as a single row.
"""

from __future__ import annotations

import re

# Newline-anchored marker: paragraph break before the digit.
# Strict — used first, for clean structural extraction.
# Allows optional whitespace between the digit and period (handles the
# "1 ." OCR artifact common in 1980s-1990s PDF-extracted regulations).
_PARA_MARKER_STRICT = re.compile(
    r"(?:^|\n)\s*(\d+[a-z]?)\s*\.\s+(?=\S)"
)

# Lenient fallback: also matches after sentence-ending punctuation when the
# body got smushed into a single string (older HTML, PDF extraction).
_PARA_MARKER_LENIENT = re.compile(
    r"(?:^|\n|(?<=[.!?:;])\s+)(\d+[a-z]?)\s*\.\s+(?=\S)"
)

# Lettered point marker: "(a) ", "(b) ", "(aa) " ... ONLY anchored to
# start-of-line. Inline cross-references like "point (f) of paragraph 1" must
# NOT match. This relies on structural newlines between source elements being
# preserved. Candidates are further validated against the drafting sequence
# (see _filter_point_sequence) so roman sub-points "(i)", "(ii)" inside a
# point stay part of that point.
_POINT_MARKER = re.compile(
    r"(?:^|\n)\s*\(([a-z]{1,2})\)\s+(?=\S)"
)

# Heuristic: is this article an "Amendments to Regulation X" article?
_AMENDING_TITLE = re.compile(
    r"^(amendments?\s+to|amending|modifications?\s+to|modifying)\b",
    re.IGNORECASE,
)

# Inside an amending article, this marker pattern indicates a mechanical edit
# (deletion, renumbering, cross-ref change). Substantive replacements contain
# a quoted block (colon followed by «...», '...', or "...").
_REPLACEMENT_INTRO = re.compile(
    r":\s*[‘“«„\"'‹]"
)

# Quoted-block detection: EU amending acts express substantive replacements as
# `replaced by the following: '...'.` The inner content is the new law; markers
# inside it (own paragraph numbers, lettered points) must NOT be split as if
# they were paragraphs of the amending act. We detect the open-quote that
# follows a colon and find its matching close.
_QUOTED_BLOCK_OPEN = re.compile(r":\s*([\'‘\"“«])")
_CLOSE_QUOTE_FOR = {"'": "'", "‘": "’", '"': '"', "“": "”", "«": "»"}

# A close-quote candidate only ends the region when followed by punctuation,
# a newline, or end-of-text (OJ convention: quoted blocks end with ’. ’; ’,).
# The same characters appear as apostrophes ("Member States’ obligations"),
# where the next character is a space or letter — those must not close.
_PUNCT_AFTER_CLOSE = ".,;:)\n"


def _find_close_quote(text: str, start: int, close_q: str) -> int:
    """Find the first close-quote that isn't a word-internal apostrophe."""
    pos = start
    while True:
        idx = text.find(close_q, pos)
        if idx == -1:
            return -1
        following = text[idx + 1 : idx + 2]
        if not following or following in _PUNCT_AFTER_CLOSE:
            return idx
        pos = idx + 1


def _find_quoted_regions(text: str) -> list[tuple[int, int]]:
    """Find quoted-replacement regions in text. Returns list of (start, end)."""
    regions: list[tuple[int, int]] = []
    pos = 0
    while pos < len(text):
        m = _QUOTED_BLOCK_OPEN.search(text, pos)
        if not m:
            break
        open_q = m.group(1)
        close_q = _CLOSE_QUOTE_FOR[open_q]
        close_idx = _find_close_quote(text, m.end(), close_q)
        if close_idx == -1:
            # No punctuation-gated close. If the close character occurs at
            # all, this is an inline quoted term ("known as: ‘Erasmus+’ and
            # shall…") — close there rather than swallowing the rest of the
            # article and suppressing its paragraph markers.
            close_idx = text.find(close_q, m.end())
        if close_idx == -1:
            # The quote genuinely never closes: after ": ‘" everything that
            # follows is replacement content.
            regions.append((m.end() - 1, len(text)))
            break
        # Region covers the open quote position through one past the close.
        regions.append((m.end() - 1, close_idx + 1))
        pos = close_idx + 1
    return regions


def _is_in_quoted_region(pos: int, regions: list[tuple[int, int]]) -> bool:
    return any(s <= pos < e for s, e in regions)


def _point_key(letter: str) -> int | None:
    """Ordering key for point letters: (a)=100, (aa)=101, (ab)=102, (b)=200 …

    Amendment-inserted points sort between their base letter and the next.
    Two-letter markers with a suffix beyond 'c' — (ii), (iv), (ix), … — are
    roman sub-points, not insertions, and get no key.
    """
    if len(letter) == 2 and letter[1] > "c":
        return None
    key = (ord(letter[0]) - ord("a") + 1) * 100
    if len(letter) == 2:
        key += ord(letter[1]) - ord("a") + 1
    return key


def _filter_point_sequence(matches: list) -> list:
    """Keep only markers that form a plausible point sequence.

    Points are an ordered list, but amendments insert letters ((aa) between
    (a) and (b)) and delete them, and extractors sometimes lose the first
    marker into the stem text. So: the sequence must start at (a), (aa), or
    (b), and each following marker must be strictly ahead of the last kept
    one by at most three base letters (gap tolerance for deleted points).
    Roman sub-points — (ii), (iv), or an (i) far from its neighbours — fail
    these rules and stay inside their parent point's text; a genuine point
    (i) directly after (h) still validates.
    """
    kept: list = []
    last_key = 0
    for m in matches:
        key = _point_key(m.group(1))
        if key is None:
            continue
        if not kept:
            if key <= 200:
                kept.append(m)
                last_key = key
        elif 0 < key - last_key <= 300:
            kept.append(m)
            last_key = key
    return kept


def _make_unit(
    *,
    type: str = "article",
    number: str | None,
    title: str | None,
    text: str,
    subtype: str | None = None,
    paragraph_num: str | None = None,
    point_letter: str | None = None,
) -> dict:
    return {
        "type": type,
        "subtype": subtype,
        "number": number,
        "paragraph_num": paragraph_num,
        "point_letter": point_letter,
        "title": title,
        "text": text,
    }


def _normalize_text(text: str) -> str:
    """Collapse newlines and runs of whitespace to single spaces."""
    return re.sub(r"\s+", " ", text).strip()


def _detect_paragraph_markers(structured_text: str) -> list[tuple[int, int, str]]:
    """Find numbered paragraph markers. Returns list of (start, end, marker).

    Strategy: collect strict (newline-anchored) markers, then also run the
    lenient (sentence-anchored) pattern. Use lenient ONLY if it strictly
    extends the strict list (every strict marker is in the lenient list, and
    lenient finds more). When strict finds nothing, do NOT fall back — that
    would produce spurious splits in concatenated annex/footnote text.

    Why "strictly extends" rather than ">= len(strict)+1": prevents a lenient
    set that disagrees with strict from overriding it; lenient is allowed only
    to add siblings that strict missed because they were on the same line.
    """
    quoted = _find_quoted_regions(structured_text)
    strict = [
        (m.start(), m.end(), m.group(1))
        for m in _PARA_MARKER_STRICT.finditer(structured_text)
        if not _is_in_quoted_region(m.start(), quoted)
    ]
    if not strict:
        # No real paragraph structure detected; do not fall back to lenient.
        return strict

    lenient = [
        (m.start(), m.end(), m.group(1))
        for m in _PARA_MARKER_LENIENT.finditer(structured_text)
        if not _is_in_quoted_region(m.start(), quoted)
    ]
    if len(lenient) <= len(strict):
        return strict

    # Lenient may add siblings, but every strict marker must be present in
    # lenient (positions match). This is the normal case since lenient's
    # anchor is a superset of strict's.
    strict_positions = {pos for pos, _, _ in strict}
    lenient_positions = {pos for pos, _, _ in lenient}
    if strict_positions.issubset(lenient_positions):
        return lenient
    return strict


def _split_paragraph_into_points(
    para_text: str, *, number: str | None, title: str | None,
    paragraph_num: str | None, subtype: str | None,
) -> list[dict] | None:
    """Split a paragraph on lettered (a), (b), ... markers.

    Emits one row per: stem (text before the first point); each point — where
    continuation lines between two points are absorbed into the PRECEDING
    point (not dropped); and each trailing subparagraph appearing after the
    LAST point (e.g. GDPR Art 6(1) ends with "Point (f) of the first
    subparagraph shall not apply to processing carried out by public
    authorities…" — that text qualifies point (f) but is structurally a
    separate subparagraph and gets its own row).

    Inside quoted replacement blocks of amending acts, inner (a)/(b) markers
    are ignored so we don't split the quoted-law content.

    Returns None when no point structure detected.
    """
    quoted = _find_quoted_regions(para_text)
    matches = _filter_point_sequence([
        m for m in _POINT_MARKER.finditer(para_text)
        if not _is_in_quoted_region(m.start(), quoted)
    ])
    if len(matches) < 2:
        return None

    # Subparagraph boundary marker: a sentence-ending line followed by an
    # uppercase-starting non-point line. Used only to detect content AFTER
    # the last point. Both conditions are needed to distinguish a genuine
    # trailing subparagraph ("…obligation.\nPoint (f) of the first
    # subparagraph shall not apply…") from a wrapped continuation of the
    # last point — whether lowercase ("…compliance\nwith a legal obligation")
    # or capitalized ("…in accordance with\nRegulation (EU) 2016/679") —
    # which must stay inside the point.
    subpara_start_re = re.compile(r"(?<=[.;:!?])\n\s*(?!\([a-z]{1,2}\)\s+)(?=[A-Z0-9])")

    units: list[dict] = []

    # Stem: text before the first (a).
    first_start = matches[0].start()
    stem = _normalize_text(para_text[:first_start])
    if stem:
        units.append(_make_unit(
            number=number, title=title, text=stem,
            paragraph_num=paragraph_num, point_letter=None, subtype=subtype,
        ))

    # Each point's text runs from its marker to the next point marker.
    # Continuation lines between two points belong to the preceding point.
    # For the LAST point, the text ends at the first subparagraph start (if
    # any) so the trailing qualifier becomes its own row.
    last_point_text_end = len(para_text)
    for i, m in enumerate(matches):
        letter = m.group(1)
        text_start = m.end()
        if i + 1 < len(matches):
            text_end = matches[i + 1].start()
        else:
            sp = subpara_start_re.search(para_text, text_start)
            text_end = sp.start() if sp else len(para_text)
            last_point_text_end = text_end
        point_text = _normalize_text(para_text[text_start:text_end])
        if not point_text:
            continue
        units.append(_make_unit(
            number=number, title=title if (i == 0 and not stem) else None,
            text=point_text,
            paragraph_num=paragraph_num, point_letter=letter, subtype=subtype,
        ))

    # Trailing subparagraph after the last point.
    if last_point_text_end < len(para_text):
        trailing = _normalize_text(para_text[last_point_text_end:])
        if trailing:
            units.append(_make_unit(
                number=number, title=None, text=trailing,
                paragraph_num=paragraph_num, point_letter=None, subtype=subtype,
            ))

    return units if units else None


def _split_into_subparagraphs(
    raw_para_text: str, *, number: str | None, title: str | None,
    paragraph_num: str | None, subtype: str | None,
) -> list[dict] | None:
    """Split a paragraph into sub-paragraphs on newline boundaries.

    Fires at "point" granularity when no lettered points were found.
    Each body_part from the HTML extractor corresponds to a <p> element,
    so newline boundaries represent genuine sub-paragraph breaks.

    Returns None if the text contains fewer than 2 sub-paragraphs.
    """
    lines = [l.strip() for l in raw_para_text.split("\n") if l.strip()]
    if len(lines) < 2:
        return None

    # Group lines: a line starting with uppercase starts a new sub-paragraph.
    # Lines starting with lowercase, digits, or quotes are continuations.
    groups: list[list[str]] = []
    for line in lines:
        if groups and not line[0].isupper():
            groups[-1].append(line)
        else:
            groups.append([line])

    if len(groups) < 2:
        return None

    # Require each group to be substantial (>= 40 chars) to avoid
    # spurious splits on short fragment lines or layout artifacts.
    if any(len(" ".join(g)) < 40 for g in groups):
        return None

    units: list[dict] = []
    for i, group in enumerate(groups):
        text = _normalize_text(" ".join(group))
        if not text:
            continue
        units.append(_make_unit(
            number=number,
            title=title if i == 0 else None,
            text=text,
            paragraph_num=paragraph_num,
            point_letter=None,
            subtype=subtype,
        ))

    return units if len(units) >= 2 else None


def is_amending_article(title: str | None, body_text: str) -> bool:
    """Heuristic detection of an amending-act article (a list of edits)."""
    if title and _AMENDING_TITLE.search(title):
        return True
    # Body-level signals: presence of replacement phrasing
    if re.search(
        r"\b(is|are)\s+(hereby\s+)?(replaced|amended|deleted|inserted)\b",
        body_text, re.IGNORECASE,
    ):
        # Require it appears at least twice (a single occurrence might just be a cross-ref).
        matches = re.findall(
            r"\b(?:is|are)\s+(?:hereby\s+)?(?:replaced|amended|deleted|inserted)\b",
            body_text, re.IGNORECASE,
        )
        return len(matches) >= 2
    return False


def _paragraph_subtype(paragraph_text: str, *, is_amending: bool) -> str | None:
    """For amending-act articles: tag mechanical edits as 'amendment_item',
    leave substantive replacements untagged (subtype=None).
    """
    if not is_amending:
        return None
    # If the paragraph introduces a quoted replacement block, treat as
    # substantive (subtype=None). Otherwise it's a mechanical edit.
    if _REPLACEMENT_INTRO.search(paragraph_text):
        return None
    return "amendment_item"


def split_article(
    body_parts: list[str],
    *,
    number: str | None,
    title: str | None,
    granularity: str = "article",
) -> list[dict]:
    """Split an article body into text_units according to granularity.

    body_parts: ordered list of strings from the source extractor (one per
    structural child element). The splitter joins them with newlines
    internally to preserve element boundaries as paragraph anchors, but emits
    final text with whitespace normalized.
    """
    # Filter empties, keep order.
    parts = [p for p in body_parts if p and p.strip()]

    if granularity == "article" or not parts:
        # Backward-compatible: single row, space-joined text (matches current).
        text = " ".join(p.strip() for p in parts)
        return [_make_unit(
            number=number, title=title, text=text,
            paragraph_num=None, point_letter=None, subtype=None,
        )]

    # Structured text with newlines preserves element boundaries for the regex.
    structured = "\n".join(p.strip() for p in parts)

    markers = _detect_paragraph_markers(structured)
    if not markers:
        # No paragraph structure detected.  At point granularity, the article
        # may still contain (a)/(b)/... points directly (no numbered paragraph
        # wrapper — e.g. InfoSoc Art. 2).
        if granularity == "point":
            point_units = _split_paragraph_into_points(
                structured, number=number, title=title,
                paragraph_num=None, subtype=None,
            )
            if point_units:
                return point_units
        text = " ".join(p.strip() for p in parts)
        return [_make_unit(
            number=number, title=title, text=text,
            paragraph_num=None, point_letter=None, subtype=None,
        )]

    is_amending = is_amending_article(title, structured)
    units: list[dict] = []

    # Preamble: text before the first numbered marker.
    first_start = markers[0][0]
    preamble = _normalize_text(structured[:first_start])
    if preamble:
        units.append(_make_unit(
            number=number, title=title, text=preamble,
            paragraph_num="0", point_letter=None, subtype=None,
        ))

    # Each numbered paragraph.
    for i, (start, end, marker) in enumerate(markers):
        text_start = end
        text_end = markers[i + 1][0] if i + 1 < len(markers) else len(structured)
        # Keep raw newlines for point detection; normalize only at emission.
        raw_para_text = structured[text_start:text_end].strip()
        para_text = _normalize_text(raw_para_text)
        if not para_text:
            continue

        subtype = _paragraph_subtype(para_text, is_amending=is_amending)
        # Title attaches to the first row when no preamble was emitted.
        row_title = title if (i == 0 and not preamble) else None

        if granularity == "point" and subtype != "amendment_item":
            point_units = _split_paragraph_into_points(
                raw_para_text, number=number, title=row_title,
                paragraph_num=marker, subtype=subtype,
            )
            if point_units:
                units.extend(point_units)
                continue
            # No lettered points — try sub-paragraph splitting.
            subpara_units = _split_into_subparagraphs(
                raw_para_text, number=number, title=row_title,
                paragraph_num=marker, subtype=subtype,
            )
            if subpara_units:
                units.extend(subpara_units)
                continue

        units.append(_make_unit(
            number=number, title=row_title, text=para_text,
            paragraph_num=marker, point_letter=None, subtype=subtype,
        ))

    return units
