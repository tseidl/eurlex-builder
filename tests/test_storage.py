"""Regression tests for the DuckDB store and Parquet export (2026-07 review).

Uses the shared store fixture from conftest.
"""

from __future__ import annotations

from pathlib import Path


def test_save_text_units_round_trip_preserves_translated_and_order(store):
    store.save_work({"celex_id": "X1"})
    store.save_text_units("X1", [
        {"type": "article", "number": "1", "text": "one", "text_translated": "pre"},
        {"type": "article", "number": "2", "text": "two"},
    ])
    rows = store.conn.execute(
        "SELECT number, text, text_translated FROM text_units ORDER BY id"
    ).fetchall()
    assert rows == [("1", "one", "pre"), ("2", "two", None)]


def test_text_units_get_deterministic_order_and_keys(store):
    store.save_work({"celex_id": "X1"})
    units = [
        {"type": "article", "number": "1", "paragraph_num": "1", "text": "one"},
        {"type": "article", "number": "1", "paragraph_num": "1", "text": "duplicate"},
        {
            "type": "article", "number": "1", "paragraph_num": "1",
            "subparagraph_num": "2", "point_letter": "a", "text": "point",
        },
    ]
    store.save_text_units("X1", units)
    first = store.conn.execute(
        "SELECT unit_order, unit_key, subparagraph_num FROM text_units ORDER BY unit_order"
    ).fetchall()
    store.save_text_units("X1", units)
    second = store.conn.execute(
        "SELECT unit_order, unit_key, subparagraph_num FROM text_units ORDER BY unit_order"
    ).fetchall()

    assert first == second
    assert [row[0] for row in first] == [1, 2, 3]
    assert first[1][1].endswith(":2")
    assert first[2][2] == "2"


def test_save_text_units_with_empty_list_clears_previous_rows(store):
    store.save_work({"celex_id": "X1"})
    store.save_text_units("X1", [{"type": "article", "number": "1", "text": "old"}])
    store.save_text_units("X1", [])
    count = store.conn.execute("SELECT count(*) FROM text_units").fetchone()[0]
    assert count == 0


def test_save_relations_deduplicates_within_batch_and_against_other_docs(store):
    store.save_work({"celex_id": "X1"})
    store.save_work({"celex_id": "X2"})
    store.save_relations(
        [{"source_celex": "X1", "target_celex": "A", "relation_type": "cites"}],
        celex_id="X1",
    )
    # Consolidated-text pattern: X2 merges its own relations with X1's.
    # X1's row already exists and must not be duplicated (nor deleted).
    store.save_relations([
        {"source_celex": "X2", "target_celex": "B", "relation_type": "cites"},
        {"source_celex": "X2", "target_celex": "B", "relation_type": "cites"},
        {"source_celex": "X1", "target_celex": "A", "relation_type": "cites"},
    ], celex_id="X2")
    rows = store.conn.execute(
        "SELECT source_celex, target_celex, relation_type FROM relations ORDER BY id"
    ).fetchall()
    assert rows == [("X1", "A", "cites"), ("X2", "B", "cites")]


def test_save_work_preserves_language_none_for_missing_content(store):
    store.save_work({"celex_id": "X1", "title": "T"})
    lang = store.conn.execute(
        "SELECT language FROM works WHERE celex_id = 'X1'"
    ).fetchone()[0]
    assert lang is None


def test_save_work_preserves_existing_content_after_missing_refetch(store):
    store.save_work({
        "celex_id": "X1",
        "language": "fra",
        "full_text": "translated",
        "full_text_original": "original",
        "content_source": "cellar_pdf_fra__translated",
    })
    store.save_work(
        {"celex_id": "X1", "title": "updated"},
        preserve_existing_content=True,
    )
    row = store.conn.execute(
        "SELECT title, language, full_text, full_text_original, content_source "
        "FROM works WHERE celex_id = 'X1'"
    ).fetchone()
    assert row == (
        "updated", "fra", "translated", "original",
        "cellar_pdf_fra__translated",
    )


def test_successful_empty_refresh_clears_relations_and_eurovoc(store):
    store.save_work({"celex_id": "X1"})
    store.save_relations([
        {"source_celex": "X1", "target_celex": "Y", "relation_type": "cites"},
    ], celex_id="X1")
    store.save_eurovoc("X1", [{"eurovoc_uri": "U", "eurovoc_label": "label"}])

    store.save_relations([], celex_id="X1")
    store.save_eurovoc("X1", [])

    assert store.conn.execute("SELECT count(*) FROM relations").fetchone()[0] == 0
    assert store.conn.execute("SELECT count(*) FROM eurovoc").fetchone()[0] == 0


def test_relation_refresh_preserves_enrichment_owned_types(store):
    store.save_work({"celex_id": "X1"})
    store.conn.execute(
        "INSERT INTO relations VALUES "
        "(nextval('relations_id_seq'), 'X1', 'Y', 'repeals'), "
        "(nextval('relations_id_seq'), 'X1', 'Z', 'cites')"
    )

    store.save_relations([], celex_id="X1")

    rows = store.conn.execute(
        "SELECT target_celex, relation_type FROM relations ORDER BY relation_type"
    ).fetchall()
    assert rows == [("Y", "repeals")]


def test_relations_skip_sources_absent_from_works(store):
    store.save_work({"celex_id": "X1"})
    store.save_relations([
        {"source_celex": "MISSING", "target_celex": "Y", "relation_type": "cites"},
        {"source_celex": "X1", "target_celex": "Z", "relation_type": "cites"},
    ], celex_id="X1")
    rows = store.conn.execute(
        "SELECT source_celex, target_celex FROM relations"
    ).fetchall()
    assert rows == [("X1", "Z")]


def test_run_manifest_hash_is_deterministic(store):
    config_a = {"data": {"celex_ids": ["X1"], "mode": "fixed"}}
    config_b = {"data": {"mode": "fixed", "celex_ids": ["X1"]}}
    run_a = store.start_run(config_a)
    run_b = store.start_run(config_b)
    store.finish_run(run_a, "complete")
    store.finish_run(run_b, "complete")

    rows = store.conn.execute(
        "SELECT status, config_sha256, completed_at IS NOT NULL "
        "FROM dataset_runs ORDER BY started_at, run_id"
    ).fetchall()
    assert len({row[1] for row in rows}) == 1
    assert all(row[0] == "complete" and row[2] for row in rows)

    source_revision, source_dirty = store.conn.execute(
        "SELECT source_revision, source_dirty FROM dataset_runs LIMIT 1"
    ).fetchone()
    assert source_revision is None or len(source_revision) == 40
    assert source_dirty is None or isinstance(source_dirty, bool)


def test_start_run_marks_stale_running_manifest_interrupted(store):
    previous = store.start_run({"run": "previous"})
    current = store.start_run({"run": "current"})

    rows = dict(store.conn.execute(
        "SELECT run_id, status FROM dataset_runs WHERE run_id IN (?, ?)",
        [previous, current],
    ).fetchall())
    assert rows == {previous: "interrupted", current: "running"}


def test_store_reopen_migrates_run_source_columns(tmp_path):
    from eurlex_builder.storage.duckdb import DuckDBStore

    db_path = str(tmp_path / "legacy-runs.duckdb")
    store = DuckDBStore(db_path)
    store.conn.execute("ALTER TABLE dataset_runs DROP COLUMN source_revision")
    store.conn.execute("ALTER TABLE dataset_runs DROP COLUMN source_dirty")
    store.close()

    reopened = DuckDBStore(db_path)
    try:
        columns = {
            row[0]
            for row in reopened.conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'dataset_runs'"
            ).fetchall()
        }
        assert {"source_revision", "source_dirty"} <= columns
    finally:
        reopened.close()


def test_store_reopen_backfills_legacy_enrichment_checkpoint(tmp_path):
    from eurlex_builder.storage.duckdb import DuckDBStore

    db_path = str(tmp_path / "legacy.duckdb")
    store = DuckDBStore(db_path)
    store.save_work({"celex_id": "X1"})
    store.conn.execute(
        "UPDATE works SET enriched_at = current_timestamp WHERE celex_id = 'X1'"
    )
    store.conn.execute("DROP TABLE _enrichment_checkpoint")
    store.close()

    reopened = DuckDBStore(db_path)
    try:
        categories = {
            row[0]
            for row in reopened.conn.execute(
                "SELECT category FROM _enrichment_checkpoint WHERE celex_id = 'X1'"
            ).fetchall()
        }
        assert categories == {"metadata", "relations", "eurovoc"}
    finally:
        reopened.close()


def test_export_writes_all_tables_even_when_empty(store, tmp_path):
    store.save_work({"celex_id": "X1"})
    store.export(str(tmp_path), ["parquet"])
    for table in ("works", "text_units", "relations", "eurovoc"):
        assert (Path(tmp_path) / f"{table}.parquet").exists()
