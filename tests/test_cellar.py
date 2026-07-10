"""Regression tests for the Cellar REST client (2026-07 review)."""

from __future__ import annotations

import pytest
import requests
from datetime import date

from eurlex_builder.errors import TransientSourceError
from eurlex_builder.sources.cellar import CellarSource, DISCOVERY_TIMEOUT, HTML_ACCEPT

# Minimal HTTP 300 Multiple Choices page with one selectable candidate.
_CHOICE_PAGE = b"""<html><body><ul>
<li title="item"><a href="http://example.invalid/doc1">doc1</a>
  <ul><li title="stream_name">ACT_PART1</li><li title="stream_order">1</li></ul>
</li>
</ul></body></html>"""


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"", reason: str = ""):
        self.status_code = status_code
        self.content = content
        self.reason = reason


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def get(self, url, headers=None, timeout=None):
        self.requests.append((url, headers, timeout))
        return self._responses.pop(0)


def _source_with(responses) -> CellarSource:
    src = CellarSource()
    src.session.close()  # don't leak the real connection pool
    src.session = _FakeSession(responses)
    return src


def test_nested_300_response_is_not_stored_as_content():
    """A 300 behind a 300 must return None, never the choice-page HTML."""
    src = _source_with([
        _FakeResponse(300, _CHOICE_PAGE),
        _FakeResponse(300, _CHOICE_PAGE, reason="Multiple Choices"),
    ])
    assert src._fetch_with_300_handling("http://example.invalid", {}, "TEST") is None


def test_empty_200_body_is_treated_as_missing():
    src = _source_with([_FakeResponse(200, b"")])
    assert src._fetch_with_300_handling("http://example.invalid", {}, "TEST") is None


def test_valid_200_after_300_redirect_returns_content():
    src = _source_with([
        _FakeResponse(300, _CHOICE_PAGE),
        _FakeResponse(200, b"<html>the document</html>"),
    ])
    content = src._fetch_with_300_handling("http://example.invalid", {}, "TEST")
    assert content == b"<html>the document</html>"


def test_content_fetch_negotiates_xhtml_and_html_in_one_request():
    src = _source_with([_FakeResponse(200, b"<html><body>document</body></html>")])
    session = src.session

    result = src.fetch_content("32024R1689")

    assert result is not None
    assert result[1:] == ("html", "eng")
    assert len(session.requests) == 1
    assert session.requests[0][1]["Accept"] == HTML_ACCEPT


def test_request_exception_is_transient():
    class FailingSession:
        def get(self, *args, **kwargs):
            raise requests.ConnectionError("offline")

    src = _source_with([])
    src.session = FailingSession()
    with pytest.raises(TransientSourceError):
        src._fetch_with_300_handling("http://example.invalid", {}, "TEST")


def test_deterministic_client_error_is_definitive_unavailability():
    src = _source_with([_FakeResponse(406, b"Not Acceptable")])
    assert src._fetch_with_300_handling("http://example.invalid", {}, "TEST") is None


def test_pdf_manifest_continues_after_non_pdf_200():
    src = _source_with([
        _FakeResponse(200, b"not a PDF"),
        _FakeResponse(200, b"%PDF-valid"),
    ])
    result = src._try_pdf_manifest(
        "TEST", "eng", {"eng": "http://example.invalid/manifest"},
    )
    assert result == (b"%PDF-valid", "pdf", "eng")


def test_enrich_one_flags_failed_query(monkeypatch):
    import eurlex_builder.sources.cellar as cellar
    from eurlex_builder.enrich import _enrich_one

    monkeypatch.setattr(cellar, "_sparql_query", lambda *a, **k: None)
    with pytest.raises(TransientSourceError):
        _enrich_one("32016R0679", "SELECT 1", {"eurovoc"})


def test_descriptive_discovery_uses_extended_timeout(monkeypatch):
    import eurlex_builder.sources.cellar as cellar

    captured = {}

    def fake_query(query, **kwargs):
        captured.update(kwargs)
        return {"results": {"bindings": []}}

    monkeypatch.setattr(cellar, "_sparql_query", fake_query)
    source = CellarSource()
    try:
        assert source.resolve_celex_ids(
            document_types=["regulation"],
            start_date=date(2020, 1, 1),
            end_date=date(2020, 12, 31),
        ) == []
    finally:
        source.session.close()
    assert captured["timeout"] == DISCOVERY_TIMEOUT
