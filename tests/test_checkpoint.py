"""Tests for DuckDB checkpoint functionality. Uses the shared store fixture."""


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


def test_checkpoint_reset_ids_preserves_unselected_rows(store):
    store.mark_processed("32016R0679")
    store.mark_processed("32024R1689")

    assert store.reset_ids(["32016R0679", "MISSING"]) == 1
    assert not store.is_processed("32016R0679")
    assert store.is_processed("32024R1689")
