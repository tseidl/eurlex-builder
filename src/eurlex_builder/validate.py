"""Read-only integrity validation for built datasets."""

from __future__ import annotations

import hashlib
from pathlib import Path

from eurlex_builder.constants import TEXT_UNIT_IDENTITY_COLUMNS


def validate_database(db_path: str | Path) -> list[dict[str, object]]:
    """Return validation issues as severity/code/count dictionaries."""
    import duckdb

    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        return _validate_connection(conn)
    finally:
        conn.close()


def _validate_connection(conn) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    required_tables = {
        "works", "text_units", "relations", "eurovoc", "_checkpoint",
        "_enrichment_checkpoint", "dataset_runs",
    }
    for table in sorted(required_tables - tables):
        issues.append({"severity": "error", "code": "missing_table", "count": 1, "detail": table})
    if required_tables - tables:
        return issues

    text_columns = {
        row[0]
        for row in conn.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_name = 'text_units'"""
        ).fetchall()
    }
    for column in TEXT_UNIT_IDENTITY_COLUMNS:
        if column not in text_columns:
            issues.append({
                "severity": "error", "code": "missing_text_unit_column",
                "count": 1, "detail": column,
            })
    if any(issue["code"] == "missing_text_unit_column" for issue in issues):
        return issues

    checks = [
        (
            "error", "checkpoint_without_work",
            """SELECT count(*) FROM _checkpoint c LEFT JOIN works w USING (celex_id)
               WHERE c.status = 'processed' AND w.celex_id IS NULL""",
        ),
        (
            "error", "work_without_checkpoint",
            """SELECT count(*) FROM works w LEFT JOIN _checkpoint c USING (celex_id)
               WHERE c.celex_id IS NULL""",
        ),
        (
            "error", "orphan_text_unit",
            """SELECT count(*) FROM text_units t LEFT JOIN works w USING (celex_id)
               WHERE w.celex_id IS NULL""",
        ),
        (
            "error", "orphan_relation_source",
            """SELECT count(*) FROM relations r
               LEFT JOIN works w ON r.source_celex = w.celex_id
               WHERE w.celex_id IS NULL""",
        ),
        (
            "error", "orphan_eurovoc",
            """SELECT count(*) FROM eurovoc e LEFT JOIN works w USING (celex_id)
               WHERE w.celex_id IS NULL""",
        ),
        (
            "error", "orphan_enrichment_checkpoint",
            """SELECT count(*) FROM _enrichment_checkpoint e
               LEFT JOIN works w USING (celex_id) WHERE w.celex_id IS NULL""",
        ),
        (
            "error", "duplicate_relation",
            """SELECT count(*) FROM (
                   SELECT source_celex, target_celex, relation_type
                   FROM relations GROUP BY ALL HAVING count(*) > 1
               )""",
        ),
        (
            "error", "duplicate_eurovoc",
            """SELECT count(*) FROM (
                   SELECT celex_id, eurovoc_uri
                   FROM eurovoc GROUP BY ALL HAVING count(*) > 1
               )""",
        ),
        (
            "error", "duplicate_unit_key",
            """SELECT count(*) FROM (
                   SELECT unit_key FROM text_units WHERE unit_key IS NOT NULL
                   GROUP BY unit_key HAVING count(*) > 1
               )""",
        ),
        (
            "error", "missing_unit_identity",
            """SELECT count(*) FROM text_units
               WHERE unit_order IS NULL OR unit_key IS NULL""",
        ),
        (
            "error", "duplicate_unit_order",
            """SELECT count(*) FROM (
                   SELECT celex_id, unit_order FROM text_units
                   GROUP BY celex_id, unit_order HAVING count(*) > 1
               )""",
        ),
        (
            "error", "translated_fallback_marker_gap",
            """SELECT count(*) FROM text_units t JOIN works w USING (celex_id)
               WHERE w.content_source LIKE '%__translated'
                 AND t.type != 'recital'
                 AND t.text_translated IS NULL""",
        ),
        (
            "error", "pending_pdf_repair",
            """SELECT count(*) FROM works
               WHERE content_source LIKE 'repair_pending__cellar_pdf%'""",
        ),
        (
            "warning", "works_without_content",
            "SELECT count(*) FROM works WHERE full_text IS NULL OR full_text = ''",
        ),
        (
            "warning", "failed_checkpoints",
            "SELECT count(*) FROM _checkpoint WHERE status = 'failed'",
        ),
        (
            "warning", "unfinished_runs",
            "SELECT count(*) FROM dataset_runs WHERE status IN ('running', 'failed')",
        ),
        (
            "warning", "structural_body_fallback",
            """SELECT count(DISTINCT w.celex_id)
               FROM works w JOIN text_units t USING (celex_id)
               WHERE w.document_type IN ('regulation', 'directive', 'decision')
                 AND t.type = 'body'""",
        ),
    ]
    for severity, code, query in checks:
        count = conn.execute(query).fetchone()[0]
        if count:
            issues.append({"severity": severity, "code": code, "count": count})

    manifest_hash_mismatches = 0
    for config_json, config_sha256 in conn.execute(
        "SELECT config_json, config_sha256 FROM dataset_runs"
    ).fetchall():
        if (
            not isinstance(config_json, str)
            or not isinstance(config_sha256, str)
            or hashlib.sha256(config_json.encode("utf-8")).hexdigest()
            != config_sha256
        ):
            manifest_hash_mismatches += 1
    if manifest_hash_mismatches:
        issues.append({
            "severity": "error",
            "code": "manifest_hash_mismatch",
            "count": manifest_hash_mismatches,
        })
    return issues
