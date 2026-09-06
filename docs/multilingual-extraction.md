# Official language extraction

Optionally collect official language versions into a **separate dataset**. The
command opens the existing database read-only to obtain its CELEX IDs, fetches
only the requested languages, and preserves their original text. It does not
run machine translation or change the ordinary pipeline, analytical tables,
checkpoints, configuration, or exports.

```bash
eurlex-builder multilingual output/eurlex_builder.duckdb --languages eng deu fra
```

This creates `output/eurlex_builder-multilingual/` with `multilingual.duckdb`
and three Parquet files:

| Table / export | Contents |
|---|---|
| `multilingual_documents` | One row per attempted document/language: status, resolved source URL, SHA-256, UTC retrieval time, extractor version, article/paragraph counts, and any failure or coverage explanation |
| `text_units_multilingual` | Whole articles and separately identified numbered paragraphs, with language, source anchor, structural coordinates, title and text |
| `multilingual_coverage` | Languages present and missing for each source identifier, including counts against all languages requested for this output |

The exact response bytes are retained in the database's `_multilingual_sources`
table. They are not duplicated into the Parquet/CSV exports.

| Argument | Description |
|---|---|
| `--languages` | Required list of EU language codes; all 24 official languages are accepted. Both `eng deu fra` and `en de fr` work |
| `--output-directory` | Separate output directory; defaults to `<database-name>-multilingual` beside the input database |
| `--formats` | `parquet`, `csv`, or both; defaults to `parquet` |
| `--limit N` | Process at most N remaining documents, across all requested languages; rerun to continue |
| `--retry-unavailable` | Retry unavailable or unsupported HTML; failed requests are retried automatically |

Rerunning resumes by document **and language**. You can add languages without
refetching completed versions; the output retains previously requested languages
and documents. Use a new output directory for a fresh retrieval or a different
multilingual extractor version. Request failures cause exit status 1 after
successful results are saved and exported. Unavailable or unsupported HTML is
reported separately and does not cause a nonzero exit.

This mode uses explicit HTML source identifiers, such as `art_16` and `016.007`.
It checks available language declarations and rejects conflicting language
evidence. Consolidated reference headers, when present, must also match the
requested CELEX and consolidation date. Legacy HTML without supported anchors,
unverified language labels, and PDFs are reported without guessed alignments.
An unavailable HTML response does **not** establish that no official PDF exists.
Recitals, annexes and lettered points are outside this mode.

Both whole articles and their directly anchored numbered paragraphs are stored;
filter on `type = 'article'` or `type = 'paragraph'` to avoid double-counting.
An article's `has_unanchored_text` flag indicates body text outside those
paragraphs, including unnumbered text and amendment instructions. That text
remains in the whole article. Identifiers inside quoted replacement law are
kept within the enclosing provision rather than emitted as provisions of the
amending act.

The multilingual `unit_key` combines the full CELEX (including any consolidation
date) and the source anchor. Use it to join the additional language versions;
it is **not** the analytical `text_units.unit_key`. For example, in
`multilingual.duckdb`:

```sql
SELECT en.celex_id, en.article_num, en.paragraph_num,
       en.text AS english, de.text AS german
FROM text_units_multilingual en
JOIN text_units_multilingual de USING (unit_key)
WHERE en.language = 'eng' AND de.language = 'deu'
  AND en.type = 'paragraph';
```

Matching identifiers establish a structural join, not a guarantee of semantic
equivalence or complete paragraph coverage. Inspect `multilingual_coverage`,
document statuses, and `has_unanchored_text` before treating the output as a
complete parallel corpus. Whole articles and saved source HTML support that
inspection.
