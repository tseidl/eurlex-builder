"""Passage-membership checks for corroborated Docling heading recovery."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from eurlex_builder.extractors import pdf
from eurlex_builder.extractors.pdf_headings import _same_body, repair_docling_heading
from eurlex_builder.extractors.pdf_languages import LEGISLATIVE_MARKERS
from eurlex_builder.pipeline import Pipeline

RECORDS = json.loads((Path(__file__).parent / "fixtures/pdf/gdpr-displaced-headings.json").read_text())
FORMULAS = {
    "fra": "ONT ADOPTÉ LE PRÉSENT RÈGLEMENT:",
    "deu": "HABEN FOLGENDE VERORDNUNG ERLASSEN:",
    "ita": "HANNO ADOTTATO IL PRESENTE REGOLAMENTO:",
    "nld": "HEBBEN DE VOLGENDE VERORDENING VASTGESTELD:",
}
OPENINGS = {
    "fra": ("Le président a pour missions", "L'accès aux documents"),
    "deu": ("Der Vorsitz hat folgende Aufgaben", "Der Zugang zu Dokumenten"),
    "ita": ("Il presidente ha il compito di", "L'accesso ai documenti"),
    "nld": ("De voorzitter heeft de volgende taken", "Op de toegang tot documenten"),
}


def parse(markdown: str, language: str = "eng", granularity: str = "article") -> list[dict]:
    """Extract all article structures for the membership assertions."""
    return pdf._parse_legislative_markdown(
        markdown, language=language, include_recitals=True, include_articles=True,
        include_annexes=True, article_granularity=granularity,
    )


def official_case(record: dict) -> tuple[str, str]:
    """Prefix exact public excerpts with empty synthetic articles 1 through 72."""
    lang = record["language"]
    word = LEGISLATIVE_MARKERS[lang].article_word
    prefix = FORMULAS[lang] + "\n\n" + "\n\n".join(f"{word} {n}" for n in range(1, 73)) + "\n\n"
    return prefix + record["docling_excerpt"], prefix + record["text_layer_excerpt"]


def synthetic_case() -> tuple[str, str, str]:
    """Displace article two's heading into article four's otherwise intact body."""
    correct = "HAS ADOPTED THIS REGULATION:\n\n" + "".join(
        f"## Article {n}\n\n## Duties of authority {n}\n\n"
        f"1. Authority {n} shall record every decision with a complete written explanation of its reasoning. "
        "Each explanation must identify the relevant evidence and state the grounds for the decision.\n"
        f"2. The final paragraph for authority {n} remains assigned to this article.\n\n"
        for n in range(1, 6)
    )
    block = "## Article 2\n\n## Duties of authority 2\n\n"
    displaced = correct.replace(block, "", 1).replace(
        "2. The final paragraph for authority 4", block + "2. The final paragraph for authority 4",
    )
    reference = re.sub(r"(?m)^## ", "", correct)
    return displaced, reference, correct


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: r["language"])
@pytest.mark.parametrize("granularity", ["article", "paragraph", "point"])
def test_real_gdpr_headings_restore_article_membership(record, granularity):
    original, reference = official_case(record)
    repaired, status = repair_docling_heading(original, reference, record["language"])
    assert status == {"status": "repaired", "number": "74"}
    assert Counter(repaired.split()) == Counter(original.split())
    units = parse(repaired, record["language"], granularity)
    assert list(dict.fromkeys(u["number"] for u in units)) == list(map(str, range(1, 78)))
    bodies = {n: " ".join(u["text"] for u in units if u["number"] == n) for n in ["73", "74", "76"]}
    bodies = {n: re.sub(r"\s+", " ", text) for n, text in bodies.items()}
    duties, access = OPENINGS[record["language"]]
    assert duties in bodies["74"] and duties not in bodies["73"]
    assert access in bodies["76"] and access not in bodies["74"]


def test_heading_move_preserves_every_other_character():
    original, reference, correct = synthetic_case()
    repaired, status = repair_docling_heading(original, reference, "eng")
    assert repaired == correct
    assert status == {"status": "repaired", "number": "2"}


@pytest.mark.parametrize("variation,reason", [
    ("wrong_title", "title_mismatch"),
    ("missing_title", "title_mismatch"),
    ("unmarked_heading", "unmarked_heading"),
    ("missing_reference", "incomplete_or_conflicting_reference"),
    ("duplicate_reference", "incomplete_or_conflicting_reference"),
    ("two_displaced_headings", "not_one_delayed_heading"),
    ("duplicate_anchor", "ambiguous_body_anchor"),
    ("tail_travelled_with_heading", "article_body_mismatch"),
    ("previous_article_tail", "article_body_mismatch"),
    ("intervening_article_body", "article_body_mismatch"),
    ("vacated_article_tail", "article_body_mismatch"),
    ("short_anchor", "short_body_anchor"),
    ("changed_paragraph_number", "ambiguous_body_anchor"),
])
def test_ambiguous_repairs_leave_markdown_byte_identical(variation, reason):
    original, reference, correct = synthetic_case()
    if variation == "wrong_title":
        original = original.replace("## Duties of authority 2", "## A different title")
    elif variation == "missing_title":
        original = original.replace("## Duties of authority 2\n", "")
    elif variation == "unmarked_heading":
        original = original.replace("## Article 2", "Article 2")
    elif variation == "missing_reference":
        reference = reference.replace("Article 2\n", "")
    elif variation == "duplicate_reference":
        reference = reference.replace("Article 3\n", "Article 2\n")
    elif variation == "two_displaced_headings":
        original = original.replace("## Article 3", "## Article TEMP").replace("## Article 4", "## Article 3").replace("## Article TEMP", "## Article 4")
    elif variation == "duplicate_anchor":
        body = "1. Authority 2" + correct.split("1. Authority 2", 1)[1].split("## Article 3", 1)[0]
        original = original.replace("## Article 1", body + "## Article 1")
    elif variation == "tail_travelled_with_heading":
        tail = "2. The final paragraph for authority 2 remains assigned to this article.\n"
        original = original.replace(tail, "").replace("## Duties of authority 2\n\n", "## Duties of authority 2\n\n" + tail)
    elif variation == "previous_article_tail":
        original = original.replace("paragraph for authority 1", "paragraph for a different authority")
    elif variation == "intervening_article_body":
        original = original.replace("paragraph for authority 3", "paragraph for a different authority")
    elif variation == "vacated_article_tail":
        original = original.replace("paragraph for authority 4", "paragraph for a different authority")
    elif variation == "short_anchor":
        reference = reference[:reference.index("1. Authority 2")] + "Short body.\n\n" + reference[reference.index("Article 3"):]
    elif variation == "changed_paragraph_number":
        original = original.replace("1. Authority 2", "3. Authority 2")
    repaired, status = repair_docling_heading(original, reference, "eng")
    assert repaired == original
    assert status == {"status": "declined", "reason": reason}


def test_quoted_body_does_not_authorize_heading_relocation():
    original, reference, _ = synthetic_case()
    original = original.replace("1. Authority 2", ": '1. Authority 2").replace(
        "## Article 3", "';\n## Article 3",
    )
    repaired, status = repair_docling_heading(original, reference, "eng")
    assert repaired == original
    assert status["status"] == "declined"


def test_correct_or_unsupported_inputs_remain_unchanged():
    original, reference, correct = synthetic_case()
    assert repair_docling_heading(correct, reference, "eng") == (correct, {})
    assert repair_docling_heading(original, reference, "ell") == (original, {})
    assert repair_docling_heading("", "", "eng") == ("", {})


def test_language_labels_are_not_ignored_without_reference_header():
    assert not _same_body("Substantive passage.\nFR\n", "Substantive passage.\n", "fra")
    reference = "Substantive passage.\n4.5.2016\nL 119/78\nJournal officiel de l'Union européenne\nFR\n"
    assert _same_body("Substantive passage.\nFR\n", reference, "fra")
    assert not _same_body("Substantive passage.\nFR\nFR\n", reference, "fra")


@pytest.mark.parametrize("with_metadata", [False, True])
def test_extractor_uses_same_pdf_evidence_and_records_provenance(monkeypatch, with_metadata):
    original, reference, correct = synthetic_case()

    def extract(celex_id, raw_content, *, out_metadata):
        out_metadata.update(pdf_backend="docling", pdf_text_layer=reference, markdown=original)
        return original

    monkeypatch.setattr(pdf, "extract_pdf_markdown", extract)
    metadata = {} if with_metadata else None
    units = pdf.PdfExtractor().extract("32000R0001", b"%PDF", out_metadata=metadata)
    assert [u["number"] for u in units] == list(map(str, range(1, 6)))
    assert "Authority 2 shall" in units[1]["text"] and "Authority 2 shall" not in units[0]["text"]
    if metadata is not None:
        assert metadata["markdown"] == correct
        assert metadata["pdf_text_layer"] == reference
        assert metadata["pdf_heading_repair"] == {"status": "repaired", "number": "2"}
        work = {"content_source": "cellar_pdf_eng"}
        Pipeline._apply_pdf_provenance(work, metadata)
        assert work["content_source"] == "cellar_pdf_eng__pymupdf_headings"
