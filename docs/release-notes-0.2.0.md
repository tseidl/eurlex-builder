# eurlex-builder 0.2.0 — HTML extraction refinements and reliability fixes

This release improves handling of specific HTML layouts and makes corpus
selection and run status more explicit. It also simplifies HTML parsing while
retaining the existing extraction strategies.

Fresh runs may include text that earlier versions omitted in affected layouts,
or corrected subparagraph boundaries. Configuration fields and database schema
are unchanged.

## Extraction fixes

- Include text from table cells, lists, headings, and other elements outside
  paragraphs in `works.full_text`, retaining exclusions for known EUR-Lex page
  headers. In the initial legislative sample, the identified omitted passages
  were already present in the corresponding structured text units.
- Correct handling of article text before and after inline formatting in some
  consolidated HTML layouts.
- Centralize XML-first HTML parsing and remove the fetch-side parse/serialize
  rewrite that could corrupt declared encodings. Preserve namespaces and decode
  optional stored HTML using its detected encoding.
- Ignore comments and processing instructions in article traversal, recognize
  `oj-` annex heading variants, and retain communication bodies when footnotes
  are detected but the main template is unrecognized.
- Keep Roman subpoints under their parent when a resumed alphabetical sequence
  disambiguates the nesting. Ambiguous cases retain the existing heuristic.

## Selection and run behavior

- `include_consolidated_texts: true` now admits sector-0 documents for the
  requested legislative types.
- Keyword selection stops with an error when no EuroVoc concepts match or all
  matches are rejected. Use `filter_keywords: []` for an unfiltered date/type
  search. Failed discovery preserves existing checkpoints.
- The CLI exits with status **1** if any document attempted in the invocation
  fails, after saving and exporting successful results. Run manifests record
  `complete_with_failures`; unrelated historical failures do not determine the
  invocation's status.
- `Pipeline.run()` returns `RunResult(processed, failed)` with invocation counts.

## Validation

The local suite passes 336 tests. The HTML comparison covers 55 downloaded
Cellar streams and existing fixtures across six languages and all six
extraction structures, producing 2,100 comparisons across extraction settings.
The parser-only refactor preserves every compared output. The completed fixes
produce 75 audited differences across settings, with previously extracted words
preserved in order; multiple comparisons use the same source document.

Within this sample, article-text corrections affected 14 articles across three
consolidated documents. These counts do not estimate the frequency of such
omissions across EUR-Lex or in existing datasets. The comparison provides
regression evidence rather than a completeness guarantee for every document.

## Reproducibility

Existing datasets are not automatically rebuilt or migrated. Replication
materials should keep the package version and Zenodo version DOI used to build
their data. The paper dataset remains tied to its archived software version.
Broader multipart support and modern communication/proposal/staff-document
template coverage remain outside this release.
