"""Regression tests for the Cellar REST client (2026-07 review)."""

from __future__ import annotations

from eurlex_builder.sources.cellar import CellarSource

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

    def get(self, url, headers=None, timeout=None):
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


def test_enrich_one_flags_failed_query(monkeypatch):
    """A failed SPARQL query must be distinguishable from an empty result —
    callers skip the save (and the EuroVoc delete) when ok is False."""
    import eurlex_builder.sources.cellar as cellar
    from eurlex_builder.enrich import _enrich_one

    monkeypatch.setattr(cellar, "_sparql_query", lambda *a, **k: None)
    result = _enrich_one("32016R0679", "SELECT 1")
    assert result["ok"] is False
    assert result["eurovoc"] == []
