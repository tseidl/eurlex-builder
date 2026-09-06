# Current project state

Updated 6 September 2026. This is the canonical shared current-state file.

The paper dataset and its version-specific Zenodo software archive remain
frozen. Do not rebuild or repair those data as part of package maintenance.
Both package version fields declare **0.2.0**, published on 6 September 2026.
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
