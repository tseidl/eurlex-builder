"""Regression tests for extractor bug fixes.

Currently covers:
- Recital inline-ref filter must not drop legitimate recitals that start with
  lowercase words like "the" or "in" (older drafting style; bug exposed
  during the 2026-05 paragraph-granularity re-extraction)
- PDF parser must recognize the enacting formula ("HAS ADOPTED THIS X") as
  the end of the recital zone, otherwise it gets glued onto the last recital
"""

from __future__ import annotations

from lxml import etree

from eurlex_builder.extractors.html import HtmlExtractor, _INLINE_REF_START_RE, _walk_article_body
from eurlex_builder.extractors.pdf import _parse_legislative_markdown
from eurlex_builder.pipeline import _should_run_translate_fallback


# ---------------------------------------------------------------------------
# _INLINE_REF_START_RE — narrow inline-cross-reference filter
# ---------------------------------------------------------------------------

def test_inline_ref_filter_keeps_recitals_starting_with_lowercase_the():
    """1990s drafting style: '(1) the operation and development...'"""
    assert not _INLINE_REF_START_RE.match(
        "(1) the operation and development of the common market"
    )


def test_inline_ref_filter_keeps_recitals_starting_with_lowercase_there():
    assert not _INLINE_REF_START_RE.match(
        "(7) there should therefore be a reform"
    )


def test_inline_ref_filter_keeps_capitalized_recitals():
    assert not _INLINE_REF_START_RE.match(
        "(8) Regulation (EEC) No 822/87 was supplemented"
    )
    assert not _INLINE_REF_START_RE.match(
        "(15) Member States may decide otherwise."
    )


def test_inline_ref_filter_catches_of_cross_reference():
    assert _INLINE_REF_START_RE.match("(3) of the Treaty.")


def test_inline_ref_filter_catches_thereof_cross_reference():
    assert _INLINE_REF_START_RE.match("(5) thereof")


def test_inline_ref_filter_catches_referred_to_cross_reference():
    assert _INLINE_REF_START_RE.match("(2) referred to in Article 3")


# ---------------------------------------------------------------------------
# PDF enacting-formula detection — must end the recital zone
# ---------------------------------------------------------------------------

def test_pdf_parser_ends_recital_zone_at_has_adopted():
    md = (
        "Whereas:\n"
        "\n"
        "(1) This is the first recital text that should stand alone.\n"
        "\n"
        "(2) This is the second recital text.\n"
        "\n"
        "HAS ADOPTED THIS REGULATION:\n"
        "\n"
        "Article 1\n"
        "\n"
        "Operative text follows.\n"
    )
    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=False,
    )
    recitals = [u for u in units if u.get("type") == "recital"]
    assert len(recitals) == 2
    # Last recital must NOT contain the enacting formula
    assert "HAS ADOPTED" not in recitals[-1]["text"]


def test_pdf_parser_handles_old_style_whereas_with_has_adopted():
    """Old-style recital uses 'Whereas the X...' without a 'Whereas:' marker.
    The enacting formula appears as a separate line and must still end the
    recital — without this fix it gets appended to the recital text via the
    continuation path (current_recital is not None).
    """
    md = (
        "Whereas the import levies on rice were fixed by Regulation No 2666/93,\n"
        "\n"
        "HAS ADOPTED THIS REGULATION:\n"
        "\n"
        "Article 1\n"
        "\n"
        "Operative text follows.\n"
    )
    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=False,
    )
    recitals = [u for u in units if u.get("type") == "recital"]
    assert len(recitals) == 1
    assert "HAS ADOPTED" not in recitals[0]["text"]


def test_pdf_parser_recognizes_have_adopted_decision_variant():
    md = (
        "Whereas:\n"
        "\n"
        "(1) Recital text.\n"
        "\n"
        "HAVE ADOPTED THIS DECISION:\n"
        "\n"
        "Article 1\n"
        "\n"
        "Operative text.\n"
    )
    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=False,
    )
    recitals = [u for u in units if u.get("type") == "recital"]
    assert len(recitals) == 1
    assert "HAVE ADOPTED" not in recitals[0]["text"]


# ---------------------------------------------------------------------------
# Translate-before-extract fallback (option 2) — heuristic lives in Pipeline
# now, so it sees doc_type as well.
# ---------------------------------------------------------------------------

def test_fallback_does_not_fire_for_english():
    units = []
    assert not _should_run_translate_fallback(units, "regulation", "eng")


def test_fallback_fires_when_non_english_legislative_produced_nothing():
    units = []
    assert _should_run_translate_fallback(units, "regulation", "fra")


def test_fallback_does_not_fire_for_non_legislative_doc_types():
    """Communications already use language-agnostic paragraph extraction;
    fallback shouldn't fire even if extraction looks sparse."""
    units = []
    assert not _should_run_translate_fallback(units, "communication", "fra")


def test_fallback_does_not_fire_when_three_or_more_recitals_present():
    """≥3 recitals = success (legislative acts have preambular recitals)."""
    units = [
        {"type": "recital", "number": "1", "text": "..."},
        {"type": "recital", "number": "2", "text": "..."},
        {"type": "recital", "number": "3", "text": "..."},
        {"type": "article", "number": "1", "text": "..."},
    ]
    assert not _should_run_translate_fallback(units, "regulation", "fra")


def test_fallback_fires_for_partial_failure_articles_without_recitals():
    """Regression for bug #7 from external review: a French PDF where
    'Article' matches (same spelling) but 'Whereas' doesn't, leaving 3+
    articles and 0 recitals. Fallback MUST fire — zero recitals on a
    legislative act is a strong failure signal."""
    units = [
        {"type": "article", "number": "1", "text": "..."},
        {"type": "article", "number": "2", "text": "..."},
        {"type": "article", "number": "3", "text": "..."},
    ]
    assert _should_run_translate_fallback(units, "regulation", "fra")


def test_fallback_fires_with_one_article_and_no_recitals():
    """Same pattern: articles present, recitals missing."""
    units = [{"type": "article", "number": "1", "text": "..."}]
    assert _should_run_translate_fallback(units, "regulation", "fra")


def test_fallback_fires_with_only_two_recitals_below_threshold():
    """Just below threshold — likely partial failure."""
    units = [
        {"type": "recital", "number": "1", "text": "..."},
        {"type": "recital", "number": "2", "text": "..."},
        {"type": "article", "number": "1", "text": "..."},
    ]
    assert _should_run_translate_fallback(units, "regulation", "fra")


def test_fallback_fires_with_only_annex_no_articles_no_recitals():
    """Annex rows alone do NOT count as successful extraction."""
    units = [{"type": "annex", "number": "I", "text": "..."}]
    assert _should_run_translate_fallback(units, "regulation", "fra")


def test_fallback_fires_with_only_paragraph_rows_no_recitals_no_articles():
    """COM-style paragraph extraction for a legislative doc — fallback should fire."""
    units = [{"type": "paragraph", "number": None, "text": "..."}]
    assert _should_run_translate_fallback(units, "decision", "deu")


# ---------------------------------------------------------------------------
# Translated-content recital markers — Considering / Having regard to the following
# ---------------------------------------------------------------------------

def test_pdf_parser_recognizes_considering_as_recital_zone_trigger():
    md = (
        "Considering the following:\n"
        "\n"
        "(1) First recital.\n"
        "\n"
        "(2) Second recital.\n"
        "\n"
        "HAS ADOPTED THIS REGULATION:\n"
        "\n"
        "Article 1\n"
        "\n"
        "Body.\n"
    )
    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=False,
    )
    recitals = [u for u in units if u.get("type") == "recital"]
    assert len(recitals) == 2


def test_pdf_parser_recognizes_having_regard_to_the_following():
    md = (
        "Having regard to the following:\n"
        "\n"
        "(1) First.\n"
        "\n"
        "(2) Second.\n"
        "\n"
        "Article 1\n"
        "\n"
        "Body.\n"
    )
    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=False,
    )
    recitals = [u for u in units if u.get("type") == "recital"]
    assert len(recitals) == 2


def test_translate_markdown_preserves_line_structure(monkeypatch):
    """Regression for bug #6 from external review: translate_markdown must
    preserve single-newline boundaries because the downstream parser is
    line-oriented. The previous implementation called translate_text on
    multi-line paragraphs which collapsed newlines with spaces."""
    from eurlex_builder import translate as tr

    # Mock translate_text and _get_model so the test doesn't need Opus-MT.
    monkeypatch.setattr(tr, "_get_model", lambda lang: ("tok", "model"))
    monkeypatch.setattr(tr, "translate_text", lambda text, lang: f"[en]{text}[en]")

    text = "Considérant:\n\n(1) Premier.\n(2) Second."
    result = tr.translate_markdown(text, "fra")
    # Each non-blank line translated individually; blank line preserved verbatim.
    expected_lines = [
        "[en]Considérant:[en]",
        "",
        "[en](1) Premier.[en]",
        "[en](2) Second.[en]",
    ]
    assert result == "\n".join(expected_lines)


def test_translate_markdown_skips_oj_footnote_refs(monkeypatch):
    from eurlex_builder import translate as tr
    monkeypatch.setattr(tr, "_get_model", lambda lang: ("tok", "model"))
    monkeypatch.setattr(tr, "translate_text", lambda text, lang: f"[en]{text}[en]")

    text = "Premier paragraphe.\n(1) OJ No L 169, 12.5.1971, p. 1."
    result = tr.translate_markdown(text, "fra")
    # OJ ref line stays untranslated; first line gets translated.
    assert result == "[en]Premier paragraphe.[en]\n(1) OJ No L 169, 12.5.1971, p. 1."


def test_pdf_parser_recognizes_considering_inline_as_old_style_recital():
    md = (
        "Considering that the import levies on rice were fixed by Regulation No 2666/93,\n"
        "\n"
        "Considering that the world market price has changed,\n"
        "\n"
        "HAS ADOPTED THIS REGULATION:\n"
        "\n"
        "Article 1\n"
        "\n"
        "Operative.\n"
    )
    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=False,
    )
    recitals = [u for u in units if u.get("type") == "recital"]
    assert len(recitals) == 2
    assert all("HAS ADOPTED" not in r["text"] for r in recitals)


# ---------------------------------------------------------------------------
# Consolidated HTML — point-level splitting with amendment markers
# ---------------------------------------------------------------------------

def _make_consolidated_article_html(article_num: str, body_xml: str) -> bytes:
    """Build a minimal XHTML doc with one consolidated-format article."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        f'<div class="eli-subdivision" id="art_{article_num}">'
        f'  <p class="title-article-norm">Article {article_num}</p>'
        '  <div class="eli-title"><p class="stitle-article-norm">Test title</p></div>'
        f'  {body_xml}'
        '</div>'
        '</body></html>'
    ).encode()


def test_consolidated_walk_descends_into_norm_divs():
    """_walk_article_body must descend into <div class="norm"> containers
    so each structural child (paragraph number, chapeau, point) becomes
    its own body_part."""
    xhtml = _make_consolidated_article_html("5", """
        <div class="norm">
          <span class="no-parag">1.  </span>
          <div class="norm inline-element">
            <p class="norm inline-element">Chapeau text:</p>
            <div class="grid-container grid-list">
              <div class="list grid-list-column-1"><span>(a) </span></div>
              <div class="grid-list-column-2"><p class="norm">first point;</p></div>
            </div>
            <div class="grid-container grid-list">
              <div class="list grid-list-column-1"><span>(b) </span></div>
              <div class="grid-list-column-2"><p class="norm">second point.</p></div>
            </div>
          </div>
        </div>
    """)
    tree = etree.fromstring(xhtml)
    art_div = tree.xpath(".//*[@id='art_5']")[0]
    parts = _walk_article_body(art_div)
    assert "1." in parts
    assert "Chapeau text:" in parts
    assert any("(a)" in p and "first point" in p for p in parts)
    assert any("(b)" in p and "second point" in p for p in parts)


def test_consolidated_walk_skips_modref_markers():
    """Amendment markers (<p class="modref">) must not appear in body_parts."""
    xhtml = _make_consolidated_article_html("1", """
        <p class="modref"><a href="#">▼M2</a></p>
        <div class="norm">
          <span class="no-parag">1.  </span>
          <div class="norm inline-element">
            <p class="norm inline-element">Chapeau:</p>
            <p class="modref"><a href="#">▼B</a></p>
            <div class="grid-container grid-list">
              <div class="list grid-list-column-1"><span>(a) </span></div>
              <div class="grid-list-column-2"><p class="norm">point text.</p></div>
            </div>
          </div>
        </div>
    """)
    tree = etree.fromstring(xhtml)
    art_div = tree.xpath(".//*[@id='art_1']")[0]
    parts = _walk_article_body(art_div)
    joined = " ".join(parts)
    assert "▼" not in joined
    assert "M2" not in joined


def test_consolidated_walk_handles_text_only_norm_div():
    """<div class="norm inline-element"> with text but no children must still
    produce a body_part (paragraphs without sub-structure)."""
    xhtml = _make_consolidated_article_html("4", """
        <div class="norm">
          <span class="no-parag">1.  </span>
          <div class="norm inline-element">Simple paragraph text.</div>
        </div>
    """)
    tree = etree.fromstring(xhtml)
    art_div = tree.xpath(".//*[@id='art_4']")[0]
    parts = _walk_article_body(art_div)
    assert "1." in parts
    assert "Simple paragraph text." in parts


def test_consolidated_point_level_extraction_end_to_end():
    """Full pipeline: consolidated HTML with amendment markers between points
    must produce point-level rows, not paragraph-level blobs."""
    xhtml = _make_consolidated_article_html("5", """
        <div class="norm">
          <span class="no-parag">1.  </span>
          <div class="norm inline-element">
            <p class="norm inline-element">Member States shall ensure:</p>
            <p class="modref"><a href="#">▼M2</a></p>
            <div class="grid-container grid-list">
              <div class="list grid-list-column-1"><span>(a) </span></div>
              <div class="grid-list-column-2"><p class="norm">data protection;</p></div>
            </div>
            <p class="modref"><a href="#">▼B</a></p>
            <div class="grid-container grid-list">
              <div class="list grid-list-column-1"><span>(b) </span></div>
              <div class="grid-list-column-2"><p class="norm">privacy rights;</p></div>
            </div>
            <div class="grid-container grid-list">
              <div class="list grid-list-column-1"><span>(c) </span></div>
              <div class="grid-list-column-2"><p class="norm">security standards.</p></div>
            </div>
          </div>
        </div>
        <div class="norm">
          <span class="no-parag">2.  </span>
          <div class="norm inline-element">Simple paragraph without points.</div>
        </div>
    """)
    ext = HtmlExtractor()
    units = ext.extract("02001L0029-20190606", xhtml,
                        include_recitals=False, include_articles=True,
                        include_annexes=False, article_granularity="point")
    assert len(units) >= 5
    letters = [u["point_letter"] for u in units if u["point_letter"]]
    assert "a" in letters
    assert "b" in letters
    assert "c" in letters
    # No amendment markers leaked into text.
    for u in units:
        assert "▼" not in u["text"]
    # Paragraph 2 present with real content.
    p2 = [u for u in units if u["paragraph_num"] == "2"]
    assert len(p2) == 1
    assert "Simple paragraph" in p2[0]["text"]


# ---------------------------------------------------------------------------
# COM signature guard (2026-07 review): prose starting "The President…" or
# "For the Commission…" must not truncate the document.
# ---------------------------------------------------------------------------

def test_com_signature_requires_short_line():
    from eurlex_builder.extractors.html import _is_com_signature

    assert _is_com_signature("Done at Brussels, 3.5.2021")
    assert _is_com_signature("For the Commission")
    assert _is_com_signature("The President")
    assert not _is_com_signature(
        "The President of the European Council stressed the need for action."
    )
    assert not _is_com_signature(
        "For the Commission to deliver on this, three steps are needed."
    )


def test_com_modern_does_not_truncate_on_president_prose():
    from eurlex_builder.extractors.html import HtmlExtractor

    raw = b"""<html xmlns="http://www.w3.org/1999/xhtml"><body>
<p class="Heading1">1. Introduction</p>
<p class="Normal">The President of the European Council stressed the need for action on digital policy.</p>
<p class="Normal">A second paragraph that must survive extraction.</p>
<p class="Normal">Done at Brussels, 3.5.2021</p>
<p class="Normal">For the Commission</p>
</body></html>"""
    units = HtmlExtractor().extract_com("52021DC0000", raw)
    paragraphs = [u for u in units if u["type"] == "paragraph"]
    assert len(paragraphs) == 2
    assert any("must survive" in u["text"] for u in paragraphs)
    assert not any("Done at" in u["text"] for u in paragraphs)


# ---------------------------------------------------------------------------
# Post-signature annexes (2026-07 review): annexes follow "Done at …" in the
# OJ layout and must still be extracted; other post-signature noise is not.
# ---------------------------------------------------------------------------

def test_pdf_parser_extracts_annexes_after_signature():
    md = """## Article 1
This Regulation establishes rules.
## Article 2
It shall apply from 1 January 2025.
Done at Brussels, 12 March 2014.
For the Council
The President
## ANNEX I
List of products
Product one
## ANNEX II
Correlation table
"""
    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=True,
    )
    annexes = [u for u in units if u["type"] == "annex"]
    assert [a["number"] for a in annexes] == ["I", "II"]
    assert "Product one" in annexes[0]["text"]
    # Signatory names between signature and first annex are discarded.
    assert not any("The President" in u["text"] for u in units)


def test_pdf_parser_discards_post_signature_noise_without_annex():
    md = """## Article 1
This Regulation establishes rules.
Done at Brussels, 12 March 2014.
For the Council
( 1 ) OJ No L 169, 12.7.1993, p. 1.
"""
    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=True,
    )
    assert [u["type"] for u in units] == ["article"]
    assert "OJ No L 169" not in units[0]["text"]


def test_text_only_extracts_annex_after_signature():
    from eurlex_builder.extractors.html import HtmlExtractor

    raw = b"""<html><body><div id="TexteOnly">
<p>HAS ADOPTED THIS REGULATION:</p>
<p>Article 1</p>
<p>Something shall apply.</p>
<p>Done at Brussels, 12 March 2014.</p>
<p>For the Council</p>
<p>ANNEX I</p>
<p>List of products</p>
<p>ANNEX II</p>
<p>Correlation table</p>
</div></body></html>"""
    units = HtmlExtractor().extract("31993R0001", raw)
    annexes = [u for u in units if u["type"] == "annex"]
    assert [a["number"] for a in annexes] == ["I", "II"]
    assert "List of products" in annexes[0]["text"]
    assert not any("For the Council" in u["text"] for u in units)


# ---------------------------------------------------------------------------
# Translate-before-extract gate: disabled recital extraction must not trigger
# the fallback (recital count is always zero and says nothing about quality).
# ---------------------------------------------------------------------------

def test_fallback_without_recitals_uses_requested_article_quality():
    from eurlex_builder.pipeline import _should_run_translate_fallback

    assert _should_run_translate_fallback(
        [], "regulation", "fra",
        include_recitals=False, include_articles=True, include_annexes=False,
    )
    assert not _should_run_translate_fallback(
        [{"type": "article", "number": "1"}], "regulation", "fra",
        include_recitals=False, include_articles=True, include_annexes=False,
    )
    assert _should_run_translate_fallback(
        [], "regulation", "fra", include_recitals=True,
    )


def test_class_based_article_subtitle_becomes_title_not_body():
    """The <p class="sti-art"> subtitle ("Scope") is the article title, not a
    body fragment that would surface as a fake preamble row."""
    from eurlex_builder.extractors.html import HtmlExtractor

    raw = b"""<html><body>
<p class="ti-art">Article 1</p>
<p class="sti-art">Scope</p>
<p class="normal">1. This Directive concerns the legal protection of copyright.</p>
<p class="normal">2. It shall apply from a given date.</p>
</body></html>"""
    units = HtmlExtractor().extract(
        "32001L0029", raw, article_granularity="paragraph",
    )
    articles = [u for u in units if u["type"] == "article"]
    assert articles[0]["title"] == "Scope"
    assert all(u["text"] != "Scope" for u in articles)
    assert [u["paragraph_num"] for u in articles] == ["1", "2"]


def test_text_only_article_title_line_becomes_title_not_preamble():
    """Classless-era HTML: '<p>Article 1</p><p>Scope</p><p>1. …</p>' — the
    short capitalized line is the title, not a fake paragraph-0 row."""
    from eurlex_builder.extractors.html import HtmlExtractor

    raw = b"""<html><body><div id="TexteOnly">
<p>HAVE ADOPTED THIS DIRECTIVE:</p>
<p>Article 1</p>
<p>Scope</p>
<p>1. This Directive concerns the legal protection of copyright.</p>
<p>2. It shall not affect existing provisions.</p>
<p>Article 2</p>
<p>Member States shall provide for the exclusive right to authorise.</p>
</div></body></html>"""
    units = HtmlExtractor().extract("32001L0029", raw, article_granularity="paragraph")
    art1 = [u for u in units if u["type"] == "article" and u["number"] == "1"]
    assert art1[0]["title"] == "Scope"
    assert [u["paragraph_num"] for u in art1] == ["1", "2"]
    # A first body line that is a real sentence must NOT become a title.
    art2 = [u for u in units if u["type"] == "article" and u["number"] == "2"]
    assert art2[0]["title"] is None
    assert "exclusive right" in art2[0]["text"]


# ---------------------------------------------------------------------------
# Second-round regressions (2026-07 xhigh review of the first fix round).
# ---------------------------------------------------------------------------

def test_text_only_one_line_article_body_is_not_lost_to_title():
    """A one-line article ('Repealed') must keep its line as text — a title
    with empty text would be dropped by the pipeline's empty-unit filter."""
    from eurlex_builder.extractors.html import HtmlExtractor

    raw = b"""<html><body><div id="TexteOnly">
<p>Article 1</p>
<p>Repealed</p>
<p>Article 2</p>
<p>Member States shall provide for the exclusive right to authorise.</p>
</div></body></html>"""
    units = HtmlExtractor().extract("31993L0000", raw)
    art1 = [u for u in units if u["type"] == "article" and u["number"] == "1"][0]
    assert art1["text"] == "Repealed"
    assert art1["title"] is None


def test_com_signature_rejects_lowercase_continuations():
    from eurlex_builder.extractors.html import _is_com_signature

    assert not _is_com_signature("The President concluded.")
    assert not _is_com_signature("The President will report in June.")
    assert not _is_com_signature("For the Commission to act, more is needed.")
    assert _is_com_signature("The President")
    assert _is_com_signature("For the Commission")
    assert _is_com_signature("Done at Brussels, 3.5.2021")


def test_pdf_parser_rejects_annexes_cover_heading():
    """'## ANNEXES' is a cover heading, not an annex numbered None titled 'ES'."""
    md = """## Article 1
Rules apply.
Done at Brussels, 3 March 2020.
## ANNEXES
## ANNEX I
Real annex content.
"""
    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=True,
    )
    annexes = [u for u in units if u["type"] == "annex"]
    assert [a["number"] for a in annexes] == ["I"]
    assert not any(a.get("title") == "ES" for a in annexes)


def test_pdf_parser_keeps_oj_footnotes_out_of_post_signature_annexes():
    md = """## Article 1
Rules apply.
Done at Brussels, 3 March 2020.
## ANNEX I
List of products
( 1 ) OJ L 123, 12.7.1993, p. 1.
"""
    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=True,
    )
    annex = [u for u in units if u["type"] == "annex"][0]
    assert "List of products" in annex["text"]
    assert "OJ L 123" not in annex["text"]


def test_class_based_empty_subtitle_yields_null_title():
    """An sti-art element with no text must give title=None, not ''."""
    from eurlex_builder.extractors.html import HtmlExtractor

    raw = b"""<html><body>
<p class="ti-art">Article 1</p>
<p class="sti-art"> </p>
<p class="normal">Member States shall take the necessary measures.</p>
</body></html>"""
    units = HtmlExtractor().extract("32001L0000", raw)
    art = [u for u in units if u["type"] == "article"][0]
    assert art["title"] is None
    assert "necessary measures" in art["text"]


def test_manual_css_consumed_subtitle_is_not_emitted_as_duplicate_article():
    raw = b"""<html><body>
    <p class="Titrearticle">Article 1</p>
    <p class="Titrearticle">Scope</p>
    <p class="Normal">This Regulation applies to relevant operators.</p>
    <p class="Titrearticle">Article 2</p>
    <p class="Normal">It shall enter into force.</p>
    </body></html>"""
    articles = [
        unit for unit in HtmlExtractor().extract("TEST", raw)
        if unit["type"] == "article"
    ]
    assert [(unit["number"], unit["title"]) for unit in articles] == [
        ("1", "Scope"), ("2", None),
    ]


def test_manual_css_subtitle_that_mentions_article_is_consumed():
    raw = b"""<html><body>
    <p class="Titrearticle">Article 1</p>
    <p class="Titrearticle">Amendments to Article 12 of Regulation (EU) 2019/1</p>
    <p class="Normal">The amendments shall apply.</p>
    <p class="Titrearticle">Article 2</p>
    <p class="Normal">It shall enter into force.</p>
    </body></html>"""
    articles = [
        unit for unit in HtmlExtractor().extract("TEST", raw)
        if unit["type"] == "article"
    ]
    assert [(unit["number"], unit["title"]) for unit in articles] == [
        ("1", "Amendments to Article 12 of Regulation (EU) 2019/1"),
        ("2", None),
    ]
    assert "amendments shall apply" in articles[0]["text"]


def test_consolidated_norm_annex_does_not_bleed_into_last_article():
    raw = b"""<html><body>
    <p class="title-article-norm">Article 1</p>
    <p class="norm">The operative provision.</p>
    <p class="title-annex-norm">ANNEX I</p>
    <p class="stitle-annex-norm">Product list</p>
    <p class="norm">Annex content.</p>
    </body></html>"""
    units = HtmlExtractor().extract("TEST", raw)
    article = [unit for unit in units if unit["type"] == "article"][0]
    annex = [unit for unit in units if unit["type"] == "annex"][0]
    assert article["text"] == "The operative provision."
    assert annex["number"] == "I"
    assert annex["title"] == "Product list"
    assert annex["text"] == "Annex content."


def test_consolidated_norm_article_keeps_prose_starting_with_annex():
    raw = b"""<html><body>
    <p class="title-article-norm">Article 1</p>
    <p class="norm">Annex I to this Regulation shall apply from 1 January.</p>
    <p class="norm">Member States shall comply.</p>
    <p class="title-annex-norm">ANNEX I</p>
    <p class="norm">Actual annex content.</p>
    </body></html>"""
    units = HtmlExtractor().extract("TEST", raw)
    article = [unit for unit in units if unit["type"] == "article"][0]
    assert "Annex I to this Regulation" in article["text"]
    assert "Member States shall comply" in article["text"]


def test_html_body_fallback_respects_disabled_articles():
    raw = b"<html><body><div id='art_1'><p>Excluded article.</p></div></body></html>"
    units = HtmlExtractor().extract(
        "TEST",
        raw,
        include_recitals=False,
        include_articles=False,
        include_annexes=False,
    )
    assert units == []


def test_pymupdf_fallback_exposes_text_for_translation(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace
    from eurlex_builder.extractors.pdf import PdfExtractor

    class FakePage:
        def get_text(self):
            return "Article 1\nOperative text."

    class FakeDoc(list):
        @property
        def page_count(self):
            return len(self)

        def load_page(self, page_number):
            return self[page_number]

        def close(self):
            return None

    monkeypatch.setitem(
        sys.modules,
        "pymupdf",
        SimpleNamespace(open=lambda path: FakeDoc([FakePage()])),
    )
    pdf_path = tmp_path / "source.pdf"
    pdf_path.write_bytes(b"%PDF-fake")
    metadata = {}
    PdfExtractor._pymupdf_fallback(
        str(pdf_path), "TEST", out_metadata=metadata,
    )
    assert metadata["markdown"] == "Article 1\nOperative text."


def test_docling_timeout_uses_managed_fallback(monkeypatch):
    import eurlex_builder.extractors.pdf as pdf_module
    from eurlex_builder.extractors.pdf import PdfExtractor

    class TimeoutResult:
        def has_timeout_errors(self):
            return True

    class Converter:
        def convert(self, path):
            return TimeoutResult()

    fallback_calls = []

    def fallback(path, celex_id, **kwargs):
        fallback_calls.append((path, celex_id))
        return [{"type": "body", "text": "fallback"}]

    monkeypatch.setattr(pdf_module, "_get_converter", lambda: Converter())
    monkeypatch.setattr(PdfExtractor, "_pymupdf_fallback", staticmethod(fallback))

    units = PdfExtractor().extract("TEST", b"%PDF-fake")

    assert units == [{"type": "body", "text": "fallback"}]
    assert len(fallback_calls) == 1
