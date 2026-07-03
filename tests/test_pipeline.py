"""Regression tests for pipeline orchestration (2026-07 review)."""

from __future__ import annotations

from eurlex_builder.extractors.html import HtmlExtractor
from eurlex_builder.pipeline import Pipeline


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


class _FailingSource:
    """Simulates a transient network failure: metadata works, content doesn't."""

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

    def save_work(self, work):
        self.calls.append("save_work")

    def save_text_units(self, celex_id, units):
        self.calls.append("save_text_units")

    def save_relations(self, relations, *, celex_id=None):
        self.calls.append("save_relations")

    def save_eurovoc(self, celex_id, descriptors):
        self.calls.append("save_eurovoc")


def test_transient_fetch_failure_does_not_clear_units():
    """A fetch failure yields units=[]; that must NOT reach save_text_units,
    which would delete the document's previously extracted rows."""
    from eurlex_builder.config import Config

    config = Config.model_validate({"data": {"mode": "fixed", "celex_ids": ["32016R0679"]}})
    store = _RecordingStore()
    pipeline = Pipeline(
        config=config, source=_FailingSource(), extractors=[],
        store=store, checkpoint=None,
    )
    pipeline._process_one("32016R0679")
    assert "save_work" in store.calls
    assert "save_text_units" not in store.calls
