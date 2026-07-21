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
from eurlex_builder.extractors.pdf import (
    PdfExtractor,
    _article_sequence_is_complete,
    _merge_complete_pymupdf_articles,
    _parse_legislative_markdown,
    _repair_displaced_operative_block,
    _repair_embedded_operative_markers,
)
from eurlex_builder.pipeline import _should_run_translate_fallback


_STUB_WORKER = "tests.docling_worker_stub"


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


def test_pdf_text_layer_articles_fill_incomplete_docling_sequence_only():
    docling = [
        {"type": "recital", "number": "1", "text": "First reason"},
        {"type": "recital", "number": "2", "text": "Second reason"},
        {"type": "article", "number": "2", "text": "Second operative rule"},
    ]
    text_layer = [
        {"type": "recital", "number": "2", "text": "Reversed reason"},
        {"type": "recital", "number": "1", "text": "Reversed reason"},
        {"type": "article", "number": "1", "text": "First operative rule"},
        {"type": "article", "number": "2", "text": "Inferior replacement"},
    ]

    merged, repaired = _merge_complete_pymupdf_articles(docling, text_layer)

    assert repaired
    assert [unit["number"] for unit in merged if unit["type"] == "recital"] == [
        "1", "2",
    ]
    assert [unit["number"] for unit in merged if unit["type"] == "article"] == [
        "1", "2",
    ]
    assert next(
        unit["text"] for unit in merged
        if unit["type"] == "article" and unit["number"] == "2"
    ) == "Second operative rule"


def test_pdf_text_layer_articles_extend_single_docling_article():
    docling = [
        {"type": "article", "number": "1", "text": "Preferred first rule"},
    ]
    text_layer = [
        {"type": "article", "number": "1", "text": "Inferior first rule"},
        {"type": "article", "number": "2", "text": "Second operative rule"},
    ]

    merged, repaired = _merge_complete_pymupdf_articles(docling, text_layer)

    assert repaired
    assert [unit["number"] for unit in merged] == ["1", "2"]
    assert merged[0]["text"] == "Preferred first rule"


def test_pdf_text_layer_normalizes_one_numeric_rotation_only():
    docling = [
        {"type": "article", "number": "1", "text": "Preferred first rule"},
        {"type": "article", "number": "4", "text": "Preferred final rule"},
    ]
    text_layer = [
        {"type": "article", "number": "2", "text": "Second operative rule"},
        {"type": "article", "number": "3", "text": "Third operative rule"},
        {"type": "article", "number": "4", "text": "Inferior final rule"},
        {"type": "article", "number": "1", "text": "Inferior first rule"},
    ]

    merged, repaired = _merge_complete_pymupdf_articles(docling, text_layer)

    assert repaired
    assert [unit["number"] for unit in merged] == ["1", "2", "3", "4"]
    assert merged[0]["text"] == "Preferred first rule"
    assert merged[-1]["text"] == "Preferred final rule"


def test_pdf_text_layer_rejects_arbitrary_numeric_reordering():
    docling = [
        {"type": "article", "number": "1", "text": "Preferred first rule"},
    ]
    text_layer = [
        {"type": "article", "number": "1", "text": "First rule"},
        {"type": "article", "number": "3", "text": "Third rule"},
        {"type": "article", "number": "2", "text": "Second rule"},
    ]

    merged, repaired = _merge_complete_pymupdf_articles(docling, text_layer)

    assert not repaired
    assert merged is docling


def test_pdf_text_layer_does_not_replace_a_single_article():
    docling = [
        {"type": "article", "number": "1", "text": "Preferred first rule"},
    ]
    text_layer = [
        {"type": "article", "number": "1", "text": "Inferior first rule"},
    ]

    merged, repaired = _merge_complete_pymupdf_articles(docling, text_layer)

    assert not repaired
    assert merged is docling


def test_pdf_text_layer_replaces_article_group_cleaned_to_empty():
    docling = [
        {"type": "article", "number": "1", "text": "Preferred first rule"},
        {
            "type": "article",
            "number": "2",
            "text": (
                "This Regulation shall be binding in its entirety and directly "
                "applicable in all Member States. Done at Brussels, 1 May 1990."
            ),
        },
    ]
    text_layer = [
        {"type": "article", "number": "1", "text": "Inferior first rule"},
        {"type": "article", "number": "2", "text": "Second operative rule"},
    ]

    merged, repaired = _merge_complete_pymupdf_articles(docling, text_layer)

    assert repaired
    assert [unit["number"] for unit in merged] == ["1", "2"]
    assert merged[0]["text"] == "Preferred first rule"
    assert merged[1]["text"] == "Second operative rule"


def test_pdf_text_layer_trims_repeated_preamble_from_recovered_article():
    docling = [
        {"type": "article", "number": "2", "text": "Preferred second rule"},
    ]
    text_layer = [
        {
            "type": "article",
            "number": "1",
            "text": (
                "First operative rule. THE COMMISSION OF THE EUROPEAN "
                "COMMUNITIES, Having regard to the Treaty, Whereas the measure..."
            ),
        },
        {"type": "article", "number": "2", "text": "Inferior second rule"},
    ]

    merged, repaired = _merge_complete_pymupdf_articles(docling, text_layer)

    assert repaired
    assert merged[0]["text"] == "First operative rule."
    assert merged[1]["text"] == "Preferred second rule"


def test_pdf_text_layer_drops_preamble_and_oj_fragments_before_real_paragraphs():
    docling = [
        {"type": "article", "number": "2", "text": "Preferred second rule"},
    ]
    text_layer = [
        {
            "type": "article",
            "number": "1",
            "text": (
                "THE COMMISSION OF THE EUROPEAN COMMUNITIES, Having regard "
                "to the Treaty, Whereas the measure should be adopted."
            ),
        },
        {"type": "article", "number": "1", "text": "(') OJ No L 281,"},
        {"type": "article", "number": "1", "text": "f5) OJ No L 205, 30."},
        {"type": "article", "number": "1", "text": "11 . 1975, p."},
        {"type": "article", "number": "1", "text": "First operative rule."},
        {
            "type": "article",
            "number": "1",
            "text": (
                "Second operative paragraph. No L 188/9 7. 81 Official Journal "
                "of the European Communities"
            ),
        },
        {
            "type": "article",
            "number": "1",
            "text": "Third operative paragraph. (') OJ No L 281, 1.1.1990, p. 1.",
        },
        {
            "type": "article",
            "number": "1",
            "text": (
                "7. 81 Official Journal of the European Communities No L 188/9 "
                "Fourth operative paragraph."
            ),
        },
        {"type": "article", "number": "2", "text": "Inferior second rule"},
    ]

    merged, repaired = _merge_complete_pymupdf_articles(docling, text_layer)

    assert repaired
    article_one = [unit["text"] for unit in merged if unit["number"] == "1"]
    assert article_one == [
        "First operative rule.",
        "Second operative paragraph.",
        "Third operative paragraph.",
        "Fourth operative paragraph.",
    ]


def test_pdf_text_layer_uses_docling_for_sanitized_empty_candidate_number():
    docling = [
        {"type": "article", "number": "1", "text": "Preferred first rule"},
    ]
    text_layer = [
        {
            "type": "article",
            "number": "1",
            "text": (
                "THE COMMISSION OF THE EUROPEAN COMMUNITIES, Having regard "
                "to the Treaty, Whereas the measure should be adopted."
            ),
        },
        {"type": "article", "number": "2", "text": "Second operative rule"},
    ]

    merged, repaired = _merge_complete_pymupdf_articles(docling, text_layer)

    assert repaired
    assert [unit["number"] for unit in merged] == ["1", "2"]
    assert merged[0]["text"] == "Preferred first rule"
    assert merged[1]["text"] == "Second operative rule"


def test_pdf_article_sequence_rejects_gaps_and_spurious_references():
    def make(numbers):
        return [
            {"type": "article", "number": number, "text": "rule"}
            for number in numbers
        ]

    assert _article_sequence_is_complete(make(["1", "1a", "2"]))
    assert not _article_sequence_is_complete(make(["2"]))
    assert not _article_sequence_is_complete(make(["1", "3"]))
    assert not _article_sequence_is_complete(make(["2", "3", "85"]))


def test_pdf_extractor_repairs_incomplete_docling_articles_from_text_layer(monkeypatch):
    import eurlex_builder.extractors.pdf as pdf_module

    docling = """Whereas the measure should be corrected,
HAS ADOPTED THIS REGULATION:
Article 2
This Regulation shall enter into force tomorrow.
"""
    text_layer = """Whereas the measure should be corrected,
HAS ADOPTED THIS REGULATION:
Article 1
The incorrect date is replaced.
Article 2
This Regulation shall enter into force tomorrow.
"""

    def extract_markdown(celex_id, raw_content, *, out_metadata=None):
        out_metadata.update({"markdown": docling, "pdf_backend": "docling"})
        return docling

    monkeypatch.setattr(pdf_module, "extract_pdf_markdown", extract_markdown)
    monkeypatch.setattr(pdf_module, "extract_pdf_full_text", lambda raw: text_layer)
    metadata = {}

    units = PdfExtractor().extract("X-PDF-DUAL", b"%PDF-fake", out_metadata=metadata)

    assert [unit["number"] for unit in units if unit["type"] == "article"] == [
        "1", "2",
    ]
    assert metadata["pdf_representation_repair"] == "pymupdf_articles"


def test_pdf_extractor_uses_cached_text_layer_for_single_article_repair(monkeypatch):
    import eurlex_builder.extractors.pdf as pdf_module

    docling = """HAS ADOPTED THIS REGULATION:
Article 1
The first operative rule.
"""
    text_layer = """HAS ADOPTED THIS REGULATION:
Article 1
The first operative rule.
Article 2
This Regulation shall enter into force tomorrow.
"""

    def extract_markdown(celex_id, raw_content, *, out_metadata=None):
        out_metadata.update({"markdown": docling, "pdf_backend": "docling"})
        return docling

    def unexpected_extract(raw_content):
        raise AssertionError("cached PDF text layer should be reused")

    monkeypatch.setattr(pdf_module, "extract_pdf_markdown", extract_markdown)
    monkeypatch.setattr(pdf_module, "extract_pdf_full_text", unexpected_extract)
    metadata = {"pdf_text_layer": text_layer}

    units = PdfExtractor().extract("X-PDF-CACHED", b"%PDF-fake", out_metadata=metadata)

    assert [unit["number"] for unit in units if unit["type"] == "article"] == [
        "1", "2",
    ]
    assert metadata["pdf_representation_repair"] == "pymupdf_articles"


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


def test_translation_model_clears_superseded_max_length(monkeypatch):
    import sys
    from types import SimpleNamespace
    from eurlex_builder import translate as tr

    tokenizer = object()
    model = SimpleNamespace(
        generation_config=SimpleNamespace(max_length=512),
    )
    calls = []

    def load_tokenizer(name, *, revision):
        calls.append(("tokenizer", name, revision))
        return tokenizer

    def load_model(name, *, revision):
        calls.append(("model", name, revision))
        return model

    fake_transformers = SimpleNamespace(
        MarianTokenizer=SimpleNamespace(from_pretrained=load_tokenizer),
        MarianMTModel=SimpleNamespace(from_pretrained=load_model),
    )
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)
    monkeypatch.setattr(tr, "_models", {})

    loaded_tokenizer, loaded_model = tr._get_model("fra")

    assert loaded_tokenizer is tokenizer
    assert loaded_model is model
    assert loaded_model.generation_config.max_length is None
    assert calls == [
        ("tokenizer", *tr.MODEL_MAP["fra"]),
        ("model", *tr.MODEL_MAP["fra"]),
    ]


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


def test_text_only_closing_article_is_not_lost_when_boilerplate_is_stripped():
    from eurlex_builder.extractors.html import HtmlExtractor
    from eurlex_builder.utils import strip_boilerplate

    raw = b"""<html><body><div id="TexteOnly">
<p>HAS ADOPTED THIS REGULATION:</p>
<p>Article 1</p><p>The levy shall be fixed at EUR 10.</p>
<p>Article 2</p><p>This Regulation shall enter into force on 1 July 1982</p>
<p>This Regulation shall be binding in its entirety and directly applicable in all Member States.</p>
<p>Done at Brussels, 29 June 1982.</p>
</div></body></html>"""

    units = HtmlExtractor().extract("31982R1680", raw)
    article = [
        unit for unit in units
        if unit["type"] == "article" and unit["number"] == "2"
    ][0]

    assert article["title"] is None
    assert strip_boilerplate(article["text"]) == (
        "This Regulation shall enter into force on 1 July 1982"
    )


def test_text_only_addressee_article_is_not_misclassified_as_title():
    raw = b"""<html><body><div id="TexteOnly">
<p>HAS ADOPTED THIS DECISION:</p>
<p>Article 1</p><p>The aid is compatible with the common market.</p>
<p>Article 2</p><p>This Decision is addressed to the French Republic</p>
<p>Done at Brussels, 11 March 1983.</p>
</div></body></html>"""

    units = HtmlExtractor().extract("31983D0142", raw)
    article = [
        unit for unit in units
        if unit["type"] == "article" and unit["number"] == "2"
    ][0]

    assert article["title"] is None
    assert article["text"].startswith("This Decision is addressed")


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


def test_pdf_parser_repairs_leading_signature_reading_order_and_titled_annex():
    md = """Done at Brussels, 2 July 1990.
(1) OJ No L 281, 1.11.1975, p. 1.
## COMMISSION REGULATION (EEC) No 1856/90
of 2 July 1990
Whereas the import levies should be altered,
HAS ADOPTED THIS REGULATION:
## Article 1
The levies shall be as set out in the Annex.
## Article 2
This Regulation shall enter into force tomorrow.
For the Commission
ANNEX to the Commission Regulation of 2 July 1990 fixing the import levies
| CN code | Levy |
| 1001 | 20 |
"""

    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=True,
    )

    assert [(unit["type"], unit.get("number")) for unit in units] == [
        ("recital", "1"),
        ("article", "1"),
        ("article", "2"),
        ("annex", None),
    ]
    assert "CN code" in units[-1]["text"]


def test_pdf_parser_moves_operative_block_emitted_after_signature():
    md = """## COMMISSION REGULATION (EEC) No 2992/92
Whereas refunds should be fixed as set out in the Annex;
Whereas the measures are in accordance with the committee opinion,
This Regulation shall be binding in its entirety and directly applicable.
Done at Brussels, 15 October 1992.
HAS ADOPTED THIS REGULATION:
## Article 1
The refunds shall be as set out in the Annex.
## Article 2
This Regulation shall enter into force tomorrow.
For the Commission
## ANNEX
| Product | Refund |
| Rice | 10 |
"""

    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=True,
    )

    assert [(unit["type"], unit.get("number")) for unit in units] == [
        ("recital", "1"),
        ("recital", "2"),
        ("article", "1"),
        ("article", "2"),
        ("annex", None),
    ]
    assert "enter into force" in units[3]["text"]


def test_pdf_parser_repairs_stranded_second_article_before_signature():
    md = """Whereas the levies Article 2
This Regulation shall enter into force tomorrow.
This Regulation shall be binding in its entirety and directly applicable.
Done at Brussels, 17 December 1979.
(1) OJ No L 1, 1.1.1979, p. 1.
at present in force should be altered to the amounts in the Annex,
HAS ADOPTED THIS REGULATION:
## Article 1
The levies shall be as set out in the Annex.
## ANNEX
| Product | Levy |
| Sugar | 20 |
"""

    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=True,
    )

    assert [(unit["type"], unit.get("number")) for unit in units] == [
        ("recital", "1"),
        ("article", "1"),
        ("article", "2"),
        ("annex", None),
    ]
    assert "at present in force" in units[0]["text"]
    assert "enter into force" in units[2]["text"]


def test_pdf_parser_moves_article_continuation_emitted_after_signature():
    md = """Whereas the measures are appropriate,
HAS ADOPTED THIS REGULATION:
## Article 1
First rule.
## Article 2
Second rule begins.
This Regulation shall be binding in its entirety and directly applicable.
Done at Brussels, 20 April 1989.
- a continuation of the second rule.
3. A further paragraph of the second rule.
## Article 3
The security shall be EUR 100.
## Article 4
The earlier Regulation is repealed.
## Article 5
This Regulation shall enter into force tomorrow.
For the Commission
## ANNEX
Annex content.
"""

    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=True,
    )

    articles = [unit for unit in units if unit["type"] == "article"]
    assert [article["number"] for article in articles] == ["1", "2", "3", "4", "5"]
    assert "continuation of the second rule" in articles[1]["text"]


def test_pdf_parser_keeps_real_post_signature_attachment_in_place():
    md = """## Article 1
The Decision is approved.
Done at Brussels, 1 January 2000.
## AGREEMENT
## Article 1
The Parties agree.
"""

    repaired = _repair_displaced_operative_block(md.split("\n"))

    assert repaired == md.split("\n")


def test_pdf_parser_keeps_post_signature_attachment_with_formula_in_place():
    md = """## Article 1
The Decision is approved.
## Article 2
This Decision shall enter into force.
Done at Brussels, 1 January 2000.
## AGREEMENT
The parties seek cooperation.
HAVE AGREED AS FOLLOWS:
## Article 1
The Parties agree.
## Article 2
The Agreement enters into force.
"""

    assert _repair_displaced_operative_block(md.split("\n")) == md.split("\n")
    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=True,
    )
    assert [unit["number"] for unit in units if unit["type"] == "article"] == [
        "1", "2",
    ]


def test_pdf_parser_keeps_modifier_led_post_signature_attachments_in_place():
    titles = [
        "## COOPERATION AGREEMENT between the parties",
        "## EXCHANGE OF LETTERS concerning trade",
        "## ADDITIONAL PROTOCOL to the Agreement",
        "## EUROPE AGREEMENT establishing an association",
        "## MEMORANDUM OF UNDERSTANDING on cooperation",
        "## ARRANGEMENT concerning products",
        "Cooperation Agreement between the parties",
        "Exchange of Letters concerning trade",
        "Additional Protocol to the Agreement",
        "Framework for Cooperation",
        "Joint Declaration",
    ]
    for title in titles:
        md = f"""## Article 1
The Decision is approved.
Done at Brussels, 1 January 2000.
{title}
The parties seek cooperation.
HAVE AGREED AS FOLLOWS:
## Article 1
The Parties agree.
"""

        assert _repair_displaced_operative_block(md.split("\n")) == md.split("\n")


def test_pdf_parser_moves_outer_articles_interleaved_into_quoted_table():
    md = """HAS ADOPTED THIS DECISION:
Article 1
Article 1 of the earlier Decision is replaced by the following:
'Article 1
| Person | City |
| A | Vienna |
| B | Brussels | Article 2 This Decision shall take effect today. Article 3
| C | Paris |
This Decision shall be published in the Official Journal.
Done at Brussels, 28 February 2000.
"""

    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=True,
    )

    articles = [unit for unit in units if unit["type"] == "article"]
    assert [article["number"] for article in articles] == ["1", "2", "3"]
    assert "| C | Paris |" in articles[0]["text"]
    assert articles[1]["text"] == "This Decision shall take effect today."
    assert "shall be published" in articles[2]["text"]


def test_pdf_parser_keeps_article_reference_inside_numbered_recital():
    md = """Whereas:
(1) The programme should be established.
(2)
Article 112 of Council Regulation (EC) No 1605/2002
lays down strict conditions for financial assistance.
(3) A further safeguard is required.
HAVE DECIDED AS FOLLOWS:
Article 1
This Decision establishes the programme.
"""

    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=False,
    )

    recitals = [unit for unit in units if unit["type"] == "recital"]
    articles = [unit for unit in units if unit["type"] == "article"]
    assert [unit["number"] for unit in recitals] == ["1", "2", "3"]
    assert "Article 112 of Council Regulation" in recitals[1]["text"]
    assert [unit["number"] for unit in articles] == ["1"]


def test_pdf_parser_drops_marker_only_table_recitals():
    md = """Whereas:
(25)
(19)
(48)
HAS ADOPTED THIS DECISION:
Article 1
The aid is compatible with the common market.
"""

    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=False,
    )

    assert not [unit for unit in units if unit["type"] == "recital"]
    assert [unit["number"] for unit in units if unit["type"] == "article"] == ["1"]


def test_pdf_parser_keeps_decreasing_number_as_recital_continuation():
    md = """Whereas:
(1) The first complete reason.
(2) The second reason starts here.
(1) of Regulation (EEC) No 1/80 continues the second reason.
(3) The third complete reason.
HAS ADOPTED THIS DECISION:
Article 1
The operative rule.
"""

    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=False,
    )

    recitals = [unit for unit in units if unit["type"] == "recital"]
    assert [unit["number"] for unit in recitals] == ["1", "2", "3"]
    assert "continues the second reason" in recitals[1]["text"]


def test_pdf_parser_preserves_restarted_capitalized_recital_sequence():
    md = """Whereas:
(7) The first seventh recital.
(8) The first eighth recital.
(7) The second seventh recital.
(8) The second eighth recital.
HAS ADOPTED THIS DECISION:
Article 1
The operative rule.
"""

    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=False,
    )

    recitals = [unit for unit in units if unit["type"] == "recital"]
    assert [unit["number"] for unit in recitals] == ["7", "8", "7", "8"]


def test_pdf_parser_keeps_numbered_page_fragment_in_old_style_recital():
    md = """Whereas the first complete reason;
Whereas the second complete reason;
Whereas the third reason starts here
(2) of Regulation (EEC) No 1/80; whereas it continues here;
Whereas the fourth complete reason,
HAS ADOPTED THIS REGULATION:
Article 1
The operative rule.
"""

    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=False,
    )

    recitals = [unit for unit in units if unit["type"] == "recital"]
    assert [unit["number"] for unit in recitals] == ["1", "2", "3", "4"]
    assert "it continues here" in recitals[2]["text"]


def test_pdf_parser_does_not_start_zone_for_higher_inline_page_fragment():
    md = """Whereas the first reason starts here
(2) of Regulation (EEC) No 1/80; whereas it continues here;
Whereas the second complete reason,
HAS ADOPTED THIS REGULATION:
Article 1
The operative rule.
"""

    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=False,
    )

    recitals = [unit for unit in units if unit["type"] == "recital"]
    assert [unit["number"] for unit in recitals] == ["1", "2"]
    assert "it continues here" in recitals[0]["text"]
    assert recitals[1]["text"] == "Whereas the second complete reason,"


def test_pdf_parser_keeps_single_lowercase_numbered_recital_without_zone_marker():
    md = """(2) applications submitted on time satisfy the quota; whereas the
corresponding percentage reduction should be fixed,
HAS ADOPTED THIS REGULATION:
Article 1
The operative rule.
"""

    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=False,
    )

    recitals = [unit for unit in units if unit["type"] == "recital"]
    assert [unit["number"] for unit in recitals] == ["2"]
    assert "percentage reduction" in recitals[0]["text"]


def test_pdf_parser_ignores_unattached_leading_reference_fragment():
    md = """(2) of Regulation (EEC) No 1/80; whereas it continues here;
HAS ADOPTED THIS REGULATION:
Article 1
The operative rule.
"""

    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=False,
    )

    assert not [unit for unit in units if unit["type"] == "recital"]


def test_pdf_parser_keeps_increasing_lowercase_numbered_recitals():
    md = """Whereas:
(1) the first numbered reason;
(2) applications submitted on time satisfy the second reason.
HAS ADOPTED THIS REGULATION:
Article 1
The operative rule.
"""

    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=False,
    )

    recitals = [unit for unit in units if unit["type"] == "recital"]
    assert [unit["number"] for unit in recitals] == ["1", "2"]
    assert recitals[1]["text"].startswith("(2) applications")


def test_pdf_parser_accepts_inline_article_body_after_enacting_formula():
    md = """HAVE DECIDED AS FOLLOWS:
Article 1 The following measure is approved.
Article 2 The addressee shall comply.
Article 3 This Decision shall enter into force.
"""

    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=False,
    )

    articles = [unit for unit in units if unit["type"] == "article"]
    assert [unit["number"] for unit in articles] == ["1", "2", "3"]
    assert articles[1]["text"] == "The addressee shall comply."


def test_pdf_parser_splits_formula_and_articles_flattened_onto_recital_line():
    md = """Whereas the measures are appropriate, HAVE ADOPTED THIS REGULATION: Article 1 The levy shall be EUR 10. Article 2 This Regulation shall enter into force tomorrow.
This Regulation shall be binding in its entirety.
"""

    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=False,
    )

    assert [(unit["type"], unit["number"]) for unit in units] == [
        ("recital", "1"),
        ("article", "1"),
        ("article", "2"),
    ]
    assert units[0]["text"] == "Whereas the measures are appropriate,"
    assert units[1]["text"] == "The levy shall be EUR 10."


def test_pdf_parser_splits_embedded_later_article_after_formula():
    md = """HAS ADOPTED THIS REGULATION:
Article 1
First rule.
Article 2
Second rule. Article 3 Regulation (EEC) No 1/80 is hereby repealed.
Article 4
This Regulation shall enter into force tomorrow.
"""

    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=False,
    )

    articles = [unit for unit in units if unit["type"] == "article"]
    assert [unit["number"] for unit in articles] == ["1", "2", "3", "4"]
    assert articles[2]["text"] == "Regulation (EEC) No 1/80 is hereby repealed."


def test_embedded_article_repair_does_not_split_inline_cross_references():
    lines = [
        "HAS ADOPTED THIS REGULATION:",
        "Article 1 The products listed in Article 1 (2) and Article 3 of Regulation "
        "No 1/80 shall qualify.",
    ]

    assert _repair_embedded_operative_markers(lines) == lines


def test_pdf_parser_does_not_promote_articles_inside_quoted_replacement_law():
    md = """HAS ADOPTED THIS REGULATION:
Article 1
Regulation (EEC) No 1/80 is amended as follows: 'Article 2 Replacement text applies. Article 3 Further replacement text applies.'
Article 2
This Regulation shall enter into force tomorrow.
"""

    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=False,
    )

    articles = [unit for unit in units if unit["type"] == "article"]
    assert [unit["number"] for unit in articles] == ["1", "2"]
    assert "Article 3 Further replacement text" in articles[0]["text"]


def test_pdf_parser_does_not_promote_multiline_quoted_replacement_articles():
    md = """HAS ADOPTED THIS REGULATION:
Article 1
Regulation (EEC) No 1/80 is amended as follows:
'Article 2
Replacement text applies.
Article 3
Further replacement text applies.'.
Article 2
This Regulation shall enter into force tomorrow.
"""

    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=False,
    )

    articles = [unit for unit in units if unit["type"] == "article"]
    assert [unit["number"] for unit in articles] == ["1", "2"]
    assert "Article 3 Further replacement text" in articles[0]["text"]


def test_embedded_article_repair_keeps_unpunctuated_cross_reference():
    md = """HAS ADOPTED THIS REGULATION:
Article 1
The measure applies subject to Article 3 This Regulation describes elsewhere.
Article 2
This Regulation shall enter into force tomorrow.
"""

    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=False,
    )

    articles = [unit for unit in units if unit["type"] == "article"]
    assert [unit["number"] for unit in articles] == ["1", "2"]
    assert "subject to Article 3" in articles[0]["text"]


def test_pdf_parser_recovers_trailing_article_markers_from_docling_columns():
    md = """Whereas the measures are appropriate, HAS ADOPTED THIS REGULATION: exported in the natural state, shall be set out in the Annex Article 1
The export refunds on the listed products
shall apply to those products, Article 2
This Regulation shall enter into force tomorrow.
"""

    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=False,
    )

    articles = [unit for unit in units if unit["type"] == "article"]
    assert [unit["number"] for unit in articles] == ["1", "2"]
    assert articles[0]["text"] == (
        "The export refunds on the listed products shall apply to those products, "
        "exported in the natural state, shall be set out in the Annex"
    )
    assert articles[1]["text"] == "This Regulation shall enter into force tomorrow."


def test_trailing_article_repair_does_not_split_end_of_line_cross_reference():
    lines = [
        "HAS ADOPTED THIS REGULATION:",
        "Article 1 The products are those referred to in Article 2",
        "of Regulation (EEC) No 1/80.",
    ]

    assert _repair_embedded_operative_markers(lines) == lines


def test_pdf_parser_rejects_line_wrapped_inline_article_reference():
    md = """Having regard to Article
7 thereof,
Whereas the measures are appropriate,
HAS ADOPTED THIS REGULATION:
Article 1
The levy shall be EUR 10.
Article 2
This Regulation shall enter into force tomorrow.
"""

    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=False,
    )

    articles = [unit for unit in units if unit["type"] == "article"]
    assert [unit["number"] for unit in articles] == ["1", "2"]


def test_pdf_parser_recognizes_multilingual_annex_heading():
    md = """## Article 1
Rules apply.
Dont at Brussels, 18 January 1983.
## ANNEXE - ANHANG - ALLEGATO - BIJLAGE - ANNEX - BILAG ΠΑΡΑΡΤΗΜΑ
| Products | Minimum prices |
| Beef | 100 |
"""

    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=True,
    )

    annex = [unit for unit in units if unit["type"] == "annex"][0]
    assert annex["number"] is None
    assert "Minimum prices" in annex["text"]


def test_pdf_parser_repairs_annex_heading_read_after_table():
    md = """## Article 1
Rules apply.
Done at Brussels, 21 June 1988.
For the Commission
| Place of storage | Quantity |
| North | 57 500 |
| South | 10 000 |
## ANNEX
## ANNEX I
"""

    units = _parse_legislative_markdown(
        md, include_recitals=True, include_articles=True, include_annexes=True,
    )

    annexes = [unit for unit in units if unit["type"] == "annex"]
    assert [(annex["number"], annex["text"]) for annex in annexes] == [
        ("I", "| Place of storage | Quantity | | North | 57 500 | | South | 10 000 |"),
    ]


def test_text_only_handles_nested_legacy_wrapper_and_inline_article_bodies():
    from eurlex_builder.extractors.html import HtmlExtractor

    raw = b"""<html><body><div id="TexteOnly"><p><TXT_TE>
<p>(2) OJ No L 198, 26.7.1988, p. 35.</p>
<p>Whereas the storage contracts were concluded during the wine year;</p>
<p>Whereas the measures are in accordance with the committee opinion,</p>
<p>HAS ADOPTED THIS REGULATION:</p>
<p>Article 1 The following Article 2a is inserted:</p>
<p>'Article 2a The amounts shall be converted.' Article 2 This Regulation enters into force today.</p>
<p>This Regulation shall be binding in its entirety and directly applicable in all Member States.</p>
</TXT_TE></p></div></body></html>"""

    units = HtmlExtractor().extract("31988R3127", raw)

    assert [(unit["type"], unit.get("number")) for unit in units] == [
        ("recital", "1"),
        ("recital", "2"),
        ("article", "1"),
        ("article", "2"),
    ]
    assert "Article 2a The amounts" in units[2]["text"]
    assert "enters into force" in units[3]["text"]


def test_text_only_does_not_split_possessive_article_reference():
    raw = b"""<html><body><div id="TexteOnly">
<p>HAS ADOPTED THIS DECISION:</p>
<p>Article 1</p>
<p>The Member States' Article 5 obligations under the Treaty shall apply.</p>
<p>Article 2</p><p>This Decision shall enter into force.</p>
</div></body></html>"""

    units = HtmlExtractor().extract("X-POSSESSIVE", raw)

    articles = [unit for unit in units if unit["type"] == "article"]
    assert [unit["number"] for unit in articles] == ["1", "2"]
    assert "Article 5 obligations" in articles[0]["text"]


def test_text_only_does_not_promote_multiline_quoted_replacement_articles():
    raw = b"""<html><body><div id="TexteOnly">
<p>HAS ADOPTED THIS REGULATION:</p>
<p>Article 1</p>
<p>Regulation (EEC) No 1/80 is amended as follows:</p>
<p>'Article 2</p>
<p>Replacement text applies.</p>
<p>Article 3</p>
<p>Further replacement text applies.'.</p>
<p>Article 2</p>
<p>This Regulation shall enter into force tomorrow.</p>
</div></body></html>"""

    units = HtmlExtractor().extract("X-QUOTED-LAW", raw)

    articles = [unit for unit in units if unit["type"] == "article"]
    assert [unit["number"] for unit in articles] == ["1", "2"]
    assert "Article 3 Further replacement text" in articles[0]["text"]


def test_text_only_legacy_backtick_quotes_do_not_hide_outer_article():
    raw = b"""<html><body><div id="TexteOnly">
<p>HAS ADOPTED THIS DECISION:</p>
<p>Article 1</p>
<p>Article 2 is replaced by the following:</p>
<p>'Article 2</p><p>France's obligations apply.`</p>
<p>2. Article 3 is replaced by the following:</p>
<p>'Article 3</p><p>Further replacement text applies.`</p>
<p>Article 2</p><p>This Decision is addressed to France.</p>
</div></body></html>"""

    units = HtmlExtractor().extract("X-LEGACY-QUOTES", raw)

    articles = [unit for unit in units if unit["type"] == "article"]
    assert [unit["number"] for unit in articles] == ["1", "2"]
    assert "Article 3" in articles[0]["text"]
    assert articles[1]["text"] == "This Decision is addressed to France."


def test_text_only_splits_run_on_capitalized_whereas_clauses():
    raw = b"""<html><body><div id="TexteOnly">
<p>THE COUNCIL, Having regard to the Treaty, Whereas the first reason applies;
whereas its second sentence qualifies that reason; Whereas the second reason applies;
Whereas the third reason applies, HAS DECIDED AS FOLLOWS:</p>
<p>Article 1</p><p>The operative rule.</p>
</div></body></html>"""

    units = HtmlExtractor().extract("X-RUN-ON", raw)
    recitals = [unit for unit in units if unit["type"] == "recital"]

    assert [unit["number"] for unit in recitals] == ["1", "2", "3"]
    assert "whereas its second sentence" in recitals[0]["text"]
    assert all("HAS DECIDED" not in unit["text"] for unit in recitals)


def test_text_only_accepts_consecutive_plain_numbers_after_whereas_marker():
    raw = b"""<html><body><div id="TexteOnly">
<p>Whereas:</p>
<p>A. THE FACTS</p>
<p>1 The first reason is set out here.</p>
<p>Supporting detail remains with the first reason.</p>
<p>2 1.1. The second reason begins with a nested section label.</p>
<p>3 2. The third reason begins with a top-level section label.</p>
<p>HAS ADOPTED THIS DECISION:</p>
<p>Article 1</p><p>The operative rule.</p>
</div></body></html>"""

    units = HtmlExtractor().extract("X-PLAIN-NUMBERS", raw)
    recitals = [unit for unit in units if unit["type"] == "recital"]

    assert [unit["number"] for unit in recitals] == ["1", "2", "3"]
    assert "Supporting detail" in recitals[0]["text"]


def test_text_only_does_not_treat_date_as_plain_numbered_recital():
    raw = b"""<html><body><div id="TexteOnly">
<p>Whereas:</p>
<p>1 The first reason is set out here.</p>
<p>2 December 1979 was fixed as the reference date.</p>
<p>2 The second reason is set out here.</p>
<p>HAS ADOPTED THIS DECISION:</p>
<p>Article 1</p><p>The operative rule.</p>
</div></body></html>"""

    units = HtmlExtractor().extract("X-DATE", raw)
    recitals = [unit for unit in units if unit["type"] == "recital"]

    assert [unit["number"] for unit in recitals] == ["1", "2"]
    assert "2 December 1979" in recitals[0]["text"]


def test_classless_table_column_numbers_do_not_suppress_real_recitals():
    raw = b"""<html><body>
<p>Whereas the first reason applies;</p>
<p>Whereas the second reason applies,</p>
<p>HAS ADOPTED THIS REGULATION:</p>
<p>Article 1</p><p>The operative rule.</p>
<p>Done at Brussels, 1 January 2000.</p>
<p>ANNEX</p>
<table><tr><td>(1)</td><td>(2)</td><td>(3)</td></tr>
<tr><td>Alpha</td><td>Beta</td><td>Gamma</td></tr></table>
</body></html>"""

    units = HtmlExtractor().extract("X-TABLE-HEADER", raw)

    assert [unit["number"] for unit in units if unit["type"] == "recital"] == [
        "1", "2",
    ]
    assert [unit["number"] for unit in units if unit["type"] == "article"] == ["1"]


def test_classless_numbered_annex_table_does_not_suppress_real_recitals():
    raw = b"""<html><body>
<p>Whereas the first reason applies;</p>
<p>Whereas the second reason applies,</p>
<p>HAS ADOPTED THIS REGULATION:</p>
<p>Article 1</p><p>The operative rule.</p>
<p>Done at Brussels, 1 January 2000.</p>
<p>ANNEX</p>
<table><tr><td>(1)</td><td>First numbered column description.</td></tr>
<tr><td>(2)</td><td>Second numbered column description.</td></tr>
<tr><td>(3)</td><td>Third numbered column description.</td></tr></table>
</body></html>"""

    units = HtmlExtractor().extract("X-NUMBERED-ANNEX", raw)

    assert [unit["number"] for unit in units if unit["type"] == "recital"] == [
        "1", "2",
    ]
    assert [unit["number"] for unit in units if unit["type"] == "article"] == ["1"]


def test_classless_credible_pre_formula_table_recitals_are_retained():
    raw = b"""<html><body>
<p>Whereas:</p>
<table><tr><td>(1)</td><td>The first complete reason.</td></tr>
<tr><td>(2)</td><td>The second complete reason.</td></tr></table>
<p>HAS ADOPTED THIS REGULATION:</p>
<p>Article 1</p><p>The operative rule.</p>
</body></html>"""

    units = HtmlExtractor().extract("X-TABLE-RECITALS", raw)

    recitals = [unit for unit in units if unit["type"] == "recital"]
    assert [unit["number"] for unit in recitals] == ["1", "2"]
    assert "first complete reason" in recitals[0]["text"]


def test_table_recitals_do_not_stop_at_incidental_enacting_phrase():
    raw = b"""<html><body>
<table><tr><td>(1)</td><td>The Commission has adopted a prior decision.</td></tr></table>
<table><tr><td>(2)</td><td>The second complete reason.</td></tr></table>
<p>HAS ADOPTED THIS DECISION:</p>
<div id="art_1"><p>The operative rule.</p></div>
</body></html>"""

    units = HtmlExtractor().extract("X-TABLE-PHRASE", raw)

    recitals = [unit for unit in units if unit["type"] == "recital"]
    assert [unit["number"] for unit in recitals] == ["1", "2"]


def test_table_recitals_keep_nested_numbered_lists_inside_parent():
    raw = b"""<html><body>
<table><tr><td>(1)</td><td>The first reason.
<table><tr><td>(1)</td><td>Nested evidence one.</td></tr>
<tr><td>(2)</td><td>Nested evidence two.</td></tr></table>
</td></tr></table>
<table><tr><td>(2)</td><td>The second complete reason.</td></tr></table>
<p>HAS ADOPTED THIS DECISION:</p>
<div id="art_1"><p>The operative rule.</p></div>
</body></html>"""

    units = HtmlExtractor().extract("X-NESTED-TABLE", raw)

    recitals = [unit for unit in units if unit["type"] == "recital"]
    assert [unit["number"] for unit in recitals] == ["1", "2"]
    assert "Nested evidence one" in recitals[0]["text"]


def test_classless_table_recitals_continue_paragraph_sequence():
    raw = b"""<html><body>
<p>Whereas:</p>
<p>(1) The first complete reason.</p>
<p>(2) The second complete reason.</p>
<table><tr><td>(3)</td><td>The third complete reason.</td></tr>
<tr><td>(4)</td><td>The fourth complete reason.</td></tr></table>
</body></html>"""

    units = HtmlExtractor().extract("X-MIXED-RECITALS", raw)

    recitals = [unit for unit in units if unit["type"] == "recital"]
    assert [unit["number"] for unit in recitals] == ["1", "2", "3", "4"]
    assert recitals[-1]["text"] == "(4) The fourth complete reason."


def test_table_recitals_skip_sibling_numbered_list_before_sequence_resumes():
    raw = b"""<html><body>
<table><tr><td>(1)</td><td>The first complete reason.</td></tr></table>
<table><tr><td>(2)</td><td>The second complete reason.</td></tr></table>
<p>(a) Legal basis</p>
<table><tr><td>(1)</td><td>First cited instrument.</td></tr></table>
<table><tr><td>(2)</td><td>Second cited instrument.</td></tr></table>
<table><tr><td>(3)</td><td>The third complete reason.</td></tr></table>
<p>HAS ADOPTED THIS DECISION:</p>
<div id="art_1"><p>The operative rule.</p></div>
</body></html>"""

    units = HtmlExtractor().extract("X-SIBLING-LIST", raw)

    recitals = [unit for unit in units if unit["type"] == "recital"]
    assert [unit["number"] for unit in recitals] == ["1", "2", "3"]
    assert all("cited instrument" not in unit["text"] for unit in recitals)


def test_text_only_does_not_promote_dotted_topic_headings_to_recitals():
    raw = b"""<html><body><div id="TexteOnly">
<p>Whereas:</p>
<p>1. Case history</p><p>Narrative discussion without recital numbers.</p>
<p>2. Assessment</p><p>Further narrative discussion.</p>
<p>HAS ADOPTED THIS DECISION:</p>
<p>Article 1</p><p>The operative rule.</p>
</div></body></html>"""

    units = HtmlExtractor().extract("X-TOPIC-HEADINGS", raw)

    assert not [unit for unit in units if unit["type"] == "recital"]
    assert [unit["number"] for unit in units if unit["type"] == "article"] == ["1"]


def test_html_full_text_uses_legacy_utf8_alias_without_nested_duplication():
    from eurlex_builder.extractors.html import extract_html_full_text

    raw = """<html><head><meta charset="UNICODE-1-1-UTF-8"/></head><body>
<p><TXT_TE><p>Österreich substantive text.</p><p>Second paragraph.</p></TXT_TE></p>
</body></html>""".encode()

    full_text = extract_html_full_text(raw)

    assert full_text == "Österreich substantive text. Second paragraph."


def test_standard_html_also_extracts_class_based_attachment_articles():
    from eurlex_builder.extractors.html import HtmlExtractor

    raw = b"""<html><body>
<div id="rct_1"><p>(1) The agreement should be approved.</p></div>
<div id="art_1"><p class="oj-ti-art">Article 1</p><p>Main decision.</p></div>
<hr class="oj-doc-sep"/>
<div id="agr_1.art_1"><p class="oj-ti-art">Article 1</p>
<div><p>Agreement first article.</p></div></div>
<div id="agr_1.art_2"><p class="oj-ti-art">Article 2</p>
<div><p>Agreement second article.</p></div></div>
</body></html>"""

    units = HtmlExtractor().extract("X1", raw)

    assert [(unit["type"], unit.get("number")) for unit in units] == [
        ("recital", "1"),
        ("article", "1"),
        ("article", "1"),
        ("article", "2"),
    ]
    assert [unit["text"] for unit in units[1:]] == [
        "Main decision.",
        "Agreement first article.",
        "Agreement second article.",
    ]


def test_classless_summary_joins_number_and_adjacent_table_cell_text():
    from eurlex_builder.extractors.html import HtmlExtractor

    raw = b"""<html><body><p>SUMMARY OF THE INFRINGEMENT</p>
<table><tr><td></td><td><p>(1)</p></td>
<td><span>The first complete summary paragraph.</span></td></tr></table>
<table><tr><td></td><td><p>(2)</p></td>
<td><span>The second complete summary paragraph.</span></td></tr></table>
</body></html>"""

    units = HtmlExtractor().extract("X2", raw)

    assert [(unit["type"], unit.get("number")) for unit in units] == [
        ("recital", "1"),
        ("recital", "2"),
    ]
    assert units[0]["text"] == "(1) The first complete summary paragraph."
    assert units[1]["text"] == "(2) The second complete summary paragraph."


def test_classless_table_recitals_keep_articles_and_annexes():
    raw = b"""<html><body>
<table><tr><td><p>(1)</p></td><td>The complete recital.</td></tr></table>
<p>HAS ADOPTED THIS DECISION:</p>
<p>Article 1</p><p>The operative rule.</p>
<p>Done at Brussels, 1 January 2000.</p>
<p>ANNEX</p><p>Supplementary content.</p>
</body></html>"""

    units = HtmlExtractor().extract("X3", raw)

    assert [(unit["type"], unit.get("number")) for unit in units] == [
        ("recital", "1"),
        ("article", "1"),
        ("annex", None),
    ]
    assert units[0]["text"] == "(1) The complete recital."
    assert units[1]["text"].startswith("The operative rule.")
    assert units[2]["text"] == "Supplementary content."


def test_text_only_joins_marker_only_recital_with_following_paragraphs():
    raw = b"""<html><body><div id="TexteOnly">
<p>Whereas:</p>
<p>(1)</p><p>The first recital begins here.</p><p>It continues here.</p>
<p>(2)</p><p>The second recital is complete.</p>
<p>HAS ADOPTED THIS DECISION:</p>
<p>Article 1</p><p>The operative rule.</p>
</div></body></html>"""

    units = HtmlExtractor().extract("X4", raw)

    assert [(unit["type"], unit.get("number")) for unit in units] == [
        ("recital", "1"),
        ("recital", "2"),
        ("article", "1"),
    ]
    assert units[0]["text"] == (
        "(1) The first recital begins here. It continues here."
    )
    assert units[1]["text"] == "(2) The second recital is complete."


def test_text_only_does_not_treat_consecutive_table_column_markers_as_recitals():
    raw = b"""<html><body><div id="TexteOnly">
<p>Whereas:</p>
<p>(1)</p><p>The market data are set out below.</p>
<p>Table 1</p><p>(1)</p><p>(2)</p><p>(3)</p><p>100</p><p>200</p><p>300</p>
<p>(2)</p><p>The legal assessment follows.</p>
<p>HAS ADOPTED THIS DECISION:</p>
<p>Article 1</p><p>The operative rule.</p>
</div></body></html>"""

    units = HtmlExtractor().extract("X5", raw)

    recitals = [unit for unit in units if unit["type"] == "recital"]
    assert [unit["number"] for unit in recitals] == ["1", "2"]
    assert "Table 1 (1) (2) (3) 100 200 300" in recitals[0]["text"]


def test_text_only_does_not_restart_recitals_at_decreasing_table_markers():
    raw = b"""<html><body><div id="TexteOnly">
<p>Whereas:</p>
<p>(7)</p><p>The seventh recital begins here.</p>
<p>(2)+(3)</p><p>(5)+(6)</p><p>Table values.</p>
<p>(8)</p><p>The eighth recital follows.</p>
<p>HAS ADOPTED THIS DECISION:</p>
<p>Article 1</p><p>The operative rule.</p>
</div></body></html>"""

    units = HtmlExtractor().extract("X6", raw)

    recitals = [unit for unit in units if unit["type"] == "recital"]
    assert [unit["number"] for unit in recitals] == ["7", "8"]
    assert "(2)+(3) (5)+(6) Table values." in recitals[0]["text"]


def test_text_only_accepts_inline_article_body_after_enacting_formula():
    raw = b"""<html><body><div id="TexteOnly">
<p>HAS ADOPTED THIS DECISION:</p>
<p>Article 1 The following measure is approved.</p>
<p>Article 2 The addressee shall comply.</p>
<p>Article 3 This Decision shall enter into force.</p>
</div></body></html>"""

    units = HtmlExtractor().extract("X7", raw)

    articles = [unit for unit in units if unit["type"] == "article"]
    assert [unit["number"] for unit in articles] == ["1", "2", "3"]
    assert articles[1]["text"] == "The addressee shall comply."


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
    import eurlex_builder.extractors.pdf as pdf_module
    from eurlex_builder.extractors.pdf import PdfExtractor, _DoclingResult

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
    class TimeoutWorker:
        def convert(self, path):
            return _DoclingResult(
                failure_reason="timeout", error="deadline exceeded",
            )

    monkeypatch.setattr(pdf_module, "_get_docling_worker", TimeoutWorker)
    metadata = {}
    units = PdfExtractor().extract("TEST", b"%PDF-fake", out_metadata=metadata)

    assert metadata["markdown"] == "Article 1\nOperative text."
    assert metadata["pdf_backend"] == "pymupdf"
    assert metadata["pdf_fallback_reason"] == "timeout"
    assert units[0]["type"] == "article"


def test_partial_conversion_result_is_not_success():
    from types import SimpleNamespace
    from eurlex_builder.extractors.pdf import _conversion_result_error

    result = SimpleNamespace(
        status=SimpleNamespace(value="partial_success"),
        errors=[SimpleNamespace(error_message="page parse failed")],
    )
    assert _conversion_result_error(result) == "page parse failed"


def test_persistent_worker_round_trip_and_reuse(tmp_path):
    from eurlex_builder.extractors.pdf import _DoclingWorkerClient

    source = tmp_path / "source.pdf"
    source.write_bytes(b"SUCCESS")
    worker = _DoclingWorkerClient(
        worker_module=_STUB_WORKER,
        startup_timeout=5,
        conversion_timeout=5,
    )
    try:
        first = worker.convert(str(source))
        first_pid = worker._process.pid
        second = worker.convert(str(source))
        assert first.markdown == "Article 1\nStub text"
        assert second.markdown == first.markdown
        assert worker._process.pid == first_pid
    finally:
        worker.close()


def test_persistent_worker_transfers_large_output(tmp_path):
    from eurlex_builder.extractors.pdf import _DoclingWorkerClient

    source = tmp_path / "large.pdf"
    source.write_bytes(b"LARGE")
    worker = _DoclingWorkerClient(
        worker_module=_STUB_WORKER,
        startup_timeout=5,
        conversion_timeout=5,
    )
    try:
        result = worker.convert(str(source))
        assert result.markdown is not None
        assert len(result.markdown) == 8 * 1024 * 1024
    finally:
        worker.close()


def test_persistent_worker_rejects_partial_result(tmp_path):
    from eurlex_builder.extractors.pdf import _DoclingWorkerClient

    source = tmp_path / "partial.pdf"
    source.write_bytes(b"PARTIAL")
    worker = _DoclingWorkerClient(
        worker_module=_STUB_WORKER,
        startup_timeout=5,
        conversion_timeout=5,
    )
    try:
        result = worker.convert(str(source))
        assert result.markdown is None
        assert result.failure_reason == "partial"
        assert result.error == "page failed"
        assert worker._process is None
    finally:
        worker.close()


def test_persistent_worker_reports_crash_without_stalling(tmp_path):
    import time
    from eurlex_builder.extractors.pdf import _DoclingWorkerClient

    source = tmp_path / "crash.pdf"
    source.write_bytes(b"CRASH")
    worker = _DoclingWorkerClient(
        worker_module=_STUB_WORKER,
        startup_timeout=5,
        conversion_timeout=5,
    )
    started = time.monotonic()
    try:
        result = worker.convert(str(source))
        assert result.failure_reason == "crash"
        assert "exitcode=17" in (result.error or "")
        assert time.monotonic() - started < 5
        assert worker._process is None
    finally:
        worker.close()


def test_persistent_worker_reports_startup_error(tmp_path):
    from eurlex_builder.extractors.pdf import _DoclingWorkerClient

    source = tmp_path / "source.pdf"
    source.write_bytes(b"SUCCESS")
    worker = _DoclingWorkerClient(
        worker_module="tests.docling_startup_error_stub",
        startup_timeout=5,
        conversion_timeout=5,
    )
    try:
        result = worker.convert(str(source))
        assert result.failure_reason == "startup"
        assert "model unavailable" in (result.error or "")
        assert worker._process is None
    finally:
        worker.close()


def test_complete_control_pipe_garbage_fails_without_stalling(tmp_path):
    import time
    from eurlex_builder.extractors.pdf import _DoclingWorkerClient

    source = tmp_path / "garbage.pdf"
    source.write_bytes(b"GARBAGE_LINE")
    worker = _DoclingWorkerClient(
        worker_module=_STUB_WORKER,
        startup_timeout=5,
        conversion_timeout=2,
    )
    started = time.monotonic()
    try:
        result = worker.convert(str(source))
        assert result.failure_reason == "crash"
        assert "invalid Docling worker response" in (result.error or "")
        assert time.monotonic() - started < 5
    finally:
        worker.close()


def test_partial_control_pipe_line_obeys_deadline(tmp_path):
    import time
    from eurlex_builder.extractors.pdf import _DoclingWorkerClient

    source = tmp_path / "partial-line.pdf"
    source.write_bytes(b"PARTIAL_LINE")
    worker = _DoclingWorkerClient(
        worker_module=_STUB_WORKER,
        startup_timeout=5,
        conversion_timeout=0.2,
    )
    started = time.monotonic()
    try:
        result = worker.convert(str(source))
        assert result.failure_reason == "timeout"
        assert time.monotonic() - started < 5
    finally:
        worker.close()


def test_three_consecutive_startup_failures_open_circuit(monkeypatch):
    import pytest
    import eurlex_builder.extractors.pdf as pdf_module
    from eurlex_builder.errors import DoclingStartupError
    from eurlex_builder.extractors.pdf import _DoclingResult

    class FailedWorker:
        def convert(self, path):
            return _DoclingResult(
                failure_reason="startup", error="model unavailable",
            )

    monkeypatch.setattr(pdf_module, "_get_docling_worker", FailedWorker)
    pdf_module.enable_docling_workers()
    fatal_flags = []
    try:
        for _ in range(3):
            with pytest.raises(DoclingStartupError) as exc_info:
                pdf_module.extract_pdf_markdown("TEST", b"%PDF-fake")
            fatal_flags.append(exc_info.value.fatal)
    finally:
        pdf_module.enable_docling_workers()

    assert fatal_flags == [False, False, True]


def test_persistent_worker_timeout_reaps_child(tmp_path):
    import time
    from eurlex_builder.extractors.pdf import _DoclingWorkerClient

    source = tmp_path / "hang.pdf"
    source.write_bytes(b"HANG")
    worker = _DoclingWorkerClient(
        worker_module=_STUB_WORKER,
        startup_timeout=5,
        conversion_timeout=0.2,
    )
    started = time.monotonic()
    try:
        result = worker.convert(str(source))
        assert result.failure_reason == "timeout"
        assert time.monotonic() - started < 5
        assert worker._process is None
    finally:
        worker.close()


def test_eight_worker_clients_run_concurrently_without_leaks(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from eurlex_builder.extractors.pdf import _DoclingWorkerClient

    def convert(index):
        source = tmp_path / f"source-{index}.pdf"
        source.write_bytes(b"SUCCESS")
        worker = _DoclingWorkerClient(
            worker_module=_STUB_WORKER,
            startup_timeout=5,
            conversion_timeout=5,
        )
        try:
            return worker.convert(str(source)).markdown
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(convert, range(8)))

    assert results == ["Article 1\nStub text"] * 8


def test_close_interrupts_active_worker_without_waiting_for_deadline(tmp_path):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from eurlex_builder.extractors.pdf import _DoclingWorkerClient

    source = tmp_path / "hang-close.pdf"
    source.write_bytes(b"HANG")
    worker = _DoclingWorkerClient(
        worker_module=_STUB_WORKER,
        startup_timeout=5,
        conversion_timeout=60,
    )
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker.convert, str(source))
        deadline = time.monotonic() + 5
        started_marker = tmp_path / "hang-close.pdf.started"
        while not started_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started_marker.exists()
        worker.close()
        result = future.result(timeout=5)

    assert result.failure_reason == "crash"
    assert time.monotonic() - started < 5
    assert worker._process is None


def test_force_close_blocks_late_worker_creation():
    import pytest
    import eurlex_builder.extractors.pdf as pdf_module

    pdf_module.close_all_docling_workers(force=True)
    try:
        with pytest.raises(RuntimeError, match="shutting down"):
            pdf_module._get_docling_worker()
    finally:
        pdf_module.enable_docling_workers()
