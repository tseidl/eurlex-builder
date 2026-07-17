"""Integration tests for quality-repair preparation."""

from __future__ import annotations

from scripts.prepare_quality_repair import prepare_quality_repair
from scripts.prepare_structural_repair import prepare_structural_repair
from eurlex_builder.storage.duckdb import DuckDBStore


def test_quality_repair_resets_translations_and_targeted_checkpoints(store, tmp_path):
    store.save_work({
        "celex_id": "X1",
        "language": "fra",
        "full_text": "Texte source complet.",
        "content_source": "cellar_pdf_fra",
    })
    store.save_text_units("X1", [{
        "type": "article", "number": "1", "text": "Texte source.",
        "text_translated": "No, " * 12,
    }])
    store.conn.execute(
        "UPDATE works SET full_text_original = full_text, full_text = ? WHERE celex_id = 'X1'",
        ["No, " * 12],
    )
    store.mark_processed("X1")

    store.save_work({
        "celex_id": "X2",
        "language": "fra",
        "full_text": "Texte source.",
        "content_source": "cellar_pdf_fra__translated",
    })
    store.save_text_units("X2", [{
        "type": "article", "number": "1", "text": "No, " * 12,
        "text_translated": "No, " * 12,
    }])
    store.mark_processed("X2")

    store.save_work({"celex_id": "X3", "full_text": "English text."})
    store.mark_processed("X3")
    audit_path = tmp_path / "quality.tsv"

    assert prepare_quality_repair(
        store.db_path, audit_path, reprocess_celex=["X3"],
    ) == (1, 1, 2)
    assert not audit_path.exists()
    assert prepare_quality_repair(
        store.db_path,
        audit_path,
        apply=True,
        reprocess_celex=["X3"],
    ) == (1, 1, 2)

    assert audit_path.read_text(encoding="utf-8").count("\n") == 5
    assert store.conn.execute(
        "SELECT full_text, full_text_original FROM works WHERE celex_id = 'X1'"
    ).fetchone() == ("Texte source complet.", None)
    assert store.conn.execute(
        "SELECT text_translated FROM text_units WHERE celex_id = 'X1'"
    ).fetchone() == (None,)
    assert store.is_processed("X1")
    assert not store.is_processed("X2")
    assert not store.is_processed("X3")


def test_structural_repair_queues_profile_regressions_with_audit(store, tmp_path):
    reference = DuckDBStore(tmp_path / "reference.duckdb")
    reference.save_work({"celex_id": "X1", "full_text": "Old one."})
    reference.save_text_units("X1", [
        {"type": "article", "number": "1", "text": "First."},
        {"type": "article", "number": "2", "text": "Second."},
    ])
    reference.save_work({"celex_id": "X2", "full_text": "Old two."})
    reference.save_text_units("X2", [
        {"type": "annex", "number": "I", "text": "Annex."},
    ])
    reference.close()

    store.save_work({
        "celex_id": "X1",
        "full_text": "New one.",
        "content_source": "cellar_html_eng",
    })
    store.save_text_units("X1", [
        {"type": "recital", "number": "1", "text": "(1)"},
        {"type": "article", "number": "1", "text": "First."},
    ])
    store.mark_processed("X1")
    store.save_work({
        "celex_id": "X2",
        "full_text": "New two.",
        "content_source": "cellar_pdf_eng",
    })
    store.mark_processed("X2")
    audit_path = tmp_path / "structural.tsv"

    assert prepare_structural_repair(
        store.db_path, tmp_path / "reference.duckdb", audit_path,
    ) == (2, {
        "marker_only_recital": 1,
        "article_count_regression": 1,
        "annex_count_regression": 1,
    })
    assert prepare_structural_repair(
        store.db_path,
        tmp_path / "reference.duckdb",
        audit_path,
        content_source_prefixes=("cellar_html",),
    ) == (1, {
        "marker_only_recital": 1,
        "article_count_regression": 1,
        "annex_count_regression": 0,
    })
    assert prepare_structural_repair(
        store.db_path,
        tmp_path / "reference.duckdb",
        audit_path,
        selected_reasons=frozenset({"annex_count_regression"}),
    ) == (1, {
        "marker_only_recital": 0,
        "article_count_regression": 0,
        "annex_count_regression": 1,
    })
    assert prepare_structural_repair(
        store.db_path,
        tmp_path / "reference.duckdb",
        audit_path,
        apply=True,
    )[0] == 2

    assert audit_path.read_text(encoding="utf-8").count("\n") == 4
    assert not store.is_processed("X1")
    assert not store.is_processed("X2")
