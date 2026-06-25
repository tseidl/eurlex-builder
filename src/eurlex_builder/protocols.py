"""Protocol definitions for eurlex-builder's pluggable architecture."""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable


@runtime_checkable
class DataSource(Protocol):
    """Fetches metadata and raw content for EU legislative documents."""

    def resolve_celex_ids(
        self,
        *,
        document_types: list[str],
        start_date: date,
        end_date: date,
        eurovoc_uris: list[str] | None = None,
        include_corrigenda: bool = False,
        include_consolidated_texts: bool = False,
    ) -> list[str]:
        """Discover CELEX IDs matching descriptive criteria."""
        ...

    def fetch_metadata(self, celex_id: str) -> dict:
        """Return metadata dict (title, date_adopted, document_type, language, etc.)."""
        ...

    def fetch_content(self, celex_id: str) -> tuple[bytes, str, str] | None:
        """Return (content_bytes, content_type, language) or None if unavailable."""
        ...

    def fetch_relations(self, celex_id: str) -> list[dict]:
        """Return list of relation dicts {source_celex, target_celex, relation_type}."""
        ...

    def fetch_pdf(self, celex_id: str) -> tuple[bytes, str, str] | None:
        """Return (pdf_bytes, content_type, language) or None if no PDF available."""
        return None

    def fetch_eurovoc(self, celex_id: str) -> list[dict]:
        """Return EuroVoc descriptors for a document."""
        return []

    def resolve_eurovoc_keywords(self, keywords: list[str]) -> dict:
        """Resolve keyword strings to EuroVoc concept URIs."""
        return {}

    def resolve_procedure_numbers(self, procedure_numbers: list[str]) -> list[str]:
        """Resolve interinstitutional procedure numbers to CELEX IDs."""
        return []


@runtime_checkable
class TextExtractor(Protocol):
    """Extracts structured text units from raw document content."""

    def can_handle(self, raw_content: bytes) -> bool:
        """Return True if this extractor can parse the given content."""
        ...

    def extract(
        self,
        celex_id: str,
        raw_content: bytes,
        *,
        include_recitals: bool = True,
        include_articles: bool = True,
        include_annexes: bool = True,
        article_granularity: str = "article",
        language: str = "eng",
        out_metadata: dict | None = None,
    ) -> list[dict]:
        """Return list of text unit dicts {type, number, title, text, paragraph_num?, point_letter?}.

        `language` is the source language reported by the data source.
        `out_metadata`, if provided, is a mutable dict the extractor may populate
        with extraction intermediates (e.g. the Docling markdown for PDFs) that
        a higher layer needs to decide on fallback strategies.
        """
        ...


@runtime_checkable
class Store(Protocol):
    """Working store for pipeline results."""

    def save_work(self, work: dict) -> None: ...
    def save_text_units(self, celex_id: str, units: list[dict]) -> None: ...
    def save_relations(self, relations: list[dict], *, celex_id: str | None = None) -> None: ...
    def save_eurovoc(self, celex_id: str, descriptors: list[dict]) -> None: ...
    def get_processed_ids(self) -> set[str]: ...
    def get_content_report(self) -> list[tuple]: ...
    def export(self, output_dir: str, formats: list[str]) -> None: ...


@runtime_checkable
class Checkpoint(Protocol):
    """Track pipeline progress for resumability."""

    def is_processed(self, celex_id: str) -> bool: ...
    def mark_processed(self, celex_id: str) -> None: ...
    def mark_failed(self, celex_id: str, error: str) -> None: ...
    def get_summary(self) -> dict: ...
    def reset(self) -> None: ...
    def reset_failed(self) -> int: ...
