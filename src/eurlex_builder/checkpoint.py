"""Checkpoint tracking for pipeline resumability.

The default implementation lives in storage/duckdb.py (DuckDBStore implements
both Store and Checkpoint). This module provides the protocol re-export and
could hold alternative checkpoint backends if needed.
"""

from eurlex_builder.protocols import Checkpoint

__all__ = ["Checkpoint"]
