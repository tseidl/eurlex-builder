"""Read-only integrity validation for built datasets."""

from __future__ import annotations

import hashlib
from pathlib import Path

from eurlex_builder.constants import TEXT_UNIT_IDENTITY_COLUMNS
from eurlex_builder.translate import translation_quality_issue
from eurlex_builder.utils import COM_STYLE_DOC_TYPES, STRUCTURAL_DOC_TYPES


# Document types with an extractor branch; anything else stores full text only.
_SUPPORTED_DOC_TYPE_SQL = ", ".join(
    f"'{t}'" for t in sorted(STRUCTURAL_DOC_TYPES | COM_STYLE_DOC_TYPES)
)


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
            "error", "duplicate_work_id",
            """SELECT count(*) FROM (
                   SELECT celex_id FROM works GROUP BY celex_id HAVING count(*) > 1
               )""",
        ),
        (
            "error", "duplicate_text_unit_id",
            """SELECT count(*) FROM (
                   SELECT id FROM text_units GROUP BY id HAVING count(*) > 1
               )""",
        ),
        (
            "error", "duplicate_relation_id",
            """SELECT count(*) FROM (
                   SELECT id FROM relations GROUP BY id HAVING count(*) > 1
               )""",
        ),
        (
            "error", "duplicate_checkpoint_id",
            """SELECT count(*) FROM (
                   SELECT celex_id FROM _checkpoint
                   GROUP BY celex_id HAVING count(*) > 1
               )""",
        ),
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
            "error", "marker_only_recital",
            """SELECT count(*) FROM text_units
               WHERE type = 'recital'
                 AND trim(text) = '(' || coalesce(number, '') || ')'""",
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
            "warning", "untranslated_non_english_unit",
            """SELECT count(*) FROM text_units t JOIN works w USING (celex_id)
               WHERE w.language != 'eng'
                 AND t.text IS NOT NULL AND t.text != ''
                 AND t.text_translated IS NULL""",
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
        (
            "error", "content_without_text_units",
            """SELECT count(*) FROM works w
               WHERE w.full_text IS NOT NULL AND w.full_text != ''
                 AND NOT EXISTS (
                     SELECT 1 FROM text_units t WHERE t.celex_id = w.celex_id
                 )""",
        ),
        (
            "warning", "unextractable_document_type",
            f"""SELECT count(*) FROM works
                WHERE document_type IS NOT NULL
                  AND document_type NOT IN ({_SUPPORTED_DOC_TYPE_SQL})""",
        ),
    ]
    for severity, code, query in checks:
        count = conn.execute(query).fetchone()[0]
        if count:
            issues.append({"severity": severity, "code": code, "count": count})

    if "_translation_failures" in tables:
        translation_failures = conn.execute(
            "SELECT count(*) FROM _translation_failures"
        ).fetchone()[0]
        if translation_failures:
            issues.append({
                "severity": "warning",
                "code": "recorded_translation_rejection",
                "count": translation_failures,
            })

    rejected_works = sum(
        translation_quality_issue(source, translated, document=True) is not None
        for source, translated in conn.execute(
            """SELECT full_text_original, full_text FROM works
               WHERE full_text_original IS NOT NULL AND full_text IS NOT NULL"""
        ).fetchall()
    )
    if rejected_works:
        issues.append({
            "severity": "error",
            "code": "degenerate_full_text_translation",
            "count": rejected_works,
        })

    rejected_units = sum(
        translation_quality_issue(source, translated) is not None
        for source, translated in conn.execute(
            """SELECT t.text, t.text_translated
               FROM text_units t JOIN works w USING (celex_id)
               WHERE w.language != 'eng'
                 AND t.text IS NOT NULL AND t.text_translated IS NOT NULL"""
        ).fetchall()
    )
    if rejected_units:
        issues.append({
            "severity": "error",
            "code": "degenerate_text_unit_translation",
            "count": rejected_units,
        })

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
