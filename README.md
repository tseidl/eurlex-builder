# eurlex-builder

> Build research-ready datasets from EU legislative data — recitals, articles, points, and the network between them.

`eurlex-builder` is a configurable Python pipeline that turns the [EUR-Lex / Cellar](https://eur-lex.europa.eu/) corpus into provision-level Parquet tables for quantitative research. It fetches every directive, regulation, decision, or communication you ask for, extracts text at the granularity you need (whole article, numbered paragraph, or lettered point), collects metadata + inter-document relations, and ships the result as one DuckDB file plus four Parquet tables.

It is, to our knowledge, the first open-source tool to extract EU legislative text at **configurable sub-article granularity** across EUR-Lex's **heterogeneous document formats** — from modern HTML to 1970s text-only pages to scanned PDFs.

The package accompanies Seidl and Kosti (2026), "Mapping Europe's Digital Acquis: A Granular History of EU Digital Policymaking" (preprint forthcoming on SocArXiv).

---

## Highlights

- **Sub-article granularity.** One config switch chooses whether each article is one row, one row per numbered paragraph (`Art. 5(1)`), or one row per lettered point (`Art. 5(1)(a)`) — following the EU Joint Practical Guide.
- **Four HTML structures + PDF fallback.** Automatic detection across EUR-Lex's four HTML eras (Standard OJ, Manual CSS, class-based, text-only) with Docling/pymupdf for older docs that have no machine-readable HTML.
- **Two query modes.** Fixed (give us a list of CELEX IDs / procedure numbers) or descriptive (date range + doc types + optional EuroVoc keyword filter).
- **Inter-document relations.** Citations, amendments, legal basis, repeals, consolidations — all in one table for network analysis.
- **Translation built in.** Non-English-only documents are translated to English via Helsinki-NLP Opus-MT, separately at document level (`works.full_text`) and per-unit (`text_units.text_translated`).
- **Reproducible + resumable.** A single YAML defines the dataset; a checkpoint table in DuckDB makes the pipeline restart-safe and incremental.
- **Parallel mode.** Multi-threaded fetching with `parallel: true`; sequential writes keep DuckDB contention-free.

---

## Quick start

```bash
pip install -e ".[all]"
```

Create a `config.yaml`. You can either request specific acts by CELEX ID (**fixed mode**) or search by date range and document type (**descriptive mode**):

**Fixed mode** — specific acts:

```yaml
metadata:
  project_name: "GDPR + AI Act"

data:
  mode: "fixed"
  celex_ids:
    - "32016R0679"    # GDPR
    - "32024R1689"    # AI Act

processing:
  text_extraction:
    article_granularity: paragraph

output:
  output_directory: "./output"
```

**Descriptive mode** — all acts matching a search:

```yaml
metadata:
  project_name: "EU digital regulations 2020–2025"

data:
  mode: "descriptive"
  document_types:
    - "regulation"
    - "directive"
  start_date: 2020-01-01
  end_date: 2025-12-31
  filter_keywords:           # optional EuroVoc filter
    - "artificial intelligence"
    - "data protection"

processing:
  text_extraction:
    article_granularity: paragraph
  parallel: true
  max_workers: 8

output:
  output_directory: "./output"
```

Run:

```bash
eurlex-builder run config.yaml
```

You'll get `output/eurlex_builder.duckdb` plus four Parquet files: `works.parquet`, `text_units.parquet`, `relations.parquet`, `eurovoc.parquet`.

```python
import polars as pl
text_units = pl.read_parquet("output/text_units.parquet")

# All paragraph-1 rows of GDPR Article 5
text_units.filter(
    (pl.col("celex_id") == "32016R0679")
    & (pl.col("type") == "article")
    & (pl.col("number") == "5")
)
```

---

## Why eurlex-builder

| | What it is | Why we built our own |
|---|---|---|
| **{eurlex} (R)** | Document retrieval | Handles retrieval but not structured extraction or translation; we provide both at multiple granularities |
| **EUPLEX** | Complexity indicators | Publishes derived metrics (readability, word counts) without the underlying text |
| **EUPROPS** | Manually curated text | One-off snapshot; not re-runnable or extendable to new time windows / doc types / granularities |
| **eurlex-builder** | **Configurable pipeline** | **Reproducible, provision-level output with structured extraction, translation, and inter-document relations — ready for NLP, classification, and network analysis** |

---

## Installation

Requires Python 3.11+.

```bash
git clone https://github.com/tseidl/eurlex-builder.git
cd eurlex-builder
python -m venv .venv && source .venv/bin/activate

# Core only (HTML extraction + parquet output)
pip install -e .

# With PDF support (Docling + pymupdf)
pip install -e ".[pdf]"

# With translation (Opus-MT via Hugging Face)
pip install -e ".[translate]"

# Everything (recommended for production runs)
pip install -e ".[all]"

# Plus dev tools (pytest)
pip install -e ".[dev]"
```

---

## Configuration reference

A single YAML file defines the dataset. Only the `data` section is required; everything else has sensible defaults.

<details>
<summary><strong>metadata</strong> &nbsp;— stamped onto the output, not used for filtering</summary>

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `project_name` | string | `"eurlex-builder Dataset"` | Embedded in output metadata |
| `author` | string | `""` | Author name |
| `description` | string | `"A dataset built with eurlex-builder."` | Free-text description |
| `version` | string | `"1.0"` | Dataset version |

</details>

<details>
<summary><strong>data (fixed mode)</strong> &nbsp;— specific documents</summary>

Provide CELEX IDs or interinstitutional procedure numbers. At least one is required.

```yaml
data:
  mode: "fixed"
  celex_ids:
    - "32016R0679"
  procedure_numbers:
    - "2021/0106"      # resolved to CELEX IDs via SPARQL
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `mode` | `"fixed"` | — | Required |
| `celex_ids` | list of strings | `[]` | CELEX identifiers |
| `procedure_numbers` | list of strings | `[]` | Interinstitutional procedure refs (e.g. `"2021/0106"`) |

</details>

<details>
<summary><strong>data (descriptive mode)</strong> &nbsp;— search by date / type / keyword</summary>

```yaml
data:
  mode: "descriptive"
  document_types:
    - "regulation"
    - "directive"
    - "decision"
    - "communication"
  start_date: 1979-01-01
  end_date: 2026-04-30
  filter_keywords:
    - "artificial intelligence"
    - "digital"
  include_corrigenda: false
  include_consolidated_texts: false
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `mode` | `"descriptive"` | — | Required |
| `document_types` | list of strings | — | Required. At least one. Mapped to CELEX type codes (`regulation`→R, `directive`→L, `decision`→D, `communication`→DC, `proposal`→PC, `staff working document`→SC) |
| `start_date` | date | — | Required (`YYYY-MM-DD`) |
| `end_date` | date | — | Required, must be after `start_date` |
| `filter_keywords` | list of strings | `[]` | EuroVoc keyword filter. Empty = no filter |
| `include_corrigenda` | bool | `false` | Include corrigenda |
| `include_consolidated_texts` | bool | `false` | Include consolidated texts (CELEX sector 0) |

</details>

<details>
<summary><strong>processing.text_extraction</strong> &nbsp;— structural extraction + granularity</summary>

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `include_recitals` | bool | `true` | Extract recitals (preamble "whereas" clauses) |
| `include_articles` | bool | `true` | Extract articles (operative provisions) |
| `include_annexes` | bool | `true` | Extract annexes |
| `strip_boilerplate` | bool | `true` | Strip signature blocks ("Done at Brussels…") and binding clauses from the last article |
| `store_raw_html` | bool | `false` | Store raw HTML in `works.full_text_html`. Substantially increases DB size; useful for debugging |
| `article_granularity` | `"article"` \| `"paragraph"` \| `"point"` | `"article"` | One row per article (default), per numbered paragraph, or per lettered point. See **Granularity** below |

</details>

<details>
<summary><strong>processing.translation</strong> &nbsp;— Opus-MT for non-English docs</summary>

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `translate_full_text` | bool | `true` | Translate `works.full_text` (one big blob per doc) |
| `translate_text_units` | bool | `true` | Translate `text_units.text` (per recital/article/paragraph) |
| `max_full_text_chars` | int | `100000` | Skip `full_text` translation above this length. `0` disables the cap. `text_units` are still translated |

</details>

<details>
<summary><strong>processing (top-level)</strong> &nbsp;— parallel, relations, EuroVoc</summary>

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `automated_mode` | bool | `false` | Skip interactive EuroVoc keyword review |
| `parallel` | bool | `false` | Multi-threaded fetching |
| `max_workers` | int (1–16) | `4` | Number of parallel threads. 8–12 is reasonable on a good connection |
| `include_relations` | bool | `true` | Fetch + store inter-document relations |
| `include_eurovoc` | bool | `false` | Include EuroVoc descriptors in metadata fetch (also via the `enrich` command) |
| `fetch_original_recitals_for_consolidated` | bool | `true` | Consolidated texts: fetch recitals from the original act |
| `fetch_original_relations_for_consolidated` | bool | `true` | Consolidated texts: merge relations from original |

</details>

<details>
<summary><strong>output</strong></summary>

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `formats` | list | `["parquet"]` | `"parquet"` and/or `"csv"`. Parquet is recommended |
| `output_directory` | string | `"./output"` | Output path. Created if missing |

</details>

---

## Granularity

EU drafting convention recognises three operative levels (Joint Practical Guide of the Council, Commission, and Parliament):

```
Article 5
  Paragraph 5(1)
    Point 5(1)(a)
    Point 5(1)(b)
```

`eurlex-builder` lets you emit each as a row:

| `article_granularity` | Row schema | Example: GDPR Art. 3 |
|---|---|---|
| `"article"` (default) | One row per article. Reproduces the conventional act-level view. `paragraph_num` and `point_letter` are NULL | 1 row |
| `"paragraph"` | One row per numbered paragraph (`1.`, `2.`, `1a.`, …). `paragraph_num` set; `point_letter` NULL | 3 rows |
| `"point"` | One row per lettered point (`(a)`, `(b)`, …) when present, else per paragraph. Both columns set when applicable | 5 rows (paragraphs 1 and 3 → 1 row each; paragraph 2 → stem + (a) + (b)) |

A short preamble before paragraph `1.` gets its own row with `paragraph_num = "0"`. Articles in amending acts (lists of edits to other regulations) are flagged `subtype = 'amendment_item'`; substantive replacement text within them stays untagged so downstream classifiers can keep it.

Recitals are emitted identically across all granularity settings.

---

## CLI commands

### `eurlex-builder run <config.yaml>`

Run the pipeline.

| Argument | Description |
|---|---|
| `config` | YAML config file |
| `--fresh` | Ignore checkpoints; re-process everything |
| `--retry-failed` | Re-attempt previously failed docs |

The pipeline resumes by default, skipping checkpointed docs.

### `eurlex-builder translate <db>`

Translate non-English content. Resumable; skips already-translated rows.

| Argument | Description |
|---|---|
| `db` | Path to the DuckDB file |
| `--max-full-text-chars` | Cap for `works.full_text` translation (default 100000) |
| `--no-full-text` | Skip `works.full_text` phase |
| `--no-text-units` | Skip `text_units.text` phase |

### `eurlex-builder enrich <db>`

Add post-hoc metadata via SPARQL — no re-fetching content. Adds entry-into-force date, ELI, author institutions, subject matter, procedure type/reference/legal basis, EuroVoc descriptors, and repeal relations.

| Argument | Description |
|---|---|
| `db` | Path to the DuckDB file |
| `--select` | Categories: `metadata`, `relations`, `eurovoc`. Default: all |
| `--parallel` | Fetch SPARQL queries in parallel |
| `--max-workers` | Number of parallel workers (default 4) |
| `--force` | Re-enrich already-enriched docs |

### `eurlex-builder status <db>`

Print checkpoint summary (processed / failed counts + failure reasons).

---

## Output schema

| File | Description |
|---|---|
| `eurlex_builder.duckdb` | Working database with all tables + checkpoint state |
| `works.parquet` | One row per document |
| `text_units.parquet` | One row per recital / article (or paragraph / point at sub-article granularity) / annex |
| `relations.parquet` | One row per inter-document relation |
| `eurovoc.parquet` | EuroVoc descriptors per document (populated by `enrich`) |
| `pipeline.log` | Full run log |
| `missing_content.tsv` | Docs with no text in any language (kept in `works` as empty rows) |
| `non_english_content.tsv` | Docs with non-English content (candidates for translation) |

<details>
<summary><strong>works</strong> &nbsp;— one row per document</summary>

| Column | Type | Description |
|---|---|---|
| `celex_id` | VARCHAR PK | CELEX identifier (e.g. `32016R0679`) |
| `title` | VARCHAR | English title from SPARQL |
| `date_adopted` | DATE | Document adoption date |
| `document_type` | VARCHAR | Derived from CELEX type code |
| `language` | VARCHAR | Language of fetched content |
| `full_text` | VARCHAR | Full document text (translated to English if non-English source) |
| `full_text_original` | VARCHAR | Original-language text (non-English docs only) |
| `full_text_html` | VARCHAR | Raw HTML (only if `store_raw_html: true`) |
| `content_source` | VARCHAR | Provenance tag (`cellar_html_eng`, `cellar_pdf_fra`, …) |
| `date_entry_into_force` | DATE | Populated by `enrich` |
| `date_end_of_validity` | DATE | Populated by `enrich`; `9999-12-31` if still in force |
| `is_in_force` | BOOLEAN | Populated by `enrich` |
| `eli` | VARCHAR | European Legislation Identifier URI |
| `author` | VARCHAR | Author institution(s) (e.g. `EP; CONSIL`) |
| `subject_matter` | VARCHAR | EU subject matter classification(s) |
| `procedure_type` | VARCHAR | e.g. `OLP` for ordinary legislative |
| `procedure_reference` | VARCHAR | e.g. `2012/0011/COD` |
| `procedure_legal_basis` | VARCHAR | Treaty legal basis |
| `enriched_at` | TIMESTAMP | NULL if not yet enriched |

</details>

<details>
<summary><strong>text_units</strong> &nbsp;— one row per recital / article / paragraph / point / annex</summary>

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment; preserves document order |
| `celex_id` | VARCHAR FK | Parent document |
| `type` | VARCHAR | `recital`, `article`, `annex`, `paragraph` (COMs), `footnote`, `body` (fallback) |
| `subtype` | VARCHAR | `"subheading"` (short recitals), `"table"` (COM table paragraphs), `"amendment_item"` (mechanical edits in amending acts), or NULL |
| `number` | VARCHAR | Unit number (e.g. `"1"`, `"IV"`, `"A"`) |
| `paragraph_num` | VARCHAR | `"1"`, `"1a"`, `"2"`, … when `article_granularity` ≠ `"article"`; `"0"` for preamble before paragraph 1. NULL otherwise |
| `point_letter` | VARCHAR | `"a"`, `"b"`, … when `article_granularity = "point"`. NULL otherwise |
| `title` | VARCHAR | Article or annex title |
| `text` | VARCHAR | Extracted text |
| `text_translated` | VARCHAR | English translation (populated by `translate`) |

</details>

<details>
<summary><strong>relations</strong> &nbsp;— one row per inter-document tie</summary>

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `source_celex` | VARCHAR FK | Source CELEX ID |
| `target_celex` | VARCHAR | Target CELEX ID |
| `relation_type` | VARCHAR | `cites`, `amends`, `adopts`, `based_on`, `proposes_to_amend`, `consolidates`, `repeals`, `implicitly_repeals` |

</details>

---

## FAQ

<details>
<summary><strong>How do I speed up large runs?</strong></summary>

Set `parallel: true` and `max_workers: 8` (or higher, up to 16) in the config. The bottleneck is HTTP fetching from EUR-Lex, so more workers help linearly. The pipeline writes to DuckDB sequentially regardless of worker count, so there's no thread contention.

</details>

<details>
<summary><strong>What's the difference between <code>paragraph</code> and <code>point</code> granularity?</strong></summary>

`paragraph` splits each article on its numbered paragraphs (`1.`, `2.`, `1a.`). Lettered sub-points stay inside the parent paragraph row.

`point` goes one level deeper: when a paragraph contains lettered points like GDPR Art. 6(1)(a)…(f), each gets its own row. Articles without lettered points behave identically under either setting.

Use `paragraph` when each numbered paragraph encodes one obligation and that's the analytical unit you want. Use `point` when paragraphs sometimes serve as umbrella stems ("processing shall be lawful only if at least one applies:") with the substantive content entirely in the points.

</details>

<details>
<summary><strong>Is the data reproducible?</strong></summary>

Mostly yes. The same config + the same EUR-Lex state will produce byte-identical recitals and identical structural splits. Two caveats:

1. EUR-Lex content can change post-publication (corrigenda, consolidation updates). Re-running later may pick up newer versions.
2. Docling's PDF layout parser is mostly deterministic but very-large or scanned PDFs may extract slightly differently across runs.

</details>

<details>
<summary><strong>How do I extend an existing run to newer legislation?</strong></summary>

Update `end_date` in the YAML and re-run **without** `--fresh`. The checkpoint table will skip the docs already processed and only fetch the new ones. Run `eurlex-builder enrich` afterwards to fill enrichment columns for the new docs.

</details>

<details>
<summary><strong>What happens to documents the pipeline can't fetch?</strong></summary>

They stay in `works` as rows with empty text columns and an entry in `missing_content.tsv`. This keeps the dataset transparent — a missing row is worse than a row with NAs, because it silently drops data.

</details>

<details>
<summary><strong>Why DuckDB + Parquet rather than SQLite?</strong></summary>

DuckDB writes columnar Parquet natively (no glue code), reads ~10× faster than SQLite for the analytical queries researchers actually run, and has direct interop with Polars / Arrow / R. The DuckDB file also acts as a single-file checkpoint store so the pipeline is restart-safe.

</details>

<details>
<summary><strong>How do I cite this in a paper?</strong></summary>

See **Citation** below. If your paper uses the dataset rather than the pipeline directly, please also reference the source: *EUR-Lex / Cellar, Publications Office of the European Union.*

</details>

<details>
<summary><strong>Can I add a custom data source / extractor?</strong></summary>

Yes. The pipeline talks to its dependencies via three protocols defined in `src/eurlex_builder/protocols.py`: `DataSource`, `TextExtractor`, `Store`. Implement those and pass instances to `Pipeline(...)`. The default wiring is `CellarSource` + `HtmlExtractor` + `PdfExtractor` + `DuckDBStore`.

</details>

<details>
<summary><strong>Why does descriptive mode miss some documents that the {eurlex} R package finds?</strong></summary>

Descriptive mode filters by CELEX type code (D, R, L) and sector. The `{eurlex}` R package filters by `work_has_resource-type` URIs, a partially overlapping semantic classification. Documents like merger decisions (CELEX type `M`), budget acts (`B`), and sector-5 Parliament documents show up in `{eurlex}` but not in a CELEX-type query. These are absent by design, not a bug. See **Roadmap** for planned resource-type support.

</details>

<details>
<summary><strong>Why does a <code>--fresh</code> re-run produce slightly different row counts for PDF-extracted documents?</strong></summary>

Docling's PDF layout parser can segment paragraphs differently across versions. Recital and article counts may vary slightly for PDF-sourced documents even when the text content is the same. This is a Docling version sensitivity, not a pipeline bug.

</details>

<details>
<summary><strong>What is the translate-before-extract fallback?</strong></summary>

The legislative PDF extractor uses English-only markers (`Whereas:`, `HAS ADOPTED THIS REGULATION:`, `ANNEX`). For non-English PDFs where these markers don't fire, the pipeline translates the Docling markdown to English via Opus-MT and re-parses from there. This fires automatically when a non-English legislative PDF produces fewer than 3 recitals. Affected rows are marked with `content_source` ending in `__translated` and have `text_translated` pre-filled. The alternative — adding native markers for every EU language — was rejected as a maintenance burden.

</details>

---

## Architecture

```
config.yaml (Pydantic-validated)
  ├─ EuroVoc keyword resolution (SPARQL, optional interactive review)
  ├─ Procedure number → CELEX resolution (SPARQL)
  ├─ CELEX ID discovery (SPARQL descriptive query)
  ├─ Per-document processing (parallel or sequential):
  │     metadata fetch         — SPARQL: title, date, relations
  │     content fetch           — REST: XHTML / HTML / PDF (six-language fallback)
  │     text extraction         — lxml: 4 HTML structures + paragraph splitting + PDF/Docling
  │     translate-before-extract — Opus-MT fallback when a non-English legislative
  │                                PDF produces zero structural units (English-only
  │                                markers like "Whereas:" wouldn't fire on a French
  │                                or German PDF). Translates the Docling markdown
  │                                and re-parses from English.
  │     storage                 — DuckDB: works, text_units, relations, checkpoint
  ├─ Translation                — Opus-MT, sequential post-processing
  ├─ Enrichment                 — SPARQL: dates, ELI, procedure, EuroVoc, repeals
  ├─ Export                     — Polars → Parquet/CSV
  └─ Reports                    — missing-content TSV, extraction stats
```

All data sourced through official EU APIs:
- **SPARQL**: `https://publications.europa.eu/webapi/rdf/sparql`
- **REST**: `https://publications.europa.eu/resource/celex/`

No scraping.

---

## Citation

If you use this package, please cite the accompanying paper and the software:

```bibtex
@article{seidl_kosti_2026,
  author  = {Seidl, Timo and Kosti, Nir},
  title   = {Mapping Europe's Digital Acquis: A Granular History of EU Digital Policymaking},
  year    = {2026},
  note    = {Working paper, preprint forthcoming on SocArXiv}
}

@software{eurlex_builder,
  author  = {Seidl, Timo},
  title   = {eurlex-builder: a configurable Python pipeline for EU legislative datasets},
  year    = {2026},
  url     = {https://github.com/tseidl/eurlex-builder},
  version = {0.1.0}
}
```

---

## Authors

- **Timo Seidl** — Assistant Professor, Technical University of Munich
- **Claude (Anthropic)** — Co-author (software design and implementation). Built with [Claude Code](https://claude.ai/code).

## Acknowledgments

- **Sebastian Rein** ([eulex-build](https://github.com/sebastianrein/eulex-build)) for the initial impetus, the YAML-driven configuration architecture, the fixed-vs-descriptive query mode design, EuroVoc keyword filtering, and the structural-decomposition target schema (recitals / articles / annexes + inter-document relations). The package builds on the foundation of his MA thesis on EU legislative data extraction (TUM, 2026).
- The maintainers of [EUR-Lex / Cellar](https://op.europa.eu/), [Docling](https://github.com/DS4SD/docling), [Helsinki-NLP Opus-MT](https://huggingface.co/Helsinki-NLP), [DuckDB](https://duckdb.org/), and [Polars](https://pola.rs/).

## Roadmap

- **Discovery by resource-type, not just CELEX-type.** Today's descriptive mode filters by CELEX type code (D, R, L) + sector. EUR-Lex also exposes `work_has_resource-type` URIs (`DEC`, `DEC_IMPL`, `DEC_DEL`, `REG_FINANC`, …) which form a *semantic* classification overlapping but not identical to the CELEX letter. Adding `type_basis: celex | resource_type | both` to the YAML — with explicit per-doc-type `resource_types` lists — would let researchers opt into broader sets (e.g. merger decisions with CELEX-type `M`, budget decisions with `B`, framework decisions, joint decisions). Default stays CELEX so existing configs reproduce the same corpus.
- **Dataset linkage layer.** Left-join helpers to enrich our `works` table with EUPROPS (manually curated text resource), EUPLEX (complexity indicators), and EUPOL (policy domain coding) via CELEX ID — combining their derived columns with our structured text for the same acts.
- **Pittsburgh Archive fallback** ([Archive of European Integration](https://aei.pitt.edu/)) as a secondary content source for documents that EUR-Lex cannot serve. The Pittsburgh archive holds digitised early-period European Community materials (1950s–1990s) that occasionally fill EUR-Lex gaps.
- **Granite Docling 258M VLM pipeline** for higher-quality scanned-PDF extraction (especially relevant for the pre-1990 corpus where Docling's default layout model struggles).
- **Fine-tuning Opus-MT** on JRC-Acquis / DGT Translation Memory for better legal-translation quality on the non-English portion of the corpus.
- **Incremental update mode** — delta runs that fetch only acts adopted since the last completed run.
- **Kreuzberg** as a faster alternative PDF backend for the cases where Docling layout-awareness isn't needed.

## License

MIT
