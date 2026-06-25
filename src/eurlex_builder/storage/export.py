"""Export pipeline results from DuckDB to Parquet/CSV using Polars."""

from __future__ import annotations

import logging
from pathlib import Path

import polars as pl

logger = logging.getLogger("eurlex_builder")

TABLES = ["works", "text_units", "relations", "eurovoc"]


def export_tables(conn, output_dir: str, formats: list[str]) -> None:
    """Export all data tables to the requested formats."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Enable large string buffers for tables exceeding 2GB of text.
    conn.execute("SET arrow_large_buffer_size = true")

    for table in TABLES:
        # Use DuckDB's native Arrow export for reliable type handling.
        arrow_table = conn.execute(f"SELECT * FROM {table}").fetch_arrow_table()
        df = pl.from_arrow(arrow_table)

        if df.is_empty():
            logger.debug(f"Skipping empty table: {table}")
            continue

        if "parquet" in formats:
            path = out / f"{table}.parquet"
            df.write_parquet(path)
            logger.info(f"Exported {table} to {path} ({len(df)} rows)")

        if "csv" in formats:
            path = out / f"{table}.csv"
            df.write_csv(path)
            logger.info(f"Exported {table} to {path} ({len(df)} rows)")
