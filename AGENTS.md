# eurlex-builder

Configurable Python pipeline that builds structured datasets from EU legislative data (EUR-Lex / Cellar). Fetches documents via SPARQL + REST, extracts text at sub-article granularity, stores results in DuckDB + Parquet.

## Architecture

- `src/eurlex_builder/` — package source
  - `pipeline.py` — main orchestrator (sequential + parallel modes)
  - `config.py` — Pydantic config models, YAML loading
  - `protocols.py` — DataSource, TextExtractor, Store, Checkpoint protocols
  - `sources/cellar.py` — SPARQL + REST API client for EUR-Lex/Cellar
  - `extractors/html.py` — 6 HTML era extractors + COM paragraph extraction
  - `extractors/pdf.py` — parent-side isolated Docling worker lifecycle + pymupdf fallback
  - `extractors/docling_process.py` — persistent standalone Docling subprocess
  - `extractors/splitter.py` — sub-article splitting (paragraph/point granularity)
  - `storage/duckdb.py` — DuckDB store + checkpoint implementation
  - `storage/export.py` — Polars-based Parquet/CSV export
  - `validate.py` — read-only dataset integrity checks
  - `translate.py` — Helsinki-NLP Opus-MT translation
  - `enrich.py` — post-hoc SPARQL metadata enrichment
  - `eurovoc_review.py` — interactive EuroVoc concept review
  - `utils.py` — CELEX validation, string normalization, boilerplate removal
  - `cli.py` — argparse CLI (run, translate, enrich, status, validate)
- `tests/` — pytest suite
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
eurlex-builder run config.yaml [--fresh] [--retry-failed] [--limit N]
eurlex-builder translate <db> [--no-full-text] [--no-text-units] [--retry-rejected]
eurlex-builder enrich <db> [--select metadata relations eurovoc] [--parallel]
eurlex-builder status <db>
eurlex-builder validate <db>
```

## Key design decisions

- Six HTML extraction eras detected at runtime (standard OJ, manual CSS, class-based, text-only, consolidated-norm, classless fallback)
- Each pipeline thread reuses one standalone Docling subprocess; a hard timeout kills the process group and the next PDF starts a clean worker
- Only complete Docling conversions are accepted; partial, failed, timed-out, crashed, and oversized conversions use pymupdf and receive a queryable `content_source` suffix
- The 50MB Docling size guard, PDF/COM conversion, and fallback provenance share one extraction path
- Same-language HTML-to-PDF structural fallback is additive: it retains source units verbatim, requires article-set corroboration, and adds only missing credible identifiers
- Translate-before-extract fallback for non-English legislative PDFs where requested structures are missing; adopted only when requested structure counts improve without regressions
- Translation uses pinned Opus-MT revisions, tokenizer-bounded chunks, retry generation, quality guards, and a policy-versioned rejection ledger; rejected output never replaces source text
- Sub-article splitter operates on `body_parts: list[str]` from extractors, not raw HTML
- Point markers are validated against the drafting sequence ((a), (aa), (b), …); roman sub-points stay inside their parent point
- "Done at …" ends the enacting terms, but extractors resume collection at ANNEX headings — annexes follow the signature in the OJ layout
- Quoted replacement law in amending acts (`: '…'`) is never split; close quotes only count when followed by punctuation, so apostrophes don't close a region
- Checkpoint in DuckDB `_checkpoint` table makes the pipeline restart-safe
- `--fresh` clears checkpoints only for the selected documents; resume an interrupted fresh rebuild without passing `--fresh` again
- `--limit N` bounds a resumable canary and cannot be combined with `--fresh`
- Relations cached from metadata SPARQL to avoid duplicate queries
- Stable text-unit keys combine CELEX and structural coordinates; `unit_order` preserves document order
- Run manifests store the validated config hash, Git state, and dependency versions in DuckDB
