# Package and extraction review — 5 September 2026

Initially reviewed version: **0.1.1**, Git revision **e9ee4db**. The findings below describe that snapshot. The subsequent fixes and extended validation are recorded in the [implementation follow-up](#implementation-follow-up). The paper database was not opened, modified, or rebuilt; no release was published.

The review found a live full-text omission, discovery/control problems, and several reproducible parser edge cases. It did not establish a widespread failure of the regulation/directive/decision text-unit extraction. Most newly demonstrated structural edge cases are synthetic; their frequency in the paper corpus is unknown.

## Evidence and limits

- The existing suite passed: **265 tests**, using the project venv and Python 3.14.2. Ruff also passed.
- The configured Python-3.11 mypy check could not complete in this local environment because installed NumPy stubs use Python-3.12 syntax. Mypy with `--python-version 3.14` passed for all 23 source files. This does not replace CI checks on the supported Python versions.
- Ten live legislative HTML documents were examined: `32000D0520`, `32016R0679`, `32018L1972`, `32018R1725`, `32019L0790`, `32022D2481`, `32022L2555`, `32022R2065`, `32023R2854`, and `32024R1689`.
- For these ten, switching from article to point granularity preserved the multiset of alphabetic words of at least three characters in article bodies. This checks for content loss/addition during splitting; it does **not** establish that every paragraph or point boundary is correct, or detect text that both granularities omit.
- Three communications were also checked: `52005DC0229`, `52020DC0067`, and `52021DC0118`. Both HTML streams of the last document were inspected separately.
- Direct extraction from downloaded bytes and extraction after `_flatten_content_divs` yielded exactly equal unit dictionaries for all **14 content streams** in this sample. This supports investigating removal of the normalization round trip, but is not proof that it is unnecessary for every historical template.
- Fifteen isolated regression probes produced **12 expected-behavior assertion failures and three passing controls**, covering the issues below. These are additional review cases; they are not failures of the existing suite.
- No new live PDF conversion, translation-model evaluation, corpus census, or paper-result sensitivity analysis was performed. Existing tests exercise PDF parsing, worker failures, and translation guards.

Session-only reproductions, downloaded responses, SHA-256 hashes, and comparison results are in `/tmp/eurlex-builder-review-20260905/`. The probe file is `test_review_regressions.py`; its assertions describe desired behavior and intentionally fail against the reviewed implementation.

## Findings

### 1. `works.full_text` omits real legislative content

**Confirmed on live documents.** [`_extract_full_body`](../src/eurlex_builder/extractors/html.py#L139) visits leaf `<p>` elements only. Text directly inside table cells, list items, and other elements is not included.

- In [Copyright Directive Article 24](https://publications.europa.eu/resource/celex/32019L0790), four replacement-text fragments, totaling **1,335 normalized characters**, were absent from `full_text` and present in the extracted Article 24 unit.
- In the [AI Act](https://publications.europa.eu/resource/celex/32024R1689), the same diagnostic found **27 fragments totaling 6,842 characters**, including text in Annex I. These fragments were retained in the corresponding annex units.
- Three Annex XI headings in `32018L1972` were also omitted from full text.

These are measured missing fragments, not estimates of total extraction coverage. A minimal reproduction is `<p>Main body.</p><table><tr><td>Table content.</td></tr></table>`: full text is only `Main body.`.

**Fix direction:** collect visible text in document order across relevant element types, avoiding duplication of nested paragraphs and retaining existing exclusions for page furniture. Add a real Article 24 fixture. The issue affects document-level text analysis; the identified passages survived in the sampled structured units. The same helper supplies unstructured body fallback, which also needs regression coverage.

### 2. Empty EuroVoc matches remove the requested filter

**Confirmed with isolated discovery calls.** In [`Pipeline._resolve_ids`](../src/eurlex_builder/pipeline.py#L561), no keyword matches produce a warning followed by a query with `eurovoc_uris=None`. Rejecting every concept in interactive review has the same effect.

The resulting search covers every document matching the date/type constraints. A misspelled keyword or an explicit rejection of all concepts can therefore produce a much larger, unintended corpus.

**Fix direction:** stop with an actionable error, or return an explicitly empty selection. Omitting keyword filtering should require an empty keyword configuration. This is a small change to selection behavior and deserves high priority for external users.

### 3. `include_consolidated_texts: true` cannot discover sector-0 acts

**Confirmed by evaluating the generated query against RDF, with live Cellar metadata corroboration.** [`_build_descriptive_query`](../src/eurlex_builder/sources/cellar.py#L341) builds its allowed sectors from document-type mappings. Regulations, directives, and decisions contribute sector `3`. Enabling consolidations removes the later exclusion of CELEX IDs starting with `0`, but never adds sector `0` to the positive sector filter.

A graph containing original GDPR `32016R0679` and consolidation `02016R0679-20160504` returns only the original under either setting. Live Cellar metadata confirms that the latter has sector `0`, type `R`, and document date `2016-05-04`. This agrees with the [official CELEX sector documentation](https://eur-lex.europa.eu/content/tools/TableOfSectors/types_of_documents_in_eurlex.html).

**Fix direction:** explicitly admit relevant sector-0 works when requested, while keeping the default exclusion intact. Fixed-mode consolidated IDs are unaffected by this discovery bug.

### 4. HTML normalization can introduce mojibake

**Confirmed with two declared-encoding examples.** [`_flatten_content_divs`](../src/eurlex_builder/sources/cellar.py#L213) parses input and serializes it as UTF-8. The HTML fallback can retain an obsolete charset declaration or lose the only usable XML encoding declaration. Downstream parsing then interprets UTF-8 bytes with the wrong encoding.

Both ISO-8859-1 HTML with a charset declaration and UTF-8 HTML with an XML declaration plus an ordinary `<br>` parsed correctly before normalization. After normalization, `Considérant le traité.` became `ConsidÃ©rant le traitÃ©.`.

The earlier [document-type verification](doc-type-verification.md) already reported a mojibake symptom. This review demonstrates a concrete general mechanism; it does not establish that every previously observed instance has this cause.

**Fix direction:** centralize HTML parsing and encoding handling, and avoid unnecessary serialization between fetching and extraction. Preserve original response bytes separately when offering “raw HTML.” The fourteen unchanged live comparisons make this a promising simplification to investigate with broader historical fixtures.

### 5. The article body walker loses mixed text and crashes on comments

**Synthetic reproductions; neither trigger was found in the ten legislative samples.** In [`_collect_body_parts`](../src/eurlex_builder/extractors/html.py#L427):

- `<div>Member States <span>shall</span> report annually.</div>` inside an article yields only `shall`. Descending into the div loses its direct text and its child's tail text.
- An HTML comment directly inside an article raises `ValueError` at `etree.QName(child)`. Other extraction helpers already recognize non-element nodes, but this walker does not.

**Fix direction:** distinguish inline content from block containers when descending, preserve text/tail nodes, and ignore comments/processing instructions before constructing a QName. Ignoring non-element nodes is especially small; preserving mixed content needs tests that retain existing point boundaries.

### 6. The class-based annex parser misses the `oj-` variant

**Synthetic reproduction.** [`_extract_class_based_annexes`](../src/eurlex_builder/extractors/html.py#L801) selects `ti-grseq-1` only. Article detection and article stopping already recognize `oj-ti-art` and `oj-ti-grseq-1`.

Changing a minimal document's heading classes from `ti-art`/`ti-grseq-1` to `oj-ti-art`/`oj-ti-grseq-1` retains its article but drops Annex I. Normal article/recital counts can prevent the optional PDF retry from noticing.

**Fix direction:** handle both variants consistently for annex headings and boundaries. Keep this confined to the class-based path and add paired fixtures.

### 7. Footnotes can suppress the COM body fallback

**Synthetic reproduction.** [`HtmlExtractor.extract_com`](../src/eurlex_builder/extractors/html.py#L2099) appends recognized footnotes before deciding whether extraction was empty. If the main template is unrecognized but contains a supported footnote block, the list is nonempty and the body fallback never runs.

Adding one `<dd id="footnote1">` to a minimal communication changes its output from a body unit to a footnote-only result. The validator sees nonzero text units and does not flag the missing body.

**Fix direction:** decide whether main-body extraction succeeded separately from footnote extraction. A fallback body should exclude separately emitted footnotes to avoid duplication.

### 8. Roman subpoints can consume later lettered points

**Synthetic reproduction; no occurrence confirmed in the ten legislative samples.** [`_filter_point_sequence`](../src/eurlex_builder/extractors/splitter.py#L152) allows gaps of up to three letters. Given outer points `(a)` through `(f)`, nested `(i)/(ii)` inside `(f)`, then outer `(g)/(h)`, it accepts the nested `(i)` as an outer point. It then rejects `(g)/(h)` as backwards and includes them in that false `(i)` row.

The text survives, but structural coordinates are wrong. The same ambiguity can arise under `(g)` or `(h)`.

**Fix direction:** use sibling continuation/lookahead or retained DOM nesting to distinguish roman sublists. Preserve support for genuinely deleted lettered points and amendment insertions. This is less suitable for an immediate heuristic tweak than the class/comment fixes.

### 9. A completely failed run still returns success to the shell

**Previously documented; reconfirmed through the CLI entry point with a temporary database.** [`_run_sequential`](../src/eurlex_builder/pipeline.py#L443) records document failures, but [`_run_impl`](../src/eurlex_builder/pipeline.py#L414) and the CLI return normally. A simulated metadata failure for the only selected document produced zero work rows, four empty exports, and a `complete_with_failures` manifest without a nonzero exit.

The stored failure is useful, but automation cannot infer it from the process result. Failed checkpoints are also warnings, rather than errors, in `validate`.

**Fix direction:** propagate an explicit run outcome and return a documented nonzero exit code when selected documents fail, while retaining resumable partial results. Scope the decision to the current selection, rather than unrelated historical failed checkpoints.

## Communications and package direction

The known multipart issue remains live. The HTTP-300 response for `52021DC0118` lists an act stream and a separate annex stream. The selector takes only the act. The annex stream contains **10,585 full-text characters** when fetched separately. Its parser output also warrants closer checking before promising complete multipart coverage.

For expansion, prioritize communications, multipart retrieval, and modern COM templates. These improvements also support proposals and staff working documents. Joining streams requires explicit stream identities and ordering so repeated paragraph numbers, headings, and footnotes remain distinguishable. Simply concatenating streams is not a sufficient design.

The package's useful contribution is the combination of explicit units of analysis, historical-format handling, configurable corpus selection, relations, and research-oriented output. Existing [eurlex R documentation](https://michalovadek.github.io/eurlex/articles/eurlexpkg.html) establishes substantial retrieval functionality, and [eurlex2lexparency](https://github.com/Lexparency/eurlex2lexparency) is another relevant document-transformation project. The README's “first open-source tool” claim merits a more systematic comparison; this review does not establish priority. The research workflow is a defensible contribution independently of that claim.

Suggested order of work:

1. Fix the full-text omission and selection/exit behavior, with narrow reproductions and a small frozen set of source documents. Keep paper artifacts tied to their existing release; output-changing fixes belong in a subsequent version.
2. Consolidate HTML parsing and address the bounded DOM/class/fallback defects. Do not merge the six era parsers as an incidental cleanup: their distinct boundary rules need independent validation.
3. Add source-preservation and coverage diagnostics: exact fetched-byte hashes, an optional source cache, and reports of substantive body classes/streams that were not consumed. Avoid presenting a passing integrity check or a simple character ratio as a completeness certificate.
4. Add an explicit `export` command. Standalone `enrich` and `translate` update DuckDB, while existing Parquet/CSV files remain unchanged. Users should be able to regenerate analytical files without re-entering the run/discovery workflow.
5. Strengthen communications and multipart support before adding a new document family.

The version-specific Zenodo archive preserves the software used for the paper. It supports keeping that workflow fixed while improving future releases. This review provides no estimate of how many paper-dataset rows, if any, encounter the newly demonstrated edge cases, and makes no claim about their effect on the paper's results.

## Implementation follow-up

The user authorized the bounded fixes and requested brief consultation with
Fable 5.1 through Claude Code, with more extensive testing before centralizing
HTML parsing. Two short, tool-free consultations used `claude-fable-5-1` through
the authenticated Claude Code subscription. Fable supported the fixes and,
after receiving the comparison results, supported the narrow parser refactor
conditional on final regression checks. Its concern about exercising both
parser branches was satisfied by **71 XML and 26 HTML-fallback input/kind
combinations**. These opinions were advisory; the source comparisons determined
acceptance. Namespaces are preserved, and the source protocol was not expanded
to carry HTTP charset hints.

### Implemented changes

- One XML-first parser now serves legislative extraction, COM extraction, and
  full-text extraction. Cellar returns original HTML bytes, subject to the
  existing 50 MB guard, without unwrapping `div.content` or serializing a DOM.
  Optional `full_text_html` uses the parsed encoding to decode the source markup.
  It is diagnostic Unicode text, not an archive of exact response bytes.
- Full text visits text and tails in document order, including table cells,
  lists, headings, and mixed containers. Scripts, styles, known page headers,
  the legacy banner, and the bare CELEX page heading are excluded. The latter
  two exclusions prevented unwanted additions found during the comparison.
- Article extraction preserves mixed text and ignores comments/processing
  instructions. Existing standalone inline-marker boundaries remain intact.
- Class-based annex headings and stopping boundaries support the `oj-` prefix.
  COM fallback depends on substantive body extraction, independently of
  footnotes; separately emitted footnotes are excluded from the fallback body.
- Unmatched or wholly rejected EuroVoc selections raise an actionable error
  before resetting checkpoints. An unsuccessful discovery still records a
  failed run manifest, retaining the existing audit behavior.
- Consolidated discovery admits sector 0 for the requested legislative types.
  RDF-based query tests cover types, sectors, dates, EuroVoc, and corrigenda;
  RDFLib is a development dependency only.
- Sequential and parallel runs return `RunResult(processed, failed)` for the
  invocation. The CLI exits 1 after saving and exporting results when any
  attempted document fails. Unrelated historical failures no longer determine
  the invocation's completion status.
- Roman `(i), (ii), …` under `(f)` or `(g)` no longer consumes resumed outer
  `(g)`/`(h)` points. The change requires that resumption as evidence; ambiguous
  lists retain the previous heuristic. Synthetic cases protect genuine outer
  `(i)`, deletion gaps, and existing amendment handling.

### Extended comparison and newly confirmed impact

The regression set contains **55 downloaded Cellar HTML streams** and **42
captured fixture/input-kind combinations**. The live sample spans 1962–2024,
six languages, four consolidated texts, communications, proposals, and staff
documents. All six extraction structures occur in the combined set. Three
additional download attempts returned 404 and were excluded. Source hashes,
parser paths, settings, and result summaries are retained in the
[comparison manifest](html-review-2026-09-05.json).

Each legislative input was compared at article, paragraph, and point
granularity under all eight inclusion masks. COM-style inputs were compared
through their own extraction API, and every input through full-text extraction.
This gives **2,100 exact-output comparisons**. Both bypassing the old rewrite
and then implementing the shared parser alone produced **zero differences and
zero errors** against the preserved source snapshot.

With the text-recovery fixes included, **2,025 cases remain exactly equal**.
The **75 changed cases** comprise 27 full-text results, 12 synthetic body
fallback results, and 36 legislative results from three consolidated documents
under different settings. All old whitespace-delimited tokens remain in order
in the corresponding new output. This conservation check does not establish
complete extraction or validate every boundary.

The wider sample confirms that the mixed-text problem is substantive in some
consolidated HTML, rather than only a synthetic edge case:

| Consolidated document | Articles with restored text |
|---|---|
| `02001L0029-20190606` | 6, 7, 12 |
| `02016R0679-20160504` | 7, 26, 43, 45, 62, 76, 92, 99 |
| `01989L0391-20081211` | 8, 13, 16 |

For example, the earlier consolidated GDPR Article 99 retained the italicized
journal title but lost the surrounding entry-into-force sentence. The repaired
walker retains the complete sentence. At point granularity, Copyright Directive
Article 7(2) and Safety Directive Article 8(2) each regain the first of two
subparagraphs, replacing one previously incomplete unit with two correctly
parented units. These are the only structural-coordinate changes in the live
comparison; the article and paragraph identifiers remain unchanged.

Permanent tests include four source-attributed [HTML excerpts](../tests/fixtures/html/README.md),
eleven encoding/BOM/entity fixtures, namespace and HTML-recovery cases, direct
text and tails, comments, nested paragraphs, header exclusions, annex variants,
COM body/footnote separation, and sequential/parallel CLI-to-export outcomes.
The complete suite passes **336 tests** on Python 3.14.2; Ruff passes and mypy
passes for all 24 source files with `--python-version 3.14`. The earlier local
NumPy-stub limitation for mypy's configured Python-3.11 target remains; these
local results do not substitute for the supported-version CI matrix.

Both the wheel and source distribution build successfully. The wheel contains
the new parser and no test/review artifacts; the source archive includes all
four HTML excerpts and their provenance. Ruff and `git diff --check` pass.

The prepared 0.2.0 implementation commit `cbd20ef` subsequently passed
[GitHub CI on Python 3.11 and 3.13](https://github.com/tseidl/eurlex-builder/actions/runs/33969382950),
including the configured mypy check, tests, and builds. Both 0.2.0 distribution
artifacts also pass `twine check`. The local NumPy-stub issue did not reproduce
in the supported-version CI environments.

The paper data remain frozen. These output-affecting changes are prepared as
version 0.2.0 under the project's versioning policy; publication status is
maintained in [project-state.md](../project-state.md). Multipart
fetching, broader COM/PC/SC template coverage, exact source caching, and an
explicit export command remain separate future work. There was no new live PDF
or translation-model validation.
