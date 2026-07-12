# PDF isolation migration and repair

The persistent isolated Docling worker changes PDF output in three deliberate
cases:

- `PARTIAL_SUCCESS` is rejected and re-extracted with PyMuPDF instead of storing
  incomplete Docling markdown.
- An empty successful Docling result is retried with PyMuPDF instead of producing
  zero units.
- Every PyMuPDF-backed extraction receives a queryable
  `__pymupdf_<reason>` provenance suffix.

These are expected extraction shifts during QA, not regressions.

## Preparing the interrupted corpus

Stop the pipeline before running the repair utility. Its default mode is a
read-only count:

```bash
~/venvs/eurlex-builder/bin/python scripts/prepare_pdf_repair.py \
  output/full-run/eurlex_builder.duckdb
```

Apply the repair only after committing the worker changes:

```bash
~/venvs/eurlex-builder/bin/python scripts/prepare_pdf_repair.py \
  output/full-run/eurlex_builder.duckdb --apply
```

The apply step writes `output/full-run/pdf_repair_candidates.tsv`, prefixes
each candidate's `content_source` with `repair_pending__`, and transactionally
deletes its processed checkpoint. It leaves `works`, `text_units`, enrichment,
and relations in place. A successful fetch overwrites the marker along with the
old extraction. If the source later returns no content, the old row is preserved
but the marker remains, so the escapee cannot pass final validation silently.

The database is intentionally invalid while repair markers and missing
checkpoints remain. Do not distribute intermediate Parquet exports or interpret
a limited run's manifest status as corpus completion.

## Canary and completion

Run a bounded canary without `--fresh`:

```bash
~/venvs/eurlex-builder/bin/eurlex-builder run configs/full-run.yaml --limit 10000
```

Inspect `pipeline.log` for timeout, crash, partial, and startup-circuit events.
Compare the fallback rate with the pre-isolation timeout baseline of about 2.4%.
The default Docling device is `auto`; an extractor-only comparison can force CPU
with `EURLEX_DOCLING_DEVICE=cpu` without changing parsing semantics.

The isolated PDF worker currently requires POSIX process groups and selectable
pipe file descriptors (macOS or Linux). Windows is not supported for PDF
extraction; core HTML-only builds remain unaffected.

Resume the remaining corpus by omitting `--limit`. After the unlimited run and
enrichment complete, run:

```bash
~/venvs/eurlex-builder/bin/eurlex-builder validate \
  output/full-run/eurlex_builder.duckdb
```

`pending_pdf_repair` must be zero. Any remaining marked row was not refreshed
and must be retried or explicitly resolved before the dataset is released.
