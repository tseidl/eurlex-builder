"""Tests for category-aware enrichment persistence."""

from __future__ import annotations

import pytest

from eurlex_builder.errors import TransientSourceError
from eurlex_builder.enrich import (
    _build_enrich_query,
    _enrich_database_inner,
    _enrich_one,
    _save_enrichment,
)


def _result(celex_id: str, **overrides) -> dict:
    result = {
        "celex_id": celex_id,
        "metadata": {},
        "relations": [],
        "eurovoc": [],
    }
    result.update(overrides)
    return result


def test_enrichment_checkpoints_are_category_specific(store):
    store.save_work({"celex_id": "X1"})
    _save_enrichment(store.conn, _result("X1"), {"eurovoc"})

    categories = {
        row[0]
        for row in store.conn.execute(
            "SELECT category FROM _enrichment_checkpoint WHERE celex_id = 'X1'"
        ).fetchall()
    }
    assert categories == {"eurovoc"}


def test_force_style_refresh_clears_stale_metadata_and_relations(store):
    store.save_work({"celex_id": "X1"})
    store.conn.execute("UPDATE works SET eli = 'stale' WHERE celex_id = 'X1'")
    store.conn.execute(
        "INSERT INTO relations VALUES (nextval('relations_id_seq'), 'X1', 'Y', 'repeals')"
    )

    _save_enrichment(store.conn, _result("X1"), {"metadata", "relations"})

    eli = store.conn.execute(
        "SELECT eli FROM works WHERE celex_id = 'X1'"
    ).fetchone()[0]
    relation_count = store.conn.execute(
        "SELECT count(*) FROM relations WHERE source_celex = 'X1' AND relation_type = 'repeals'"
    ).fetchone()[0]
    assert eli is None
    assert relation_count == 0


def test_incomplete_success_response_cannot_clear_existing_data(monkeypatch, store):
    import eurlex_builder.sources.cellar as cellar

    store.save_work({"celex_id": "X1"})
    store.conn.execute("UPDATE works SET eli = 'keep' WHERE celex_id = 'X1'")
    monkeypatch.setattr(
        cellar,
        "_sparql_query",
        lambda *args, **kwargs: {
            "results": {
                "bindings": [{
                    "data_type": {"value": "author"},
                    "value": {"value": "http://example.test/COM"},
                }],
            },
        },
    )

    with pytest.raises(TransientSourceError, match="missing completion"):
        _enrich_one("X1", "SELECT", {"metadata"})

    assert store.conn.execute(
        "SELECT eli FROM works WHERE celex_id = 'X1'"
    ).fetchone()[0] == "keep"


def test_completion_sentinel_makes_empty_result_authoritative(monkeypatch):
    import eurlex_builder.sources.cellar as cellar

    monkeypatch.setattr(
        cellar,
        "_sparql_query",
        lambda *args, **kwargs: {
            "results": {
                "bindings": [{
                    "data_type": {"value": "zz_complete_eurovoc"},
                    "value": {"value": "1"},
                }],
            },
        },
    )

    result = _enrich_one("X1", "SELECT", {"eurovoc"})

    assert result["eurovoc"] == []


def test_enrichment_query_includes_each_category_completion_sentinel():
    query = _build_enrich_query("32016R0679", {"metadata", "relations", "eurovoc"})
    assert "zz_complete_metadata" in query
    assert "zz_complete_relations" in query
    assert "zz_complete_eurovoc" in query
    assert "ORDER BY ?data_type ?value" in query


def test_legacy_enriched_at_backfills_all_categories_without_refetch(
    store, monkeypatch,
):
    import eurlex_builder.enrich as enrich

    store.save_work({"celex_id": "X1"})
    store.conn.execute(
        "UPDATE works SET enriched_at = current_timestamp WHERE celex_id = 'X1'"
    )
    store.conn.execute("DROP TABLE _enrichment_checkpoint")

    def unexpected_fetch(*args, **kwargs):
        raise AssertionError("legacy enriched row should not be fetched")

    monkeypatch.setattr(enrich, "_enrich_one", unexpected_fetch)
    _enrich_database_inner(
        store.conn,
        categories=frozenset({"metadata", "relations", "eurovoc"}),
        parallel=False,
        max_workers=1,
        force=False,
    )

    categories = {
        row[0]
        for row in store.conn.execute(
            "SELECT category FROM _enrichment_checkpoint WHERE celex_id = 'X1'"
        ).fetchall()
    }
    assert categories == {"metadata", "relations", "eurovoc"}


def test_legacy_backfill_does_not_expand_new_selective_checkpoint(store):
    from eurlex_builder.constants import ensure_enrichment_checkpoint

    store.save_work({"celex_id": "LEGACY"})
    store.save_work({"celex_id": "SELECTIVE"})
    store.conn.execute(
        "UPDATE works SET enriched_at = current_timestamp WHERE celex_id = 'LEGACY'"
    )
    _save_enrichment(store.conn, _result("SELECTIVE"), {"eurovoc"})

    ensure_enrichment_checkpoint(store.conn)

    rows = store.conn.execute(
        "SELECT celex_id, category FROM _enrichment_checkpoint ORDER BY ALL"
    ).fetchall()
    assert rows == [
        ("LEGACY", "eurovoc"),
        ("LEGACY", "metadata"),
        ("LEGACY", "relations"),
        ("SELECTIVE", "eurovoc"),
    ]
