"""Tests for CELEX utilities."""

from eurlex_builder.utils import (
    convert_consolidated_to_original,
    get_document_type,
    is_consolidated_celex,
    is_valid_celex,
)


def test_valid_celex():
    assert is_valid_celex("32016R0679")
    assert is_valid_celex("52005DC0229")
    assert not is_valid_celex("abc")
    assert not is_valid_celex("")


def test_consolidated_detection():
    assert is_consolidated_celex("02016R0679")
    assert not is_consolidated_celex("32016R0679")


def test_consolidated_to_original():
    assert convert_consolidated_to_original("02016R0679-20210101") == "32016R0679"
    assert convert_consolidated_to_original("32016R0679") == "32016R0679"


def test_document_type():
    assert get_document_type("32016R0679") == "regulation"
    assert get_document_type("32000L0031") == "directive"
    assert get_document_type("52005DC0229") == "communication"
    assert get_document_type("32021D2034") == "decision"
