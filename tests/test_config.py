"""Tests for configuration validation."""

import pytest
from eurlex_builder.config import Config


def test_valid_fixed_config(sample_config_dict):
    config = Config.model_validate(sample_config_dict)
    assert config.data.mode == "fixed"
    assert "32016R0679" in config.data.celex_ids


def test_valid_descriptive_config(sample_descriptive_config_dict):
    config = Config.model_validate(sample_descriptive_config_dict)
    assert config.data.mode == "descriptive"
    assert "regulation" in config.data.document_types


def test_descriptive_accepts_communication():
    """Communication should NOT be rejected — no hardcoded type restriction."""
    config = Config.model_validate({
        "data": {
            "mode": "descriptive",
            "document_types": ["communication"],
            "start_date": "2005-07-01",
            "end_date": "2005-07-31",
        },
    })
    assert "communication" in config.data.document_types


def test_fixed_mode_requires_input():
    with pytest.raises(Exception):
        Config.model_validate({
            "data": {"mode": "fixed"},
        })


def test_descriptive_mode_requires_dates_order():
    with pytest.raises(Exception):
        Config.model_validate({
            "data": {
                "mode": "descriptive",
                "document_types": ["regulation"],
                "start_date": "2023-12-31",
                "end_date": "2020-01-01",
            },
        })
