"""Prepare PDF-derived checkpoints for a conservative extraction repair."""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import os
from pathlib import Path


PENDING_PREFIX = "repair_pending__"


def _sha256(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def prepare_pdf_repair(
    db_path: str | Path,
    audit_path: str | Path,
    *,
    apply: bool = False,
) -> int:
    """Audit PDF checkpoints and optionally mark them pending and reset them."""
    import duckdb

    db_path = Path(db_path)
    audit_path = Path(audit_path)
    conn = duckdb.connect(str(db_path))
    transaction_open = False
    audit_created = False
    try:
        if apply:
            conn.execute("BEGIN TRANSACTION")
            transaction_open = True
        rows = conn.execute(
            """SELECT w.celex_id, w.content_source, w.full_text,
                      count(t.id) AS text_unit_count
               FROM works w
               JOIN _checkpoint c USING (celex_id)
               LEFT JOIN text_units t USING (celex_id)
               WHERE c.status = 'processed'
                 AND w.content_source LIKE 'cellar_pdf%'
               GROUP BY w.celex_id, w.content_source, w.full_text
               ORDER BY w.celex_id"""
        ).fetchall()
        if not apply:
            return len(rows)
        if not rows:
            conn.execute("COMMIT")
            transaction_open = False
            return 0
        if audit_path.exists():
            raise FileExistsError(f"refusing to overwrite repair audit: {audit_path}")

        audit_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_audit = audit_path.with_suffix(audit_path.suffix + ".tmp")
        try:
            with temporary_audit.open("x", encoding="utf-8", newline="") as audit:
                writer = csv.writer(audit, delimiter="\t", lineterminator="\n")
                writer.writerow([
                    "celex_id", "prior_content_source", "full_text_sha256",
                    "text_unit_count",
                ])
                for celex_id, content_source, full_text, unit_count in rows:
                    writer.writerow([
                        celex_id, content_source, _sha256(full_text), unit_count,
                    ])
                audit.flush()
                os.fsync(audit.fileno())
            temporary_audit.replace(audit_path)
            audit_created = True
        finally:
            temporary_audit.unlink(missing_ok=True)

        conn.execute(
            """UPDATE works
               SET content_source = ? || content_source
               WHERE celex_id IN (
                   SELECT w.celex_id FROM works w
                   JOIN _checkpoint c USING (celex_id)
                   WHERE c.status = 'processed'
                     AND w.content_source LIKE 'cellar_pdf%'
               )""",
            [PENDING_PREFIX],
        )
        conn.execute(
            """DELETE FROM _checkpoint
               WHERE celex_id IN (
                   SELECT celex_id FROM works
                   WHERE content_source LIKE ?
               )""",
            [f"{PENDING_PREFIX}cellar_pdf%"],
        )
        remaining_row = conn.execute(
            """SELECT count(*) FROM _checkpoint c JOIN works w USING (celex_id)
               WHERE w.content_source LIKE ?""",
            [f"{PENDING_PREFIX}cellar_pdf%"],
        ).fetchone()
        assert remaining_row is not None
        remaining = remaining_row[0]
        if remaining:
            raise RuntimeError(
                f"repair preparation left {remaining} checkpoint(s) behind"
            )
        conn.execute("COMMIT")
        transaction_open = False
        return len(rows)
    except Exception:
        if transaction_open:
            with contextlib.suppress(Exception):
                conn.execute("ROLLBACK")
        if audit_created:
            audit_path.unlink(missing_ok=True)
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit or reset PDF checkpoints for isolated re-extraction."
    )
    parser.add_argument("db", help="Path to eurlex_builder.duckdb")
    parser.add_argument(
        "--audit",
        help="Audit TSV path (default: pdf_repair_candidates.tsv beside the DB)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the audit, mark candidates repair_pending, and reset checkpoints.",
    )
    args = parser.parse_args()
    db_path = Path(args.db)
    audit_path = Path(args.audit) if args.audit else db_path.with_name(
        "pdf_repair_candidates.tsv"
    )
    count = prepare_pdf_repair(db_path, audit_path, apply=args.apply)
    action = "prepared" if args.apply else "would prepare"
    print(f"{action} {count} PDF-derived checkpoint(s)")
    if args.apply and count:
        print(f"audit: {audit_path}")


if __name__ == "__main__":
    main()
