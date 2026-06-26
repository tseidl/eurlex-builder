"""HTML text extractor for EUR-Lex documents.

Handles three EUR-Lex HTML structures:
- Standard OJ format (structured divs with class markers)
- Manual CSS format (older documents with inline styles)
- Text-only format (minimal structure, TexteOnly div)
"""

from __future__ import annotations

import logging
import re

from lxml import etree, html

from eurlex_builder.utils import normalize_string
from eurlex_builder.extractors.splitter import split_article

logger = logging.getLogger("eurlex_builder")

# Namespace map for regex-based XPath queries.
NSMAP = {"re": "http://exslt.org/regular-expressions"}


# ---------------------------------------------------------------------------
# Structure detection
# ---------------------------------------------------------------------------

def _is_standard_structure(tree) -> bool:
    """Standard OJ format: divs with id starting with 'rct_' or 'art_'."""
    return bool(tree.xpath(
        ".//*[local-name()='div' and (starts-with(@id, 'rct_') or starts-with(@id, 'art_'))]"
    ))


def _is_manual_structure(tree) -> bool:
    """Manual CSS format: p elements with specific classes."""
    return bool(tree.xpath(
        ".//*[local-name()='p' and ("
        "@class='li ManualConsidrant' or "
        "@class='Titrearticle' or "
        "@class='Annexetitre')]"
    ))


def _is_text_only_structure(tree) -> bool:
    """Text-only format: div with id='TexteOnly'."""
    return bool(tree.xpath(
        ".//*[local-name()='div' and @id='TexteOnly']"
    ))


def _is_class_based_structure(tree) -> bool:
    """Class-based OJ format: p elements with class='ti-art' or 'oj-ti-art'."""
    return bool(tree.xpath(
        ".//*[local-name()='p' and (@class='ti-art' or @class='oj-ti-art')]"
    ))


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _extract_text(element) -> str:
    """Extract and normalize text from an lxml element."""
    if not isinstance(element, etree._Element) or isinstance(element, (etree._Comment, etree._ProcessingInstruction)):
        return ""
    text = " ".join(t for t in element.itertext() if t.strip())
    return normalize_string(text)


# ---------------------------------------------------------------------------
# Recital post-processing helpers
# ---------------------------------------------------------------------------

# Matches inline cross-references that look like recitals — phrases that
# clearly identify a fragment as a cross-reference rather than a substantive
# recital. Older drafting style legitimately starts recitals with lowercase
# words like "(1) the operation..." or "(7) there should therefore...", so the
# filter must be narrow.
_INLINE_REF_START_RE = re.compile(
    r"^\(\d+\)\s+(?:of\s|thereof\b|referred\s+to\b|cited\b|above\b)",
)

# Matches footnote refs that leak into the recital zone: "(1) OJ No 13, ..."
# or "(5) [1974] ECR 837." Keep this narrow — `See`, `Case`, `Opinion` can
# start legitimate recitals (e.g., "(5) Case law suggests...").
_FOOTNOTE_REF_RE = re.compile(
    r"^\(\d+\)\s+(?:OJ\s+(?:No\s+)?[LC]?\s*\d|"
    r"\[\d{4}\]\s*ECR|Ibidem|Ibid\.|Cf\.\s)",
)

# Trailing OJ footnote anchor glued to the end of a recital body:
# "...measures. (1) OJ No L 169, 12.7.1993, p. 1."
_RECITAL_TAIL_OJ_RE = re.compile(
    r"\s*\(\s*\d+\s*\)\s*OJ\s+(?:No\s+)?[LC]?\s*\d+.*$",
    re.DOTALL,
)


def _classify_recital(text: str) -> str | None:
    """Return a subtype for this recital text, or None for normal recitals.

    Short recitals (<= 30 chars) are flagged as "subheading". This captures:
      - bare markers like "(24)" that appear as sub-paragraph labels
      - numbered subheadings like "(9) Export price" within recital zones
    Downstream consumers can filter on subtype if they want prose-only recitals.
    """
    if len(text.strip()) <= 30:
        return "subheading"
    return None


def _strip_recital_tail(text: str) -> str:
    """Strip trailing OJ/ECR footnote anchors glued to a recital."""
    return _RECITAL_TAIL_OJ_RE.sub("", text).rstrip()


def extract_html_full_text(raw_content: bytes) -> str | None:
    """Extract all visible text from HTML content. Used for full_text column."""
    try:
        tree = etree.fromstring(raw_content)
    except Exception:
        try:
            tree = html.fromstring(raw_content)
        except Exception:
            return None
    return _extract_full_body(tree)


def _extract_full_body(tree) -> str | None:
    """Extract all visible text from the document body as a single string.

    Used as fallback for documents without legislative structure (e.g.
    communications, reports). Skips header/banner elements.
    """
    # Try to find the most meaningful container.
    body = tree.xpath(".//*[local-name()='body']")
    root = body[0] if body else tree

    parts: list[str] = []
    for p in root.iter("{http://www.w3.org/1999/xhtml}p", "p"):
        cls = p.get("class", "")
        # Skip page headers, banners, and language selectors.
        if cls in ("bglang", "hd-date", "hd-lg", "hd-oj", "hd-ti"):
            continue
        text = _extract_text(p)
        if text:
            parts.append(text)

    full = " ".join(parts)
    return full if full else None


# ---------------------------------------------------------------------------
# Standard structure extraction
# ---------------------------------------------------------------------------

def _extract_standard_recitals(tree) -> list[dict]:
    """Extract recitals from standard OJ format.

    First tries rct_ divs. If none found, falls back to table-structured
    recitals where (N) is in one <td> and text is in the adjacent <td>.
    """
    units: list[dict] = []
    divs = tree.xpath(
        ".//*[local-name()='div' and starts-with(@id, 'rct_')]"
    )
    for div in divs:
        div_id = div.get("id", "")
        number = div_id.replace("rct_", "", 1)
        text = _extract_text(div)
        if text:
            cleaned = _strip_recital_tail(text)
            units.append({
                "type": "recital",
                "subtype": _classify_recital(cleaned),
                "number": number,
                "title": None,
                "text": cleaned,
            })

    # Fallback: table-structured recitals (common in decisions).
    # Pattern: <table> with two <td> columns — first has "(N)", second has text.
    # Only look before the first art_ div (recitals precede articles).
    if not units:
        units = _extract_table_recitals(tree)

    return units


def _extract_table_recitals(tree) -> list[dict]:
    """Extract recitals from table cells where (N) and text are in adjacent <td>s.

    Scans all <table> elements before the first article div, looking for
    rows where the first cell contains a bare "(N)" pattern.
    """
    units: list[dict] = []
    numbered_re = re.compile(r"^\((\d+)\)$")

    # Find the first article div to know where recitals end.
    first_art = tree.xpath(
        ".//*[local-name()='div' and starts-with(@id, 'art')]"
    )
    first_art_el = first_art[0] if first_art else None

    for table in tree.iter("{http://www.w3.org/1999/xhtml}table", "table"):
        # Stop if we've passed the first article.
        if first_art_el is not None:
            # Compare document position: if table comes after the article, stop.
            # lxml doesn't have a direct position compare, so use a simple heuristic:
            # check if the table is a descendant or following sibling of art div's parent.
            try:
                if table.getparent() != first_art_el.getparent():
                    # Different parent — check if table's sourceline is after article's
                    if (table.sourceline or 0) > (first_art_el.sourceline or 0):
                        break
            except Exception:
                pass

        rows = table.xpath(".//*[local-name()='tr']")
        for row in rows:
            cells = row.xpath(".//*[local-name()='td']")
            if len(cells) >= 2:
                first_text = _extract_text(cells[0]).strip()
                m = numbered_re.match(first_text)
                if m:
                    body_text = _extract_text(cells[1])
                    if body_text:
                        full = f"({m.group(1)}) {body_text}"
                        cleaned = _strip_recital_tail(full)
                        units.append({
                            "type": "recital",
                            "subtype": _classify_recital(cleaned),
                            "number": m.group(1),
                            "title": None,
                            "text": cleaned,
                        })

    return units


def _extract_standard_articles(tree, *, granularity: str = "article") -> list[dict]:
    """Extract articles from standard OJ format."""
    units: list[dict] = []
    # Match art_1, art_2a, etc. and also bare "art" (Sole Article).
    divs = tree.xpath(
        ".//*[local-name()='div' and re:test(@id, '^art(_\\d+[a-z]*)?$')]",
        namespaces=NSMAP,
    )
    for div in divs:
        div_id = div.get("id", "")
        number = div_id.replace("art_", "", 1) if "_" in div_id else "sole"

        # Extract title from child div with class 'eli-title'.
        title_els = div.xpath(
            "./*[local-name()='div' and @class='eli-title']"
        )
        title = _extract_text(title_els[0]) if title_els else None

        # Body: walk children, descending into unclassed <div>s
        # (modern OJ wraps each numbered paragraph in such a div, with the
        # stem and each lettered point as separate child elements).
        body_parts = _walk_article_body(div)

        if body_parts or title:
            units.extend(split_article(
                body_parts, number=number, title=title, granularity=granularity,
            ))
    return units


def _walk_article_body(article_div) -> list[str]:
    """Collect body_parts from an article div, preserving point-level granularity.

    Descends into unclassed <div> containers (which wrap numbered paragraphs in
    modern OJ HTML) so each nested element (stem, lettered point) becomes its
    own body_part. Leaf elements (<p>, <table>, …) are taken directly.
    """
    parts: list[str] = []
    for child in article_div:
        child_class = child.get("class", "") or ""
        if child_class in ("eli-title", "oj-ti-art"):
            continue
        tag = etree.QName(child).localname
        # Unclassed div = paragraph container — descend one level.
        if tag == "div" and not child_class:
            for grandchild in child:
                text = _extract_text(grandchild)
                if text:
                    parts.append(text)
        else:
            text = _extract_text(child)
            if text:
                parts.append(text)
    return parts


def _extract_standard_annexes(tree) -> list[dict]:
    """Extract annexes from standard OJ format."""
    units: list[dict] = []
    divs = tree.xpath(
        ".//*[local-name()='div' and re:test(@id, '^anx_[IVXLCDMivxlcdm0-9]+$')]",
        namespaces=NSMAP,
    )
    for div in divs:
        div_id = div.get("id", "")
        number = div_id.replace("anx_", "", 1)

        # Title: child with class 'oj-doc-ti', but skip pure ANNEX headers.
        title = None
        title_els = div.xpath(
            "./*[@class='oj-doc-ti']"
        )
        for tel in title_els:
            t = _extract_text(tel)
            if t and not re.match(r"^ANNEX\s*[IVXLCDM]*$", t):
                title = t
                break

        # Body: all other children's text.
        body_parts: list[str] = []
        for child in div:
            child_class = child.get("class", "")
            if child_class == "oj-doc-ti":
                continue
            part = _extract_text(child)
            if part:
                body_parts.append(part)
        text = " ".join(body_parts)

        if text or title:
            units.append({
                "type": "annex",
                "number": number,
                "title": title,
                "text": text,
            })
    return units


# ---------------------------------------------------------------------------
# Manual structure extraction
# ---------------------------------------------------------------------------

def _extract_manual_recitals(tree) -> list[dict]:
    """Extract recitals from manual CSS format."""
    units: list[dict] = []
    paras = tree.xpath(
        ".//*[local-name()='p' and @class='li ManualConsidrant']"
    )
    for p in paras:
        # Number from span with class 'num'.
        number = None
        num_spans = p.xpath("./*[local-name()='span' and @class='num']")
        if num_spans:
            num_text = _extract_text(num_spans[0])
            m = re.search(r"\((\d+)\)", num_text)
            if m:
                number = m.group(1)

        text = _extract_text(p)
        if text:
            cleaned = _strip_recital_tail(text)
            units.append({
                "type": "recital",
                "subtype": _classify_recital(cleaned),
                "number": number,
                "title": None,
                "text": cleaned,
            })
    return units


# Classes that mark the end of an article or annex body.
_MANUAL_STOP_CLASSES = frozenset({
    "Titrearticle", "Annexetitre", "Fait", "Fichefinanciretitre",
})


def _extract_manual_articles(tree, *, granularity: str = "article") -> list[dict]:
    """Extract articles from manual CSS format."""
    units: list[dict] = []
    title_paras = tree.xpath(
        ".//*[local-name()='p' and @class='Titrearticle']"
    )
    for p in title_paras:
        number = None
        article_title: str | None = None

        # Check for <br> split: text before br has article number, after has title.
        brs = p.xpath("./*[local-name()='br']")
        if brs:
            br = brs[0]
            text_before = (p.text or "").strip()
            text_after = (br.tail or "").strip()
            m = re.search(r"Article\s+(\d+[a-z]*)", text_before)
            if m:
                number = m.group(1)
            article_title = text_after if text_after else None
        else:
            # Try spans: first span has number.
            spans = p.xpath("./*[local-name()='span']")
            if spans:
                span_text = _extract_text(spans[0])
                m = re.search(r"Article\s+(\d+[a-z]*)", span_text)
                if m:
                    number = m.group(1)
            else:
                full = _extract_text(p)
                m = re.search(r"Article\s+(\d+[a-z]*)", full)
                if m:
                    number = m.group(1)

        # Collect body: following siblings until a stop class.
        body_parts: list[str] = []
        sibling = p.getnext()

        # If next sibling is also Titrearticle and we have no title, it is the title.
        if (
            sibling is not None
            and sibling.get("class") == "Titrearticle"
            and not article_title
        ):
            article_title = _extract_text(sibling)
            sibling = sibling.getnext()

        while sibling is not None:
            sib_class = sibling.get("class", "")
            if sib_class in _MANUAL_STOP_CLASSES:
                break
            part = _extract_text(sibling)
            if part:
                body_parts.append(part)
            sibling = sibling.getnext()

        if body_parts or article_title or number:
            units.extend(split_article(
                body_parts, number=number, title=article_title, granularity=granularity,
            ))
    return units


def _extract_manual_annexes(tree) -> list[dict]:
    """Extract annexes from manual CSS format."""
    units: list[dict] = []
    title_paras = tree.xpath(
        ".//*[local-name()='p' and @class='Annexetitre']"
    )
    for p in title_paras:
        number = None
        annex_title: str | None = None

        full_text = _extract_text(p)
        m = re.search(r"ANNEX\s+([IVXLCDMivxlcdm]+)", full_text)
        if m:
            number = m.group(1)
            # Title is the text after the match.
            remainder = full_text[m.end():].strip()
            annex_title = remainder if remainder else None

        # If title is empty, check if next sibling has class NormalCentered.
        sibling = p.getnext()
        if not annex_title and sibling is not None:
            if sibling.get("class") == "NormalCentered":
                annex_title = _extract_text(sibling)
                sibling = sibling.getnext()

        # Collect body until a stop class.
        body_parts: list[str] = []
        if sibling is None:
            sibling = p.getnext()
            # Skip the title sibling we already consumed.
            if sibling is not None and sibling.get("class") == "NormalCentered" and annex_title:
                sibling = sibling.getnext()

        while sibling is not None:
            sib_class = sibling.get("class", "")
            if sib_class in _MANUAL_STOP_CLASSES:
                break
            part = _extract_text(sibling)
            if part:
                body_parts.append(part)
            sibling = sibling.getnext()

        text = " ".join(body_parts)
        if text or annex_title or number:
            units.append({
                "type": "annex",
                "number": number,
                "title": annex_title,
                "text": text,
            })
    return units


# ---------------------------------------------------------------------------
# Class-based OJ structure extraction
# ---------------------------------------------------------------------------

# Classes that mark boundaries between sections in class-based structure.
_CLASS_STOP = frozenset({
    "ti-art", "oj-ti-art", "ti-grseq-1", "oj-ti-grseq-1",
    "signatory", "oj-signatory", "final", "doc-end", "doc-sep",
})


def _extract_class_based_recitals(tree) -> list[dict]:
    """Extract recitals from class-based OJ format.

    Recitals are <p class="normal"> paragraphs before the first article.
    Handles both "Whereas" style and numbered "(N)" style, including
    table-structured recitals where (N) is in one <td> and text in another.
    """
    units: list[dict] = []
    counter = 0
    numbered_re = re.compile(r"^\((\d+)\)")

    first_art = tree.xpath(".//*[local-name()='p' and (@class='ti-art' or @class='oj-ti-art')]")
    if not first_art:
        return units

    # Buffer for table-structured recitals: (N) in one <p>, text in next <p>.
    pending_number: str | None = None

    for p in tree.iter("{http://www.w3.org/1999/xhtml}p", "p"):
        cls = p.get("class", "")
        if cls in ("ti-art", "oj-ti-art"):
            break
        if cls not in ("normal", "oj-normal"):
            continue
        text = _extract_text(p)
        if not text:
            continue

        # Skip standalone "Whereas:" marker.
        if text.strip().rstrip(":").upper() == "WHEREAS":
            continue

        if text.upper().startswith("WHEREAS"):
            counter += 1
            cleaned = _strip_recital_tail(text)
            units.append({
                "type": "recital",
                "subtype": _classify_recital(cleaned),
                "number": str(counter),
                "title": None,
                "text": cleaned,
            })
            pending_number = None
        elif numbered_re.match(text) and len(text) < 10:
            # Bare "(N)" — buffer it, text comes in the next <p>.
            pending_number = numbered_re.match(text).group(1)
        elif pending_number is not None:
            # This is the recital text following a bare "(N)".
            full = f"({pending_number}) {text}"
            cleaned = _strip_recital_tail(full)
            units.append({
                "type": "recital",
                "subtype": _classify_recital(cleaned),
                "number": pending_number,
                "title": None,
                "text": cleaned,
            })
            pending_number = None
        elif numbered_re.match(text) and len(text) >= 10:
            # Full "(N) text..." in one paragraph.
            # Skip footnote refs / inline cross-refs disguised as recitals.
            if _FOOTNOTE_REF_RE.match(text) or _INLINE_REF_START_RE.match(text):
                continue
            m = numbered_re.match(text)
            cleaned = _strip_recital_tail(text)
            units.append({
                "type": "recital",
                "subtype": _classify_recital(cleaned),
                "number": m.group(1),
                "title": None,
                "text": cleaned,
            })
    return units


def _extract_class_based_articles(tree, *, granularity: str = "article") -> list[dict]:
    """Extract articles from class-based OJ format.

    Articles are <p class="ti-art"> elements; body is following <p class="normal">
    siblings until the next ti-art or stop class.
    """
    units: list[dict] = []
    art_paras = tree.xpath(
        ".//*[local-name()='p' and (@class='ti-art' or @class='oj-ti-art')]"
    )

    for p in art_paras:
        title_text = _extract_text(p)
        number = None
        m = re.search(r"Article\s+(\d+[a-z]*)", title_text, re.IGNORECASE)
        if m:
            number = m.group(1)
        elif re.search(r"Sole\s+Article", title_text, re.IGNORECASE):
            number = "sole"

        # Collect body: following siblings until a stop class.
        body_parts: list[str] = []
        sibling = p.getnext()
        while sibling is not None:
            sib_class = sibling.get("class", "")
            if sib_class in _CLASS_STOP:
                break
            part = _extract_text(sibling)
            if part:
                body_parts.append(part)
            sibling = sibling.getnext()

        if body_parts or number:
            units.extend(split_article(
                body_parts, number=number, title=None, granularity=granularity,
            ))
    return units


def _extract_class_based_annexes(tree) -> list[dict]:
    """Extract annexes from class-based OJ format.

    Annexes start with <p class="ti-grseq-1"> containing "ANNEX".
    """
    units: list[dict] = []
    headings = tree.xpath(
        ".//*[local-name()='p' and @class='ti-grseq-1']"
    )

    for p in headings:
        heading_text = _extract_text(p)
        if not re.match(r"ANNEX", heading_text, re.IGNORECASE):
            continue

        number = None
        annex_title = None
        m = re.match(r"ANNEX\s*([IVXLCDMivxlcdm0-9]*)\s*(.*)", heading_text, re.IGNORECASE)
        if m:
            number = m.group(1).strip() or None
            annex_title = m.group(2).strip() or None

        body_parts: list[str] = []
        sibling = p.getnext()
        while sibling is not None:
            sib_class = sibling.get("class", "")
            if sib_class in ("ti-grseq-1", "signatory", "final", "doc-end"):
                break
            part = _extract_text(sibling)
            if part:
                body_parts.append(part)
            sibling = sibling.getnext()

        text = " ".join(body_parts)
        if text or annex_title or number:
            units.append({
                "type": "annex",
                "number": number,
                "title": annex_title,
                "text": text,
            })
    return units


# ---------------------------------------------------------------------------
# Consolidated-norm structure extraction
# ---------------------------------------------------------------------------

_CONSOLIDATED_NORM_SKIP = frozenset({
    "arrow", "modref", "reference", "disclaimer",
    "title-doc-first", "title-doc-last", "title-doc-oj-reference",
    "hd-modifiers", "hd-toc-1", "hd-toc-2", "hd-toc-3",
    "toc-1", "toc-2", "toc-3",
    "title-fam-member", "title-fam-member-star",
    "footnote",
})


def _is_consolidated_norm_structure(tree) -> bool:
    """Consolidated-norm format: p elements with class 'title-article-norm'."""
    return bool(tree.xpath(
        ".//*[local-name()='p' and @class='title-article-norm']"
    ))


def _extract_consolidated_norm_recitals(tree) -> list[dict]:
    """Extract recitals from consolidated-norm format."""
    units: list[dict] = []
    numbered_re = re.compile(r"^\((\d+)\)")
    pending_number: str | None = None

    for p in tree.iter("{http://www.w3.org/1999/xhtml}p", "p"):
        cls = p.get("class", "")
        if cls == "title-article-norm":
            break
        if cls != "norm":
            continue
        text = _extract_text(p)
        if not text:
            continue

        m = numbered_re.match(text)
        if m and len(text) < 10:
            pending_number = m.group(1)
        elif pending_number is not None:
            full = f"({pending_number}) {text}"
            cleaned = _strip_recital_tail(full)
            units.append({
                "type": "recital",
                "subtype": _classify_recital(cleaned),
                "number": pending_number,
                "title": None,
                "text": cleaned,
            })
            pending_number = None
        elif numbered_re.match(text) and len(text) >= 10:
            if _FOOTNOTE_REF_RE.match(text) or _INLINE_REF_START_RE.match(text):
                continue
            m = numbered_re.match(text)
            cleaned = _strip_recital_tail(text)
            units.append({
                "type": "recital",
                "subtype": _classify_recital(cleaned),
                "number": m.group(1),
                "title": None,
                "text": cleaned,
            })

    return units


def _extract_consolidated_norm_articles(
    tree, *, granularity: str = "article",
) -> list[dict]:
    """Extract articles from consolidated-norm format."""
    units: list[dict] = []

    art_paras = tree.xpath(
        ".//*[local-name()='p' and @class='title-article-norm']"
    )

    for p in art_paras:
        title_text = _extract_text(p)
        number = None
        m = re.search(r"Article\s+(\d+[a-z]*)", title_text, re.IGNORECASE)
        if m:
            number = m.group(1)
        elif re.search(r"Sole\s+Article", title_text, re.IGNORECASE):
            number = "sole"

        article_title: str | None = None
        sibling = p.getnext()
        if sibling is not None and sibling.get("class") == "stitle-article-norm":
            article_title = _extract_text(sibling)
            sibling = sibling.getnext()

        body_parts: list[str] = []
        while sibling is not None:
            sib_class = sibling.get("class", "")
            sib_tag = etree.QName(sibling).localname if isinstance(sibling.tag, str) else ""
            if sib_class in ("title-article-norm", "title-division-1"):
                break
            if sib_class in _CONSOLIDATED_NORM_SKIP or sib_class.startswith("title-"):
                sibling = sibling.getnext()
                continue
            if sib_class == "norm":
                part = _extract_text(sibling)
                if part:
                    body_parts.append(part)
            elif sib_tag == "div" and not sib_class:
                for child in sibling:
                    if child.get("class", "") == "norm":
                        part = _extract_text(child)
                        if part:
                            body_parts.append(part)
            sibling = sibling.getnext()

        if body_parts or number:
            units.extend(split_article(
                body_parts, number=number, title=article_title,
                granularity=granularity,
            ))

    return units


# ---------------------------------------------------------------------------
# Text-only structure extraction
# ---------------------------------------------------------------------------

_CHAPTER_HEADING_RE = re.compile(
    r"^(?:CHAPTER|TITLE|PART|SECTION)\s+[IVXLCDM\d]+\s*$", re.IGNORECASE,
)

_CONSOLIDATED_MARKER_RE = re.compile(r"^[▼►▲◄]\s*[A-Z]\d*$")


def _extract_text_only(tree, *, include_recitals: bool, include_articles: bool,
                       include_annexes: bool,
                       article_granularity: str = "article",
                       container=None) -> list[dict]:
    """Extract text units from minimal text-only format.

    Handles two recital styles:
    - Old style (pre-~2000): paragraphs starting with "Whereas"
    - Modern style (post-~2000): "Whereas:" marker followed by numbered
      paragraphs like "(1) ...", "(2) ..."
    """
    units: list[dict] = []
    if container is not None:
        root = container
    else:
        text_div = tree.xpath(".//*[local-name()='div' and @id='TexteOnly']")
        if not text_div:
            return units
        root = text_div[0]

    paragraphs = root.xpath(".//*[local-name()='p']")

    recital_counter = 0
    in_recital_zone = False  # True after seeing "Whereas:" marker
    current_article: dict | None = None
    current_annex: dict | None = None

    # Pattern for modern numbered recitals: "(1) ...", "(2) ...", etc.
    numbered_recital_re = re.compile(r"^\((\d+)\)\s*(.*)")

    # Pre-process: some older EUR-Lex documents cram article headings into
    # the same <p> as preceding text (e.g., "HAS ADOPTED THIS REGULATION:
    # Article 1"). Split these into separate virtual paragraphs.
    # Only fire when the word "Article" follows a sentence terminator
    # (`:` `.` `;` or closing paren) — this avoids splitting inline references
    # like "... thereof, and Article 1" where "Article 1" is not a heading.
    _mid_article_re = re.compile(
        r"(?<=[:.;)])\s+((?:Sole\s+)?Article(?!s)(?:\s+\d+[a-z]*)?)\s*$", re.IGNORECASE
    )
    raw_texts: list[str] = []
    for p in paragraphs:
        text = _extract_text(p)
        if not text:
            continue
        # Split if "Article N" appears at the end after a non-article prefix.
        m = _mid_article_re.search(text)
        if m and len(text) > len(m.group(0)) + 5:
            raw_texts.append(text[:m.start()].strip())
            raw_texts.append(m.group(1).strip())
        else:
            raw_texts.append(text)

    # "Done at <city>, <date>" signals the end of legislative content. Anything
    # after (annex reference lists, archival footnotes) is noise for our
    # purposes and gets stripped by strip_boilerplate later anyway.
    _signature_re = re.compile(r"^Done at \w+[,\s]+\d", re.IGNORECASE)
    past_signature = False
    skip_next_caps = False

    for idx, text in enumerate(raw_texts):
        if not text:
            continue

        # If already past the signature block, discard all subsequent content.
        if past_signature:
            continue

        if _signature_re.match(text):
            past_signature = True
            # Append the signature line itself to the last article/annex once,
            # so strip_boilerplate can find and trim it cleanly.
            if current_article is not None:
                current_article["_body"].append(text)
            elif current_annex is not None:
                current_annex["_body"].append(text)
            continue

        # Skip chapter/division headings and consolidated modification markers.
        if _CHAPTER_HEADING_RE.match(text):
            skip_next_caps = True
            continue
        if _CONSOLIDATED_MARKER_RE.match(text):
            continue
        # Division subtitle: ALL CAPS short text immediately after a chapter
        # heading (e.g. "RIGHTS AND EXCEPTIONS" after "CHAPTER II").
        if (
            skip_next_caps
            and re.match(r"^[A-Z][A-Z\s,\-/()]+$", text)
            and len(text) < 80
        ):
            skip_next_caps = False
            continue
        skip_next_caps = False

        # Only match Article/ANNEX as section headings, not inline references.
        # A heading like "ARTICLE 1" or "Article 3 Subject matter" is short and
        # doesn't continue with legislative prose. Inline references like
        # "Article 2(2) of Regulation (EC)..." or "Annex I to Directive..."
        # are longer and contain prepositions/conjunctions after the number.
        art_match = re.match(r"^(?:Sole\s+)?Article(?!s)(?:\s+(\d+[a-z]*))?", text, re.IGNORECASE)
        if art_match:
            after = text[art_match.end():].strip()
            # Reject bare "Article" (no number AND not "Sole Article" prefix).
            # These are annex table placeholders like "Article -" or "Article |".
            if art_match.group(1) is None and not re.match(r"^Sole\s+Article", text, re.IGNORECASE):
                art_match = None
            # Treat pure-punctuation remainders ("Article N.", "Article N;",
            # "Article N -") the same as empty — bare headings or sentence-end
            # references, not body text to keep.
            elif after and re.match(r"^[.,;:\-–—\|]+$", after):
                after = ""
            # If text after "Article N" looks like a sentence continuation.
            if art_match and after and re.match(r"(?:\([a-z0-9]+\)|of |to |the |is |shall |and |in |for |or |which |has |was |provides |referred |,)", after, re.IGNORECASE):
                art_match = None
            # If standalone "Article N" but next paragraph continues the sentence
            # (starts with lowercase or "thereof" or opening paren+digit), it's inline.
            elif art_match and not after:
                next_text = ""
                for future in raw_texts[idx + 1:]:
                    if future.strip():
                        next_text = future
                        break
                if next_text and (
                    next_text[0].islower()
                    or re.match(r"^\(\d+\)\s*(?:of\b|thereof)", next_text)
                    or next_text.startswith("thereof")
                ):
                    art_match = None

        # Match "ANNEX", "ANNEX I", "ANNEX 1", etc. but not "Annexes" (always inline)
        # or inline references like "Annex A to Regulation ...".
        annex_match = re.match(r"^ANNEX\s*([IVXLCDMivxlcdm0-9]*)\s*(.*)", text, re.IGNORECASE)
        if annex_match and text[:7].lower().startswith("annexe"):
            annex_match = None  # "Annexes" / "Annexe" = inline reference
        if annex_match:
            remainder = annex_match.group(2).strip()
            # If remainder looks like a sentence continuation, it's inline text.
            if re.match(r"(?:[A-Za-z]\s+|\([A-Za-z0-9]+\)\s*)?(?:to |of |the |is |shall |and |in |for |,)", remainder, re.IGNORECASE):
                annex_match = None

        if art_match:
            in_recital_zone = False
            # Flush previous article/annex.
            if current_article and include_articles:
                units.extend(split_article(
                    current_article["_body"],
                    number=current_article.get("number"),
                    title=current_article.get("title"),
                    granularity=article_granularity,
                ))
            if current_annex:
                if include_annexes:
                    current_annex["text"] = " ".join(current_annex["_body"])
                    del current_annex["_body"]
                    units.append(current_annex)
                current_annex = None

            current_article = {
                "type": "article",
                "number": art_match.group(1) or "sole",
                "title": None,
                "text": "",
                "_body": [],
            }
            # Remainder after "Article N" is part of body. Skip pure punctuation
            # ("Article 8.", "Article -") — bare heading or placeholder.
            remainder = text[art_match.end():].strip()
            if remainder and not re.match(r"^[.,;:\-–—\|]+$", remainder):
                current_article["_body"].append(remainder)

        elif annex_match:
            in_recital_zone = False
            # Flush previous article/annex.
            if current_article:
                if include_articles:
                    units.extend(split_article(
                        current_article["_body"],
                        number=current_article.get("number"),
                        title=current_article.get("title"),
                        granularity=article_granularity,
                    ))
                current_article = None
            if current_annex:
                if include_annexes:
                    current_annex["text"] = " ".join(current_annex["_body"])
                    del current_annex["_body"]
                    units.append(current_annex)

            annex_number = annex_match.group(1).strip() or None
            annex_title = annex_match.group(2).strip() or None
            current_annex = {
                "type": "annex",
                "number": annex_number,
                "title": annex_title,
                "text": "",
                "_body": [],
            }

        elif current_annex is not None:
            current_annex["_body"].append(text)

        elif current_article is not None:
            current_article["_body"].append(text)

        elif text.strip().rstrip(":").upper() == "WHEREAS":
            # "Whereas:" marker — enter modern numbered recital zone.
            in_recital_zone = True

        elif in_recital_zone:
            # Modern style: numbered recitals after "Whereas:"
            num_match = numbered_recital_re.match(text)
            if num_match and include_recitals:
                # Skip footnote refs ("(1) OJ No 13, ...") and inline
                # cross-refs ("(3) of the Treaty.") that look numbered.
                if _FOOTNOTE_REF_RE.match(text) or _INLINE_REF_START_RE.match(text):
                    continue
                cleaned = _strip_recital_tail(text)
                units.append({
                    "type": "recital",
                    "subtype": _classify_recital(cleaned),
                    "number": num_match.group(1),
                    "title": None,
                    "text": cleaned,
                })

        elif numbered_recital_re.match(text) and "WHEREAS" in text.upper():
            # Numbered recital with "Whereas" but no prior "Whereas:" marker.
            # E.g. "(1) Whereas within the framework..."
            in_recital_zone = True
            if include_recitals:
                num_match = numbered_recital_re.match(text)
                cleaned = _strip_recital_tail(text)
                units.append({
                    "type": "recital",
                    "subtype": _classify_recital(cleaned),
                    "number": num_match.group(1),
                    "title": None,
                    "text": cleaned,
                })

        elif re.match(r"^\d+\.\s+Whereas\b", text, re.IGNORECASE):
            # Numbered "N. Whereas" format (e.g. "7. Whereas the competent...")
            if include_recitals:
                m = re.match(r"^(\d+)\.\s+(.*)", text)
                if m:
                    cleaned = _strip_recital_tail(m.group(2))
                    units.append({
                        "type": "recital",
                        "subtype": _classify_recital(cleaned),
                        "number": m.group(1),
                        "title": None,
                        "text": cleaned,
                    })

        elif text.upper().startswith("WHEREAS") or "WHEREAS" in text.upper()[:50]:
            # Old style: "Whereas [text]" or run-on Whereas after semicolons
            # (e.g. "...Article 7 thereof, Whereas experience has shown...")
            if include_recitals:
                recital_counter += 1
                cleaned = _strip_recital_tail(text)
                units.append({
                    "type": "recital",
                    "subtype": _classify_recital(cleaned),
                    "number": str(recital_counter),
                    "title": None,
                    "text": cleaned,
                })

        elif numbered_recital_re.match(text) and not current_article and not current_annex:
            # Bare numbered recital without "Whereas" marker — enter recital zone
            # if this looks like the start of a recital sequence (starts with (1)).
            num_match = numbered_recital_re.match(text)
            num_val = int(num_match.group(1))
            if num_val == 1 or in_recital_zone or (units and units[-1].get("type") == "recital"):
                # Skip footnotes / inline refs disguised as recitals.
                if _FOOTNOTE_REF_RE.match(text) or _INLINE_REF_START_RE.match(text):
                    continue
                in_recital_zone = True
                if include_recitals:
                    cleaned = _strip_recital_tail(text)
                    units.append({
                        "type": "recital",
                        "subtype": _classify_recital(cleaned),
                        "number": num_match.group(1),
                        "title": None,
                        "text": cleaned,
                    })

    # Flush trailing article/annex (always clear state, only append if included).
    if current_article:
        if include_articles:
            units.extend(split_article(
                current_article["_body"],
                number=current_article.get("number"),
                title=current_article.get("title"),
                granularity=article_granularity,
            ))
        current_article = None
    if current_annex:
        if include_annexes:
            current_annex["text"] = " ".join(current_annex["_body"])
            del current_annex["_body"]
            units.append(current_annex)
        current_annex = None

    return units


# ---------------------------------------------------------------------------
# Communications: paragraph-level extraction
# ---------------------------------------------------------------------------

# CSS classes that indicate header boilerplate in COM documents.
_COM_SKIP_CLASSES = frozenset({
    "Logo", "Emission", "Rfrenceinstitutionnelle", "Rfrenceinstitutionelle",
    "cpMarking", "Typedudocument_cp", "Titreobjet_cp",
    "Prliminairetype", "Prliminairetitre", "Nomdelinstitution",
    "Confidentialit", "Langue",
    "TOCHeading", "TOC1", "TOC2", "TOC3",
    "li TOC1", "li TOC2", "li TOC3",
    "FootnoteText", "FootnoteReference",
})

# CSS classes that indicate section headings.
_COM_HEADING_CLASSES = frozenset({
    "li Heading1", "li Heading2", "li Heading3",
    "Heading1", "Heading2", "Heading3",
    "li ManualHeading1", "li ManualHeading2", "li ManualHeading3",
    # Observed across older/budget/report COMs:
    "Heading", "li Heading", "li Heading110",
})

# CSS classes that indicate body paragraphs.
_COM_BODY_CLASSES = frozenset({
    "Normal", "CommentText", "Default",
    "Text1", "Text2", "Text3",
    "ListParagraph", "li ListParagraph",
    "li Normal", "NormalWeb", "li NormalWeb",
    "li ListBullet", "li ListBullet1", "li ListBullet2",
    "li ManualNumPar1",
    # Observed across report/budget/proposal COMs — all carry body prose:
    "Body", "li Body", "BodyText", "li BodyText",
    "Bodytext10", "li Bodytext10",
    "Corps", "li Corps",
    "MBT", "li MBT",
    "EEATextbody", "li EEATextbody",
    "Text", "li Text",
    "paragraph", "li paragraph",
    "Textstand-alone", "DGTextstand-alone",
})

# Regex for budget/report template classes (Text9pItalicLeft, Heading10pNormalRight025, etc.).
# These templates use <SizeFmtJustification> naming — the prefix alone is enough
# to classify since all such paragraphs carry readable text.
_COM_BODY_CLASS_RE = re.compile(r"^(?:li\s+)?(?:Text\d+p|BodyText\d+|Corps\d+)")
_COM_HEADING_CLASS_RE = re.compile(r"^(?:li\s+)?(?:Heading\d+p|HEADER(?:TITLE)?\d*)")
# Figures* classes are numeric tables — skip them to avoid polluting paragraph
# counts with price/quantity cells.
_COM_FIGURES_CLASS_RE = re.compile(r"^(?:li\s+)?Figures\d+p")


def _is_com_modern_structure(tree) -> bool:
    """Modern/transitional COM format: p elements with heading or document type classes."""
    return bool(tree.xpath(
        ".//*[local-name()='p' and ("
        "contains(@class, 'Heading1') or "
        "contains(@class, 'Heading2') or "
        "contains(@class, 'ManualHeading1') or "
        "contains(@class, 'Typedudocument_cp') or "
        "contains(@class, 'Prliminairetype'))]"
    ))


def _is_com_legacy_structure(tree) -> bool:
    """Legacy COM format: TexteOnly div containing TXT_TE with nested <p> tags."""
    text_only = tree.xpath(".//*[local-name()='div' and @id='TexteOnly']")
    if not text_only:
        return False
    # Legacy COMs have TXT_TE inside TexteOnly; legislative text-only docs don't.
    return bool(text_only[0].xpath(".//*[local-name()='TXT_TE' or local-name()='txt_te']"))


def _extract_table_text(table) -> str:
    """Extract table content as pipe-delimited rows."""
    rows = []
    for tr in table.iter("{http://www.w3.org/1999/xhtml}tr", "tr"):
        cells = []
        for td in tr.iter(
            "{http://www.w3.org/1999/xhtml}td", "td",
            "{http://www.w3.org/1999/xhtml}th", "th",
        ):
            cells.append(_extract_text(td))
        if any(c.strip() for c in cells):
            rows.append(" | ".join(c.strip() for c in cells))
    return "\n".join(rows)


def _strip_footnote_refs(elem) -> None:
    """Remove footnote reference markers from an element tree (in-place)."""
    for fn_ref in elem.iter("{http://www.w3.org/1999/xhtml}a", "a"):
        if fn_ref.get("class") in ("footnoteRef", "FootnoteReference"):
            # Preserve tail text (follows the <a>) but remove the ref number.
            tail = fn_ref.tail or ""
            fn_ref.text = ""
            fn_ref.tail = tail
    # Also strip the superscript <span class="FootnoteReference"> wrappers.
    for span in elem.iter("{http://www.w3.org/1999/xhtml}span", "span"):
        if span.get("class") == "FootnoteReference":
            span.text = ""


def _extract_com_footnotes(tree) -> list[dict]:
    """Extract footnotes from COM XHTML. Returns list of footnote units."""
    units: list[dict] = []
    for dd in tree.iter("{http://www.w3.org/1999/xhtml}dd", "dd"):
        fn_id = dd.get("id", "")
        if not fn_id.startswith("footnote"):
            continue
        # Extract footnote number from the (N) link.
        num_span = dd.xpath(
            ".//*[local-name()='a' and @class='footnote']"
        )
        fn_num = None
        if num_span:
            num_text = _extract_text(num_span[0]).strip().strip("()")
            fn_num = num_text
        # Extract footnote text from contained <p> or <span>.
        text_parts = []
        for p in dd.iter("{http://www.w3.org/1999/xhtml}p", "p"):
            t = _extract_text(p)
            if t.strip():
                text_parts.append(t)
        if not text_parts:
            # Fallback: direct text content of dd.
            t = _extract_text(dd)
            if t.strip():
                text_parts.append(t)
        text = " ".join(text_parts).strip()
        # Strip leading "(N)" if it duplicates the footnote number.
        if fn_num and text.startswith(f"({fn_num})"):
            text = text[len(f"({fn_num})"):].strip()
        if text:
            units.append({
                "type": "footnote",
                "subtype": None,
                "number": fn_num,
                "title": None,
                "text": text,
            })
    return units


_COM_SIGNATURE_RE = re.compile(
    r"^(?:Done at \w+[,\s]+\d|For the Commission|For the President|The President)\b",
    re.IGNORECASE,
)


def _extract_com_modern(tree) -> list[dict]:
    """Extract paragraphs from modern/transitional COM XHTML with CSS classes."""
    units: list[dict] = []
    counter = 0
    current_heading: str | None = None
    past_signature = False

    content_divs = tree.xpath(".//*[local-name()='div' and @class='content']")
    if not content_divs:
        # After _flatten_content_divs, content divs are gone.
        # Try contentWrapper, then body, then root.
        content_divs = tree.xpath(".//*[local-name()='div' and @class='contentWrapper']")
    if not content_divs:
        body = tree.xpath(".//*[local-name()='body']")
        content_divs = [body[0]] if body else [tree]

    # Skip footnote <dl> sections at the end.
    skip_tags = frozenset({"dl"})

    for div in content_divs:
        for elem in div:
            tag = etree.QName(elem.tag).localname if isinstance(elem.tag, str) else ""

            if tag in skip_tags:
                continue

            # Handle tables — skip boilerplate tables before first heading.
            if tag == "table":
                if current_heading is None:
                    continue
                table_text = _extract_table_text(elem)
                if table_text.strip():
                    counter += 1
                    units.append({
                        "type": "paragraph",
                        "subtype": "table",
                        "number": str(counter),
                        "title": current_heading,
                        "text": table_text,
                    })
                continue

            # Handle div.border (boxed content) — recurse into its <p> children.
            if tag == "div":
                div_class = elem.get("class", "")
                if "border" in div_class:
                    for child_p in elem.iter("{http://www.w3.org/1999/xhtml}p", "p"):
                        cls = child_p.get("class", "")
                        if cls in _COM_SKIP_CLASSES:
                            continue
                        _strip_footnote_refs(child_p)
                        text = _extract_text(child_p)
                        if text.strip():
                            counter += 1
                            units.append({
                                "type": "paragraph",
                                "subtype": None,
                                "number": str(counter),
                                "title": current_heading,
                                "text": text,
                            })
                elif "pageBreak" in div_class:
                    continue
                continue

            if tag != "p":
                continue

            cls = elem.get("class", "")

            # Skip boilerplate and footnotes.
            if cls in _COM_SKIP_CLASSES:
                continue
            # Skip numeric tables from budget-report templates.
            if _COM_FIGURES_CLASS_RE.match(cls):
                continue

            # Strip footnote refs before extracting text.
            _strip_footnote_refs(elem)

            text = _extract_text(elem)
            if not text.strip():
                continue

            # Stop emitting once we pass the signature block ("Done at ...",
            # "For the Commission", "The President"). Footnote extraction below
            # still captures the tail.
            if past_signature:
                continue
            if _COM_SIGNATURE_RE.match(text):
                past_signature = True
                continue

            is_body = cls in _COM_BODY_CLASSES or bool(_COM_BODY_CLASS_RE.match(cls))
            is_heading = cls in _COM_HEADING_CLASSES or bool(_COM_HEADING_CLASS_RE.match(cls))

            # Section headings update the current heading context.
            if is_heading:
                current_heading = text
                continue

            # Heuristic: numbered sub-headings in body classes (e.g. "1.1 Skills shortages").
            # Short text + starts with numbered pattern → treat as heading.
            if (
                is_body
                and len(text) < 80
                and re.match(r"^\d+\.\d+[\.\d]*\s+[A-Z]", text)
            ):
                current_heading = text
                continue

            # Body paragraph.
            if is_body or cls == "":
                counter += 1
                units.append({
                    "type": "paragraph",
                    "subtype": None,
                    "number": str(counter),
                    "title": current_heading,
                    "text": text,
                })

    return units


# Regex for legacy COM section headings: numbered ("1.", "1.1.", "2.1.3.") or ALL CAPS.
_LEGACY_HEADING_NUM_RE = re.compile(r"^\d+(\.\d+)*\.?\s+[A-Z]")
_LEGACY_HEADING_CAPS_RE = re.compile(r"^[A-Z][A-Z\s,:()\-/]{5,}$")
# Footnote pattern at end of document: [1], [2], etc.
_LEGACY_FOOTNOTE_RE = re.compile(r"^\[\d+\]\s")


def _extract_com_legacy(tree) -> list[dict]:
    """Extract paragraphs from legacy COM HTML (TexteOnly + TXT_TE structure)."""
    units: list[dict] = []

    text_only = tree.xpath(".//*[local-name()='div' and @id='TexteOnly']")
    if not text_only:
        return units

    # Find TXT_TE element inside TexteOnly.
    txt_te = text_only[0].xpath(".//*[local-name()='TXT_TE' or local-name()='txt_te']")
    container = txt_te[0] if txt_te else text_only[0]

    # Collect all <p> text.
    paragraphs: list[str] = []
    for p in container.iter("{http://www.w3.org/1999/xhtml}p", "p"):
        text = _extract_text(p)
        if text.strip():
            paragraphs.append(text)

    if not paragraphs:
        return units

    # Skip header boilerplate: institution name, date, COM number, title repetition.
    # Heuristic: skip until we see a numbered heading or a paragraph > 100 chars.
    start_idx = 0
    for i, para in enumerate(paragraphs):
        if _LEGACY_HEADING_NUM_RE.match(para):
            start_idx = i
            break
        if len(para) > 100 and not para.startswith("[pic]"):
            start_idx = i
            break
    else:
        start_idx = 0

    # Detect where footnotes begin.
    end_idx = len(paragraphs)
    for i in range(len(paragraphs) - 1, start_idx, -1):
        if _LEGACY_FOOTNOTE_RE.match(paragraphs[i]):
            end_idx = i
        else:
            break

    counter = 0
    current_heading: str | None = None

    for para in paragraphs[start_idx:end_idx]:
        # Check if this is a heading.
        is_heading = False
        if _LEGACY_HEADING_NUM_RE.match(para) and len(para) < 120:
            is_heading = True
        elif _LEGACY_HEADING_CAPS_RE.match(para.strip()):
            is_heading = True

        if is_heading:
            current_heading = para
            continue

        counter += 1
        units.append({
            "type": "paragraph",
            "subtype": None,
            "number": str(counter),
            "title": current_heading,
            "text": para,
        })

    return units


# Regex for OCR noise: lines that are just punctuation, single characters, or digits.
_OCR_NOISE_RE = re.compile(
    r"^[\s;:,.·•■\-–—!?|*\\/<>'\"\[\](){}iIjJtlrA-Z0-9]{1,4}$"
)

# Regex for archival cover page markers.
_ARCHIVAL_COVER_RE = re.compile(
    r"ARCHIVES HISTORIQUES|COLLECTION RELIEE|^Vol\.\s*\d{4}|"
    r"^Disclaimer$|^Conformément au règlement|"
    r"^In accordance with Council Regulation.*historical archives|"
    r"^In Übereinstimmung mit der Verordnung.*historischen Archive",
    re.IGNORECASE,
)

# Markdown heading.
_MD_HEADING_RE = re.compile(r"^#{1,3}\s+(.*)")

# Markdown image / HTML comment.
_MD_NOISE_RE = re.compile(r"^<!--.*-->$|^\.{3,}$")

# Footnote reference at start of line: ( 1 ) OJ ... or [1] ... or ( 1 ) Council Decision ...
_FOOTNOTE_LINE_RE = re.compile(
    r"^\(\s*\d+\s*\)\s*(?:OJ\b|Council\b|Commission\b|Second|Third|Initial|See\b)|"
    r"^\[\d+\]\s"
)


def extract_com_from_text(text: str) -> list[dict]:
    """Split plain text or Docling markdown into COM paragraph units.

    Handles both pymupdf plain text and Docling markdown output.
    Strips archival cover pages, OCR noise, and markdown artifacts.
    """
    # Split on blank lines (Docling and pymupdf both use these as paragraph breaks).
    raw_paragraphs = re.split(r"\n\s*\n", text)

    # Clean each paragraph: strip, skip noise/artifacts.
    paragraphs: list[str] = []
    for p in raw_paragraphs:
        p = p.strip()
        if not p:
            continue
        if _OCR_NOISE_RE.match(p):
            continue
        if _MD_NOISE_RE.match(p):
            continue
        # Skip markdown table blocks.
        if p.startswith("|") and "|" in p[1:]:
            continue
        paragraphs.append(p)

    if not paragraphs:
        return []

    # Skip archival boilerplate at the start (cover pages, disclaimers).
    # Look for the first substantive heading or long paragraph that isn't boilerplate.
    start_idx = 0
    for i, para in enumerate(paragraphs):
        if _ARCHIVAL_COVER_RE.search(para):
            start_idx = i + 1
            continue
        # Markdown heading — could be the real start.
        md_match = _MD_HEADING_RE.match(para)
        if md_match:
            heading_text = md_match.group(1).strip()
            # Skip short metadata-like headings (e.g. "## COM (78) 761").
            if len(heading_text) < 20 and not _LEGACY_HEADING_NUM_RE.match(heading_text):
                start_idx = i + 1
                continue
            # This looks like a real section heading.
            start_idx = i
            break
        # Legacy numbered heading.
        if _LEGACY_HEADING_NUM_RE.match(para):
            start_idx = i
            break
        # Long paragraph — probably real content.
        if len(para) > 100:
            start_idx = i
            break

    # Detect trailing footnotes.
    end_idx = len(paragraphs)
    for i in range(len(paragraphs) - 1, start_idx, -1):
        if _FOOTNOTE_LINE_RE.match(paragraphs[i]) or _LEGACY_FOOTNOTE_RE.match(paragraphs[i]):
            end_idx = i
        else:
            break

    units: list[dict] = []
    counter = 0
    current_heading: str | None = None

    for para in paragraphs[start_idx:end_idx]:
        # Detect headings: markdown ## or legacy numbered/caps.
        is_heading = False
        display_text = para

        md_match = _MD_HEADING_RE.match(para)
        if md_match:
            display_text = md_match.group(1).strip()
            # Short heading-like text.
            if len(display_text) < 120:
                is_heading = True
        elif _LEGACY_HEADING_NUM_RE.match(para) and len(para) < 120:
            is_heading = True
        elif _LEGACY_HEADING_CAPS_RE.match(para.strip()):
            is_heading = True

        if is_heading:
            current_heading = display_text
            continue

        counter += 1
        units.append({
            "type": "paragraph",
            "subtype": None,
            "number": str(counter),
            "title": current_heading,
            "text": display_text,
        })

    return units


# ---------------------------------------------------------------------------
# Main extractor class
# ---------------------------------------------------------------------------

class HtmlExtractor:
    """Extracts recitals, articles, and annexes from EUR-Lex HTML documents."""

    def can_handle(self, raw_content: bytes) -> bool:
        """Return True if content looks like HTML/XHTML."""
        try:
            header = raw_content[:500].lower()
            return b"<html" in header or b"<div" in header or b"<!doctype" in header
        except Exception:
            return False

    @staticmethod
    def _try_standard(tree, inc_rec, inc_art, inc_anx, granularity="article") -> list[dict]:
        units: list[dict] = []
        if inc_rec: units.extend(_extract_standard_recitals(tree))
        if inc_art: units.extend(_extract_standard_articles(tree, granularity=granularity))
        if inc_anx: units.extend(_extract_standard_annexes(tree))
        return units

    @staticmethod
    def _try_manual(tree, inc_rec, inc_art, inc_anx, granularity="article") -> list[dict]:
        units: list[dict] = []
        if inc_rec: units.extend(_extract_manual_recitals(tree))
        if inc_art: units.extend(_extract_manual_articles(tree, granularity=granularity))
        if inc_anx: units.extend(_extract_manual_annexes(tree))
        return units

    @staticmethod
    def _try_class_based(tree, inc_rec, inc_art, inc_anx, granularity="article") -> list[dict]:
        units: list[dict] = []
        if inc_rec: units.extend(_extract_class_based_recitals(tree))
        if inc_art: units.extend(_extract_class_based_articles(tree, granularity=granularity))
        if inc_anx: units.extend(_extract_class_based_annexes(tree))
        return units

    @staticmethod
    def _try_consolidated_norm(tree, inc_rec, inc_art, inc_anx, granularity="article") -> list[dict]:
        units: list[dict] = []
        if inc_rec:
            units.extend(_extract_consolidated_norm_recitals(tree))
        if inc_art:
            units.extend(_extract_consolidated_norm_articles(tree, granularity=granularity))
        return units

    @staticmethod
    def _try_text_only(tree, inc_rec, inc_art, inc_anx, granularity="article") -> list[dict]:
        return _extract_text_only(
            tree, include_recitals=inc_rec, include_articles=inc_art, include_annexes=inc_anx,
            article_granularity=granularity,
        )

    def extract_com(
        self,
        celex_id: str,
        raw_content: bytes,
    ) -> list[dict]:
        """Extract paragraph-level text units from a COM/communication document."""
        tree = None
        try:
            tree = etree.fromstring(raw_content)
        except Exception:
            try:
                tree = html.fromstring(raw_content)
            except Exception as e:
                logger.error("Failed to parse HTML for COM %s: %s", celex_id, e)
                return []

        units: list[dict] = []

        if _is_com_modern_structure(tree):
            units = _extract_com_modern(tree)
            if units:
                logger.debug("Used modern COM structure for %s", celex_id)
        if not units and _is_com_legacy_structure(tree):
            units = _extract_com_legacy(tree)
            if units:
                logger.debug("Used legacy COM structure for %s", celex_id)

        # Extract footnotes (modern/transitional only — legacy uses inline [N]).
        footnotes = _extract_com_footnotes(tree)
        if footnotes:
            units.extend(footnotes)
            logger.debug("Extracted %d footnotes from COM %s", len(footnotes), celex_id)

        # Fallback: full body as single unit (same as before).
        if not units:
            body_text = _extract_full_body(tree)
            if body_text:
                units.append({
                    "type": "body",
                    "subtype": None,
                    "number": None,
                    "title": None,
                    "text": body_text,
                })
                logger.debug("COM fallback to full body for %s", celex_id)

        logger.info("Extracted %d paragraph units from COM %s", len(units), celex_id)
        return units

    def extract(
        self,
        celex_id: str,
        raw_content: bytes,
        *,
        include_recitals: bool = True,
        include_articles: bool = True,
        include_annexes: bool = True,
        article_granularity: str = "article",
        language: str = "eng",  # accepted for protocol parity; HTML extraction is language-agnostic
        out_metadata: dict | None = None,  # ditto — HTML extractor doesn't populate
    ) -> list[dict]:
        """Parse HTML and extract structured text units."""
        units: list[dict] = []

        # Try XHTML first, fall back to plain HTML.
        tree = None
        try:
            tree = etree.fromstring(raw_content)
            logger.debug("Parsed XHTML for %s", celex_id)
        except Exception:
            try:
                tree = html.fromstring(raw_content)
                logger.debug("Parsed plain HTML for %s", celex_id)
            except Exception as e:
                logger.error("Failed to parse any HTML for %s: %s", celex_id, e)
                return units

        # Detect structure and extract. If primary structure yields 0 units,
        # try the next structure type before falling back to body/PDF.
        extractors = []
        if _is_standard_structure(tree):
            extractors.append(("standard OJ", self._try_standard))
        if _is_manual_structure(tree):
            extractors.append(("manual CSS", self._try_manual))
        if _is_class_based_structure(tree):
            extractors.append(("class-based OJ", self._try_class_based))
        if _is_consolidated_norm_structure(tree):
            extractors.append(("consolidated-norm", self._try_consolidated_norm))
        if _is_text_only_structure(tree):
            extractors.append(("text-only", self._try_text_only))

        for name, extractor_fn in extractors:
            units = extractor_fn(
                tree, include_recitals, include_articles, include_annexes,
                article_granularity,
            )
            if units:
                logger.debug("Used %s structure for %s", name, celex_id)
                break
        else:
            if extractors:
                logger.debug(
                    "All detected structures yielded 0 units for %s", celex_id
                )

        # Fallback: classless HTML (e.g. early consolidated texts) — same
        # structure as text-only but without the <div id="TexteOnly"> wrapper.
        if not units:
            body = tree.xpath(".//*[local-name()='body']")
            if body:
                units = _extract_text_only(
                    tree,
                    include_recitals=include_recitals,
                    include_articles=include_articles,
                    include_annexes=include_annexes,
                    article_granularity=article_granularity,
                    container=body[0],
                )
                if units:
                    logger.debug(
                        "Used text-only fallback on body for %s", celex_id,
                    )

        # Fallback: extract entire body as a single "body" text unit.
        # Useful for communications and other non-legislative documents.
        if not units:
            body_text = _extract_full_body(tree)
            if body_text:
                units.append({
                    "type": "body",
                    "number": None,
                    "title": None,
                    "text": body_text,
                })
                logger.debug(
                    "No structured units found for %s — extracted full body text",
                    celex_id,
                )

        logger.info(
            "Extracted %d text units from %s", len(units), celex_id,
        )
        return units
