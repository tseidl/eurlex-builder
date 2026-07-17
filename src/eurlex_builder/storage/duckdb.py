"""DuckDB-backed store and checkpoint implementation."""

from __future__ import annotations

import logging
import hashlib
import json
import platform
import subprocess
import uuid
from collections.abc import Sequence
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import duckdb

from eurlex_builder import __version__
from eurlex_builder.constants import (
    ENRICHMENT_RELATION_TYPES,
    TEXT_UNIT_IDENTITY_COLUMNS,
    ensure_enrichment_checkpoint,
)

logger = logging.getLogger("eurlex_builder")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS works (
    celex_id VARCHAR PRIMARY KEY,
    title VARCHAR,
    date_adopted DATE,
    document_type VARCHAR,
    language VARCHAR,
    full_text VARCHAR,
    full_text_original VARCHAR,
    full_text_html VARCHAR,
    content_source VARCHAR
);

CREATE TABLE IF NOT EXISTS text_units (
    id INTEGER PRIMARY KEY,
    celex_id VARCHAR REFERENCES works(celex_id),
    type VARCHAR,
    subtype VARCHAR,
    number VARCHAR,
    paragraph_num VARCHAR,
    subparagraph_num VARCHAR,
    point_letter VARCHAR,
    unit_order INTEGER,
    unit_key VARCHAR,
    title VARCHAR,
    text VARCHAR,
    text_translated VARCHAR
);

CREATE SEQUENCE IF NOT EXISTS text_units_id_seq START 1;

CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY,
    source_celex VARCHAR REFERENCES works(celex_id),
    target_celex VARCHAR,
    relation_type VARCHAR
);

CREATE SEQUENCE IF NOT EXISTS relations_id_seq START 1;

CREATE TABLE IF NOT EXISTS eurovoc (
    celex_id VARCHAR REFERENCES works(celex_id),
    eurovoc_uri VARCHAR,
    eurovoc_label VARCHAR
);

CREATE TABLE IF NOT EXISTS _checkpoint (
    celex_id VARCHAR PRIMARY KEY,
    status VARCHAR,
    error VARCHAR,
    timestamp TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS dataset_runs (
    run_id VARCHAR PRIMARY KEY,
    started_at TIMESTAMP DEFAULT current_timestamp,
    completed_at TIMESTAMP,
    status VARCHAR,
    config_json VARCHAR,
    config_sha256 VARCHAR,
    package_version VARCHAR,
    source_revision VARCHAR,
    source_dirty BOOLEAN,
    python_version VARCHAR,
    dependency_versions VARCHAR
);
"""


def _source_state() -> tuple[str | None, bool | None]:
    """Return the enclosing Git revision and whether its worktree is dirty."""
    git_dir = Path(__file__).resolve().parent
    try:
        revision = subprocess.run(
            ["git", "-C", str(git_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if revision.returncode != 0:
            return None, None
        status = subprocess.run(
            ["git", "-C", str(git_dir), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    dirty = bool(status.stdout.strip()) if status.returncode == 0 else None
    return revision.stdout.strip(), dirty


class DuckDBStore:
    """Working store and checkpoint using DuckDB.

    Implements both the Store and Checkpoint protocols.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = duckdb.connect(db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        """Create tables if they don't exist, then run migrations."""
        for statement in SCHEMA_SQL.strip().split(";"):
            stmt = statement.strip()
            if stmt:
                self.conn.execute(stmt)
        self._migrate()
        backfilled = ensure_enrichment_checkpoint(self.conn)
        if backfilled:
            logger.info(
                "Migrated legacy enrichment checkpoints for %d work(s)", backfilled
            )

    def _migrate(self) -> None:
        """Add columns that may be missing from older databases."""
        tu_cols = {
            row[0]
            for row in self.conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'text_units'"
            ).fetchall()
        }
        if "subtype" not in tu_cols:
            self.conn.execute("ALTER TABLE text_units ADD COLUMN subtype VARCHAR")
        if "paragraph_num" not in tu_cols:
            self.conn.execute("ALTER TABLE text_units ADD COLUMN paragraph_num VARCHAR")
        if "point_letter" not in tu_cols:
            self.conn.execute("ALTER TABLE text_units ADD COLUMN point_letter VARCHAR")
        identity_types = {
            "subparagraph_num": "VARCHAR",
            "unit_order": "INTEGER",
            "unit_key": "VARCHAR",
        }
        for column in TEXT_UNIT_IDENTITY_COLUMNS:
            if column not in tu_cols:
                self.conn.execute(
                    f"ALTER TABLE text_units ADD COLUMN {column} {identity_types[column]}"
                )

        # Enrichment columns on works table.
        works_cols = {
            row[0]
            for row in self.conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'works'"
            ).fetchall()
        }
        enrich_columns = {
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
        for col, sql_type in enrich_columns.items():
            if col not in works_cols:
                self.conn.execute(f"ALTER TABLE works ADD COLUMN {col} {sql_type}")

        run_cols = {
            row[0]
            for row in self.conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'dataset_runs'"
            ).fetchall()
        }
        run_columns = {
            "source_revision": "VARCHAR",
            "source_dirty": "BOOLEAN",
        }
        for col, sql_type in run_columns.items():
            if col not in run_cols:
                self.conn.execute(
                    f"ALTER TABLE dataset_runs ADD COLUMN {col} {sql_type}"
                )

    @contextmanager
    def _transaction(self):
        """BEGIN/COMMIT with rollback on error, never masking the original
        exception (a fatal error may have already aborted the transaction,
        in which case ROLLBACK itself raises)."""
        self.conn.execute("BEGIN TRANSACTION")
        try:
            yield
            self.conn.execute("COMMIT")
        except Exception:
            try:
                self.conn.execute("ROLLBACK")
            except Exception:
                logger.debug("Rollback failed — transaction already aborted")
            raise

    # --- Store protocol ---

    def save_work(
        self, work: dict, *, preserve_existing_content: bool = False,
    ) -> None:
        """Insert or update the base columns of a work record.

        Only the base columns (title, dates, types, full_text, content_source) are
        written. Enrichment columns (`date_entry_into_force`, `eli`, `author`,
        `subject_matter`, `procedure_*`, `enriched_at`) populated by the `enrich`
        command are deliberately preserved across re-runs — `INSERT OR REPLACE`
        would null them out on every re-process.
        """
        celex_id = work.get("celex_id")
        # Try UPDATE first; INSERT only if the row doesn't exist yet.
        existing = self.conn.execute(
            "SELECT 1 FROM works WHERE celex_id = ?", [celex_id]
        ).fetchone()
        if existing:
            if preserve_existing_content:
                self.conn.execute(
                    """UPDATE works SET
                           title = ?, date_adopted = ?, document_type = ?
                       WHERE celex_id = ?""",
                    [
                        work.get("title"),
                        work.get("date_adopted"),
                        work.get("document_type"),
                        celex_id,
                    ],
                )
            else:
                self.conn.execute(
                    """UPDATE works SET
                           title = ?, date_adopted = ?, document_type = ?, language = ?,
                           full_text = ?, full_text_original = ?, full_text_html = ?,
                           content_source = ?
                       WHERE celex_id = ?""",
                    [
                        work.get("title"),
                        work.get("date_adopted"),
                        work.get("document_type"),
                        work.get("language"),
                        work.get("full_text"),
                        work.get("full_text_original"),
                        work.get("full_text_html"),
                        work.get("content_source"),
                        celex_id,
                    ],
                )
        else:
            self.conn.execute(
                """INSERT INTO works
                   (celex_id, title, date_adopted, document_type, language,
                    full_text, full_text_original, full_text_html, content_source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    celex_id,
                    work.get("title"),
                    work.get("date_adopted"),
                    work.get("document_type"),
                    work.get("language"),
                    work.get("full_text"),
                    work.get("full_text_original"),
                    work.get("full_text_html"),
                    work.get("content_source"),
                ],
            )

    def save_text_units(self, celex_id: str, units: list[dict]) -> None:
        """Insert text units for a document, replacing any existing ones.

        Runs as one transaction so a crash can't leave a document half-written.
        Persists `text_translated` when set by the extractor (e.g. the
        translate-before-extract fallback prefills it so the phase-2 translation
        step doesn't run a non-English model on already-English text).
        """
        rows = []
        key_counts: dict[str, int] = {}
        for unit_order, unit in enumerate(units, start=1):
            key_base = ":".join([
                celex_id,
                str(unit.get("type") or "-"),
                str(unit.get("number") or "-"),
                str(unit.get("paragraph_num") or "-"),
                str(unit.get("subparagraph_num") or "-"),
                str(unit.get("point_letter") or "-"),
            ])
            occurrence = key_counts.get(key_base, 0) + 1
            key_counts[key_base] = occurrence
            unit_key = key_base if occurrence == 1 else f"{key_base}:{occurrence}"
            rows.append([
                celex_id,
                unit.get("type"),
                unit.get("subtype"),
                unit.get("number"),
                unit.get("paragraph_num"),
                unit.get("subparagraph_num"),
                unit.get("point_letter"),
                unit_order,
                unit_key,
                unit.get("title"),
                unit.get("text"),
                unit.get("text_translated"),
            ])
        with self._transaction():
            self.conn.execute("DELETE FROM text_units WHERE celex_id = ?", [celex_id])
            if rows:
                self.conn.executemany(
                    """INSERT INTO text_units
                       (id, celex_id, type, subtype, number, paragraph_num,
                        subparagraph_num, point_letter, unit_order, unit_key,
                        title, text, text_translated)
                       VALUES (nextval('text_units_id_seq'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    rows,
                )

    def save_eurovoc(self, celex_id: str, descriptors: list[dict]) -> None:
        """Replace EuroVoc descriptors for a document."""
        with self._transaction():
            self.conn.execute("DELETE FROM eurovoc WHERE celex_id = ?", [celex_id])
            if descriptors:
                self.conn.executemany(
                    "INSERT INTO eurovoc (celex_id, eurovoc_uri, eurovoc_label) VALUES (?, ?, ?)",
                    [[celex_id, d.get("eurovoc_uri"), d.get("eurovoc_label")] for d in descriptors],
                )

    def save_relations(self, relations: list[dict], *, celex_id: str | None = None) -> None:
        """Insert relation records for a document.

        When celex_id is provided, deletes only that document's existing
        relations before inserting. This avoids wiping enrichment-added
        relations (repeals, implicitly_repeals) for the original act when
        processing a consolidated text that merges both sets — relations whose
        source is NOT being deleted are instead deduplicated against the table.
        """
        if not relations and celex_id is None:
            return
        delete_ids: list[str]
        if celex_id:
            delete_ids = [celex_id]
        else:
            delete_ids = sorted({
                source
                for rel in relations
                if isinstance((source := rel.get("source_celex")), str)
            })

        rows: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for rel in relations:
            source = rel.get("source_celex")
            target = rel.get("target_celex")
            relation_type = rel.get("relation_type")
            if (
                not isinstance(source, str)
                or not isinstance(target, str)
                or not isinstance(relation_type, str)
            ):
                logger.warning("Skipping malformed relation: %r", rel)
                continue
            key = (source, target, relation_type)
            if key not in seen:
                seen.add(key)
                rows.append(key)

        source_ids = sorted({row[0] for row in rows if row[0]})
        if source_ids:
            placeholders = ", ".join("?" for _ in source_ids)
            existing_sources = {
                row[0]
                for row in self.conn.execute(
                    f"SELECT celex_id FROM works WHERE celex_id IN ({placeholders})",
                    source_ids,
                ).fetchall()
            }
            skipped_sources = set(source_ids) - existing_sources
            if skipped_sources:
                logger.warning(
                    "Skipping %d relation source(s) absent from works: %s",
                    len(skipped_sources), ", ".join(sorted(skipped_sources)),
                )
                rows = [row for row in rows if row[0] in existing_sources]

        other_ids = sorted({r[0] for r in rows if r[0]} - set(delete_ids))
        if other_ids:
            placeholders = ", ".join("?" for _ in other_ids)
            existing = {
                tuple(row)
                for row in self.conn.execute(
                    "SELECT source_celex, target_celex, relation_type FROM relations "
                    f"WHERE source_celex IN ({placeholders})", other_ids,
                ).fetchall()
            }
            rows = [r for r in rows if r not in existing]

        with self._transaction():
            for sid in delete_ids:
                placeholders = ", ".join("?" for _ in ENRICHMENT_RELATION_TYPES)
                self.conn.execute(
                    f"DELETE FROM relations WHERE source_celex = ? "
                    f"AND relation_type NOT IN ({placeholders})",
                    [sid, *sorted(ENRICHMENT_RELATION_TYPES)],
                )
            if rows:
                self.conn.executemany(
                    """INSERT INTO relations (id, source_celex, target_celex, relation_type)
                       VALUES (nextval('relations_id_seq'), ?, ?, ?)""",
                    [list(r) for r in rows],
                )

    def get_content_report(self) -> list[tuple]:
        """Return (celex_id, title, language, content_source) for all works."""
        return self.conn.execute(
            "SELECT celex_id, title, language, content_source FROM works"
        ).fetchall()

    def get_processed_ids(self) -> set[str]:
        """Return set of CELEX IDs that have been successfully processed."""
        result = self.conn.execute(
            "SELECT celex_id FROM _checkpoint WHERE status = 'processed'"
        ).fetchall()
        return {row[0] for row in result}

    def export(self, output_dir: str, formats: Sequence[str]) -> None:
        """Export tables to Parquet and/or CSV."""
        from eurlex_builder.storage.export import export_tables
        export_tables(self.conn, output_dir, formats)

    def start_run(self, config: dict) -> str:
        """Record the validated configuration and runtime versions for a run."""
        self.conn.execute(
            """UPDATE dataset_runs
               SET status = 'interrupted', completed_at = current_timestamp
               WHERE status = 'running'"""
        )
        config_json = json.dumps(config, sort_keys=True, separators=(",", ":"))
        dependencies = {}
        for package in (
            "eurlex-builder", "duckdb", "polars", "pyarrow", "lxml",
            "requests", "docling", "onnxruntime", "pymupdf", "transformers",
            "torch", "sacremoses",
        ):
            try:
                dependencies[package] = version(package)
            except PackageNotFoundError:
                continue

        run_id = str(uuid.uuid4())
        source_revision, source_dirty = _source_state()
        self.conn.execute(
            """INSERT INTO dataset_runs
               (run_id, status, config_json, config_sha256, package_version,
                source_revision, source_dirty, python_version, dependency_versions)
               VALUES (?, 'running', ?, ?, ?, ?, ?, ?, ?)""",
            [
                run_id,
                config_json,
                hashlib.sha256(config_json.encode("utf-8")).hexdigest(),
                dependencies.get("eurlex-builder", __version__),
                source_revision,
                source_dirty,
                platform.python_version(),
                json.dumps(dependencies, sort_keys=True),
            ],
        )
        return run_id

    def finish_run(self, run_id: str, status: str) -> None:
        """Mark a recorded run complete or failed."""
        self.conn.execute(
            """UPDATE dataset_runs
               SET completed_at = current_timestamp, status = ?
               WHERE run_id = ?""",
            [status, run_id],
        )

    # --- Checkpoint protocol ---

    def is_processed(self, celex_id: str) -> bool:
        result = self.conn.execute(
            "SELECT 1 FROM _checkpoint WHERE celex_id = ? AND status = 'processed'",
            [celex_id],
        ).fetchone()
        return result is not None

    def mark_processed(self, celex_id: str) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO _checkpoint (celex_id, status, error)
               VALUES (?, 'processed', NULL)""",
            [celex_id],
        )

    def mark_failed(self, celex_id: str, error: str) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO _checkpoint (celex_id, status, error)
               VALUES (?, 'failed', ?)""",
            [celex_id, error],
        )

    def get_summary(self) -> dict:
        """Return checkpoint summary with counts and failed details."""
        processed_row = self.conn.execute(
            "SELECT COUNT(*) FROM _checkpoint WHERE status = 'processed'"
        ).fetchone()
        processed = processed_row[0] if processed_row else 0
        failed_rows = self.conn.execute(
            "SELECT celex_id, error FROM _checkpoint WHERE status = 'failed'"
        ).fetchall()
        return {
            "processed": processed,
            "failed": len(failed_rows),
            "failed_details": {row[0]: row[1] for row in failed_rows},
        }

    def reset_failed(self) -> int:
        """Remove failed entries from checkpoint so they can be retried. Returns count removed."""
        count_row = self.conn.execute(
            "SELECT COUNT(*) FROM _checkpoint WHERE status = 'failed'"
        ).fetchone()
        count = count_row[0] if count_row else 0
        self.conn.execute("DELETE FROM _checkpoint WHERE status = 'failed'")
        return count

    def reset(self) -> None:
        """Clear all checkpoint data."""
        self.conn.execute("DELETE FROM _checkpoint")

    def reset_ids(self, celex_ids: Sequence[str]) -> int:
        """Clear checkpoints for a selected rebuild while preserving other rows."""
        ids = list(celex_ids)
        if not ids:
            return 0
        count_row = self.conn.execute(
            "SELECT count(*) FROM _checkpoint WHERE celex_id = ANY(?)", [ids]
        ).fetchone()
        count = count_row[0] if count_row else 0
        self.conn.execute(
            "DELETE FROM _checkpoint WHERE celex_id = ANY(?)", [ids]
        )
        return count

    def close(self) -> None:
        self.conn.close()
