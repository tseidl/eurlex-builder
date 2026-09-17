"""Conservative preamble cleanup for the PyMuPDF legislative fallback only."""

from __future__ import annotations

import logging
import re
from collections import Counter

from eurlex_builder.extractors.pdf_languages import LEGISLATIVE_MARKERS
from eurlex_builder.extractors.splitter import _find_quoted_regions

logger = logging.getLogger("eurlex_builder")
_NUMBERED = re.compile(r"^\(\s*\d+\s*\)\s*")
_JOURNAL = re.compile(
    r"\b(?:OJ|JO|ABl\.|GU|PB|DO)\s+(?:[LC]\s+)?\d|"
    r"\bSee page \d+ of this Official Journal\b|\bEuropean Court Reports \d{4}\b",
)
_ENACTING = re.compile(r"^(?:HAS|HAVE)\s+(?:ADOPTED|DECIDED|AGREED)\b")
_ARTICLE = re.compile(r"^(?:Article\s+1|Sole\s+Article)\s*[.:]?\s*$", re.I)


def _footnote_lines(
    lines: list[dict], body_size: float, height: float,
    recital: re.Pattern[str] | None = None,
) -> set[int]:
    """Identify small numbered citation groups at the bottom of a preamble page."""
    small_blocks: list[list[int]] = []
    for block in {line["block"] for line in lines}:
        indexes = [i for i, line in enumerate(lines) if line["block"] == block]
        if (
            min(lines[i]["bbox"][1] for i in indexes) > height * 0.5
            and max(lines[i]["size"] for i in indexes) < min(body_size * 0.94, body_size - 0.5)
        ):
            small_blocks.append(indexes)
    groups: list[list[int]] = []
    for indexes in sorted(small_blocks, key=lambda ix: lines[ix[0]]["bbox"][1]):
        for group in groups:
            previous, current = lines[group[-1]]["bbox"], lines[indexes[0]]["bbox"]
            # Glyph boxes can overlap slightly across PyMuPDF versions.
            if abs(previous[0] - current[0]) < 15 and -2 <= current[1] - previous[3] < 16:
                group.extend(indexes)
                break
        else:
            groups.append(indexes.copy())

    notes: set[int] = set()
    for group in groups:
        text = " ".join(lines[i]["text"] for i in group)
        start = lines[group[0]]["bbox"]
        size = max(lines[i]["size"] for i in group)
        preceding = [
            line for line in lines
            if line["bbox"][3] <= start[1]
            and min(line["bbox"][2], start[2]) > max(line["bbox"][0], start[0])
            and sum(c.isalpha() for c in line["text"]) >= 15
            and not line["text"].isupper()
        ]
        previous_size = (
            max(preceding, key=lambda line: line["bbox"][3])["size"] if preceding else 0
        )
        # The adjacent body must also be larger: a large title must not make a
        # normal-sized recital at the bottom of the page look like a footnote.
        if (
            _NUMBERED.match(lines[group[0]]["text"])
            and _JOURNAL.search(text)
            and max(lines[i]["bbox"][3] for i in group) > height * 0.85
            and size < min(previous_size * 0.94, previous_size - 0.5)
        ):
            notes.update(group)
    labels = {
        re.sub(r"\D", "", match.group())
        for i in notes if (match := _NUMBERED.match(lines[i]["text"]))
    }
    # Some PDFs emit a superscript anchor as a separate line far from its
    # sentence. Remove it only when a matching footnote was identified here.
    for i, line in enumerate(lines):
        if (
            _NUMBERED.fullmatch(line["text"])
            and re.sub(r"\D", "", line["text"]) in labels
            and 0 < line.get("number_size", line["size"]) < body_size * 0.75
            and not (recital and i + 1 < len(lines) and recital.match(lines[i + 1]["text"]))
            and any(
                body["size"] >= body_size * 0.94
                and sum(c.isalpha() for c in body["text"]) >= 3
                and body["bbox"][0] < line["bbox"][0] <= body["bbox"][2] + body_size
                and body["bbox"][1] < line["bbox"][3]
                and body["bbox"][3] > line["bbox"][1]
                for body in lines
            )
        ):
            notes.add(i)
    return notes


def _column_order(
    lines: list[dict], kept: list[int], width: float,
    formula: int, recital: re.Pattern[str],
) -> list[int]:
    """Reorder only a left preamble paired with a right operative column."""
    top = lines[formula]["bbox"][1] - 1
    spans = sorted(
        (lines[i]["bbox"][0], lines[i]["bbox"][2]) for i in kept
        if lines[i]["bbox"][1] >= top
    )
    merged: list[list[float]] = []
    for x0, x1 in spans:
        if merged and x0 <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], x1)
        else:
            merged.append([x0, x1])
    gaps = [
        (a[1], b[0]) for a, b in zip(merged, merged[1:])
        if b[0] - a[1] >= 15 and 0.4 * width < (a[1] + b[0]) / 2 < 0.6 * width
    ]
    if len(gaps) != 1:
        return kept
    cut = sum(gaps[0]) / 2
    # Include the whole body below the last centred/full-width title. A right
    # column that starts with recital continuation is deliberately unsupported.
    body_top = max((
        lines[i]["bbox"][3] for i in kept
        if lines[i]["bbox"][0] < cut < lines[i]["bbox"][2]
        and lines[i]["bbox"][3] <= top
    ), default=0)
    band = [i for i in kept if lines[i]["bbox"][1] >= body_top]
    left = [i for i in band if lines[i]["bbox"][2] < cut]
    right = [i for i in band if lines[i]["bbox"][0] > cut]
    if (
        len(left) < 8 or len(right) < 8 or formula not in right
        or any(lines[i]["bbox"][1] < top for i in right)
        or not any(recital.match(lines[i]["text"]) for i in left)
        or max(left) < formula
    ):
        return kept
    reordered = sorted(left, key=lambda i: lines[i]["bbox"][1]) + sorted(
        right, key=lambda i: lines[i]["bbox"][1],
    )
    if sorted(reordered) != sorted(band):
        return kept
    return [i for i in kept if i not in band] + reordered


def prepare_pymupdf_structure(
    raw_content: bytes, source_text: str, language: str,
) -> tuple[str, dict[str, list[int]]]:
    """Prepare a structural view; retain the unmodified text on any mismatch."""
    profile = LEGISLATIVE_MARKERS.get(language)
    enacting = profile.compile(profile.enacting) if profile else _ENACTING
    recital = (
        profile.compile(f"{profile.recital_marker}|{profile.recital_start}")
        if profile else re.compile(r"^Whereas\b", re.I)
    )
    article = profile.compile(profile.article + r"\s*$") if profile else _ARTICLE
    signature = profile.compile(profile.signature) if profile else re.compile(r"^Done at\b")
    annex = profile.compile(profile.annex_word + r"\b") if profile else re.compile(r"^ANNEX\b")
    changes: dict[str, list[int]] = {}
    raw_parts: list[str] = []
    parts: list[str] = []
    quoted_regions = _find_quoted_regions(source_text)
    operative = False
    in_preamble = False
    try:
        import pymupdf

        with pymupdf.open(stream=raw_content, filetype="pdf") as doc:
            for page_no, page in enumerate(doc):
                raw = page.get_text().strip()
                if not raw:
                    continue
                raw_parts.append(raw)
                if operative:
                    parts.append(raw)
                    continue
                lines: list[dict] = []
                fonts: Counter[float] = Counter()
                for block_no, block in enumerate(page.get_text(
                    "dict", flags=pymupdf.TEXTFLAGS_TEXT,
                )["blocks"]):
                    if block["type"] != 0:
                        continue
                    for line in block["lines"]:
                        text = "".join(span["text"] for span in line["spans"]).strip()
                        if not text:
                            continue
                        sizes: list[float] = []
                        for span in line["spans"]:
                            letters = sum(c.isalpha() for c in span["text"])
                            if letters:
                                sizes.append(span["size"])
                                if line["bbox"][1] < page.rect.height * 0.6:
                                    fonts[round(span["size"], 1)] += letters
                        lines.append({"text": text, "bbox": line["bbox"],
                                      "size": max(sizes, default=max(
                                          (span["size"] for span in line["spans"]), default=0,
                                      )), "block": block_no,
                                      "number_size": max((
                                          span["size"] for span in line["spans"]
                                          if any(c.isdigit() for c in span["text"])
                                      ), default=0)})
                formula = next((
                    i for i, line in enumerate(lines)
                    if enacting.match(line["text"]) and line["text"].isupper()
                ), None)
                operative = formula is not None or any(
                    article.match(line["text"]) or signature.match(line["text"])
                    or annex.match(line["text"]) for line in lines
                )
                in_preamble = in_preamble or any(recital.match(line["text"]) for line in lines)
                if not in_preamble or not fonts or [s.strip() for s in raw.splitlines() if s.strip()] != [
                    line["text"] for line in lines
                ]:
                    parts.append(raw)
                    continue
                notes = _footnote_lines(lines, fonts.most_common(1)[0][0], page.rect.height, recital)
                kept = [i for i in range(len(lines)) if i not in notes]
                page_start = sum(len(part) + 2 for part in raw_parts[:-1])
                quoted_page = any(
                    start < page_start + len(raw) and end > page_start
                    for start, end in quoted_regions
                )
                order = _column_order(lines, kept, page.rect.width, formula, recital) if (
                    formula is not None and formula in kept and not quoted_page
                ) else kept
                if notes:
                    changes.setdefault("recital_footnotes", []).append(page_no + 1)
                if order != kept:
                    changes.setdefault("preamble_columns", []).append(page_no + 1)
                parts.append("\n".join(lines[i]["text"] for i in order) if (
                    notes or order != kept
                ) else raw)
    except Exception as exc:
        logger.debug("Retaining unmodified PDF text layer: %s", exc)
        return source_text, {}
    if "\n\n".join(raw_parts) != source_text:
        return source_text, {}
    return "\n\n".join(parts), changes
