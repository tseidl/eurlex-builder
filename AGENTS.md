# eurlex-builder

Configurable Python pipeline that builds structured datasets from EU legislative data (EUR-Lex / Cellar). Fetches documents via SPARQL + REST, extracts text at sub-article granularity, stores results in DuckDB + Parquet.

## Architecture

- `src/eurlex_builder/` — package source
  - `pipeline.py` — main orchestrator (sequential + parallel modes)
  - `config.py` — Pydantic config models, YAML loading
  - `protocols.py` — DataSource, TextExtractor, Store, Checkpoint protocols
  - `sources/cellar.py` — SPARQL + REST API client for EUR-Lex/Cellar
  - `extractors/html.py` — 4 HTML era extractors + COM paragraph extraction
  - `extractors/pdf.py` — Docling + pymupdf PDF extraction
  - `extractors/splitter.py` — sub-article splitting (paragraph/point granularity)
  - `storage/duckdb.py` — DuckDB store + checkpoint implementation
  - `storage/export.py` — Polars-based Parquet/CSV export
  - `translate.py` — Helsinki-NLP Opus-MT translation
  - `enrich.py` — post-hoc SPARQL metadata enrichment
  - `eurovoc_review.py` — interactive EuroVoc concept review
  - `utils.py` — CELEX validation, string normalization, boilerplate removal
  - `cli.py` — argparse CLI (run, translate, enrich, status)
- `tests/` — pytest suite (58 tests)
- `configs/` — example run configurations

## Coding conventions

- Python 3.11+, type hints throughout
- `from __future__ import annotations` in every module
- Protocols for pluggable architecture (no ABC)
- Lazy imports for heavy dependencies (Docling, transformers, torch)
- DuckDB for working storage; Polars for export
- Thread safety: per-thread HTTP sessions, locks for shared model caches
- Logging via `logging.getLogger("eurlex_builder")`
- No comments unless the WHY is non-obvious
- Tests use pytest with conftest fixtures

## CLI

```bash
eurlex-builder run config.yaml [--fresh] [--retry-failed]
eurlex-builder translate <db> [--no-full-text] [--no-text-units]
eurlex-builder enrich <db> [--select metadata relations eurovoc] [--parallel]
eurlex-builder status <db>
```

## Key design decisions

- Four HTML extraction eras detected at runtime (standard OJ, manual CSS, class-based, text-only)
- PDF fallback via Docling with pymupdf as last resort; 50MB size guard
- Translate-before-extract fallback for non-English legislative PDFs where English-only markers fail
- Sub-article splitter operates on `body_parts: list[str]` from extractors, not raw HTML
- Checkpoint in DuckDB `_checkpoint` table makes the pipeline restart-safe
- Relations cached from metadata SPARQL to avoid duplicate queries
