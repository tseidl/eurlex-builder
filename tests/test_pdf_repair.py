"""Integration tests for conservative PDF checkpoint repair."""

from __future__ import annotations

from scripts.prepare_pdf_repair import PENDING_PREFIX, prepare_pdf_repair
from eurlex_builder.validate import _validate_connection


def test_pdf_repair_marks_resets_and_audits_only_processed_pdf_rows(store, tmp_path):
    store.save_work({
        "celex_id": "PDF1",
        "content_source": "cellar_pdf_eng",
        "full_text": "suspect PDF text",
    })
    store.save_text_units("PDF1", [{"type": "body", "text": "suspect"}])
    store.mark_processed("PDF1")
    store.save_work({
        "celex_id": "HTML1",
        "content_source": "cellar_html_eng",
        "full_text": "trusted HTML text",
    })
    store.save_text_units("HTML1", [{"type": "article", "text": "trusted"}])
    store.mark_processed("HTML1")
    audit_path = tmp_path / "repair.tsv"

    assert prepare_pdf_repair(store.db_path, audit_path) == 1
    assert not audit_path.exists()
    assert prepare_pdf_repair(
        store.db_path, audit_path, apply=True,
    ) == 1

    assert audit_path.read_text(encoding="utf-8").splitlines()[1].startswith(
        "PDF1\tcellar_pdf_eng\t"
    )
    assert not store.is_processed("PDF1")
    assert store.is_processed("HTML1")
    source = store.conn.execute(
        "SELECT content_source FROM works WHERE celex_id = 'PDF1'"
    ).fetchone()[0]
    assert source == f"{PENDING_PREFIX}cellar_pdf_eng"


def test_refetch_without_content_remains_visible_until_successful_overwrite(
    store, tmp_path,
):
    store.save_work({
        "celex_id": "PDF1",
        "content_source": "cellar_pdf_eng",
        "full_text": "suspect PDF text",
    })
    store.save_text_units("PDF1", [{"type": "body", "text": "suspect"}])
    store.mark_processed("PDF1")
    prepare_pdf_repair(store.db_path, tmp_path / "repair.tsv", apply=True)

    store.save_work(
        {"celex_id": "PDF1", "title": "Still listed"},
        preserve_existing_content=True,
    )
    store.mark_processed("PDF1")
    codes = {issue["code"] for issue in _validate_connection(store.conn)}
    assert "pending_pdf_repair" in codes

    store.save_work({
        "celex_id": "PDF1",
        "content_source": "cellar_pdf_eng__pymupdf_timeout",
        "full_text": "fresh text",
    })
    store.save_text_units("PDF1", [{"type": "body", "text": "fresh"}])
    store.mark_processed("PDF1")
    codes = {issue["code"] for issue in _validate_connection(store.conn)}
    assert "pending_pdf_repair" not in codes
