"""Tests for read-only dataset validation."""

from __future__ import annotations

from eurlex_builder.validate import _validate_connection


def test_validate_clean_database(store):
    store.save_work({"celex_id": "X1", "full_text": "text"})
    store.save_text_units("X1", [{"type": "article", "number": "1", "text": "text"}])
    store.mark_processed("X1")
    run_id = store.start_run({"data": {"mode": "fixed", "celex_ids": ["X1"]}})
    store.finish_run(run_id, "complete")

    assert _validate_connection(store.conn) == []


def test_validate_reports_missing_checkpoint_and_identity(store):
    store.save_work({"celex_id": "X1", "full_text": "text"})
    store.conn.execute(
        """INSERT INTO text_units
           (id, celex_id, type, number, text)
           VALUES (nextval('text_units_id_seq'), 'X1', 'article', '1', 'text')"""
    )
    store.start_run({})

    issues = _validate_connection(store.conn)
    codes = {issue["code"] for issue in issues}
    assert "work_without_checkpoint" in codes
    assert "missing_unit_identity" in codes


def test_validate_reports_manifest_hash_mismatch(store):
    run_id = store.start_run({"data": {"mode": "fixed"}})
    store.finish_run(run_id, "complete")
    store.conn.execute(
        "UPDATE dataset_runs SET config_json = '{}' WHERE run_id = ?", [run_id]
    )

    issues = _validate_connection(store.conn)

    assert any(issue["code"] == "manifest_hash_mismatch" for issue in issues)


def test_translated_fallback_marker_allows_original_recital_without_translation(store):
    store.save_work({
        "celex_id": "X1",
        "full_text": "text",
        "content_source": "cellar_pdf_fra__translated",
    })
    store.save_text_units("X1", [
        {"type": "recital", "number": "1", "text": "Original recital"},
        {
            "type": "article", "number": "1", "text": "Translated article",
            "text_translated": "Translated article",
        },
    ])
    store.mark_processed("X1")
    run_id = store.start_run({})
    store.finish_run(run_id, "complete")

    codes = {issue["code"] for issue in _validate_connection(store.conn)}
    assert "translated_fallback_marker_gap" not in codes

    store.conn.execute(
        "UPDATE text_units SET text_translated = NULL WHERE type = 'article'"
    )
    codes = {issue["code"] for issue in _validate_connection(store.conn)}
    assert "translated_fallback_marker_gap" in codes
