# Current project state

Updated 5 September 2026. This is the canonical shared current-state file.

The paper dataset and its version-specific Zenodo software archive remain
frozen. Do not rebuild or repair those data as part of package maintenance.
Both package version fields now declare **0.2.0**. The September maintenance
changes and release preparation are being committed and pushed to `main` at the
user's request. Publication remains pending: no `v0.2.0` tag, GitHub Release,
PyPI upload, or Zenodo record has been created for this work.

The prepared release is **0.2.0**, to be tagged **v0.2.0**, with the title
“eurlex-builder 0.2.0 — HTML extraction refinements and reliability fixes”.
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
sensitivity. The complete suite has 336 passing tests; see the
[review and implementation evidence](docs/quality-review-2026-09-05.md#implementation-follow-up)
for scope, source provenance, and local type-check limitations.

Keep the six extraction strategies distinct. Ambiguous Roman-point nesting
retains its existing heuristic. Broader communications/proposals/staff-document
coverage, multipart Cellar streams, exact source caching, and an explicit
export command remain future work; court cases are outside this batch.
