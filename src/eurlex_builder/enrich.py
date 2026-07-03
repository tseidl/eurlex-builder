"""Post-hoc enrichment of works with additional SPARQL metadata."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

from eurlex_builder.utils import RateLimiter

logger = logging.getLogger("eurlex_builder")

# Minimum interval between SPARQL requests (seconds), shared across all
# workers — a per-thread sleep would multiply the rate by the worker count.
_sparql_limiter = RateLimiter(0.15)

# Multi-valued scalar fields — joined with "; " when multiple values returned.
_MULTI_VALUED = {"author", "subject_matter"}

# Fields that should have their URI tail extracted as a readable label.
_URI_TAIL_FIELDS = {"author", "subject_matter", "procedure_type"}

# Relation types extracted during enrichment.
ENRICH_RELATION_TYPES = {"repeals", "implicitly_repeals"}

# Scalar metadata fields and their SQL types (for migration).
ENRICH_COLUMNS: dict[str, str] = {
    "date_entry_into_force": "DATE",
    "date_end_of_validity": "DATE",
    "is_in_force": "BOOLEAN",
    "eli": "VARCHAR",
    "author": "VARCHAR",
    "subject_matter": "VARCHAR",
    "procedure_type": "VARCHAR",
    "procedure_reference": "VARCHAR",
    "procedure_legal_basis": "VARCHAR",
    "enriched_at": "TIMESTAMP",
}

# Mapping from SPARQL data_type to works column name.
_METADATA_FIELDS = {
    "date_entry_into_force",
    "date_end_of_validity",
    "is_in_force",
    "eli",
    "author",
    "subject_matter",
    "procedure_type",
    "procedure_reference",
    "procedure_legal_basis",
}


def _extract_uri_tail(uri: str) -> str:
    """Extract readable label from authority URI (last path segment)."""
    return uri.rsplit("/", 1)[-1] if "/" in uri else uri


def _build_enrich_query(celex_id: str, categories: set[str]) -> str:
    """Build SPARQL query for enrichment fields."""
    encoded = quote(celex_id, safe="")
    blocks: list[str] = []

    if "metadata" in categories:
        blocks.extend([
            # Entry into force date
            """{ ?work cdm:resource_legal_date_entry-into-force ?value .
              BIND("date_entry_into_force" AS ?data_type) }""",
            # End of validity date
            """{ ?work cdm:resource_legal_date_end-of-validity ?value .
              BIND("date_end_of_validity" AS ?data_type) }""",
            # In-force status
            """{ ?work cdm:resource_legal_in-force ?value .
              BIND("is_in_force" AS ?data_type) }""",
            # European Legislation Identifier
            """{ ?work cdm:resource_legal_eli ?value .
              BIND("eli" AS ?data_type) }""",
            # Author institution (URI — resolved to label in Python)
            """{ ?work cdm:work_created_by_agent ?a .
              BIND("author" AS ?data_type) . BIND(STR(?a) AS ?value) }""",
            # Subject matter (URI — resolved to label in Python)
            """{ ?work cdm:resource_legal_is_about_subject-matter ?sm .
              BIND("subject_matter" AS ?data_type) . BIND(STR(?sm) AS ?value) }""",
            # Procedure type (via dossier reverse link)
            """{ ?d cdm:dossier_produces_resource_legal ?work .
              ?d cdm:procedure_code_interinstitutional_has_type_concept_type_procedure_code_interinstitutional ?pt .
              BIND("procedure_type" AS ?data_type) . BIND(STR(?pt) AS ?value) }""",
            # Procedure reference
            """{ ?d cdm:dossier_produces_resource_legal ?work .
              ?d cdm:procedure_code_interinstitutional_reference_procedure ?ref .
              BIND("procedure_reference" AS ?data_type) . BIND(STR(?ref) AS ?value) }""",
            # Procedure legal basis
            """{ ?d cdm:dossier_produces_resource_legal ?work .
              ?d cdm:procedure_code_interinstitutional_basis_legal ?bl .
              BIND("procedure_legal_basis" AS ?data_type) . BIND(STR(?bl) AS ?value) }""",
        ])

    if "relations" in categories:
        blocks.extend([
            """{ ?work cdm:resource_legal_repeals_resource_legal ?w .
              ?w cdm:resource_legal_id_celex ?value .
              BIND("repeals" AS ?data_type) }""",
            """{ ?work cdm:resource_legal_implicitly_repeals_resource_legal ?w .
              ?w cdm:resource_legal_id_celex ?value .
              BIND("implicitly_repeals" AS ?data_type) }""",
        ])

    if "eurovoc" in categories:
        blocks.append(
            """{ ?work cdm:work_is_about_concept_eurovoc ?ev .
              OPTIONAL { ?ev <http://www.w3.org/2004/02/skos/core#prefLabel> ?evlabel .
                         FILTER(LANG(?evlabel) = "en") }
              BIND("eurovoc" AS ?data_type)
              BIND(CONCAT(STR(?ev), "|", COALESCE(?evlabel, "")) AS ?value) }"""
        )

    if not blocks:
        return ""

    union = "\n    UNION\n    ".join(blocks)
    return f"""
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>

SELECT DISTINCT ?data_type ?value
WHERE {{
    ?work owl:sameAs <http://publications.europa.eu/resource/celex/{encoded}> .
    {union}
}}
"""


def _enrich_one(celex_id: str, query: str) -> dict:
    """Run enrichment SPARQL for one document, return parsed results.

    `ok` is False when the query itself failed — the caller must then skip
    the save entirely (an empty result from a FAILED query must not be
    mistaken for "this document has no descriptors").
    """
    from eurlex_builder.sources.cellar import _sparql_query, _parse_value

    result = _sparql_query(query, label=f"Enrich {celex_id}")
    if result is None:
        return {"celex_id": celex_id, "metadata": {}, "relations": [], "eurovoc": [], "ok": False}

    bindings = result.get("results", {}).get("bindings", [])

    metadata: dict[str, list] = {}
    relations: list[dict] = []
    eurovoc: list[dict] = []

    for binding in bindings:
        data_type = _parse_value(binding.get("data_type", {}))
        value = _parse_value(binding.get("value", {}))

        if data_type in _METADATA_FIELDS:
            # Convert URI-based fields to readable labels.
            if data_type in _URI_TAIL_FIELDS and isinstance(value, str):
                value = _extract_uri_tail(value)
            metadata.setdefault(data_type, []).append(value)

        elif data_type in ENRICH_RELATION_TYPES:
            relations.append({
                "source_celex": celex_id,
                "target_celex": value,
                "relation_type": data_type,
            })

        elif data_type == "eurovoc":
            parts = str(value).split("|", 1)
            eurovoc.append({
                "celex_id": celex_id,
                "eurovoc_uri": parts[0],
                "eurovoc_label": parts[1] if len(parts) > 1 else "",
            })

    # Collapse multi-valued fields to "; " joined strings, take first for single-valued.
    collapsed: dict[str, object] = {}
    for field, values in metadata.items():
        if field in _MULTI_VALUED:
            # Deduplicate while preserving order.
            seen: set = set()
            unique = []
            for v in values:
                s = str(v)
                if s not in seen:
                    seen.add(s)
                    unique.append(s)
            collapsed[field] = "; ".join(unique)
        else:
            collapsed[field] = values[0]

    return {
        "celex_id": celex_id,
        "metadata": collapsed,
        "relations": relations,
        "eurovoc": eurovoc,
        "ok": True,
    }


def enrich_database(
    db_path: str,
    *,
    categories: set[str] = frozenset({"metadata", "relations", "eurovoc"}),
    parallel: bool = False,
    max_workers: int = 4,
    force: bool = False,
) -> None:
    """Enrich all documents in a DuckDB database with additional SPARQL metadata."""
    import duckdb
    from tqdm import tqdm

    conn = duckdb.connect(db_path)

    # Ensure enrichment columns exist.
    works_cols = {
        row[0]
        for row in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'works'"
        ).fetchall()
    }
    for col, sql_type in ENRICH_COLUMNS.items():
        if col not in works_cols:
            conn.execute(f"ALTER TABLE works ADD COLUMN {col} {sql_type}")

    # Find documents to enrich.
    if force:
        celex_ids = [
            row[0] for row in conn.execute("SELECT celex_id FROM works").fetchall()
        ]
    else:
        celex_ids = [
            row[0]
            for row in conn.execute(
                "SELECT celex_id FROM works WHERE enriched_at IS NULL"
            ).fetchall()
        ]

    if not celex_ids:
        logger.info("All documents already enriched. Use --force to re-enrich.")
        conn.close()
        return

    logger.info("Enriching %d document(s) with categories: %s", len(celex_ids), ", ".join(sorted(categories)))

    # Build a template query to verify categories produce SPARQL.
    test_query = _build_enrich_query("test", categories)
    if not test_query:
        logger.warning("No enrichment categories selected.")
        conn.close()
        return

    enriched_count = 0
    rel_count = 0
    ev_count = 0

    def process_one(celex_id: str) -> dict:
        _sparql_limiter.wait()
        query = _build_enrich_query(celex_id, categories)
        return _enrich_one(celex_id, query)

    if parallel and max_workers > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(process_one, cid): cid for cid in celex_ids}
            for future in tqdm(as_completed(futures), total=len(celex_ids), desc="Enriching"):
                celex_id = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    logger.warning("Enrichment failed for %s: %s", celex_id, exc)
                    continue
                if not result["ok"]:
                    # Not marked enriched — picked up again on the next run.
                    logger.warning("Enrichment query failed for %s — skipping save.", celex_id)
                    continue
                counts = _save_enrichment(conn, result, categories)
                enriched_count += 1
                rel_count += counts[0]
                ev_count += counts[1]
    else:
        for celex_id in tqdm(celex_ids, desc="Enriching"):
            try:
                result = process_one(celex_id)
            except Exception as exc:
                logger.warning("Enrichment failed for %s: %s", celex_id, exc)
                continue
            if not result["ok"]:
                # Not marked enriched — picked up again on the next run.
                logger.warning("Enrichment query failed for %s — skipping save.", celex_id)
                continue
            counts = _save_enrichment(conn, result, categories)
            enriched_count += 1
            rel_count += counts[0]
            ev_count += counts[1]

    logger.info(
        "Enriched %d of %d document(s). Added %d relations, %d EuroVoc tags.",
        enriched_count, len(celex_ids), rel_count, ev_count,
    )
    conn.close()


def _save_enrichment(conn, result: dict, categories: set[str]) -> tuple[int, int]:
    """Write enrichment results to DB. Returns (relations_added, eurovoc_added).

    Runs as one transaction so `enriched_at` is committed atomically with the
    data writes — an interrupt can't leave a document marked enriched with
    its descriptors deleted.
    """
    celex_id = result["celex_id"]
    metadata = result["metadata"]
    relations = result["relations"]
    eurovoc = result["eurovoc"]

    rel_added = 0
    ev_added = 0

    conn.execute("BEGIN TRANSACTION")
    try:
        # Update scalar metadata columns.
        if metadata and "metadata" in categories:
            set_clauses = []
            values = []
            for field in _METADATA_FIELDS:
                if field in metadata:
                    set_clauses.append(f"{field} = ?")
                    values.append(metadata[field])
            set_clauses.append("enriched_at = current_timestamp")
            sql = f"UPDATE works SET {', '.join(set_clauses)} WHERE celex_id = ?"
            values.append(celex_id)
            conn.execute(sql, values)
        else:
            # No metadata found (or not requested), but still mark as enriched.
            conn.execute(
                "UPDATE works SET enriched_at = current_timestamp WHERE celex_id = ?",
                [celex_id],
            )

        # Insert new relation types (don't duplicate existing ones).
        if relations and "relations" in categories:
            for rel in relations:
                existing = conn.execute(
                    "SELECT 1 FROM relations WHERE source_celex = ? AND target_celex = ? AND relation_type = ?",
                    [rel["source_celex"], rel["target_celex"], rel["relation_type"]],
                ).fetchone()
                if not existing:
                    conn.execute(
                        "INSERT INTO relations (id, source_celex, target_celex, relation_type) "
                        "VALUES (nextval('relations_id_seq'), ?, ?, ?)",
                        [rel["source_celex"], rel["target_celex"], rel["relation_type"]],
                    )
                    rel_added += 1

        # Replace EuroVoc descriptors. The query succeeded (callers gate on
        # result["ok"]), so an empty list means the document genuinely has
        # none — stale rows must go too.
        if "eurovoc" in categories:
            conn.execute("DELETE FROM eurovoc WHERE celex_id = ?", [celex_id])
            if eurovoc:
                conn.executemany(
                    "INSERT INTO eurovoc (celex_id, eurovoc_uri, eurovoc_label) VALUES (?, ?, ?)",
                    [[celex_id, d["eurovoc_uri"], d["eurovoc_label"]] for d in eurovoc],
                )
                ev_added = len(eurovoc)
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            logger.debug("Rollback failed — transaction already aborted")
        raise

    return rel_added, ev_added
