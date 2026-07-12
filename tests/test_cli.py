"""CLI argument validation tests."""

from __future__ import annotations

import pytest

from eurlex_builder import cli


def test_run_limit_is_forwarded(monkeypatch):
    received = {}

    def fake_run(config_path, *, resume, retry_failed, limit):
        received.update({
            "config_path": config_path,
            "resume": resume,
            "retry_failed": retry_failed,
            "limit": limit,
        })

    monkeypatch.setattr(cli, "_run", fake_run)
    cli.main(["run", "config.yaml", "--limit", "500"])

    assert received == {
        "config_path": "config.yaml",
        "resume": True,
        "retry_failed": False,
        "limit": 500,
    }


def test_run_limit_rejects_fresh(monkeypatch):
    monkeypatch.setattr(cli, "_run", lambda *args, **kwargs: None)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["run", "config.yaml", "--fresh", "--limit", "1"])

    assert exc_info.value.code == 2


@pytest.mark.parametrize("value", ["0", "-1"])
def test_run_limit_must_be_positive(value):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["run", "config.yaml", "--limit", value])

    assert exc_info.value.code == 2
