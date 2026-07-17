"""Audit old/new structural regressions and queue targeted re-extraction."""

from __future__ import annotations

import argparse
import contextlib
import csv
import os
from pathlib import Path


def prepare_structural_repair(
    db_path: str | Path,
    reference_path: str | Path,
    audit_path: str | Path,
    *,
    apply: bool = False,
    content_source_prefixes: tuple[str, ...] = (),
    selected_reasons: frozenset[str] = frozenset(),
) -> tuple[int, dict[str, int]]:
    """Find structural review candidates and optionally clear their checkpoints."""
    import duckdb

    db_path = Path(db_path)
    reference_path = Path(reference_path)
    audit_path = Path(audit_path)
    conn = duckdb.connect(str(db_path))
    attached = False
    transaction_open = False
    audit_created = False
    try:
        reference_sql = str(reference_path.resolve()).replace("'", "''")
        conn.execute(f"ATTACH '{reference_sql}' AS structural_reference (READ_ONLY)")
        attached = True
        rows = conn.execute(
            """WITH old_profile AS (
                   SELECT celex_id,
                          count(*) FILTER (WHERE type = 'recital') AS recitals,
                          count(DISTINCT coalesce(number, '__unnumbered__'))
                              FILTER (WHERE type = 'article') AS articles,
                          count(*) FILTER (WHERE type = 'annex') AS annexes,
                          sum(length(coalesce(text, ''))) AS unit_chars
                   FROM structural_reference.text_units GROUP BY celex_id
               ), new_profile AS (
                   SELECT celex_id,
                          count(*) FILTER (WHERE type = 'recital') AS recitals,
                          count(DISTINCT coalesce(number, '__unnumbered__'))
                              FILTER (WHERE type = 'article') AS articles,
                          count(*) FILTER (WHERE type = 'annex') AS annexes,
                          sum(length(coalesce(text, ''))) AS unit_chars,
                          count(*) FILTER (
                              WHERE type = 'recital'
                                AND trim(text) = '(' || coalesce(number, '') || ')'
                          ) AS marker_only_recitals
                   FROM text_units GROUP BY celex_id
               )
               SELECT w.celex_id, w.content_source,
                      coalesce(o.recitals, 0), coalesce(n.recitals, 0),
                      coalesce(o.articles, 0), coalesce(n.articles, 0),
                      coalesce(o.annexes, 0), coalesce(n.annexes, 0),
                      coalesce(o.unit_chars, 0), coalesce(n.unit_chars, 0),
                      coalesce(n.marker_only_recitals, 0)
               FROM works w
               JOIN structural_reference.works old_work USING (celex_id)
               LEFT JOIN old_profile o USING (celex_id)
               LEFT JOIN new_profile n USING (celex_id)
               WHERE coalesce(n.marker_only_recitals, 0) > 0
                  OR coalesce(n.articles, 0) < coalesce(o.articles, 0)
                  OR coalesce(n.annexes, 0) < coalesce(o.annexes, 0)
               ORDER BY w.celex_id"""
        ).fetchall()

        records: list[dict[str, object]] = []
        reason_counts = {
            "marker_only_recital": 0,
            "article_count_regression": 0,
            "annex_count_regression": 0,
        }
        candidates: set[str] = set()
        for row in rows:
            (
                celex_id, content_source, old_recitals, new_recitals,
                old_articles, new_articles, old_annexes, new_annexes,
                old_chars, new_chars, marker_only,
            ) = row
            if content_source_prefixes and not any(
                (content_source or "").startswith(prefix)
                for prefix in content_source_prefixes
            ):
                continue
            reasons: list[str] = []
            if marker_only:
                reasons.append("marker_only_recital")
            if new_articles < old_articles:
                reasons.append("article_count_regression")
            if new_annexes < old_annexes:
                reasons.append("annex_count_regression")
            if selected_reasons:
                reasons = [reason for reason in reasons if reason in selected_reasons]
            if not reasons:
                continue
            candidates.add(celex_id)
            for reason in reasons:
                reason_counts[reason] += 1
                records.append({
                    "celex_id": celex_id,
                    "reason": reason,
                    "content_source": content_source or "",
                    "old_recitals": old_recitals,
                    "new_recitals": new_recitals,
                    "old_articles": old_articles,
                    "new_articles": new_articles,
                    "old_annexes": old_annexes,
                    "new_annexes": new_annexes,
                    "old_unit_chars": old_chars,
                    "new_unit_chars": new_chars,
                    "marker_only_recitals": marker_only,
                })

        if not apply:
            return len(candidates), reason_counts
        if audit_path.exists():
            raise FileExistsError(f"refusing to overwrite repair audit: {audit_path}")

        conn.execute("BEGIN TRANSACTION")
        transaction_open = True
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_audit = audit_path.with_suffix(audit_path.suffix + ".tmp")
        try:
            with temporary_audit.open("x", encoding="utf-8", newline="") as audit:
                fieldnames = [
                    "celex_id", "reason", "content_source",
                    "old_recitals", "new_recitals",
                    "old_articles", "new_articles",
                    "old_annexes", "new_annexes",
                    "old_unit_chars", "new_unit_chars",
                    "marker_only_recitals",
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

        if candidates:
            conn.executemany(
                "DELETE FROM _checkpoint WHERE celex_id = ?",
                [(celex_id,) for celex_id in sorted(candidates)],
            )
        conn.execute("COMMIT")
        transaction_open = False
        return len(candidates), reason_counts
    except Exception:
        if transaction_open:
            with contextlib.suppress(Exception):
                conn.execute("ROLLBACK")
        if audit_created:
            audit_path.unlink(missing_ok=True)
        raise
    finally:
        if attached:
            conn.execute("DETACH structural_reference")
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit structural old/new regressions and queue targeted retries."
    )
    parser.add_argument("db", help="Target DuckDB database")
    parser.add_argument("reference", help="Prior reference DuckDB database")
    parser.add_argument("--audit", required=True, help="Audit TSV path")
    parser.add_argument("--apply", action="store_true", help="Clear candidate checkpoints")
    parser.add_argument(
        "--content-source-prefix",
        action="append",
        default=[],
        help="Limit candidates to a content_source prefix; repeat as needed",
    )
    parser.add_argument(
        "--reason",
        action="append",
        choices=(
            "marker_only_recital",
            "article_count_regression",
            "annex_count_regression",
        ),
        default=[],
        help="Limit candidates to a structural reason; repeat as needed",
    )
    args = parser.parse_args()
    candidates, reasons = prepare_structural_repair(
        args.db,
        args.reference,
        args.audit,
        apply=args.apply,
        content_source_prefixes=tuple(args.content_source_prefix),
        selected_reasons=frozenset(args.reason),
    )
    action = "queued" if args.apply else "would queue"
    details = ", ".join(f"{reason}={count}" for reason, count in reasons.items())
    print(f"{action} {candidates} structural candidate(s); {details}")
    if args.apply:
        print(f"audit: {args.audit}")


if __name__ == "__main__":
    main()
