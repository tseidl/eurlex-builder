"""Tests for DuckDB checkpoint functionality."""

import pytest
from eurlex_builder.storage.duckdb import DuckDBStore


@pytest.fixture
def store(tmp_path):
    db = DuckDBStore(str(tmp_path / "test.duckdb"))
    yield db
    db.close()


def test_checkpoint_round_trip(store):
    assert not store.is_processed("32016R0679")
    store.mark_processed("32016R0679")
    assert store.is_processed("32016R0679")


def test_checkpoint_failed(store):
    store.mark_failed("32016R0679", "Connection timeout")
    summary = store.get_summary()
    assert summary["failed"] == 1
    assert "32016R0679" in summary["failed_details"]


def test_checkpoint_summary(store):
    store.mark_processed("32016R0679")
    store.mark_processed("32024R1689")
    store.mark_failed("32000L0031", "Parse error")
    summary = store.get_summary()
    assert summary["processed"] == 2
    assert summary["failed"] == 1


def test_checkpoint_reset(store):
    store.mark_processed("32016R0679")
    store.reset()
    assert not store.is_processed("32016R0679")
