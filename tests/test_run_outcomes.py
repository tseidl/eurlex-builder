"""Check selection and invocation outcomes through real storage and exports."""

from __future__ import annotations

from unittest.mock import Mock

import duckdb
import polars as pl
import pytest

from eurlex_builder.cli import main
from eurlex_builder.config import Config
from eurlex_builder.errors import SelectionError, TransientSourceError
from eurlex_builder.extractors.html import HtmlExtractor
from eurlex_builder.pipeline import Pipeline, RunResult
from eurlex_builder.storage.duckdb import DuckDBStore


class _Source:
    """Serve two documents without HTTP or optional model dependencies."""

    def __init__(self, failed_ids=()):
        self.failed_ids = set(failed_ids)
        self.session = Mock()

    def fetch_metadata(self, celex_id):
        if celex_id in self.failed_ids:
            raise TransientSourceError("Temporary metadata failure")
        return {"celex_id": celex_id, "document_type": "regulation"}

    def fetch_content(self, celex_id):
        return b'<html><body><div id="art_1"><p>Operative text.</p></div></body></html>', "html", "eng"

    def fetch_pdf(self, celex_id):
        return None


def _make_pipeline(tmp_path, monkeypatch, *, parallel=False, failed_ids=()):
    """Use real DuckDB writes and exports with deterministic source responses."""
    config = Config.model_validate({
        "data": {"mode": "fixed", "celex_ids": ["32020R0001", "32020R0002"]},
        "processing": {
            "parallel": parallel,
            "include_relations": False,
            "include_eurovoc": False,
            "translation": {"translate_full_text": False, "translate_text_units": False},
        },
        "output": {"output_directory": str(tmp_path)},
    })
    store = DuckDBStore(str(tmp_path / "eurlex_builder.duckdb"))
    pipeline = Pipeline(config, _Source(failed_ids), [HtmlExtractor()], store, store)
    monkeypatch.setattr(pipeline, "_setup_logging", lambda: None)
    monkeypatch.setattr("eurlex_builder.sources.cellar.CellarSource", lambda: _Source(failed_ids))
    return pipeline


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("failed_ids", [[], ["32020R0002"], ["32020R0001", "32020R0002"]])
def test_cli_reports_invocation_failures_after_persisting_results(tmp_path, monkeypatch, parallel, failed_ids):
    pipeline = _make_pipeline(tmp_path, monkeypatch, parallel=parallel, failed_ids=failed_ids)
    # An unrelated historical failure must not change this invocation's outcome.
    pipeline.checkpoint.mark_failed("31999R0001", "Historical failure")
    monkeypatch.setattr(Pipeline, "from_config_file", lambda path: pipeline)

    if failed_ids:
        with pytest.raises(SystemExit) as error:
            main(["run", "unused.yaml"])
        assert error.value.code == 1
    else:
        main(["run", "unused.yaml"])

    expected_status = "complete_with_failures" if failed_ids else "complete"
    with duckdb.connect(str(tmp_path / "eurlex_builder.duckdb"), read_only=True) as conn:
        assert conn.execute("SELECT status FROM dataset_runs").fetchone()[0] == expected_status
        assert conn.execute("SELECT count(*) FROM works").fetchone()[0] == 2 - len(failed_ids)
        assert conn.execute("SELECT count(*) FROM _checkpoint WHERE status='failed'").fetchone()[0] == 1 + len(failed_ids)
    assert pl.read_parquet(tmp_path / "works.parquet").height == 2 - len(failed_ids)


def test_api_result_counts_only_documents_attempted_with_limit(tmp_path, monkeypatch):
    pipeline = _make_pipeline(tmp_path, monkeypatch, failed_ids=["32020R0002"])
    pipeline.checkpoint.mark_failed("32020R0002", "Previous failure")

    assert pipeline.run(resume=True, limit=1) == RunResult(processed=1)

    pipeline = _make_pipeline(tmp_path, monkeypatch)
    pipeline.checkpoint.mark_processed("32020R0002")
    assert pipeline.run(resume=True) == RunResult()


@pytest.mark.parametrize("review", ["no_matches", "reject_all", "accept", "no_filter"])
def test_eurovoc_selection_never_silently_broadens_query(tmp_path, monkeypatch, review):
    source = Mock()
    matches = {} if review == "no_matches" else {"http://eurovoc.europa.eu/1": {"privacy"}}
    source.resolve_eurovoc_keywords.return_value = {"privacy": matches}
    source.resolve_celex_ids.return_value = ["32020R0001"]
    monkeypatch.setattr("eurlex_builder.eurovoc_review.review_eurovoc_matches", lambda *args: [])
    config = Config.model_validate({
        "data": {
            "mode": "descriptive", "document_types": ["regulation"],
            "start_date": "2020-01-01", "end_date": "2020-12-31",
            "filter_keywords": [] if review == "no_filter" else ["privacy"],
        },
        "processing": {"automated_mode": review != "reject_all"},
        "output": {"output_directory": str(tmp_path)},
    })
    store = DuckDBStore(str(tmp_path / "eurlex_builder.duckdb"))
    pipeline = Pipeline(config, source, [], store, store)
    monkeypatch.setattr(pipeline, "_setup_logging", lambda: None)
    store.mark_failed("32020R0001", "Keep checkpoint on failed selection")

    if review in {"no_matches", "reject_all"}:
        with pytest.raises(SelectionError, match="filter_keywords"):
            pipeline.run(retry_failed=True)
        source.resolve_celex_ids.assert_not_called()
        with duckdb.connect(store.db_path, read_only=True) as conn:
            assert conn.execute("SELECT status FROM _checkpoint").fetchone()[0] == "failed"
            assert conn.execute("SELECT status FROM dataset_runs").fetchone()[0] == "failed"
    else:
        try:
            assert pipeline._resolve_ids() == ["32020R0001"]
            expected = None if review == "no_filter" else ["http://eurovoc.europa.eu/1"]
            assert source.resolve_celex_ids.call_args.kwargs["eurovoc_uris"] == expected
        finally:
            store.close()
