# Smoke-test of CELEX-list coverage against the {eurlex} R package (Ovadek,
# https://github.com/michalovadek/eurlex). This is NOT an extraction benchmark
# — {eurlex} can fetch raw text but does not split recitals/articles. Use it
# only to confirm that the set of CELEX IDs we have, sliced by doc-type and
# year, is in the same ballpark as what {eurlex}'s SPARQL query returns.
#
# Apples-to-apples caveats — read before interpreting the diffs:
#  • {eurlex} filters by CELEX year prefix (chars 2-5 of the CELEX ID).
#    Ours uses year(date_adopted). A doc adopted 2024-12-30 and published as
#    32025R0004 appears in ours under 2024 and in {eurlex} under 2025. These
#    are not coverage gaps; they are slice-edge differences.
#  • {eurlex}'s "decision" query uses work_has_resource-type URIs (DEC,
#    DEC_IMPL, DEC_DEL, …) which includes M-type merger decisions, B-type
#    budget acts, and sector-5 Parliament documents that our pipeline
#    intentionally excludes. Most of the Decisions 2020 "only-eurlex" set is
#    this category, not actual missing docs.
#  • A small number of CELEX values from {eurlex} come back as NA from
#    OPTIONAL bindings; we drop those before counting.
#
# Install once:
#   install.packages(c("arrow", "tidyverse", "fs", "lubridate"))
#   remotes::install_github("michalovadek/eurlex")

library(eurlex)
library(arrow)
library(dplyr)
library(tibble)
library(purrr)
library(fs)
library(lubridate)

# ---------------------------------------------------------------------------
# Load our corpus once.

ours <- read_parquet(path("output", "full-run", "works.parquet")) |>
  as_tibble() |>
  select(celex_id, document_type, date_adopted)

cat("Loaded our corpus:", nrow(ours), "works\n\n")

# ---------------------------------------------------------------------------
# One scenario at a time. {eurlex} doesn't support a date filter in the query
# itself, so we pull all CELEXes of a given resource_type and post-filter by
# the year encoded in the CELEX (chars 2–5). Slow first call per type; cache
# in memory if you re-run.

# Cache the per-type queries so repeated scenarios on the same doc_type don't
# re-hit SPARQL.
eurlex_cache <- new.env()

celex_year <- function(celex) {
  suppressWarnings(as.integer(substr(celex, 2, 5)))
}

fetch_eurlex <- function(resource_type) {
  if (!is.null(eurlex_cache[[resource_type]])) return(eurlex_cache[[resource_type]])
  cat("Fetching all", resource_type, "CELEX IDs from {eurlex} via SPARQL...\n")
  res <- elx_make_query(resource_type = resource_type) |>
    elx_run_query()
  # Drop NA CELEX rows (OPTIONAL bindings in the SPARQL query sometimes
  # produce them) so they don't inflate the counts.
  ids <- unique(res$celex)
  ids <- ids[!is.na(ids)]
  eurlex_cache[[resource_type]] <- ids
  cat("  got", length(ids), "CELEX IDs\n")
  ids
}

compare_scenario <- function(label, eurlex_type, our_type, year_) {
  eurlex_all <- fetch_eurlex(eurlex_type)
  eurlex_celex <- eurlex_all[celex_year(eurlex_all) == year_]

  ours_celex <- ours |>
    filter(
      document_type == our_type,
      year(date_adopted) == year_
    ) |>
    pull(celex_id) |>
    unique()

  both        <- intersect(eurlex_celex, ours_celex)
  only_eurlex <- setdiff(eurlex_celex, ours_celex)
  only_ours   <- setdiff(ours_celex,  eurlex_celex)

  # How many of the "only in eurlex" docs are actually IN our corpus but
  # under a different adoption-year slice? That's the slice-edge effect, not
  # a coverage gap.
  ours_all_for_type <- ours |> filter(document_type == our_type) |> pull(celex_id)
  not_in_same_slice <- intersect(only_eurlex, ours_all_for_type)

  cat("\n--- ", label, " ---\n", sep = "")
  cat("  in {eurlex}:                ", length(eurlex_celex), "\n")
  cat("  in ours:                    ", length(ours_celex),   "\n")
  cat("  in both:                    ", length(both),         "\n")
  cat("  only in eurlex (any year):  ", length(only_eurlex),  "\n")
  cat("    of which present in ours at a different year:  ",
      length(not_in_same_slice),
      "(slice-edge, not a gap)\n")
  cat("    of which truly absent from our corpus:         ",
      length(only_eurlex) - length(not_in_same_slice),
      "(may include doc-type definition diffs: M-merger, B-budget, sector-5 DP)\n")
  cat("  only in ours (any year):    ", length(only_ours),    " (typically slice-edge: adopted-but-CELEX-yr-different)\n")
  if (length(only_eurlex)) {
    cat("  sample only-in-eurlex: ", paste(head(only_eurlex, 5), collapse = ", "), "\n")
  }
  if (length(only_ours)) {
    cat("  sample only-in-ours: ",   paste(head(only_ours, 5),   collapse = ", "), "\n")
  }

  tibble(
    scenario     = label,
    doc_type     = our_type,
    year         = year_,
    in_eurlex    = length(eurlex_celex),
    in_ours      = length(ours_celex),
    in_both      = length(both),
    only_eurlex  = length(only_eurlex),
    only_ours    = length(only_ours),
    overlap_pct  = round(100 * length(both) / pmax(length(eurlex_celex), 1L), 1)
  )
}

# ---------------------------------------------------------------------------
# Three scenarios spanning eras and doc types.

results <- bind_rows(
  compare_scenario("Regulations 2024", "regulation", "regulation", 2024L),
  compare_scenario("Directives 2015",  "directive",  "directive",  2015L),
  compare_scenario("Decisions 2020",   "decision",   "decision",   2020L)
)

cat("\n=== Summary ===\n")
print(results)

# ---------------------------------------------------------------------------
# How to read the results:
#
# - overlap_pct ~100%: we have the same documents as {eurlex} for that scope.
# - only_eurlex > 0: {eurlex} lists CELEXes we don't have. Possible causes:
#     * Corrigenda (we exclude by default; check celex prefix 0 / R-type variant)
#     * Consolidated texts (sector 0; we exclude unless include_consolidated_texts: true)
#     * Recent additions to EUR-Lex since our last --fresh run
#     * Genuine fetch failures recorded in missing_content.tsv
# - only_ours > 0: we have CELEXes {eurlex} doesn't return. Usually:
#     * Different doc-type mapping (we route "implementing/delegated" variants
#       to the same document_type; {eurlex} may classify them separately)
#     * {eurlex} resource_type uses cdm:work_resource_type, ours may be wider
