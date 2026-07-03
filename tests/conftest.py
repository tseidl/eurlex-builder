"""Shared test fixtures."""

import pytest

from eurlex_builder.storage.duckdb import DuckDBStore


@pytest.fixture
def store(tmp_path):
    db = DuckDBStore(str(tmp_path / "test.duckdb"))
    yield db
    db.close()


@pytest.fixture
def sample_config_dict():
    return {
        "metadata": {"project_name": "Test Dataset"},
        "data": {
            "mode": "fixed",
            "celex_ids": ["32016R0679"],
        },
        "output": {"formats": ["parquet"], "output_directory": "./test-output"},
    }


@pytest.fixture
def sample_descriptive_config_dict():
    return {
        "data": {
            "mode": "descriptive",
            "document_types": ["regulation", "directive"],
            "start_date": "2020-01-01",
            "end_date": "2023-12-31",
        },
    }


SAMPLE_CELEX_IDS = [
    "32016R0679",   # GDPR (regulation)
    "32024R1689",   # AI Act (regulation)
    "32000L0031",   # E-Commerce Directive
    "52005DC0229",  # Communication
]
