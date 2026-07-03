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


def test_export_writes_all_tables_even_when_empty(store, tmp_path):
    store.save_work({"celex_id": "X1"})
    store.export(str(tmp_path), ["parquet"])
    for table in ("works", "text_units", "relations", "eurovoc"):
        assert (Path(tmp_path) / f"{table}.parquet").exists()
