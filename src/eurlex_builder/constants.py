"""Shared persistence and enrichment contracts."""

from __future__ import annotations

ENRICHMENT_CATEGORIES = frozenset({"metadata", "relations", "eurovoc"})
ENRICHMENT_RELATION_TYPES = frozenset({"repeals", "implicitly_repeals"})
TEXT_UNIT_IDENTITY_COLUMNS = ("subparagraph_num", "unit_order", "unit_key")

ENRICHMENT_CHECKPOINT_SQL = """
CREATE TABLE IF NOT EXISTS _enrichment_checkpoint (
    celex_id VARCHAR REFERENCES works(celex_id),
    category VARCHAR,
    timestamp TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (celex_id, category)
)
"""


def ensure_enrichment_checkpoint(conn) -> int:
    """Create category checkpoints and backfill legacy all-category runs once."""
    conn.execute(ENRICHMENT_CHECKPOINT_SQL)

    legacy_rows = conn.execute(
        """SELECT w.celex_id, w.enriched_at
           FROM works w
           WHERE w.enriched_at IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM _enrichment_checkpoint c
                 WHERE c.celex_id = w.celex_id
             )"""
    ).fetchall()
    if not legacy_rows:
        return 0

    rows = [
        [celex_id, category, enriched_at]
        for celex_id, enriched_at in legacy_rows
        for category in sorted(ENRICHMENT_CATEGORIES)
    ]
    conn.executemany(
        """INSERT OR IGNORE INTO _enrichment_checkpoint
           (celex_id, category, timestamp) VALUES (?, ?, ?)""",
        rows,
    )
    return len(legacy_rows)
