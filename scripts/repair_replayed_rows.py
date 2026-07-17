"""Repair identical primary-key rows replayed from a stale DuckDB WAL."""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import os
from pathlib import Path


PRIMARY_KEYS = {
    "works": "celex_id",
    "text_units": "id",
    "relations": "id",
    "_checkpoint": "celex_id",
}


def _row_hash(row: tuple) -> str:
    return hashlib.sha256(repr(row).encode("utf-8")).hexdigest()


def repair_replayed_rows(
    db_path: str | Path,
    audit_path: str | Path,
    *,
    apply: bool = False,
) -> tuple[int, dict[str, int]]:
    """Audit identical duplicate keys and optionally queue affected works."""
    import duckdb

    db_path = Path(db_path)
    audit_path = Path(audit_path)
    conn = duckdb.connect(str(db_path))
    transaction_open = False
    audit_created = False
    mutation_committed = False
    try:
        duplicate_keys: dict[str, list[object]] = {}
        records: list[dict[str, object]] = []
        for table, key in PRIMARY_KEYS.items():
            keys = conn.execute(
                f"""SELECT {key}, count(*) AS multiplicity
                    FROM {table} GROUP BY {key} HAVING count(*) > 1
                    ORDER BY {key}"""
            ).fetchall()
            duplicate_keys[table] = [value for value, _ in keys]
            columns = [row[0] for row in conn.execute(f"DESCRIBE {table}").fetchall()]
            selected = ", ".join(columns)
            for value, multiplicity in keys:
                rows = conn.execute(
                    f"SELECT {selected} FROM {table} WHERE {key} = ?",
                    [value],
                ).fetchall()
                if any(row != rows[0] for row in rows[1:]):
                    raise RuntimeError(
                        f"duplicate {table}.{key}={value!r} has conflicting rows"
                    )
                records.append({
                    "table": table,
                    "key": value,
                    "multiplicity": multiplicity,
                    "row_sha256": _row_hash(rows[0]),
                })

        duplicate_work_ids = {str(value) for value in duplicate_keys["works"]}
        orphan_duplicate_units = conn.execute(
            """SELECT DISTINCT celex_id FROM text_units
               WHERE id IN (
                   SELECT id FROM text_units GROUP BY id HAVING count(*) > 1
               )"""
        ).fetchall()
        outside = {row[0] for row in orphan_duplicate_units} - duplicate_work_ids
        if outside:
            raise RuntimeError(
                "duplicate text-unit IDs exist outside duplicate works: "
                + ", ".join(sorted(outside))
            )

        counts = {table: len(keys) for table, keys in duplicate_keys.items()}
        if not apply:
            return len(duplicate_work_ids), counts
        if audit_path.exists():
            raise FileExistsError(f"refusing to overwrite repair audit: {audit_path}")

        audit_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_audit = audit_path.with_suffix(audit_path.suffix + ".tmp")
        try:
            with temporary_audit.open("x", encoding="utf-8", newline="") as audit:
                writer = csv.DictWriter(
                    audit,
                    fieldnames=["table", "key", "multiplicity", "row_sha256"],
                    delimiter="\t",
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerows(records)
                audit.flush()
                os.fsync(audit.fileno())
            temporary_audit.replace(audit_path)
            audit_created = True
        finally:
            temporary_audit.unlink(missing_ok=True)

        if duplicate_work_ids:
            values = sorted(duplicate_work_ids)
            conn.execute("BEGIN TRANSACTION")
            transaction_open = True
            conn.execute(
                "DELETE FROM _enrichment_checkpoint WHERE celex_id = ANY(?)", [values]
            )
            conn.execute("DELETE FROM eurovoc WHERE celex_id = ANY(?)", [values])
            conn.execute("DELETE FROM text_units WHERE celex_id = ANY(?)", [values])
            conn.execute(
                "DELETE FROM relations WHERE source_celex = ANY(?)", [values]
            )
            conn.execute("DELETE FROM _checkpoint WHERE celex_id = ANY(?)", [values])
            conn.execute("COMMIT")
            transaction_open = False
            mutation_committed = True

        conn.execute("BEGIN TRANSACTION")
        transaction_open = True
        if duplicate_work_ids:
            conn.execute("DELETE FROM works WHERE celex_id = ANY(?)", [values])

        for table, key in (("relations", "id"), ("_checkpoint", "celex_id")):
            conn.execute(
                f"""DELETE FROM {table} WHERE rowid IN (
                       SELECT rowid FROM (
                           SELECT rowid,
                                  row_number() OVER (PARTITION BY {key} ORDER BY rowid) AS occurrence
                           FROM {table}
                       ) WHERE occurrence > 1
                   )"""
            )

        remaining = {
            table: conn.execute(
                f"""SELECT count(*) FROM (
                       SELECT {key} FROM {table}
                       GROUP BY {key} HAVING count(*) > 1
                   )"""
            ).fetchone()[0]
            for table, key in PRIMARY_KEYS.items()
        }
        if any(remaining.values()):
            raise RuntimeError(f"duplicate keys remain after repair: {remaining}")
        conn.execute("COMMIT")
        transaction_open = False
        mutation_committed = True
        return len(duplicate_work_ids), counts
    except Exception:
        if transaction_open:
            with contextlib.suppress(Exception):
                conn.execute("ROLLBACK")
        if audit_created and not mutation_committed:
            audit_path.unlink(missing_ok=True)
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit or repair identical rows replayed from a stale DuckDB WAL."
    )
    parser.add_argument("db", help="Path to eurlex_builder.duckdb")
    parser.add_argument("--audit", required=True, help="Audit TSV path")
    parser.add_argument("--apply", action="store_true", help="Apply the audited repair")
    args = parser.parse_args()
    works, counts = repair_replayed_rows(
        args.db, args.audit, apply=args.apply,
    )
    action = "queued" if args.apply else "would queue"
    details = ", ".join(f"{table}={count}" for table, count in counts.items())
    print(f"{action} {works} duplicate work(s) for reprocessing; {details}")
    if args.apply:
        print(f"audit: {args.audit}")


if __name__ == "__main__":
    main()
