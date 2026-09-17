# Current project state

Updated 17 September 2026. This is the canonical shared current-state file.

The paper dataset and its version-specific Zenodo software archive remain
frozen. Do not rebuild or repair those data as part of package maintenance.
Both package version fields declare **0.3.0**, published on 17 September 2026
with the user's approval. Release commit/tag `v0.3.0` resolves to
`9e5e6f79cd0beb9235529f300692dd3cc0584364`.
The [GitHub release](https://github.com/tseidl/eurlex-builder/releases/tag/v0.3.0)
matches the concise [release notes](docs/release-notes-0.3.0.md).
[PyPI 0.3.0](https://pypi.org/project/eurlex-builder/0.3.0/) is published through
[Trusted Publishing](https://github.com/tseidl/eurlex-builder/actions/runs/35192893156).
The downloaded published wheel matches all source modules at the release
commit and excludes local instruction files/private inventories. Zenodo
archived the release as version DOI
[`10.5281/zenodo.22807281`](https://doi.org/10.5281/zenodo.22807281), under the
unchanged concept DOI `10.5281/zenodo.21496963`.
[Release-commit CI](https://github.com/tseidl/eurlex-builder/actions/runs/35192511874)
and [tag CI](https://github.com/tseidl/eurlex-builder/actions/runs/35192893176)
pass on Python 3.11 and 3.13, including all 555 tests, lint, configured type
checking and builds. Publication and verification are complete; no release
step remains pending. The subsequent state-note commit on `main` is docs only
and does not move the release tag or require another version.

The preceding release was **0.2.0**, published on 6 September 2026.
Tag `v0.2.0` points to `a10b75e35be7a457b6e35e2708bcc42a889ba549`.
The [GitHub release](https://github.com/tseidl/eurlex-builder/releases/tag/v0.2.0)
and [PyPI wheel and source archive](https://pypi.org/project/eurlex-builder/0.2.0/)
are public. [PyPI publishing succeeded](https://github.com/tseidl/eurlex-builder/actions/runs/34047665052).
Zenodo archived the release as version DOI
[`10.5281/zenodo.22546593`](https://doi.org/10.5281/zenodo.22546593).

The release title is
“eurlex-builder 0.2.0 — HTML refinements and optional multilingual extraction”.
It includes both the maintenance fixes and the optional multilingual command
on `main`. The user requested one combined release; the feature does not need
a separate release or branch. The release notes link issue #1 and credit the
contributor's suggestion and validation work.
[Release notes](docs/release-notes-0.2.0.md) match the published GitHub release.
Do not move the tag or replace published artifacts. Subsequent documentation
and shared-state updates on `main` do not require another release.
The user requested measured release wording: describe the affected HTML layouts
and retain the validation findings without implying a widespread extraction
failure or claiming that its overall impact is known to be small.

This file holds the shared current state. Local, Git-ignored `AGENTS.md` and
`CLAUDE.md` point here so Claude and Codex use the same current state; those
instruction files are not published to GitHub. Claude's local auto-memory index
also points here; historical memory entries are supporting context, not the
current-state authority. The README's general software citation keeps
the concept DOI without hard-coding a version; replication citations remain
tied to the version actually used.

The authorized September maintenance fixes are implemented: shared XML-first
HTML parsing without fetch-side DOM rewriting, mixed-text and full-text
recovery, comment handling, OJ annex prefix support, COM body fallback despite
footnotes, conservative Roman-point disambiguation, fail-closed EuroVoc
selection, consolidated discovery, and invocation-specific failure reporting.
`Pipeline.run()` returns `RunResult`; a document failure causes CLI exit 1 after
successful data have been saved and exported. Failed discovery preserves
checkpoints and records a failed run manifest.

Two brief Fable 5.1 consultations through Claude Code supported the bounded
changes. The parser-only refactor matched all 2,100 comparisons; the completed
fixes have 75 explained output changes and preserve previously extracted words
in order. Extended sampling confirmed restored article text in three
consolidated documents. This does not estimate prevalence or paper-result
sensitivity. The maintenance suite passed 336 tests before the multilingual
addition, and its wheel and source archive passed `twine check`.
Supported-version CI also passed; see the
[review and implementation evidence](docs/quality-review-2026-09-05.md#implementation-follow-up)
for scope, source provenance, and local type-check limitations.

Keep the six extraction strategies distinct. Ambiguous Roman-point nesting
retains its existing heuristic. Broader communications/proposals/staff-document
coverage, multipart Cellar streams, exact source caching in the ordinary
pipeline, and an explicit export command remain future work; court cases are
outside this batch.

[Issue #1](https://github.com/tseidl/eurlex-builder/issues/1) proposes optional
extraction of aligned official language versions; the contributor offered to
implement it. The user approved the opt-in direction and then requested that
we implement it now and draft a reply for approval before posting.

The implementation is on `main` and included in the published 0.2.0 release.
The separate
`eurlex-builder multilingual <db> --languages eng deu fra` command reads the
input database's CELEX list without writes. It produces a separate DuckDB
database and Parquet/CSV exports containing official HTML articles, directly
anchored numbered paragraphs, expression statuses and language coverage.
Original response bytes, hashes, URLs and language evidence are retained.
The ordinary pipeline, configuration defaults and analytical tables are
unchanged. No machine translation runs on this optional path. It resumes by
document and language; unavailable or unsupported HTML is explicit, and failed
requests cause exit 1 after successful data have been exported.

The scope is source-ID alignment in HTML. Legacy layouts without supported IDs,
PDFs, recitals, annexes and point-level alignment are outside this mode. Dated
consolidated texts can contribute articles where paragraph anchors are absent.
Quoted replacement-law identifiers stay in the enclosing amending provision.
Matching IDs do not establish text completeness or semantic equivalence.

The user regards this as a niche optional feature. The README explains it only
in the FAQ, with detailed usage in
[the multilingual extraction guide](docs/multilingual-extraction.md).

Validation adds 39 tests, including original-database byte preservation,
resumption, incomplete coverage, conflicting language labels, and six live
source excerpts. All 375 local tests pass, as do Ruff, the local Python 3.14
type check, and wheel/source builds with `twine check`. Both artifacts exclude
the local instruction files; the source archive includes the six new fixtures.
Supported Python 3.11/3.13 type checking runs in CI. A live check downloaded
20 document/language pairs across DORA, UCITS, GDPR and a dated GDPR
consolidation in English, German, Greek, Croatian and Irish. Replaying the
saved responses with the final extractor
produced 18 extracted expressions (five at article level only), one unsupported
Croatian UCITS layout, and one unavailable Irish UCITS HTML response. All
supported source-ID sets matched English; 5,754 units were retained. A resume
made no new requests and left units unchanged. See the
[validation manifest](docs/multilingual-validation-2026-09-05.json).
[Main CI](https://github.com/tseidl/eurlex-builder/actions?query=branch%3Amain)
tracks supported-version checks.

The final release commit `a10b75e` passed
[CI on Python 3.11 and 3.13](https://github.com/tseidl/eurlex-builder/actions/runs/34034314287),
including lint, configured type checking, all 375 tests and builds. Final local
wheel/source inspection and `twine check` also passed, including the README FAQ.

The user approved both publication and the final informal issue reply on
6 September. The [approved reply was posted](https://github.com/tseidl/eurlex-builder/issues/1#issuecomment-5560859339)
and verified against the draft. It thanks the contributor, explains the optional
pairing of official passages across selectable languages, links the release and
FAQ, and describes the smaller validation sample accurately. Issue #1 remains
open for feedback and has one comment. No publication or posting steps remain.
Check the issue next session and close it only if the request is resolved.
Local `AGENTS.md` instructs both tools to check open issues and relevant comments
at the start of work.

On 16 September, an inspection confirmed that the non-English PDF structure
limitation remains in 0.2.0: `_parse_legislative_markdown` recognises English
article headings, and the PDF parser and translation module are unchanged
between v0.1.1 and v0.2.0. Replaying the saved Spanish text of `31982D0809`
through a simulated Docling timeout yields one body unit. Timeout metadata
retains the text and translation is already eligible; the digital-policy
audit's model replay rejected one wrapped line, causing whole-document
translation to return no result. This session independently confirmed the
parser/timeout result and rejection control flow, without rerunning the model.

Source-language structural extraction, including signature and annex boundaries,
was the next-version fix direction; keep translation quality guards. The earlier
262-row heading-renaming probe counts paragraph rows and includes attached
convention material, so it is not a validated article count or repair. Body-only
statistics and the `structural_body_fallback` validation warning already exist.
The local ignored issue note and per-act inventory hold the snapshot counts;
those are screening flags, not a count of confirmed extraction defects. No
package code or frozen paper data changed during that initial inspection. The
historical run's exact translation failure remains unverified.

The user authorised package fixes for future runs, then extended the work to
remaining PDF limitations and newly found issues, with conservative regression
checks and optional Fable 5.1 consultations. The changes were published as
**0.3.0** with the user's subsequent approval. The preceding
published tags, packages, DOIs and frozen paper data remain untouched.

The known fetch language now reaches the PDF parser on Docling and text-layer
paths. Profiles for French, German, Italian, Dutch and Spanish recognise
articles, recital openers, enacting formulae, signatures and annex boundaries.
Attached convention articles remain inside their enclosing annex. Translation
quality guards and adoption rules are unchanged.

English PDF fixes distinguish operative headings from cross-references and
ordinary adoption prose, retain wrapped enacting formulae, recover wrapped or
spaced older recital markers, recognise a final article despite an isolated OCR
character, and keep correlation-table references within their annex. Genuine
subsequent annexes and quoted replacement law have regression coverage.

A same-PDF check now also repairs one delayed Docling heading/title when the
complete reference sequence, unique body anchor and every touched article's
full body agree. Fresh French, German, Italian and Dutch GDPR conversions had
article 74's heading after article 76, mixing bodies despite correct counts.
The bounded move restores article membership, preserves all Markdown words and
leaves every other unit unchanged; it records `__pymupdf_headings`. Ambiguous
or incomplete evidence leaves the source unchanged and records/logs a decline.
No generic sorting or body replacement is used.

A separate, bounded PyMuPDF preamble helper uses original font/position evidence
to exclude corroborated footnotes from structural parsing. It reorders only a
left preamble beside a right column that begins with the enacting formula,
requires a clear gutter and an exact line permutation, and declines quoted or
ambiguous pages. It never operates on Docling Markdown, communications, or later
operative/annex pages. Geometry/source mismatches retain the original input.
Stored `full_text` remains the unmodified text layer; successful structured
cleanup records `__recital_footnotes` and/or `__preamble_columns` after the
existing fallback suffix. If parsing still yields only the body fallback,
source text and source Markdown are retained without cleanup provenance.

Fresh Spanish `31982D0809` still has the exact saved raw-text hash. Its fallback
now yields two recitals, two decision articles and annexes I/II; the preamble
no longer contaminates article 1. The column reorder preserves every token,
and the attached agreement remains intact inside annex II. A fresh successful
Docling conversion also yields those outer structures, taking 118.35 seconds
against the unchanged 120-second conversion limit; completion timing is not
portable across machines.

The concern about GDPR was checked directly against the frozen paper Parquet
exports, read-only: its source is `cellar_html_eng`, with all 99 distinct article
identifiers (389 subdivision rows) and all 173 recitals, no missing/extra
identifiers and no empty rows. The English failure was a separately forced
PDF text-layer fallback. Fresh Docling conversions yield 99 articles and 173
recitals in all six tested languages; the corrected fallback does too.

Final validation and remaining limitations are recorded in
[the follow-up review](docs/pdf-preamble-review-2026-09-17.md) and its linked
machine-readable evidence. The earlier 446-test/5,568-unchanged-comparison
results describe the initial article-only stage, not the completed follow-up.
The follow-up checks 25 official PDFs, includes all flag/granularity combinations,
and preserves exact default-parser outputs on the separate 230-input regression
corpus. Live English PDF outputs intentionally change where defects were found.
A dedicated Python 3.13 check environment is at
`~/venvs/eurlex-builder-py313-check`; the existing working environment is unchanged.

Final checks pass 555 tests on both Python 3.13.9 and 3.14.2, Ruff, configured
type checking on 3.13 and the local 3.14 type check. All 5,520 saved-input parser
comparisons remain identical. The 25 live PDF results have identical complete
unit hashes, identifiers and cleanup provenance across PyMuPDF 1.27.2.3/1.28.2;
all 24 flag/granularity combinations pass. Eight fresh successful Docling
conversions were replayed with the final code across 192 combinations. Four
brief Fable consultations informed the final safeguards. The release parser
comparison of the two English Docling inputs has 12 explained changes in 48
comparisons: ordinary prose is restored in GDPR articles 45 and 85. There is
no remote CI result in that pre-release local validation record; release
verification is recorded separately above.
Local wheel/source builds and `twine check` pass. Artifact inspection confirms
the tested source hashes, all PDF fixtures in the source archive, and exclusion
of `AGENTS.md`, `CLAUDE.md` and the private per-act inventory. The version number
in these temporary verification builds is not a release or publication.

General column reconstruction remains unresolved. `31962R0017`, `31983D0142`
and `31995L0046` retain source-order/OCR problems in the PyMuPDF fallback; the
1995 directive now exposes all 72 recital IDs, but their order and text
membership are not fully repaired. Correct counts must not be presented as
complete extraction. No paper snapshot flags have been reclassified as
confirmed package defects beyond the independently reproduced cases.

Issue #1 was checked on 17 September: it remains open with the existing maintainer
reply and no contributor response. The unrelated Positron startup error came
from LTeX's bundled Intel Java on this ARM Mac. At the user's request, the
`valentjn.vscode-ltex` grammar extension was uninstalled and its absence verified;
no Java override was added. Reload the editor window to unload any active copy.
