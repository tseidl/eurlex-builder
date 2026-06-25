"""Regression tests for extractor bug fixes.

Currently covers:
- Recital inline-ref filter must not drop legitimate recitals that start with
  lowercase words like "the" or "in" (older drafting style; bug exposed
  during the 2026-05 paragraph-granularity re-extraction)
- PDF parser must recognize the enacting formula ("HAS ADOPTED THIS X") as
  the end of the recital zone, otherwise it gets glued onto the last recital
"""

from __future__ import annotations

import re

from eurlex_builder.extractors.html import _INLINE_REF_START_RE
from eurlex_builder.extractors.pdf import _parse_legislative_markdown


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

from eurlex_builder.pipeline import _should_run_translate_fallback


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
