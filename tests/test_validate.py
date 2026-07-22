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


def test_validate_reports_degenerate_translation(store):
    store.save_work({
        "celex_id": "X1",
        "full_text": "Texte source.",
        "language": "fra",
    })
    store.save_text_units("X1", [{
        "type": "article",
        "number": "1",
        "text": "Texte source.",
        "text_translated": "No, " * 12,
    }])
    store.mark_processed("X1")
    run_id = store.start_run({"data": {"mode": "fixed"}})
    store.finish_run(run_id, "complete")

    codes = {issue["code"] for issue in _validate_connection(store.conn)}

    assert "degenerate_text_unit_translation" in codes


def test_validate_reports_marker_only_recital(store):
    store.save_work({"celex_id": "X1", "full_text": "Summary text."})
    store.save_text_units("X1", [{
        "type": "recital",
        "number": "1",
        "text": "(1)",
    }])
    store.mark_processed("X1")

    codes = {issue["code"] for issue in _validate_connection(store.conn)}

    assert "marker_only_recital" in codes


def test_validate_flags_unextractable_type_with_content_and_no_units(store):
    store.save_work({
        "celex_id": "62018CJ0311",
        "document_type": "unknown",
        "full_text": "Judgment of the Court (Grand Chamber).",
    })
    store.mark_processed("62018CJ0311")

    issues = _validate_connection(store.conn)

    by_code = {issue["code"]: issue for issue in issues}
    assert by_code["content_without_text_units"]["severity"] == "error"
    assert by_code["unextractable_document_type"]["severity"] == "warning"


def test_validate_allows_empty_content_and_supported_types(store):
    # Unfetchable documents are kept as rows with empty content columns;
    # they must not be flagged as silent extraction failures.
    store.save_work({"celex_id": "X1", "document_type": "regulation"})
    store.save_work({
        "celex_id": "X2", "document_type": "communication", "full_text": "text",
    })
    store.save_text_units("X2", [
        {"type": "paragraph", "number": "1", "text": "text"},
    ])
    store.mark_processed("X1")
    store.mark_processed("X2")

    codes = {issue["code"] for issue in _validate_connection(store.conn)}

    assert "content_without_text_units" not in codes
    assert "unextractable_document_type" not in codes


def test_validate_reports_recorded_translation_rejection(store):
    store.save_work({"celex_id": "X1", "full_text": "Texte source."})
    store.save_text_units("X1", [{
        "type": "article", "number": "1", "text": "Texte source.",
    }])
    store.mark_processed("X1")
    store.conn.execute(
        """CREATE TABLE _translation_failures (
               scope VARCHAR, record_id VARCHAR, celex_id VARCHAR,
               source_sha256 VARCHAR, reason VARCHAR, attempted_at TIMESTAMP
           )"""
    )
    store.conn.execute(
        """INSERT INTO _translation_failures VALUES
           ('unit', '1', 'X1', 'hash', 'repetitive_output', current_timestamp)"""
    )

    issues = _validate_connection(store.conn)

    assert any(
        issue["code"] == "recorded_translation_rejection" and issue["count"] == 1
        for issue in issues
    )
