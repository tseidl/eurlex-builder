# Current project state

Updated 5 September 2026. This is the canonical shared current-state file.

The paper dataset and its version-specific Zenodo software archive remain
frozen. Do not rebuild or repair those data as part of package maintenance.
Both package version fields declare **0.2.0**. The September maintenance changes
and release preparation were committed as
[`cbd20ef`](https://github.com/tseidl/eurlex-builder/commit/cbd20eff21a761c0ba6dfbe662937a7c8c22609e)
and pushed to `origin/main`. [CI passed on Python 3.11 and 3.13](https://github.com/tseidl/eurlex-builder/actions/runs/33969382950),
including lint, the configured type check, tests, and builds. Publication remains
pending: no `v0.2.0` tag, GitHub Release, PyPI upload, or Zenodo record has been
created for this work.

The prepared release is **0.2.0**, to be tagged **v0.2.0**, with the title
“eurlex-builder 0.2.0 — HTML extraction refinements and reliability fixes”.
Its target remains `main` at `5af1b03`; the optional multilingual feature is
implemented separately on `feature/official-multilingual` and must not be
included in that prepared maintenance tag.
[Release notes](docs/release-notes-0.2.0.md) are prepared. The release commit must pass
CI before its tag is pushed, because tag pushes start PyPI publication
independently of CI. Publishing the GitHub Release is the separate Zenodo step.
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
sensitivity. The complete local suite has 336 passing tests, and the 0.2.0 wheel
and source archive pass `twine check`. Supported-version CI also passes; see the
[review and implementation evidence](docs/quality-review-2026-09-05.md#implementation-follow-up)
for scope, source provenance, and local type-check limitations.

Keep the six extraction strategies distinct. Ambiguous Roman-point nesting
retains its existing heuristic. Broader communications/proposals/staff-document
coverage, multipart Cellar streams, exact source caching, and an explicit
export command remain future work; court cases are outside this batch.

[Issue #1](https://github.com/tseidl/eurlex-builder/issues/1) proposes optional
extraction of aligned official language versions; the contributor offered to
implement it. The user approved the opt-in direction and then requested that
we implement it now and draft a reply for approval before posting.

The implementation is on `feature/official-multilingual`. The separate
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

Validation adds 39 tests, including original-database byte preservation,
resumption, incomplete coverage, conflicting language labels, and six live
source excerpts. All 375 local tests pass, as do Ruff, the local Python 3.14
type check, and wheel/source builds with `twine check`. Both artifacts exclude
the local instruction files; the source archive includes the six new fixtures.
Supported Python 3.11/3.13 type checking runs in branch CI. A live check
downloaded 20 document/language pairs across
DORA, UCITS, GDPR and a dated GDPR consolidation in English, German, Greek,
Croatian and Irish. Replaying the saved responses with the final extractor
produced 18 extracted expressions (five at article level only), one unsupported
Croatian UCITS layout, and one unavailable Irish UCITS HTML response. All
supported source-ID sets matched English; 5,754 units were retained. A resume
made no new requests and left units unchanged. See the
[validation manifest](docs/multilingual-validation-2026-09-05.json).
[Branch CI](https://github.com/tseidl/eurlex-builder/actions?query=branch%3Afeature%2Fofficial-multilingual)
tracks supported-version checks.

The user wants a friendly, informal issue reply: thank the contributor, explain
the optional command plainly, credit their checks and describe our smaller
related sample accurately. Posting requires approval of the actual draft; no
reply has been posted. Keep the issue open for feedback, check it next session,
and close it only if the request is resolved. Local `AGENTS.md` now instructs
both tools to check open issues and relevant comments at the start of work.
