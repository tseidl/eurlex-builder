# PDF recovery follow-up, 17 September 2026

This review records the PDF fixes and pre-release validation for **0.3.0**.
The published 0.2.0 release and the paper dataset remain unchanged. This review supersedes
the remaining-limitations section of the
[initial article-marker review](pdf-language-review-2026-09-16.md).
The [validation manifest](pdf-preamble-validation-2026-09-17.json) records
source hashes, environments, per-document results and the scope of each check.

## The English GDPR check

The PDF investigation does **not** show a failure in the paper's stored GDPR
record. A read-only check of the frozen exports found `cellar_html_eng`, all
99 distinct article identifiers (389 subdivision rows), and all 173 recital
identifiers, with no missing/extra identifiers or empty rows. This check
confirmed the reported structural validation; it was not a new word-by-word
audit of the paper dataset.

The separate forced PyMuPDF fallback did have defects: cross-references were
sometimes treated as article headings, and footnotes or ordinary adoption
prose could interfere with recital recognition. Its corrected output has
articles 1–99 and recitals 1–173 in order. No HTML extraction code changed.

## Changes and safeguards

- **Native legislative markers.** The known fetch language reaches both PDF
  paths. French, German, Italian, Dutch and Spanish profiles now recognise
  articles, recital openers, enacting formulae, signatures and annexes.
  Attached agreement articles remain within their enclosing annex. Numbering
  gaps and restarts in recitals are retained; a strict sequence rule would
  reject legitimate material. Translation quality/adoption rules are unchanged.
- **English parser corrections.** References before the main enacting formula
  cannot start operative articles. Wrapped references receive context checks,
  while a genuine bare next article remains eligible. Ordinary phrases such
  as “has adopted” are retained as prose; they do not act as embedded enacting
  formulae. Wrapped formulae, older spaced recital markers, and an isolated OCR
  character before a final article have regression coverage. Bare references
  inside an explicitly identified correlation table no longer create extra
  annexes; genuine following annexes remain eligible.
- **Footnote separation in the text-layer fallback.** A small helper uses
  [PyMuPDF's text and geometry API](https://pymupdf.readthedocs.io/en/latest/app1.html).
  It requires a numbered citation group low on a preamble page, smaller type
  than both the dominant and adjacent body text, and an identifiable journal
  citation. It handles split footnote blocks and corroborated detached
  superscripts. Normal-sized recital numbers and inline legal citations remain.
  A real PyMuPDF 1.28 fixture covers slightly overlapping glyph boxes.
- **One bounded column layout.** The helper can put a left-hand preamble before
  a right-hand column that begins with the enacting formula. It requires a
  clear gutter, source/geometry agreement, and an exact permutation of retained
  lines. It declines quotations, ambiguous bands and right-column recital
  continuation. It does not reconstruct general columns, tables or annexes.
- **One corroborated Docling heading move.** Fresh French, German, Italian and
  Dutch GDPR conversions placed the article 74 heading/title after article 76.
  Counts remained correct, but article 74's body entered article 73 and part
  of article 76 entered article 74. A separate helper moves only the delayed
  heading/title, using the same PDF's text layer as reference. It requires a
  complete unique reference sequence, a single delayed heading, an exact title,
  a unique body-start anchor between the expected neighbours, and agreement of
  **every affected article's complete body** after the candidate move.
  Comparison ignores whitespace, Markdown formatting, soft hyphens, image
  placeholders and a corroborated OJ page-header block. Original Markdown words
  are retained. Ambiguous anchors, travelling body fragments, changed paragraph
  numbers, quotation boundaries and incomplete references cause a decline.

The preamble helper applies only to the PyMuPDF legislative fallback and stops
after the first operative/signature/annex page. The heading helper applies only
to Docling legislative extraction when articles are requested. Communications
and HTML extraction are unchanged. Neither helper rewrites stored `full_text`.
If preamble cleanup still produces no requested structures, the body fallback
and its Markdown remain the original source, without cleanup provenance.

Successful preamble cleanup adds `__recital_footnotes` and/or
`__preamble_columns` after the existing `__pymupdf_<reason>` suffix.
A successful heading move adds `__pymupdf_headings`. The extraction metadata
records a heading-repair outcome; declined moves also log their reason. The
existing additive missing-article repair remains separate and can still add
`__pymupdf_articles`. There are no configuration or schema changes.

## Live-source findings

Counts below refer to outer articles, recitals and annexes, not paragraph rows.
The baseline is the released 0.2.0 parser applied to the same PDF text layer.
These selected cases are diagnostic examples, not a prevalence estimate.

| PDF case | Released fallback: recitals / articles / annexes | Corrected fallback | Check |
|---|---:|---:|---|
| Spanish `31982D0809` | Body only | 2 / 2 / 2 | Exact source hash retained; preamble separated from article 1; agreement remains inside annex II |
| English GDPR `32016R0679` | 153 / 108 / 0 | 173 / 99 / 0 | Complete identifier sequences |
| GDPR in FR/DE/IT/NL/ES | French: 0 / 98 / 0; others body only | 173 / 99 / 0 in each | Complete identifier sequences |
| `32020D1350`, six languages | English: 13 / 6 / 0; French: 0 / 5 / 0; others body only | 13 / 6 / 0 in each | Complete identifier sequences |
| English `31988R3127` | 2 / 1 / 0 | 2 / 2 / 0 | Final article restored; matches HTML identifiers |
| English `32001L0029` | 65 / 16 / 0 | 61 / 15 / 0 | Matches HTML identifiers |
| English `32010R1095` | 28 / 97 / 0 | 69 / 82 / 0 | Matches HTML identifiers |
| English `32012R1215` | 41 / 86 / 11 | 41 / 81 / 3 | Correlation references remain within their annex |
| English `32014L0065` | 170 / 101 / 8 | 170 / 97 / 4 | Matches HTML identifiers |
| English `32019L0790` | 101 / 32 / 0 | 86 / 32 / 0 | Footnotes no longer inflate recital count |
| English `32019R2088` | 52 / 20 / 0 | 35 / 20 / 0 | Large citation footnote band handled |
| English `32022R2554` | 131 / 64 / 0 | 106 / 64 / 0 | Matches HTML identifiers |

For the Spanish 1982 decision, the fresh raw text exactly matches the saved
129,027-character failure case. Its SHA-256 remains
`9a39892f47dc9aef1e86b9bb02d2901f94f01ff4636bb55fc85f2fdd361f9149`.
The column repair preserves every token. The earlier heading-renaming probe's
262 rows include paragraph subdivisions and attached convention articles;
262 was never a validated count of the decision's own articles.

Eight real Docling 2.107 conversions completed successfully: GDPR in six
languages, English `32020D1350`, and Spanish `31982D0809`. The final extraction
code was then replayed against their saved Markdown and the original PDFs.
All six GDPR results now have the complete article and recital sequences in
order, including the four repaired heading cases. Every unit outside articles
73, 74 and 76 stays identical when that heading move is applied.

The released parser already gave the correct English Docling GDPR counts.
The current prose guard also restores “has decided” in article 45 and “has
adopted” in article 85, which the earlier embedded-formula recogniser removed.
Those are the only changed article-level units in that English Markdown replay.
The English decision's saved Docling parse remains identical.

The Spanish conversion took 118.35 seconds against the unchanged 120-second
limit. That timing is machine-dependent; a timeout still gives the corrected
2-recital/2-article/2-annex text-layer fallback. Its broader attached material
does not satisfy the Docling heading-move guard, which correctly declines.

## Regression evidence

- **555 tests pass** on Python 3.13.9 and 3.14.2, including 180 additions since
  the 375-test release baseline. All tests are offline; fixtures contain public
  EUR-Lex source excerpts and geometry, not frozen dataset rows.
- **5,520 exact parser-output comparisons are unchanged:** 230 saved inputs,
  each with eight include-flag combinations and three granularities, using the
  existing default English parser API. This is
  the separate preservation corpus, not the deliberately repaired live PDFs.
- **25 official PDF/language pairs, 15 acts**, were checked with PyMuPDF
  1.27.2.3 and 1.28.2. The comparison includes output hashes, raw-source hashes,
  identifier sequences and cleanup pages. All 24 flag/granularity combinations
  were exercised per pair. The modern English cases were also checked against
  fresh official HTML identifier sequences. Matching identifiers do not prove
  exhaustive text coverage.
- On the separately sampled English PDF text layers, **162 of 336** parser
  comparisons intentionally change. For the two saved English Docling inputs,
  **12 of 48** comparisons change because the GDPR prose words above are
  retained. These are explained fixes, not claimed unchanged regressions.
- The eight saved successful Docling conversions pass all **192** include and
  granularity combinations through `PdfExtractor`, using their original PDFs.
  Fixture checks also cover article membership and unchanged surrounding units.
- Ruff, configured Python-3.11-targeted mypy on Python 3.13, the local Python
  3.14 type check, and Python 3.11 grammar parsing pass. There is no new GitHub
  CI result or Python 3.11 runtime test in this local validation record. Release
  CI is recorded separately in `project-state.md`. Wheel and source
  builds and `twine check` pass. Both artifacts contain the tested modules and
  exclude local instructions/private inventories; the source archive contains
  all PDF fixtures. Their hashes are recorded in the manifest. These are local
  verification builds, not replacements for published 0.2.0 artifacts.

Four brief Fable 5.1 consultations through Claude Code informed the safeguards.
Suggestions adopted included wrapped formula support, checking the adjacent
body font, protecting small recital labels, skipping page headers before
classifying correlation-table cells, and verifying article membership after a
heading move. A proposed strict recital sequence rule was rejected, and an
assumption about the Spanish column layout was checked against the rendered
source rather than accepted. The consultations were advisory; test and source
evidence determined the changes.

## Remaining limits

| Older English PDF fallback | Current result | Unresolved problem |
|---|---|---|
| `31962R0017` | 13 recitals, 25 article units; HTML has 24 articles | OCR and column order still produce incorrect article boundaries |
| `31983D0142` | 5 recitals, 3 articles; HTML has 4 recitals | Right-column recital continuation makes the narrow reorder unsafe; source remains unreordered |
| `31995L0046` | All 72 recital identifiers, 33 article units; HTML has 34 articles | Recital order/membership and OCR article boundaries remain incomplete |

The first and third cases improve on some markers, but are **not repaired
documents**. General column reconstruction, missing/OCR-corrupted headings,
other languages, complex attachments and more extensive Docling reading-order
damage need separate evidence and tests. The new heading guard declines when
the same-PDF reference cannot confirm every touched body. Correct counts alone
remain insufficient validation.

No frozen dataset was changed, no snapshot screening flags were globally
reclassified as confirmed defects, and no paper-estimate sensitivity was
inferred. The original translation-model rejection was not rerun; its quality
guard remains intact. The user subsequently authorised publication as 0.3.0;
published 0.2.0 artifacts must not be replaced.
