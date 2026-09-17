"""Regressions for PDF preambles, cross-references and conservative layout recovery."""

from __future__ import annotations

import copy
import itertools
import json
import re
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from eurlex_builder.extractors import pdf
from eurlex_builder.extractors.pdf_layout import (
    _footnote_lines,
    prepare_pymupdf_structure,
)
from eurlex_builder.pipeline import Pipeline

FIXTURES = Path(__file__).parent / "fixtures" / "pdf"
PAGES = {p["id"]: p for p in json.loads((FIXTURES / "preamble-layout-pages.json").read_text())}
FIRST_PAGES = [p for p in PAGES.values() if p["celex_id"] == "32016R0679" and p["page"] == 1]


def parse(text: str, **options) -> list[dict]:
    """Request all structures unless a test selects a particular subset."""
    return pdf._parse_legislative_markdown(text, **{
        "include_recitals": True, "include_articles": True, "include_annexes": True, **options,
    })


@pytest.fixture
def fake_pdf(monkeypatch):
    """Replay saved public PDF geometry through the same API used by PyMuPDF."""
    class Page:
        def __init__(self, record):
            self.record = record
            self.rect = SimpleNamespace(width=record["width"], height=record["height"])

        def get_text(self, kind=None, **kwargs):
            return {"blocks": self.record["blocks"]} if kind == "dict" else self.record["raw_text"]

    class Document(list):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        @property
        def page_count(self):
            return len(self)

        def load_page(self, n):
            return self[n]

        def close(self):
            pass

    def install(records):
        monkeypatch.setitem(sys.modules, "pymupdf", SimpleNamespace(
            TEXTFLAGS_TEXT=195, open=lambda *a, **kw: Document(Page(p) for p in records),
        ))
        return "\n\n".join(p["raw_text"] for p in records if p["raw_text"])

    return install


@pytest.mark.parametrize("record", FIRST_PAGES, ids=lambda p: p["language"])
def test_official_first_page_recitals_exclude_small_citation_footnotes(fake_pdf, record):
    source = fake_pdf([record])
    text, changes = prepare_pymupdf_structure(b"%PDF-fixture", source, record["language"])
    units = parse(text, language=record["language"])
    assert [u["number"] for u in units if u["type"] == "recital"] == ["1", "2", "3"]
    assert changes == {"recital_footnotes": [1]}
    assert "95/46" in units[-1]["text"]  # The actual recital also cites legislation.
    assert "229" not in text  # The smaller-font OJ/JO/ABl/GU/PB/DO citation is a footnote.
    assert pdf.extract_pdf_full_text(b"%PDF-fixture") == source


@pytest.mark.parametrize("key", ["32016R0679-ita-p27", "32016R0679-nld-p30", "32019R2088-eng-p2"])
def test_footnotes_can_span_blocks_or_much_of_the_bottom_half(fake_pdf, key):
    page = PAGES[key]
    first = next(p for p in FIRST_PAGES if p["language"] == page["language"])
    source = fake_pdf([first, page])
    text, changes = prepare_pymupdf_structure(b"%PDF-fixture", source, page["language"])
    assert changes["recital_footnotes"] == [1, 2]
    second = text.split("\n\n", 1)[1]
    assert "(1) Regolamento (UE)" not in second
    assert "(1) Verordening (EU)" not in second
    assert "(4) Directive 2009/65/EC" not in second
    assert "recital_footnotes" in changes


def test_slightly_overlapping_footnote_boxes_on_pymupdf_128(fake_pdf):
    first = next(p for p in FIRST_PAGES if p["language"] == "eng")
    page = PAGES["32014L0065-eng-p2-pymupdf128"]
    source = fake_pdf([first, page])
    text, changes = prepare_pymupdf_structure(b"%PDF-fixture", source, "eng")
    assert changes["recital_footnotes"] == [1, 2]
    assert "( 1 ) Regulation (EU) No 600/2014" not in text
    assert "The limitation of the scope concerning commodity derivatives" in text
    assert "parties and trade repositories (OJ L 201" not in text


def test_spanish_columns_recover_recitals_and_correct_article_membership(fake_pdf):
    page = PAGES["31982D0809-spa-p1"]
    source = (FIXTURES / "31982D0809-spa.md").read_text()
    rest = source.removeprefix(page["raw_text"]).strip()
    assert fake_pdf([page, {**page, "raw_text": rest, "blocks": []}]) == source
    text, changes = prepare_pymupdf_structure(b"%PDF-fixture", source, "spa")
    assert changes == {"preamble_columns": [1]}
    assert Counter(text.split()) == Counter(source.split())
    units = parse(text, language="spa")
    assert [(u["type"], u["number"]) for u in units] == [
        ("recital", "1"), ("recital", "2"), ("article", "1"), ("article", "2"),
        ("annex", "I"), ("annex", "II"),
    ]
    assert units[0]["text"].startswith("Considerando que el Sexto Convenio")
    assert units[1]["text"].startswith("Considerando que es, pues, conveniente")
    assert units[2]["text"].startswith("Para el 30 de abril de 1982")
    assert "Visto" not in units[2]["text"]
    assert "Considerando" not in units[2]["text"]
    assert units[3]["text"].startswith("Se autoriza al Presidente del Consejo")
    old_annexes = [u for u in parse(source, language="spa") if u["type"] == "annex"]
    assert units[-2:] == old_annexes
    assert pdf.extract_pdf_full_text(b"%PDF-fixture") == source


def test_right_column_recital_continuation_is_not_partially_reordered(fake_pdf):
    page = PAGES["31983D0142-eng-p1"]
    source = fake_pdf([page])
    assert prepare_pymupdf_structure(b"%PDF-fixture", source, "eng") == (source, {})


@pytest.mark.parametrize("variation", ["source_mismatch", "geometry_mismatch", "no_gutter", "wrong_language"])
def test_ambiguous_or_mismatched_layout_retains_original_text(fake_pdf, variation):
    page = copy.deepcopy(PAGES["31982D0809-spa-p1"])
    language = "spa"
    if variation == "geometry_mismatch":
        page["blocks"][0]["lines"][0]["spans"][0]["text"] += " extra"
    if variation == "no_gutter":
        for block in page["blocks"]:
            for line in block["lines"]:
                line["bbox"] = [0, line["bbox"][1], page["width"], line["bbox"][3]]
    if variation == "wrong_language":
        language = "eng"
    source = fake_pdf([page])
    if variation == "source_mismatch":
        source += "\nAdditional source text"
    assert prepare_pymupdf_structure(b"%PDF-fixture", source, language) == (source, {})


def test_failed_geometry_read_preserves_source(monkeypatch):
    monkeypatch.setitem(sys.modules, "pymupdf", SimpleNamespace(open=Mock(side_effect=RuntimeError)))
    assert prepare_pymupdf_structure(b"bad", "Original text", "eng") == ("Original text", {})


def test_floating_footnote_digits_do_not_start_extra_recitals(fake_pdf):
    source = fake_pdf([PAGES["32010R1095-eng-p1"], PAGES["32010R1095-eng-p2"]])
    text, changes = prepare_pymupdf_structure(b"%PDF-fixture", source, "eng")
    units = parse(text)
    assert [u["number"] for u in units] == list(map(str, range(1, 11)))
    assert changes == {"recital_footnotes": [1, 2]}
    assert "establishing a European single rule book" in units[4]["text"]
    assert pdf.extract_pdf_full_text(b"%PDF-fixture") == source


@pytest.mark.parametrize("label,size,expected_removed", [("1", 6, True), ("1", 10, False), ("2", 6, False)])
def test_floating_marker_requires_small_digit_and_matching_footnote(label, size, expected_removed):
    lines = [
        {"text": "The preceding substantive body continues.", "bbox": [60, 500, 540, 520], "size": 10, "block": 0},
        {"text": "(1) OJ L 1, p. 2.", "bbox": [60, 750, 540, 760], "size": 8, "block": 1},
        {"text": f"({label})", "bbox": [100, 500, 107, 510], "size": 10, "number_size": size, "block": 2},
    ]
    assert (2 in _footnote_lines(lines, 10, 800)) == expected_removed


@pytest.mark.parametrize("variation", ["body_font", "not_at_bottom", "no_citation", "no_number", "title_dominates"])
def test_footnote_guards_preserve_ordinary_preamble_text(variation):
    lines = [
        {"text": "The preceding substantive text is retained.", "bbox": [60, 600, 540, 620], "size": 10, "block": 0},
        {"text": "(1) Directive 1/2020 (OJ L 1, p. 2).", "bbox": [60, 750, 540, 760], "size": 8, "block": 1},
    ]
    body_size = 10
    if variation == "body_font":
        lines[1]["size"] = 10
    elif variation == "not_at_bottom":
        lines[1]["bbox"] = [60, 640, 540, 650]
    elif variation == "no_citation":
        lines[1]["text"] = "(1) A substantive numbered reason without a citation."
    elif variation == "no_number":
        lines[1]["text"] = "A substantive reason citing OJ L 1, p. 2."
    else:
        body_size = 20
        lines[1]["size"] = 10
    assert _footnote_lines(lines, body_size, 800) == set()


def test_layout_cleanup_does_not_apply_to_operative_or_annex_pages(fake_pdf):
    first = PAGES["31982D0809-spa-p1"]
    subsequent = PAGES["32016R0679-spa-p1"]
    source = fake_pdf([first, subsequent])
    text, changes = prepare_pymupdf_structure(b"%PDF-fixture", source, "spa")
    assert changes == {"preamble_columns": [1]}
    assert text.split("\n\n", 1)[1] == subsequent["raw_text"]


@pytest.mark.parametrize("flags", list(itertools.product([False, True], repeat=3)))
def test_timeout_integration_keeps_raw_text_and_records_layout_provenance(fake_pdf, monkeypatch, flags):
    page = PAGES["31982D0809-spa-p1"]
    source = fake_pdf([page])
    worker = Mock()
    worker.convert.return_value = pdf._DoclingResult(failure_reason="timeout")
    monkeypatch.setattr(pdf, "_get_docling_worker", lambda: worker)
    meta = {}
    recitals, articles, annexes = flags
    units = pdf.PdfExtractor().extract(
        "31982D0809", b"%PDF-fixture", language="spa", out_metadata=meta,
        include_recitals=recitals, include_articles=articles, include_annexes=annexes,
    )
    assert Counter(u["type"] for u in units) == {
        k: v for k, v in [("recital", 2 if recitals else 0), ("article", 2 if articles else 0)] if v
    }
    assert meta.get("pdf_layout_changes", {}) == ({"preamble_columns": [1]} if units else {})
    metadata = {"content_source": "cellar_pdf_spa"}
    Pipeline._apply_pdf_provenance(metadata, meta)
    assert metadata["content_source"] == "cellar_pdf_spa__pymupdf_timeout" + (
        "__preamble_columns" if units else ""
    )
    assert pdf.extract_pdf_full_text(b"%PDF-fixture") == source


def test_docling_markdown_does_not_receive_pymupdf_geometry_repair(monkeypatch):
    def markdown(*args, out_metadata):
        out_metadata.update(pdf_backend="docling")
        return "Whereas:\n(1) The genuine reason.\nHAS ADOPTED THIS DECISION:\nArticle 1\nThe rule."

    monkeypatch.setattr(pdf, "extract_pdf_markdown", markdown)
    repair = Mock(side_effect=AssertionError("Geometry belongs only to the raw text fallback"))
    monkeypatch.setattr(pdf, "prepare_pymupdf_structure", repair)
    assert len(pdf.PdfExtractor().extract("TEST", b"%PDF-fixture")) == 2
    repair.assert_not_called()


def test_body_fallback_retains_original_text_after_layout_cleanup(fake_pdf, monkeypatch):
    source = fake_pdf([PAGES["31982D0809-spa-p1"]])
    worker = Mock()
    worker.convert.return_value = pdf._DoclingResult(failure_reason="timeout")
    monkeypatch.setattr(pdf, "_get_docling_worker", lambda: worker)
    monkeypatch.setattr(pdf, "_parse_legislative_markdown", lambda *a, **kw: [])
    meta = {}
    units = pdf.PdfExtractor().extract("TEST", b"%PDF-fixture", language="spa", out_metadata=meta)
    assert units == [{"type": "body", "number": None, "title": None, "text": source}]
    assert meta["markdown"] == source
    assert "pdf_layout_changes" not in meta


def test_quoted_legislative_text_is_not_a_column_repair_anchor(fake_pdf):
    page = copy.deepcopy(PAGES["31982D0809-spa-p1"])
    page["raw_text"] = "Replacement wording: ‘\n" + page["raw_text"] + "\n’."
    page["blocks"].insert(0, {"type": 0, "lines": [
        {"bbox": [50, 0, 530, 10], "spans": [{"text": "Replacement wording: ‘", "size": 10}]},
    ]})
    page["blocks"].append({"type": 0, "lines": [
        {"bbox": [50, 800, 55, 810], "spans": [{"text": "’.", "size": 10}]},
    ]})
    source = fake_pdf([page])
    assert prepare_pymupdf_structure(b"%PDF-fixture", source, "spa") == (source, {})


@pytest.mark.parametrize("reference", ["Article 263 TFEU.", "Article 267 TFEU as interpreted by the Court."])
def test_preamble_cross_references_cannot_end_recitals(reference):
    text = f"Whereas:\n(143) A remedy is available under\n{reference}\n(144) The next reason.\nHAS ADOPTED THIS REGULATION:\nArticle 1\nThe rule."
    units = parse(text)
    assert [(u["type"], u["number"]) for u in units] == [("recital", "143"), ("recital", "144"), ("article", "1")]
    assert reference in units[0]["text"]


@pytest.mark.parametrize("reference,number", [("Article 51;", "51"), ("Article 2;", "2"), ("Article 2.", "2"), ("Article 39 by providing resources.", "39"), ("Article 16 concerning guidelines.", "16"), ("Article 5 thereof, shall apply.", "5")])
def test_wrapped_references_remain_in_their_enclosing_article(reference, number):
    text = f"HAS ADOPTED THIS REGULATION:\nArticle 1\nThe measure is specified in\n{reference}\nArticle 2\nThe second rule."
    units = parse(text)
    assert [u["number"] for u in units] == ["1", "2"]
    assert reference in units[0]["text"]


@pytest.mark.parametrize("formula", ["", "HAS\nADOPTED THIS DECISION:\n", "Has adopted this decision:\n"])
def test_genuine_standalone_articles_survive_missing_or_wrapped_formula(formula):
    units = parse(f"Whereas the first reason;\n{formula}Article 1.\nThe first rule.\nArticle 2\nThe second rule.")
    assert [u["number"] for u in units if u["type"] == "article"] == ["1", "2"]


def test_next_article_survives_incomplete_preceding_line():
    units = parse("Article 1\nAn OCR-truncated body ends in\nArticle 2\nThe second rule.")
    assert [u["number"] for u in units] == ["1", "2"]


@pytest.mark.parametrize("prose", ["the Union whereby they have adopted and are applying", "have adopted and are applying"])
def test_ordinary_adoption_prose_cannot_end_recitals(prose):
    text = f"Whereas:\n(44) The reason concerns\n{prose}\nUnion law.\n(45) The next complete reason.\nHAVE ADOPTED THIS REGULATION:\nArticle 1\nThe rule."
    units = parse(text)
    assert [(u["type"], u["number"]) for u in units] == [("recital", "44"), ("recital", "45"), ("article", "1")]
    assert prose in units[0]["text"]


@pytest.mark.parametrize("record", FIRST_PAGES, ids=lambda p: p["language"])
def test_native_recital_gaps_and_restarts_are_preserved(record):
    markers = {
        "eng": "Whereas:", "fra": "considérant ce qui suit:", "deu": "in Erwägung nachstehender Gründe:",
        "ita": "considerando quanto segue:", "nld": "Overwegende hetgeen volgt:", "spa": "Considerando lo siguiente:",
    }
    units = parse(markers[record["language"]] + "\n(7) First complete reason.\n(9) Second complete reason.\n(7) Restarted complete reason.", language=record["language"])
    assert [u["number"] for u in units] == ["7", "9", "7"]


def test_wrapped_unnumbered_whereas_does_not_discard_following_recitals():
    units = parse("Whereas the first complete reason;\nWhereas\nall decisions require review;\nWhereas the third complete reason;\nHAS ADOPTED THIS DECISION:\nArticle 1\nThe rule.")
    recitals = [u for u in units if u["type"] == "recital"]
    assert [u["number"] for u in recitals] == ["1", "2", "3"]
    assert recitals[1]["text"] == "Whereas all decisions require review;"


def test_spaced_numbered_whereas_keeps_its_explicit_identifier_and_words():
    units = parse("( 4 )\nWhereas the fourth reason;\n(1 )\nWhereas\nthe first reason;\n( 2)\nWhereas the second reason;\nHAVE ADOPTED THIS DIRECTIVE:\nArticle 1\nThe rule.")
    recitals = [u for u in units if u["type"] == "recital"]
    assert [u["number"] for u in recitals] == ["4", "1", "2"]
    assert recitals[1]["text"] == "(1) Whereas the first reason;"


def test_spaced_number_without_whereas_is_not_promoted_to_a_recital():
    units = parse("Having regard to the Treaty;\n( 1 )\nA citation without a recital opener.\nHAS ADOPTED THIS DECISION:\nArticle 1\nThe rule.")
    assert [(u["type"], u["number"]) for u in units] == [("article", "1")]


def test_isolated_ocr_letter_does_not_hide_final_article_after_quoted_amendment():
    units = parse("HAS ADOPTED THIS REGULATION:\nArticle 1\nThe following Article 2a is inserted:\n'Article 2a\nThe quoted amendment applies.'\nArticle 2\nl\nThis Regulation shall enter into force tomorrow.")
    assert [u["number"] for u in units] == ["1", "2"]
    assert "Article 2a" in units[0]["text"]
    assert units[1]["text"] == "l This Regulation shall enter into force tomorrow."


def test_isolated_letter_before_reference_continuation_does_not_create_article():
    units = parse("Article 1\nThe rule applies under\nArticle 2\nl\nof the preceding regulation.")
    assert [u["number"] for u in units] == ["1"]
    assert "Article 2" in units[0]["text"]


def test_correlation_table_annex_references_stay_inside_the_table():
    text = "Article 1\nThe rule.\nDone at Brussels, 1 January 2020.\nANNEX I\nThe first attachment.\nANNEX III\nCORRELATION TABLE\nArticle 76\nArticle 81\nAnnex I\nArticle 76(1), point (a)\nAnnex II\nAnnex I and Annex II\nAnnex VI\nAnnex II\n—\nAnnex III\nEN"
    units = parse(text)
    annexes = [u for u in units if u["type"] == "annex"]
    assert [u["number"] for u in annexes] == ["I", "III"]
    assert annexes[-1]["text"] == " ".join(text.split("ANNEX III\n", 1)[1].splitlines())


@pytest.mark.parametrize("heading", ["ANNEX III", "## Annex III", "Annex III"])
def test_genuine_annex_after_a_correlation_table_still_starts_a_unit(heading):
    units = parse(f"Article 1\nThe rule.\nDone at Brussels, 1 January 2020.\nANNEX II\nCorrelation table\nArticle 1\nArticle 2\n{heading}\nSupplementary technical requirements apply.")
    assert [u["number"] for u in units if u["type"] == "annex"] == ["II", "III"]


def test_annex_reference_guard_requires_an_explicit_correlation_table():
    units = parse("Article 1\nThe rule.\nDone at Brussels, 1 January 2020.\nANNEX I\nThe first attachment.\nAnnex II\nArticle 1\nAn attached legal instrument.")
    assert [u["number"] for u in units if u["type"] == "annex"] == ["I", "II"]


def test_attached_article_after_correlation_table_is_not_a_table_row():
    units = parse("Article 1\nThe rule.\nDone at Brussels, 1 January 2020.\nANNEX II\nCorrelation table\nArticle 1\nArticle 2\nAnnex III\nArticle 1\nAn attached legal instrument starts here.")
    assert [u["number"] for u in units if u["type"] == "annex"] == ["II", "III"]


def test_final_correlation_cells_before_page_footer_do_not_split_annex():
    units = parse("Article 1\nThe rule.\nDone at Brussels, 1 January 2020.\nANNEX IV\nCorrelation table\nArticle 1\nArticle 2\nAnnex I\nAnnex I\nAnnex II\nAnnex II\nEN")
    annexes = [u for u in units if u["type"] == "annex"]
    assert [u["number"] for u in annexes] == ["IV"]
    assert annexes[0]["text"].endswith("Annex I Annex I Annex II Annex II EN")


def test_next_outer_annex_before_footer_is_retained():
    units = parse("Article 1\nThe rule.\nDone at Brussels, 1 January 2020.\nANNEX II\nCorrelation table\nArticle 1\nArticle 2\nAnnex III\nArticle 1\nEN\nSubstantive content on the next page.")
    assert [u["number"] for u in units if u["type"] == "annex"] == ["II", "III"]


@pytest.mark.parametrize("formula", ["HAS ADOPTED THIS\nREGULATION:", "HAS ADOPTED THIS FRAMEWORK DECISION:", "HAS ADOPTED THE FOLLOWING REGULATION:", "Has adopted this guideline:"])
def test_wrapped_and_alternative_act_formulae_do_not_leak_into_recitals(formula):
    units = parse(f"Whereas:\n(1) The substantive reason.\n{formula}\nArticle 1\nThe rule.")
    assert units[0]["text"] == "(1) The substantive reason."
    assert [u["number"] for u in units if u["type"] == "article"] == ["1"]


@pytest.mark.parametrize("next_text", ["Whereas the complete reason is stated here.", "The complete reason is stated here."])
def test_small_recital_label_is_not_mistaken_for_a_footnote_anchor(next_text):
    lines = [
        {"text": "The preceding substantive body continues.", "bbox": [60, 480, 540, 490], "size": 10, "block": 0},
        {"text": "(1)", "bbox": [60, 500, 70, 510], "size": 6, "number_size": 6, "block": 1},
        {"text": next_text, "bbox": [90, 500, 540, 520], "size": 10, "block": 1},
        {"text": "(1) OJ L 1, p. 2.", "bbox": [60, 750, 540, 760], "size": 8, "block": 2},
    ]
    assert _footnote_lines(lines, 10, 800, re.compile(r"^Whereas\b")) == {3}


def test_annex_after_correlation_table_survives_page_header_and_numbering_gap():
    units = parse("Article 1\nThe rule.\nDone at Brussels, 1 January 2020.\nANNEX II\nCorrelation table\nArticle 1\nArticle 2\nAnnex IV\nEN\nL 351/32\nOfficial Journal of the European Union\n20.12.2012\nArticle 1\nSubstantive content on the next page.")
    assert [u["number"] for u in units if u["type"] == "annex"] == ["II", "IV"]
