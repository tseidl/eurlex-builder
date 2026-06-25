"""DuckDB-backed store and checkpoint implementation."""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

logger = logging.getLogger("eurlex_builder")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS works (
    celex_id VARCHAR PRIMARY KEY,
    title VARCHAR,
    date_adopted DATE,
    document_type VARCHAR,
    language VARCHAR DEFAULT 'eng',
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
    point_letter VARCHAR,
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
"""


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

    # --- Store protocol ---

    def save_work(self, work: dict) -> None:
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
                    work.get("language", "eng"),
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
                    work.get("language", "eng"),
                    work.get("full_text"),
                    work.get("full_text_original"),
                    work.get("full_text_html"),
                    work.get("content_source"),
                ],
            )

    def save_text_units(self, celex_id: str, units: list[dict]) -> None:
        """Insert text units for a document, replacing any existing ones.

        Persists `text_translated` when set by the extractor (e.g. the
        translate-before-extract fallback prefills it so the phase-2 translation
        step doesn't run a non-English model on already-English text).
        """
        self.conn.execute("DELETE FROM text_units WHERE celex_id = ?", [celex_id])
        for unit in units:
            self.conn.execute(
                """INSERT INTO text_units
                   (id, celex_id, type, subtype, number, paragraph_num, point_letter, title, text, text_translated)
                   VALUES (nextval('text_units_id_seq'), ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    celex_id,
                    unit.get("type"),
                    unit.get("subtype"),
                    unit.get("number"),
                    unit.get("paragraph_num"),
                    unit.get("point_letter"),
                    unit.get("title"),
                    unit.get("text"),
                    unit.get("text_translated"),
                ],
            )

    def save_eurovoc(self, celex_id: str, descriptors: list[dict]) -> None:
        """Insert EuroVoc descriptors for a document."""
        if not descriptors:
            return
        self.conn.execute("DELETE FROM eurovoc WHERE celex_id = ?", [celex_id])
        for d in descriptors:
            self.conn.execute(
                "INSERT INTO eurovoc (celex_id, eurovoc_uri, eurovoc_label) VALUES (?, ?, ?)",
                [celex_id, d.get("eurovoc_uri"), d.get("eurovoc_label")],
            )

    def save_relations(self, relations: list[dict], *, celex_id: str | None = None) -> None:
        """Insert relation records for a document.

        When celex_id is provided, deletes only that document's existing
        relations before inserting. This avoids wiping enrichment-added
        relations (repeals, implicitly_repeals) for the original act when
        processing a consolidated text that merges both sets.
        """
        if not relations:
            return
        if celex_id:
            self.conn.execute("DELETE FROM relations WHERE source_celex = ?", [celex_id])
        else:
            source_ids = list({rel.get("source_celex") for rel in relations if rel.get("source_celex")})
            for sid in source_ids:
                self.conn.execute("DELETE FROM relations WHERE source_celex = ?", [sid])
        for rel in relations:
            existing = self.conn.execute(
                "SELECT 1 FROM relations WHERE source_celex = ? AND target_celex = ? AND relation_type = ?",
                [rel.get("source_celex"), rel.get("target_celex"), rel.get("relation_type")],
            ).fetchone()
            if not existing:
                self.conn.execute(
                    """INSERT INTO relations (id, source_celex, target_celex, relation_type)
                       VALUES (nextval('relations_id_seq'), ?, ?, ?)""",
                    [rel.get("source_celex"), rel.get("target_celex"), rel.get("relation_type")],
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

    def export(self, output_dir: str, formats: list[str]) -> None:
        """Export tables to Parquet and/or CSV."""
        from eurlex_builder.storage.export import export_tables
        export_tables(self.conn, output_dir, formats)

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
        processed = self.conn.execute(
            "SELECT COUNT(*) FROM _checkpoint WHERE status = 'processed'"
        ).fetchone()[0]
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
        count = self.conn.execute(
            "SELECT COUNT(*) FROM _checkpoint WHERE status = 'failed'"
        ).fetchone()[0]
        self.conn.execute("DELETE FROM _checkpoint WHERE status = 'failed'")
        return count

    def reset(self) -> None:
        """Clear all checkpoint data."""
        self.conn.execute("DELETE FROM _checkpoint")

    def close(self) -> None:
        self.conn.close()
