"""Regression tests for pipeline orchestration (2026-07 review)."""

from __future__ import annotations

import sys

from eurlex_builder.extractors.html import HtmlExtractor
from eurlex_builder.errors import TransientSourceError
from eurlex_builder.pipeline import (
    Pipeline,
    _should_run_translate_fallback,
    _translated_parse_is_better,
)


class _StubSource:
    """Returns fixed HTML bytes for any fetch_content call."""

    def __init__(self, raw: bytes):
        self._raw = raw

    def fetch_content(self, celex_id):
        return (self._raw, "html", "eng")


def _pipeline() -> Pipeline:
    return Pipeline(
        config=None, source=None, extractors=[HtmlExtractor()],
        store=None, checkpoint=None,
    )


def test_original_recitals_fetch_filters_out_body_fallback():
    """When the original act's HTML yields no recital structure, the
    extractor's whole-document 'body' fallback must not be prepended to the
    consolidated text's units."""
    raw = b"""<html><body>
<p>Some unstructured original act text without recognisable classes.</p>
<p>More prose that is definitely not a recital.</p>
</body></html>"""
    units = _pipeline()._fetch_original_recitals_with_source(
        "02016R0679-20210101", _StubSource(raw),
    )
    assert units == []


def test_original_recitals_fetch_returns_recitals_when_present():
    raw = b"""<html><body>
<div id="rct_1">(1) A recital that is long enough to count as real prose.</div>
<div id="art_1"><p>Article body text.</p></div>
</body></html>"""
    units = _pipeline()._fetch_original_recitals_with_source(
        "02016R0679-20210101", _StubSource(raw),
    )
    assert len(units) == 1
    assert units[0]["type"] == "recital"


def test_optional_original_recital_failure_keeps_primary_recitals():
    class Source:
        def fetch_content(self, celex_id):
            raise TransientSourceError("temporary outage")

    pipeline = _pipeline()
    primary = [{"type": "recital", "number": "1", "text": "Primary"}]
    original = pipeline._fetch_original_recitals_with_source(
        "02016R0679-20210101", Source(),
    )
    assert pipeline._merge_original_recitals(primary, original) == primary


class _UnavailableSource:
    """Simulates definitive content unavailability after metadata succeeds."""

    def fetch_metadata(self, celex_id):
        return {
            "celex_id": celex_id, "title": None, "date_adopted": None,
            "document_type": "regulation", "language": None, "full_text_html": None,
        }

    def fetch_content(self, celex_id):
        return None

    def fetch_relations(self, celex_id):
        return []

    def fetch_eurovoc(self, celex_id):
        return []


class _RecordingStore:
    def __init__(self):
        self.calls = []

    def save_work(self, work, *, preserve_existing_content=False):
        self.calls.append("save_work")

    def save_text_units(self, celex_id, units):
        self.calls.append("save_text_units")

    def save_relations(self, relations, *, celex_id=None):
        self.calls.append("save_relations")

    def save_eurovoc(self, celex_id, descriptors):
        self.calls.append("save_eurovoc")


def test_unavailable_content_does_not_clear_units():
    """Unavailable content yields units=[]; that must NOT reach save_text_units,
    which would delete the document's previously extracted rows."""
    from eurlex_builder.config import Config

    config = Config.model_validate({"data": {"mode": "fixed", "celex_ids": ["32016R0679"]}})
    store = _RecordingStore()
    pipeline = Pipeline(
        config=config, source=_UnavailableSource(), extractors=[],
        store=store, checkpoint=None,
    )
    pipeline._process_one("32016R0679")
    assert "save_work" in store.calls
    assert "save_text_units" not in store.calls


def test_translated_fallback_prioritizes_recitals_without_losing_structure():
    source = [
        *[{"type": "article", "number": "1"} for _ in range(100)],
        {"type": "annex", "number": "I"},
    ]
    translated = [
        *[{"type": "recital", "number": str(i)} for i in range(1, 21)],
        *[{"type": "article", "number": "1"} for _ in range(79)],
        {"type": "annex", "number": "I"},
    ]
    assert _translated_parse_is_better(source, translated)

    translated_without_annex = [
        unit for unit in translated if unit["type"] != "annex"
    ]
    assert not _translated_parse_is_better(source, translated_without_annex)


def test_articles_only_translation_can_improve_extraction():
    source = [{"type": "body", "number": None}]
    translated = [{"type": "article", "number": "1"}]
    assert _should_run_translate_fallback(
        source, "regulation", "deu",
        include_recitals=False, include_articles=True, include_annexes=False,
    )
    assert _translated_parse_is_better(
        source,
        translated,
        include_recitals=False,
        include_articles=True,
        include_annexes=False,
    )


def test_original_recital_merge_preserves_matching_translation():
    primary = [
        {
            "type": "recital", "number": "1", "text": "English",
            "text_translated": "English",
        },
        {"type": "article", "number": "1", "text": "Article"},
    ]
    original = [{"type": "recital", "number": "1", "text": "Original"}]
    merged = Pipeline._merge_original_recitals(primary, original)
    assert merged[0]["text"] == "Original"
    assert merged[0]["text_translated"] == "English"


def test_optional_pdf_retry_failure_keeps_html_extraction():
    from eurlex_builder.config import Config

    class Source:
        def fetch_metadata(self, celex_id):
            return {
                "celex_id": celex_id, "title": "Test", "date_adopted": None,
                "document_type": "regulation", "language": None,
                "full_text_html": None,
            }

        def fetch_content(self, celex_id):
            return (
                b"<html><body><div id='art_1'><p>Operative text.</p></div></body></html>",
                "html", "eng",
            )

        def fetch_pdf(self, celex_id):
            raise TransientSourceError("PDF endpoint unavailable")

    config = Config.model_validate({
        "data": {"mode": "fixed", "celex_ids": ["32020R0001"]},
        "processing": {"include_relations": False},
    })
    pipeline = Pipeline(config, Source(), [HtmlExtractor()], None, None)
    result = pipeline._fetch_and_extract("32020R0001", Source())
    assert result["fetched"] is True
    assert any(unit["type"] == "article" for unit in result["units"])


def test_optional_original_relation_failure_keeps_primary_relations():
    from eurlex_builder.config import Config

    consolidated = "02016R0679-20210101"
    original = "32016R0679"

    class Source:
        def fetch_metadata(self, celex_id):
            if celex_id == original:
                raise TransientSourceError("metadata endpoint unavailable")
            return {
                "celex_id": celex_id, "title": "Test", "date_adopted": None,
                "document_type": "regulation", "language": None,
                "full_text_html": None,
            }

        def fetch_content(self, celex_id):
            return None

        def fetch_relations(self, celex_id):
            return [{
                "source_celex": consolidated,
                "target_celex": "32000R0001",
                "relation_type": "cites",
            }]

    config = Config.model_validate({
        "data": {"mode": "fixed", "celex_ids": [consolidated, original]},
        "processing": {"fetch_original_recitals_for_consolidated": False},
    })
    source = Source()
    pipeline = Pipeline(config, source, [], None, None)
    pipeline._selected_ids = {consolidated, original}
    result = pipeline._fetch_and_extract(consolidated, source)
    assert [relation["target_celex"] for relation in result["relations"]] == [
        "32000R0001"
    ]


def test_pdf_retry_honors_requested_structures():
    recitals_only = [
        {"type": "recital", "number": str(i), "text": "text"}
        for i in range(1, 4)
    ]
    assert Pipeline._should_retry_with_pdf(recitals_only, include_articles=True)
    assert not Pipeline._should_retry_with_pdf(
        recitals_only,
        include_recitals=True,
        include_articles=False,
        include_annexes=False,
    )


def test_run_manifest_is_marked_failed_on_discovery_error(store, monkeypatch):
    import duckdb
    from eurlex_builder.config import Config

    class DiscoveryFailure:
        def resolve_celex_ids(self, **kwargs):
            raise TransientSourceError("endpoint unavailable")

    config = Config.model_validate({
        "data": {
            "mode": "descriptive",
            "document_types": ["regulation"],
            "start_date": "2020-01-01",
            "end_date": "2020-12-31",
        },
    })
    pipeline = Pipeline(config, DiscoveryFailure(), [], store, store)
    monkeypatch.setattr(pipeline, "_setup_logging", lambda: None)

    import pytest
    with pytest.raises(TransientSourceError):
        pipeline.run(resume=True)

    check = duckdb.connect(store.db_path)
    try:
        status = check.execute("SELECT status FROM dataset_runs").fetchone()[0]
    finally:
        check.close()
    assert status == "failed"


def test_fresh_run_resets_only_selected_checkpoints(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from eurlex_builder.config import Config

    config = Config.model_validate({
        "data": {"mode": "fixed", "celex_ids": ["32020R0001"]},
        "processing": {
            "translation": {
                "translate_full_text": False,
                "translate_text_units": False,
            },
        },
        "output": {"output_directory": str(tmp_path)},
    })
    store = Mock()
    checkpoint = Mock()
    checkpoint.reset_ids.return_value = 1
    checkpoint.get_summary.return_value = {
        "processed": 1, "failed": 0, "failed_details": {},
    }
    pipeline = Pipeline(config, None, [], store, checkpoint)
    monkeypatch.setattr(pipeline, "_run_sequential", lambda ids: None)
    monkeypatch.setattr(pipeline, "_report_missing_content", lambda: None)
    monkeypatch.setattr(pipeline, "_report_extraction_stats", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "eurlex_builder.translate",
        SimpleNamespace(translate_database=lambda *args, **kwargs: None),
    )

    pipeline._run_impl(
        run_id="run-id", resume=False, retry_failed=False,
    )

    checkpoint.reset_ids.assert_called_once_with(["32020R0001"])
    store.get_processed_ids.assert_not_called()


def test_empty_missing_content_report_removes_stale_files(tmp_path):
    from unittest.mock import Mock

    from eurlex_builder.config import Config

    config = Config.model_validate({
        "data": {"mode": "fixed", "celex_ids": ["32020R0001"]},
        "output": {"output_directory": str(tmp_path)},
    })
    for name in ("missing_content.tsv", "non_english_content.tsv"):
        (tmp_path / name).write_text("stale\n", encoding="utf-8")

    store = Mock()
    store.get_content_report.return_value = []
    pipeline = Pipeline(config, None, [], store, None)
    pipeline._report_missing_content()

    assert not (tmp_path / "missing_content.tsv").exists()
    assert not (tmp_path / "non_english_content.tsv").exists()


def test_pipeline_run_end_to_end_with_manifest_and_exports(tmp_path, monkeypatch):
    import duckdb
    from eurlex_builder.config import Config
    from eurlex_builder.storage.duckdb import DuckDBStore
    from eurlex_builder.validate import validate_database

    class Source:
        def fetch_metadata(self, celex_id):
            return {
                "celex_id": celex_id,
                "title": "Test act",
                "date_adopted": None,
                "document_type": "regulation",
                "language": None,
                "full_text_html": None,
            }

        def fetch_content(self, celex_id):
            return (
                b"<html><body><div id='art_1'><p>Operative text.</p></div></body></html>",
                "html",
                "eng",
            )

        def fetch_pdf(self, celex_id):
            return None

        def fetch_relations(self, celex_id):
            return []

        def fetch_eurovoc(self, celex_id):
            return []

    config = Config.model_validate({
        "data": {"mode": "fixed", "celex_ids": ["32020R0001"]},
        "processing": {
            "include_eurovoc": True,
            "translation": {
                "translate_full_text": False,
                "translate_text_units": False,
            },
        },
        "output": {"output_directory": str(tmp_path)},
    })
    db_path = tmp_path / "eurlex_builder.duckdb"
    store = DuckDBStore(str(db_path))
    pipeline = Pipeline(config, Source(), [HtmlExtractor()], store, store)
    monkeypatch.setattr(pipeline, "_setup_logging", lambda: None)
    pipeline.run(resume=True)

    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        assert conn.execute("SELECT status FROM dataset_runs").fetchone()[0] == "complete"
        assert conn.execute("SELECT count(*) FROM text_units").fetchone()[0] == 1
    finally:
        conn.close()
    assert validate_database(db_path) == []
    for table in ("works", "text_units", "relations", "eurovoc"):
        assert (tmp_path / f"{table}.parquet").exists()
