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


def test_unknown_config_keys_are_rejected():
    with pytest.raises(Exception):
        Config.model_validate({
            "data": {"mode": "fixed", "celex_ids": ["32016R0679"]},
            "processing": {"translatoin": {"max_full_text_chars": 10}},
        })


def test_translation_limit_must_be_nonnegative():
    with pytest.raises(Exception):
        Config.model_validate({
            "data": {"mode": "fixed", "celex_ids": ["32016R0679"]},
            "processing": {"translation": {"max_full_text_chars": -1}},
        })


def test_fixed_mode_normalizes_and_validates_celex_ids():
    config = Config.model_validate({
        "data": {
            "mode": "fixed",
            "celex_ids": [
                " 32016r0679 ",
                "52001dc0775(02)",
                "02016r0679-20210101",
                "12012e/txt",
            ],
        },
    })
    assert config.data.celex_ids == [
        "32016R0679", "52001DC0775(02)", "02016R0679-20210101",
        "12012E/TXT",
    ]

    with pytest.raises(Exception):
        Config.model_validate({
            "data": {"mode": "fixed", "celex_ids": ["not-a-celex"]},
        })
