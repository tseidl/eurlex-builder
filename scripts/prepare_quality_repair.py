"""Audit and prepare rejected translations and targeted extraction repairs."""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import os
from pathlib import Path

from eurlex_builder.translate import translation_quality_issue


def _sha256(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def prepare_quality_repair(
    db_path: str | Path,
    audit_path: str | Path,
    *,
    apply: bool = False,
    reprocess_celex: list[str] | None = None,
) -> tuple[int, int, int]:
    """Audit quality failures and optionally make them retryable."""
    import duckdb

    db_path = Path(db_path)
    audit_path = Path(audit_path)
    requested = set(reprocess_celex or [])
    conn = duckdb.connect(str(db_path))
    transaction_open = False
    audit_created = False
    try:
        known = {
            row[0]
            for row in conn.execute(
                "SELECT celex_id FROM works WHERE celex_id = ANY(?)",
                [sorted(requested)],
            ).fetchall()
        } if requested else set()
        unknown = requested - known
        if unknown:
            raise ValueError(f"unknown CELEX ID(s): {', '.join(sorted(unknown))}")

        records: list[dict[str, object]] = []
        work_repairs: list[str] = []
        for celex_id, source, translated in conn.execute(
            """SELECT celex_id, full_text_original, full_text
               FROM works WHERE full_text_original IS NOT NULL
               ORDER BY celex_id"""
        ).fetchall():
            issue = translation_quality_issue(source, translated, document=True)
            if issue:
                work_repairs.append(celex_id)
                records.append({
                    "scope": "full_text",
                    "celex_id": celex_id,
                    "record_id": "",
                    "reason": issue,
                    "action": "reset_full_text",
                    "source_sha256": _sha256(source),
                    "output_sha256": _sha256(translated),
                })

        unit_repairs: list[int] = []
        reprocess = set(requested)
        for unit_id, celex_id, source, translated, content_source in conn.execute(
            """SELECT t.id, t.celex_id, t.text, t.text_translated,
                      w.content_source
               FROM text_units t JOIN works w USING (celex_id)
               WHERE w.language != 'eng'
                 AND t.text IS NOT NULL AND t.text_translated IS NOT NULL
               ORDER BY t.celex_id, t.id"""
        ).fetchall():
            issue = translation_quality_issue(source, translated)
            if not issue:
                continue
            translated_parse = bool(content_source and "__translated" in content_source)
            action = "reprocess_work" if translated_parse else "reset_text_unit"
            if translated_parse:
                reprocess.add(celex_id)
            else:
                unit_repairs.append(unit_id)
            records.append({
                "scope": "text_unit",
                "celex_id": celex_id,
                "record_id": unit_id,
                "reason": issue,
                "action": action,
                "source_sha256": _sha256(source),
                "output_sha256": _sha256(translated),
            })

        existing_reprocess = {
            str(record["celex_id"])
            for record in records
            if record["action"] == "reprocess_work"
        }
        for celex_id in sorted(requested - existing_reprocess):
            records.append({
                "scope": "structure",
                "celex_id": celex_id,
                "record_id": "",
                "reason": "requested_structural_repair",
                "action": "reprocess_work",
                "source_sha256": "",
                "output_sha256": "",
            })

        summary = (len(work_repairs), len(unit_repairs), len(reprocess))
        if not apply:
            return summary
        if audit_path.exists():
            raise FileExistsError(f"refusing to overwrite repair audit: {audit_path}")

        conn.execute("BEGIN TRANSACTION")
        transaction_open = True
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_audit = audit_path.with_suffix(audit_path.suffix + ".tmp")
        try:
            with temporary_audit.open("x", encoding="utf-8", newline="") as audit:
                fieldnames = [
                    "scope", "celex_id", "record_id", "reason", "action",
                    "source_sha256", "output_sha256",
                ]
                writer = csv.DictWriter(
                    audit, fieldnames=fieldnames, delimiter="\t", lineterminator="\n",
                )
                writer.writeheader()
                writer.writerows(records)
                audit.flush()
                os.fsync(audit.fileno())
            temporary_audit.replace(audit_path)
            audit_created = True
        finally:
            temporary_audit.unlink(missing_ok=True)

        if work_repairs:
            conn.executemany(
                """UPDATE works
                   SET full_text = full_text_original, full_text_original = NULL
                   WHERE celex_id = ?""",
                [(celex_id,) for celex_id in work_repairs],
            )
        if unit_repairs:
            conn.executemany(
                "UPDATE text_units SET text_translated = NULL WHERE id = ?",
                [(unit_id,) for unit_id in unit_repairs],
            )
        if reprocess:
            conn.executemany(
                "DELETE FROM _checkpoint WHERE celex_id = ?",
                [(celex_id,) for celex_id in sorted(reprocess)],
            )
        conn.execute("COMMIT")
        transaction_open = False
        return summary
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
        description="Audit or prepare translation-quality and structural retries."
    )
    parser.add_argument("db", help="Path to eurlex_builder.duckdb")
    parser.add_argument(
        "--audit",
        help="Audit TSV path (default: quality_repair_candidates.tsv beside the DB)",
    )
    parser.add_argument(
        "--reprocess-celex",
        action="append",
        default=[],
        help="Additional CELEX ID whose extraction checkpoint should be reset.",
    )
    parser.add_argument("--apply", action="store_true", help="Apply the audited resets.")
    args = parser.parse_args()
    db_path = Path(args.db)
    audit_path = Path(args.audit) if args.audit else db_path.with_name(
        "quality_repair_candidates.tsv"
    )
    works, units, reprocess = prepare_quality_repair(
        db_path,
        audit_path,
        apply=args.apply,
        reprocess_celex=args.reprocess_celex,
    )
    action = "prepared" if args.apply else "would prepare"
    print(
        f"{action} {works} full-text translation(s), {units} text-unit "
        f"translation(s), and {reprocess} extraction checkpoint(s)"
    )
    if args.apply:
        print(f"audit: {audit_path}")


if __name__ == "__main__":
    main()
