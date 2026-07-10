"""Export pipeline results from DuckDB to Parquet/CSV using Polars."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

logger = logging.getLogger("eurlex_builder")

TABLES = ["works", "text_units", "relations", "eurovoc"]


def export_tables(conn, output_dir: str, formats: Sequence[str]) -> None:
    """Export all data tables to the requested formats."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Enable large string buffers for tables exceeding 2GB of text.
    conn.execute("SET arrow_large_buffer_size = true")

    for table in TABLES:
        # DuckDB's native Polars export (via Arrow) for reliable type handling.
        # Empty tables are exported too (with their schema) so downstream
        # code can rely on all four files existing.
        df = conn.execute(f"SELECT * FROM {table}").pl()

        if "parquet" in formats:
            path = out / f"{table}.parquet"
            df.write_parquet(path)
            logger.info(f"Exported {table} to {path} ({len(df)} rows)")

        if "csv" in formats:
            path = out / f"{table}.csv"
            df.write_csv(path)
            logger.info(f"Exported {table} to {path} ({len(df)} rows)")
