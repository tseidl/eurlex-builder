"""Native PDF article recovery without translating or changing the source text."""

from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from eurlex_builder.config import Config
from eurlex_builder.extractors import pdf
from eurlex_builder.pipeline import Pipeline

FIXTURES = Path(__file__).parent / "fixtures" / "pdf"
OFFICIAL_EXCERPTS = json.loads((FIXTURES / "gdpr-articles-1-2.json").read_text())


def parse(text: str, language: str = "spa", **kwargs) -> list[dict]:
    """Keep all requested structures on unless a test explicitly excludes one."""
    options = {"include_recitals": True, "include_articles": True, "include_annexes": True}
    options.update(kwargs)
    return pdf._parse_legislative_markdown(text, language=language, **options)


@pytest.fixture
def spanish_decision() -> str:
    return (FIXTURES / "31982D0809-spa.md").read_text(encoding="utf-8")


@pytest.fixture
def timeout_fallback(monkeypatch, spanish_decision):
    worker = Mock()
    worker.convert.return_value = pdf._DoclingResult(failure_reason="timeout")
    monkeypatch.setattr(pdf, "_get_docling_worker", lambda: worker)
    monkeypatch.setattr(pdf, "_pymupdf_text_from_path", lambda *args: spanish_decision)


def test_spanish_decision_keeps_agreement_articles_and_appendices_inside_annex(spanish_decision):
    assert hashlib.sha256(spanish_decision.encode()).hexdigest() == (
        "9a39892f47dc9aef1e86b9bb02d2901f94f01ff4636bb55fc85f2fdd361f9149"
    )
    units = parse(spanish_decision)
    assert [(u["type"], u["number"]) for u in units] == [
        ("article", "1"), ("article", "2"), ("annex", "I"), ("annex", "II"),
    ]
    assert "Para el 30 de abril de 1982" in units[0]["text"]
    assert "Se autoriza al Presidente del Consejo" in units[1]["text"]
    assert "Hecho en Bruselas" not in units[1]["text"]
    assert "SEXTO CONVENIO INTERNACIONAL DEL ESTAÑO" in units[3]["text"]
    assert "Artículo 61" in units[3]["text"]
    assert "ANEXO G" in units[3]["text"]
    # Preserve the complete convention, including the line that translation rejected.
    convention = spanish_decision.split("ANEXO II\n", 1)[1]
    assert units[3]["text"] == " ".join(line.strip() for line in convention.splitlines() if line.strip())


@pytest.mark.parametrize("granularity", ["article", "paragraph", "point"])
@pytest.mark.parametrize("flags", list(itertools.product([False, True], repeat=3)))
def test_native_parser_honors_requested_structures(spanish_decision, granularity, flags):
    recitals, articles, annexes = flags
    units = parse(
        spanish_decision, include_recitals=recitals, include_articles=articles,
        include_annexes=annexes, article_granularity=granularity,
    )
    assert {u["number"] for u in units if u["type"] == "article"} == (
        {"1", "2"} if articles else set()
    )
    assert [u["number"] for u in units if u["type"] == "annex"] == (
        ["I", "II"] if annexes else []
    )
    assert not any(u["type"] == "body" for u in units)


@pytest.mark.parametrize("record", OFFICIAL_EXCERPTS, ids=lambda r: r["language"])
def test_official_article_excerpts_preserve_source_words(record):
    formula, articles, signature = record["fragments"]
    units = parse("\n".join([formula, articles, signature]), record["language"])
    assert [(u["type"], u["number"]) for u in units] == [("article", "1"), ("article", "2")]
    assert all(len(u["text"]) > 100 for u in units)
    # The source body must occur verbatim apart from line joining.
    joined_source = " ".join(line.strip() for line in articles.splitlines() if line.strip())
    assert all(u["text"] in joined_source for u in units)
    assert signature.splitlines()[0].strip() not in units[-1]["text"]


@pytest.mark.parametrize(("language", "heading", "number"), [
    ("spa", "Artículo único", "sole"),
    ("fra", "Article premier", "1"),
    ("fra", "Article 1er", "1"),
    ("fra", "Article unique", "sole"),
    ("deu", "Einziger Artikel", "sole"),
    ("ita", "Articolo unico", "sole"),
    ("nld", "Enig artikel", "sole"),
    ("spa", "Artículo 2a", "2a"),
])
@pytest.mark.parametrize("markdown", [False, True])
def test_native_first_and_sole_articles(language, heading, number, markdown):
    text = f"{'## ' if markdown else ''}{heading}\nOperative text retained in its source language."
    units = parse(text, language)
    assert [(u["type"], u["number"]) for u in units] == [("article", number)]


@pytest.mark.parametrize(("language", "word"), [
    ("spa", "Artículo"), ("fra", "Article"), ("deu", "Artikel"),
    ("ita", "Articolo"), ("nld", "Artikel"),
])
def test_native_headings_split_over_lines(language, word):
    units = parse(f"## {word}\n1\nOperative text.\n{word}\n2\nMore operative text.", language)
    assert [u["number"] for u in units] == ["1", "2"]


def test_native_references_and_quoted_replacement_are_not_outer_articles():
    body = """Texto de la disposición:
«Artículo 9
Texto de sustitución.
Hecho en Bruselas, el 1 de enero de 2020.
ANEXO I
Texto de sustitución del anexo».
Artículo 7 del Reglamento anterior.
Artículo 8
del Reglamento anterior.
artículo 65
Texto que continúa una referencia.
Artículo 78.
Texto que continúa después de la referencia."""
    units = parse(f"DECIDE:\nArtículo 1\n{body}\nArtículo 2\nDisposición final.")
    assert [u["number"] for u in units] == ["1", "2"]
    assert units[0]["text"] == " ".join(body.splitlines())


def test_native_preamble_reference_is_not_an_article_when_formula_follows():
    units = parse("Artículo 5\nUn texto de referencia.\nDECIDE:\nArtículo 1\nTexto operativo.")
    assert [u["number"] for u in units] == ["1"]


def test_french_inline_body_remains_supported_without_promoting_references():
    units = parse(
        "Article premier Les données sont protégées.\n"
        "Article 9 du règlement précédent.\n"
        "Article 10 paragraphe 2.\n"
        "Article 2 Une disposition finale.\n"
        "Article 2bis Des dispositions supplémentaires.", "fra",
    )
    assert [u["number"] for u in units] == ["1", "2", "2bis"]
    assert units[0]["text"] == (
        "Les données sont protégées. Article 9 du règlement précédent. Article 10 paragraphe 2."
    )
    assert units[1]["text"] == "Une disposition finale."
    assert units[2]["text"] == "Des dispositions supplémentaires."


def test_annex_formula_does_not_suppress_enclosing_articles_without_formula():
    text = """Artículo 1
Texto operativo.
ANEXO I
HAN CONVENIDO EN LO SIGUIENTE:
Artículo 1
Texto del convenio.
ANEXO II
Segundo anexo.
Artículo 2
Otro artículo del segundo anexo."""
    units = parse(text)
    assert [(u["type"], u["number"]) for u in units] == [
        ("article", "1"), ("annex", "I"), ("annex", "II"),
    ]
    assert "HAN CONVENIDO EN LO SIGUIENTE:" in units[1]["text"]
    assert "Artículo 1" in units[1]["text"]
    assert "Artículo 2" in units[2]["text"]


def test_native_annexes_require_an_outer_sequence_and_preserve_ambiguous_headings():
    units = parse("ANEXO 1\nTexto.\nANEXO 2\nTexto.\nANEXO IV\nTexto anidado.")
    assert [u["number"] for u in units] == ["1", "2"]
    assert "ANEXO IV" in units[-1]["text"]


@pytest.mark.parametrize("language", ["eng", "ell", "unknown"])
def test_native_patterns_are_gated_by_the_known_source_language(language):
    assert parse("Artículo 1\nTexto operativo.", language) == []


def test_timeout_extracts_native_structure_and_retains_source_metadata(timeout_fallback, spanish_decision):
    metadata = {}
    units = pdf.PdfExtractor().extract(
        "31982D0809", b"%PDF-test", language="spa", out_metadata=metadata,
    )
    assert metadata["pdf_backend"] == "pymupdf"
    assert metadata["pdf_fallback_reason"] == "timeout"
    assert metadata["markdown"] == spanish_decision
    assert [(u["type"], u["number"]) for u in units] == [
        ("article", "1"), ("article", "2"), ("annex", "I"), ("annex", "II"),
    ]


def test_native_text_layer_can_complete_docling_articles_without_replacing_them(monkeypatch):
    worker = Mock()
    worker.convert.return_value = pdf._DoclingResult(markdown="Artículo 1\nTexto de Docling.")
    monkeypatch.setattr(pdf, "_get_docling_worker", lambda: worker)
    metadata = {"pdf_text_layer": "Artículo 1\nTexto distinto.\nArtículo 2\nTexto recuperado."}

    units = pdf.PdfExtractor().extract(
        "31982D0809", b"%PDF-test", language="spa", out_metadata=metadata,
    )

    assert [u["text"] for u in units] == ["Texto de Docling.", "Texto recuperado."]
    assert metadata["pdf_representation_repair"] == "pymupdf_articles"


@pytest.mark.parametrize(("language", "article", "annex", "signature"), [
    ("spa", "Artículo", "ANEXO", "Hecho en Bruselas, el 1 de enero de 2020."),
    ("fra", "Article", "ANNEXE", "Fait à Bruxelles, le 1 janvier 2020."),
    ("deu", "Artikel", "ANHANG", "Geschehen zu Brüssel am 1. Januar 2020."),
    ("ita", "Articolo", "ALLEGATO", "Fatto a Bruxelles, il 1 gennaio 2020."),
    ("nld", "Artikel", "BIJLAGE", "Gedaan te Brussel, 1 januari 2020."),
])
def test_native_signatures_and_annexes_in_docling_headings(language, article, annex, signature):
    text = (
        f"## {article} 1\nTesto.\n{signature}\nSignatory.\n"
        f"## {annex}\nI\nAttached text.\n{article} 1\nAttached article.\n"
        f"## {annex} II – Title\nMore attached text."
    )
    units = parse(text, language)
    assert [(u["type"], u["number"]) for u in units] == [
        ("article", "1"), ("annex", "I"), ("annex", "II"),
    ]
    assert units[0]["text"] == "Testo."
    assert f"{article} 1" in units[1]["text"]
    assert units[2]["title"] == "Title"
    assert units[2]["text"] == "More attached text."


def test_rejected_translation_keeps_native_units_and_full_text(
    monkeypatch, timeout_fallback, spanish_decision,
):
    import eurlex_builder.translate as translate

    monkeypatch.setattr(translate, "_get_model", lambda *args: object())
    rejected = Mock(return_value=None)
    monkeypatch.setattr(translate, "translate_text", rejected)
    source = Mock()
    source.fetch_metadata.return_value = {"celex_id": "31982D0809", "document_type": "decision"}
    source.fetch_content.return_value = (b"%PDF-test", "pdf", "spa")
    source.fetch_relations.return_value = []
    config = Config.model_validate({"data": {"mode": "fixed", "celex_ids": ["31982D0809"]}})
    pipeline = Pipeline(config, source, [pdf.PdfExtractor()], None, None)

    result = pipeline._fetch_and_extract("31982D0809", source)

    rejected.assert_called_once()
    assert result["metadata"]["full_text"] == spanish_decision
    assert result["metadata"]["language"] == "spa"
    assert result["metadata"]["content_source"] == "cellar_pdf_spa__pymupdf_timeout"
    assert [(u["type"], u["number"]) for u in result["units"]] == [
        ("article", "1"), ("article", "2"), ("annex", "I"), ("annex", "II"),
    ]
    assert all(not u.get("text_translated") for u in result["units"])


@pytest.mark.parametrize(("recitals", "articles", "annexes", "adopted"), [
    (3, 2, 2, True), (3, 1, 2, False), (3, 2, 1, False), (0, 2, 2, False),
])
def test_translation_adoption_still_requires_improvement_without_structure_loss(
    monkeypatch, timeout_fallback, spanish_decision, recitals, articles, annexes, adopted,
):
    translated = "Whereas:\n" + "\n".join(
        f"({i}) A translated recital about this decision." for i in range(1, recitals + 1)
    )
    translated += "\nHAS ADOPTED THIS DECISION:\n" + "\n".join(
        f"Article {i}\nTranslated operative provision." for i in range(1, articles + 1)
    )
    translated += "\nDone at Brussels, 31 March 1982.\n" + "\n".join(
        f"ANNEX {n}\nTranslated annex." for n in ["I", "II"][:annexes]
    )
    monkeypatch.setattr("eurlex_builder.pipeline._safe_translate_markdown", lambda *args: translated)
    source = Mock()
    source.fetch_metadata.return_value = {"celex_id": "31982D0809", "document_type": "decision"}
    source.fetch_content.return_value = (b"%PDF-test", "pdf", "spa")
    source.fetch_relations.return_value = []
    config = Config.model_validate({"data": {"mode": "fixed", "celex_ids": ["31982D0809"]}})
    pipeline = Pipeline(config, source, [pdf.PdfExtractor()], None, None)

    result = pipeline._fetch_and_extract("31982D0809", source)

    assert result["metadata"]["full_text"] == spanish_decision
    assert result["metadata"]["content_source"].endswith("__translated") is adopted
    assert {u["number"] for u in result["units"] if u["type"] == "article"} == {"1", "2"}
    assert {u["number"] for u in result["units"] if u["type"] == "annex"} == {"I", "II"}
    if adopted:
        assert all(u["text_translated"] == u["text"] for u in result["units"])
    else:
        assert "Para el 30 de abril de 1982" in result["units"][0]["text"]
        assert all(not u.get("text_translated") for u in result["units"])
