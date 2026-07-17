"""Import completed enrichment from a prior compatible dataset."""

from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path

from eurlex_builder.constants import ENRICHMENT_RELATION_TYPES


def import_enrichment(
    target_path: str | Path,
    source_path: str | Path,
    audit_path: str | Path,
    *,
    apply: bool = False,
) -> dict[str, int]:
    """Copy timestamped enrichment while retaining target base extraction."""
    import duckdb

    target_path = Path(target_path)
    source_path = Path(source_path)
    audit_path = Path(audit_path)
    conn = duckdb.connect(str(target_path))
    attached = False
    try:
        source_sql = str(source_path.resolve()).replace("'", "''")
        conn.execute(f"ATTACH '{source_sql}' AS enrichment_source (READ_ONLY)")
        attached = True
        counts = {
            "metadata_works": conn.execute(
                """SELECT count(*) FROM enrichment_source.works s
                   JOIN main.works t USING (celex_id)
                   WHERE s.enriched_at IS NOT NULL"""
            ).fetchone()[0],
            "relations": conn.execute(
                """SELECT count(*) FROM enrichment_source.relations s
                   JOIN main.works t ON t.celex_id = s.source_celex
                   WHERE s.relation_type = ANY(?)""",
                [sorted(ENRICHMENT_RELATION_TYPES)],
            ).fetchone()[0],
            "eurovoc": conn.execute(
                """SELECT count(*) FROM enrichment_source.eurovoc s
                   JOIN main.works t USING (celex_id)"""
            ).fetchone()[0],
            "checkpoints": conn.execute(
                """SELECT count(*) FROM enrichment_source._enrichment_checkpoint s
                   JOIN main.works t USING (celex_id)"""
            ).fetchone()[0],
        }
        if not apply:
            return counts
        if audit_path.exists():
            raise FileExistsError(f"refusing to overwrite import audit: {audit_path}")

        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute(
                """UPDATE main.works AS target SET
                       date_entry_into_force = source.date_entry_into_force,
                       date_end_of_validity = source.date_end_of_validity,
                       is_in_force = source.is_in_force,
                       eli = source.eli,
                       author = source.author,
                       subject_matter = source.subject_matter,
                       procedure_type = source.procedure_type,
                       procedure_reference = source.procedure_reference,
                       procedure_legal_basis = source.procedure_legal_basis,
                       enriched_at = source.enriched_at
                   FROM enrichment_source.works AS source
                   WHERE target.celex_id = source.celex_id
                     AND source.enriched_at IS NOT NULL
                     AND (target.enriched_at IS NULL
                          OR target.enriched_at < source.enriched_at)"""
            )
            conn.execute(
                "DELETE FROM main.relations WHERE relation_type = ANY(?)",
                [sorted(ENRICHMENT_RELATION_TYPES)],
            )
            conn.execute(
                """INSERT INTO main.relations
                       (id, source_celex, target_celex, relation_type)
                   SELECT nextval('relations_id_seq'), source_celex,
                          target_celex, relation_type
                   FROM enrichment_source.relations s
                   WHERE s.relation_type = ANY(?)
                     AND EXISTS (
                         SELECT 1 FROM main.works w
                         WHERE w.celex_id = s.source_celex
                     )""",
                [sorted(ENRICHMENT_RELATION_TYPES)],
            )
            conn.execute("DELETE FROM main.eurovoc")
            conn.execute(
                """INSERT INTO main.eurovoc
                       (celex_id, eurovoc_uri, eurovoc_label)
                   SELECT s.celex_id, s.eurovoc_uri, s.eurovoc_label
                   FROM enrichment_source.eurovoc s
                   WHERE EXISTS (
                       SELECT 1 FROM main.works w WHERE w.celex_id = s.celex_id
                   )"""
            )
            conn.execute(
                """INSERT OR IGNORE INTO main._enrichment_checkpoint
                       (celex_id, category, timestamp)
                   SELECT s.celex_id, s.category, s.timestamp
                   FROM enrichment_source._enrichment_checkpoint s
                   WHERE EXISTS (
                       SELECT 1 FROM main.works w WHERE w.celex_id = s.celex_id
                   )"""
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        audit_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_audit = audit_path.with_suffix(audit_path.suffix + ".tmp")
        try:
            with temporary_audit.open("x", encoding="utf-8", newline="") as audit:
                writer = csv.writer(audit, delimiter="\t", lineterminator="\n")
                writer.writerow(["source", "target", "category", "rows"])
                for category, count in counts.items():
                    writer.writerow([source_path, target_path, category, count])
                audit.flush()
                os.fsync(audit.fileno())
            temporary_audit.replace(audit_path)
        finally:
            temporary_audit.unlink(missing_ok=True)
        return counts
    finally:
        if attached:
            conn.execute("DETACH enrichment_source")
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import timestamped enrichment from a prior dataset."
    )
    parser.add_argument("target", help="Target DuckDB database")
    parser.add_argument("source", help="Prior enriched DuckDB database")
    parser.add_argument("--audit", required=True, help="Audit TSV path")
    parser.add_argument("--apply", action="store_true", help="Apply the import")
    args = parser.parse_args()
    counts = import_enrichment(
        args.target, args.source, args.audit, apply=args.apply,
    )
    action = "imported" if args.apply else "would import"
    print(action + " " + ", ".join(f"{key}={value}" for key, value in counts.items()))
    if args.apply:
        print(f"audit: {args.audit}")


if __name__ == "__main__":
    main()
