# Native PDF article recovery — 16 September 2026

This is an unreleased package fix for future runs. The paper dataset and its
archived software version have not been rebuilt, repaired or replaced.

This note records the initial article-marker fix and its validation. The later
[preamble and fallback follow-up](pdf-preamble-review-2026-09-17.md) extends it
with native recitals, bounded layout cleanup and English parser fixes. The
remaining-limits section below describes the initial stage, not the final
development state.

## Confirmed failure and scope

The Spanish PDF of `31982D0809` contains recognisable article headings but the
0.2.0 Markdown parser only recognised English ones. After a Docling timeout,
PyMuPDF preserved the source text and the extractor returned one body unit.
Translation was already eligible. An independent model replay rejected one
wrapped line and consequently returned no translated document; relaxing that
quality guard would accept unrelated generated text.

A fresh [official Spanish PDF](https://eur-lex.europa.eu/legal-content/ES/TXT/PDF/?uri=CELEX:31982D0809)
download produced exactly the same 129,027-character text as the saved failure
case. Its SHA-256 is
`9a39892f47dc9aef1e86b9bb02d2901f94f01ff4636bb55fc85f2fdd361f9149`.
The other five language URLs returned 404 in this check.

The fix passes the known fetch language into the PDF parser, including the
Docling/text-layer comparison. Small marker profiles cover French, German,
Italian, Dutch and Spanish: the five non-English languages already in the
ordinary fetch chain. They recognise article numbers, first/sole articles,
enacting formulae, signatures and annex boundaries. Source wording is retained.
The normal English path, HTML extraction, translation quality rules and storage
schema are unchanged.

Native headings normally occupy a whole line. Existing French same-line article
bodies remain supported. Preamble references before an available enacting
formula, lowercase references and quoted replacement law are not promoted to
outer articles. Within an annex, article headings remain attached content.
Further annex splits require a consecutive outer number sequence; ambiguous
headings stay in the enclosing annex rather than being discarded.

For `31982D0809`, this recovers articles 1 and 2 and annexes I and II. The tin
agreement's internal articles and annexes A–G stay inside annex II, whose text
is preserved completely apart from line joining. The earlier heading-renaming
probe's 262 paragraph rows mixed those levels and was not a correct target.

## Validation

- **446 tests pass**, including 71 new cases. Coverage includes the complete
  Spanish source, five official-language excerpts, all eight include-flag
  combinations at three granularities, first/sole headings, cross-references,
  quoted amendments, signatures, attached conventions and ambiguous annexes.
  A mocked timeout exercises the real fallback/provenance path. A pipeline
  replay rejects translation and verifies that native units, Spanish language
  metadata and complete source text survive. Another test verifies that the
  text layer can add missing native articles without replacing Docling's text.
  Successful translations are also checked: an improvement can be adopted,
  while lost articles, lost annexes and unchanged structure retain the native parse.
- **5,568 exact default/English-path comparisons against v0.2.0** find no
  differences: 40 existing test inputs, 190 read-only diagnostic source texts,
  and two fresh English PDF text layers, each under 24 configurations. This
  compares complete unit dictionaries, not just counts. It establishes
  unchanged behaviour for those inputs, not that all existing outputs are
  correct. The diagnostic inventory remains local and ignored.
- **13 successful official PDF downloads** were checked through the existing
  PyMuPDF text-layer helper: the Spanish failure case plus all six fetch
  languages for GDPR (`32016R0679`) and Decision `32020D1350`. Each of the five
  non-English GDPR versions yields articles 1–99 exactly, and each decision
  version yields articles 1–6 exactly. French previously missed the first
  article; the other four non-English versions had no recognised articles.
  Correct identifiers do not by themselves establish complete or clean text.
- Ruff and mypy pass (local mypy uses `--python-version 3.14`). The test runtime
  is Python 3.14.2 on macOS ARM64. Changed files also parse under Python 3.11's
  grammar, checked using Python 3.13. Full supported-version CI has not run on
  this uncommitted change; the bare local 3.13 interpreter lacks the package's
  dependencies.
- Wheel/source builds and `twine check` pass. The wheel includes the new marker
  module; the source archive includes the four public-source fixture files.
  Neither artifact includes local agent instructions or the diagnostic CSV.
  These are temporary validation builds, not published replacements for 0.2.0.

Machine-readable counts and hashes are in
[`pdf-language-validation-2026-09-16.json`](pdf-language-validation-2026-09-16.json).
Source fixtures and exact excerpt provenance are in
[`tests/fixtures/pdf`](../tests/fixtures/pdf/README.md).

## Remaining limits

The fix does not repair PDF column order. In the Spanish decision, introductory
citations still follow article 1's heading in the text layer, and the recitals
appear after the signature. Native recital extraction is deliberately outside
this change: a broader marker trial also counted footnotes as recitals.
The existing guarded translation fallback remains available for missing
structures; no full translation-model run or fresh Docling conversion was
performed for this change.

Unknown languages, OCR-damaged headings and more complicated attachment
hierarchies still need separate validation. A gap or restart in annex numbering
is retained as part of the preceding annex, so its text survives but its
classification may remain incomplete.

The live English GDPR text-layer check also exposes pre-existing false article
boundaries (108 article rows, including references to articles 263 and 267).
Both released and modified parsers produce exactly the same result. This is a
separate English fallback issue to investigate with its own regression work;
it does not establish how the normal Docling path or the paper run behaved.

Body-only counts are screening flags, not a count of this language defect or an
estimate of its effect on the paper. Existing body-only reporting remains in
place. No database, dataset export, manuscript, release tag or version DOI was
changed.
