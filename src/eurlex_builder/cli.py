"""Command-line interface for eurlex-builder."""

from __future__ import annotations

import argparse
import sys

from eurlex_builder import __version__


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="eurlex-builder",
        description="Build research-ready datasets from EU legislative data.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    # run subcommand
    run_parser = sub.add_parser("run", help="Run the pipeline with a config file.")
    run_parser.add_argument("config", help="Path to YAML configuration file.")
    run_parser.add_argument("--fresh", action="store_true", help="Ignore existing checkpoints and re-process all documents from scratch.")
    run_parser.add_argument("--retry-failed", action="store_true", help="Re-attempt previously failed documents.")

    # status subcommand
    status_parser = sub.add_parser("status", help="Show pipeline checkpoint status.")
    status_parser.add_argument("db", help="Path to DuckDB database file.")

    # translate subcommand
    translate_parser = sub.add_parser(
        "translate", help="Translate non-English text units to English."
    )
    translate_parser.add_argument("db", help="Path to DuckDB database file.")
    translate_parser.add_argument(
        "--max-full-text-chars", type=int, default=100_000,
        help="Skip full_text translation for documents longer than this "
        "(default: 100000). Text units are still translated. Set to 0 to disable.",
    )
    translate_parser.add_argument(
        "--no-full-text", action="store_true",
        help="Skip works.full_text translation entirely (only translate text_units).",
    )
    translate_parser.add_argument(
        "--no-text-units", action="store_true",
        help="Skip text_units.text translation entirely (only translate works.full_text).",
    )

    # enrich subcommand
    enrich_parser = sub.add_parser(
        "enrich", help="Enrich documents with additional SPARQL metadata."
    )
    enrich_parser.add_argument("db", help="Path to DuckDB database file.")
    enrich_parser.add_argument(
        "--select", nargs="+", default=["all"],
        choices=["all", "metadata", "relations", "eurovoc"],
        help="Categories to enrich (default: all).",
    )
    enrich_parser.add_argument("--parallel", action="store_true", help="Fetch SPARQL in parallel.")
    enrich_parser.add_argument("--max-workers", type=int, default=4, help="Number of parallel workers (default: 4).")
    enrich_parser.add_argument("--force", action="store_true", help="Re-enrich already enriched documents.")

    args = parser.parse_args(argv)

    if args.command == "run":
        _run(args.config, resume=not args.fresh, retry_failed=args.retry_failed)
    elif args.command == "status":
        _status(args.db)
    elif args.command == "translate":
        _translate(
            args.db,
            max_full_text_chars=args.max_full_text_chars,
            translate_full_text=not args.no_full_text,
            translate_text_units=not args.no_text_units,
        )
    elif args.command == "enrich":
        _enrich(args.db, select=args.select, parallel=args.parallel,
                max_workers=args.max_workers, force=args.force)
    else:
        parser.print_help()
        sys.exit(1)


def _require_db(db_path: str) -> None:
    """Exit with an error if the DuckDB file doesn't exist.

    Without this check, duckdb.connect() would silently create a new empty
    database at a mistyped path.
    """
    from pathlib import Path
    if not Path(db_path).exists():
        sys.exit(f"Error: database file not found: {db_path}")


def _run(config_path: str, *, resume: bool = False, retry_failed: bool = False) -> None:
    from eurlex_builder.pipeline import Pipeline

    pipeline = Pipeline.from_config_file(config_path)
    pipeline.run(resume=resume, retry_failed=retry_failed)


def _translate(
    db_path: str,
    *,
    max_full_text_chars: int = 100_000,
    translate_full_text: bool = True,
    translate_text_units: bool = True,
) -> None:
    _require_db(db_path)
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from eurlex_builder.translate import translate_database
    translate_database(
        db_path,
        translate_full_text=translate_full_text,
        translate_text_units=translate_text_units,
        max_full_text_chars=max_full_text_chars,
    )


def _enrich(db_path: str, *, select: list[str], parallel: bool, max_workers: int, force: bool) -> None:
    _require_db(db_path)
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from eurlex_builder.enrich import enrich_database
    categories = {"metadata", "relations", "eurovoc"} if "all" in select else set(select)
    enrich_database(db_path, categories=categories, parallel=parallel,
                    max_workers=max_workers, force=force)


def _status(db_path: str) -> None:
    _require_db(db_path)
    from eurlex_builder.storage.duckdb import DuckDBStore

    store = DuckDBStore(db_path)
    summary = store.get_summary()
    print(f"Processed: {summary.get('processed', 0)}")
    print(f"Failed:    {summary.get('failed', 0)}")
    failed = summary.get("failed_details", {})
    if failed:
        print("\nFailed documents:")
        for celex_id, error in failed.items():
            print(f"  {celex_id}: {error}")
