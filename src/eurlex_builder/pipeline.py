"""Main pipeline orchestrator."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from logging.handlers import RotatingFileHandler
from pathlib import Path

from tqdm import tqdm

from eurlex_builder.config import Config, DescriptiveMode, FixedMode, load_config
from eurlex_builder.errors import TransientSourceError
from eurlex_builder.protocols import Checkpoint, DataSource, Store, TextExtractor
from eurlex_builder.utils import (
    is_consolidated_celex, convert_consolidated_to_original,
    COM_STYLE_DOC_TYPES, STRUCTURAL_DOC_TYPES,
    strip_boilerplate,
)

logger = logging.getLogger("eurlex_builder")


def _structure_profile(units: list[dict]) -> tuple[int, int, int]:
    """Return recital count and distinct article/annex identifiers."""
    recitals = sum(1 for unit in units if unit.get("type") == "recital")
    articles = {
        unit.get("number") or "__unnumbered__"
        for unit in units
        if unit.get("type") == "article"
    }
    annexes = {
        unit.get("number") or "__unnumbered__"
        for unit in units
        if unit.get("type") == "annex"
    }
    return recitals, len(articles), len(annexes)


def _translated_parse_is_better(
    source_units: list[dict],
    translated_units: list[dict],
    *,
    include_recitals: bool = True,
    include_articles: bool = True,
    include_annexes: bool = True,
) -> bool:
    """Require improvement in requested structures without a regression."""
    source_recitals, source_articles, source_annexes = _structure_profile(source_units)
    translated_recitals, translated_articles, translated_annexes = _structure_profile(
        translated_units
    )
    comparisons = []
    if include_recitals:
        comparisons.append((translated_recitals, source_recitals))
    if include_articles:
        comparisons.append((translated_articles, source_articles))
    if include_annexes:
        comparisons.append((translated_annexes, source_annexes))
    return bool(comparisons) and all(new >= old for new, old in comparisons) and any(
        new > old for new, old in comparisons
    )


def _should_run_translate_fallback(
    units: list[dict], doc_type: str | None, language: str,
    *,
    include_recitals: bool = True,
    include_articles: bool = True,
    include_annexes: bool = True,
) -> bool:
    """Decide whether to translate the source markdown to English and re-extract.

    Fires when:
      - the source language is not English (no translation needed otherwise),
      - the document is legislative (R/L/D) — recitals are mandatory drafting
        elements for these doc types,
      - AND at least one requested structure is conspicuously absent.

    Three recitals is the design threshold. Missing requested articles also
    triggers the fallback because the English marker does not match several
    supported source languages. Annex-only extraction retries only when no
    annex structure was recovered.
    """
    if language == "eng":
        return False
    if doc_type not in ("regulation", "directive", "decision"):
        return False
    recitals, articles, annexes = _structure_profile(units)
    if include_recitals and recitals < 3:
        return True
    if include_articles and articles == 0:
        return True
    return include_annexes and not include_recitals and not include_articles and annexes == 0


def _safe_translate_markdown(markdown: str, language: str, celex_id: str) -> str | None:
    """Wrap translate_markdown with graceful failure for missing deps / models."""
    try:
        from eurlex_builder.translate import translate_markdown
    except ImportError:
        logger.warning(
            "Translation not available (install eurlex-builder[translate]) — "
            "skipping fallback for %s", celex_id,
        )
        return None
    try:
        return translate_markdown(markdown, language)
    except Exception as exc:  # noqa: BLE001 — translation never blocks extraction
        logger.warning("Translation failed for %s: %s", celex_id, exc)
        return None


class Pipeline:
    """Orchestrates document fetching, extraction, and storage."""

    def __init__(
        self,
        config: Config,
        source: DataSource,
        extractors: list[TextExtractor],
        store: Store,
        checkpoint: Checkpoint,
    ):
        self.config = config
        self.source = source
        self.extractors = extractors
        self.store = store
        self.checkpoint = checkpoint
        self._selected_ids: set[str] = set()

    @classmethod
    def from_config_file(cls, path: str | Path) -> Pipeline:
        """Create a pipeline from a YAML config file with default wiring."""
        config = load_config(path)

        # Import here to avoid circular imports
        from eurlex_builder.sources.cellar import CellarSource
        from eurlex_builder.extractors.html import HtmlExtractor
        from eurlex_builder.extractors.pdf import PdfExtractor
        from eurlex_builder.storage.duckdb import DuckDBStore

        output_dir = Path(config.output.output_directory)
        output_dir.mkdir(parents=True, exist_ok=True)

        db_path = output_dir / "eurlex_builder.duckdb"
        store = DuckDBStore(str(db_path))

        return cls(
            config=config,
            source=CellarSource(),
            extractors=[HtmlExtractor(), PdfExtractor()],
            store=store,
            checkpoint=store,  # DuckDBStore implements both Store and Checkpoint
        )

    def run(self, *, resume: bool = False, retry_failed: bool = False) -> None:
        """Run the pipeline."""
        self._setup_logging()
        run_id: str | None = None
        try:
            run_id = self.store.start_run(self.config.model_dump(mode="json"))
            self._run_impl(
                run_id=run_id, resume=resume, retry_failed=retry_failed,
            )
        except BaseException:
            if run_id is not None:
                try:
                    self.store.finish_run(run_id, "failed")
                except Exception:
                    logger.exception("Could not mark run %s as failed", run_id)
            raise
        finally:
            if hasattr(self.store, "close"):
                self.store.close()

    def _run_impl(
        self, *, run_id: str, resume: bool, retry_failed: bool,
    ) -> None:
        logger.info(f"eurlex-builder v0.1.0 — {self.config.metadata.project_name}")

        if retry_failed:
            reset_count = self.checkpoint.reset_failed()
            if reset_count:
                logger.info(f"Reset {reset_count} failed document(s) for retry.")
            resume = True

        celex_ids = self._resolve_ids()
        self._selected_ids = set(celex_ids)
        logger.info(f"Found {len(celex_ids)} documents to process.")

        if not resume:
            reset_count = self.checkpoint.reset_ids(celex_ids)
            logger.info(
                "Fresh rebuild cleared %d selected checkpoint(s).", reset_count
            )
        else:
            already_done = self.store.get_processed_ids()
            celex_ids = [celex_id for celex_id in celex_ids if celex_id not in already_done]
            logger.info(f"Resuming — {len(celex_ids)} remaining after skipping processed.")

        if not celex_ids:
            logger.info("Nothing to process.")
        elif self.config.processing.parallel and len(celex_ids) > 1:
            self._run_parallel(celex_ids)
        else:
            self._run_sequential(celex_ids)

        try:
            from eurlex_builder.translate import translate_database
            tr = self.config.processing.translation
            db_path = str(Path(self.config.output.output_directory) / "eurlex_builder.duckdb")
            translate_database(
                db_path,
                translate_full_text=tr.translate_full_text,
                translate_text_units=tr.translate_text_units,
                max_full_text_chars=tr.max_full_text_chars,
            )
        except ImportError:
            logger.debug("Translation not available (install eurlex-builder[translate])")

        summary = self.checkpoint.get_summary()
        run_status = "complete_with_failures" if summary.get("failed", 0) else "complete"
        self.store.finish_run(run_id, run_status)

        logger.info("Exporting results...")
        self.store.export(
            self.config.output.output_directory,
            self.config.output.formats,
        )

        logger.info(
            f"Done. Processed: {summary.get('processed', 0)}, "
            f"Failed: {summary.get('failed', 0)}"
        )

        failed_details = summary.get("failed_details", {})
        if failed_details:
            logger.warning("Failed documents:")
            for celex_id, error in failed_details.items():
                logger.warning(f"  {celex_id}: {error}")

        self._report_missing_content()
        self._report_extraction_stats()

    # ------------------------------------------------------------------
    # Sequential mode
    # ------------------------------------------------------------------

    def _run_sequential(self, celex_ids: list[str]) -> None:
        for celex_id in tqdm(celex_ids, desc="Processing documents"):
            try:
                self._process_one(celex_id)
                self.checkpoint.mark_processed(celex_id)
            except Exception as e:
                logger.error(f"Failed to process {celex_id}: {e}")
                self.checkpoint.mark_failed(celex_id, str(e))

    # ------------------------------------------------------------------
    # Parallel mode
    # ------------------------------------------------------------------

    def _run_parallel(self, celex_ids: list[str]) -> None:
        """Fetch and parse in parallel threads, write to DB sequentially."""
        max_workers = self.config.processing.max_workers
        logger.info(f"Parallel mode: {max_workers} workers")

        # Each worker gets its own CellarSource (own HTTP session).
        from eurlex_builder.sources.cellar import CellarSource

        import threading
        _thread_sources: dict[int, CellarSource] = {}
        _sources_lock = threading.Lock()

        def _get_thread_source() -> CellarSource:
            tid = threading.get_ident()
            if tid not in _thread_sources:
                with _sources_lock:
                    if tid not in _thread_sources:
                        _thread_sources[tid] = CellarSource()
            return _thread_sources[tid]

        def fetch_and_parse(celex_id: str) -> dict:
            """Worker: delegates to the shared extraction core."""
            return self._fetch_and_extract(celex_id, _get_thread_source())

        # Submit all work, then write results as they complete.
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(fetch_and_parse, cid): cid for cid in celex_ids
            }

            with tqdm(total=len(celex_ids), desc="Processing documents") as pbar:
                for future in as_completed(futures):
                    celex_id = futures[future]
                    try:
                        result = future.result()
                        # Sequential DB writes in main thread. save_text_units
                        # runs even with zero units so a re-extraction that now
                        # yields nothing clears the previous rows — but only
                        # when content was actually fetched: a transient fetch
                        # failure must not wipe previously extracted units.
                        self._persist_result(celex_id, result)
                        self.checkpoint.mark_processed(celex_id)
                    except Exception as e:
                        logger.error(f"Failed to process {celex_id}: {e}")
                        self.checkpoint.mark_failed(celex_id, str(e))
                    pbar.update(1)

    # ------------------------------------------------------------------
    # Shared logic
    # ------------------------------------------------------------------

    def _resolve_ids(self) -> list[str]:
        """Get the list of CELEX IDs to process based on config mode."""
        data = self.config.data

        if isinstance(data, FixedMode):
            ids = list(data.celex_ids)
            if data.procedure_numbers:
                resolved = self.source.resolve_procedure_numbers(data.procedure_numbers)
                ids.extend(resolved)
            return sorted(set(ids))

        if isinstance(data, DescriptiveMode):
            # Resolve EuroVoc keywords to concept URIs if provided.
            eurovoc_uris: list[str] | None = None
            if data.filter_keywords:
                eurovoc_map = self.source.resolve_eurovoc_keywords(data.filter_keywords)

                # Collect all matched concept URIs.
                all_uris: set[str] = set()
                for concepts in eurovoc_map.values():
                    all_uris.update(concepts.keys())

                if not all_uris:
                    logger.warning(
                        "No EuroVoc concepts matched any keywords — "
                        "query will run without keyword filter."
                    )
                elif self.config.processing.automated_mode:
                    # Automated: accept all matches.
                    logger.info(
                        "Using %d EuroVoc concept(s) from %d keyword(s)",
                        len(all_uris), len(data.filter_keywords),
                    )
                    eurovoc_uris = sorted(all_uris)
                else:
                    # Interactive: walk user through each concept.
                    from eurlex_builder.eurovoc_review import review_eurovoc_matches
                    eurovoc_uris = review_eurovoc_matches(
                        eurovoc_map, self.source,
                    )
                    if not eurovoc_uris:
                        logger.warning(
                            "All EuroVoc concepts rejected — "
                            "query will run without keyword filter."
                        )

            return self.source.resolve_celex_ids(
                document_types=data.document_types,
                start_date=data.start_date,
                end_date=data.end_date,
                eurovoc_uris=eurovoc_uris,
                include_corrigenda=data.include_corrigenda,
                include_consolidated_texts=data.include_consolidated_texts,
            )

        raise ValueError(f"Unknown data mode: {data}")

    @staticmethod
    def _unpack_content(fetch_result, metadata: dict) -> tuple[bytes | None, dict]:
        """Unpack fetch_content result into raw bytes and updated metadata."""
        if fetch_result is not None:
            raw, content_type, language = fetch_result
            # Only store decoded HTML in full_text_html; PDF bytes are not useful as text.
            if content_type == "html":
                metadata["full_text_html"] = raw.decode("utf-8", errors="replace")
            else:
                metadata["full_text_html"] = None
            metadata["content_source"] = f"cellar_{content_type}_{language}"
            metadata["language"] = language
            return raw, metadata
        metadata["content_source"] = None
        return None, metadata

    def _process_one(self, celex_id: str) -> None:
        """Sequential: delegate to the shared core, then write to DB.

        save_text_units runs even with zero units so a re-extraction that now
        yields nothing clears the previous rows — but only when content was
        actually fetched: a transient fetch failure must not wipe previously
        extracted units.
        """
        result = self._fetch_and_extract(celex_id, self.source)
        self._persist_result(celex_id, result)

    def _persist_result(self, celex_id: str, result: dict) -> None:
        """Persist one successfully fetched result in dependency order."""
        self.store.save_work(
            result["metadata"],
            preserve_existing_content=not result["fetched"],
        )
        if result["fetched"]:
            self.store.save_text_units(celex_id, result["units"])
        if self.config.processing.include_relations:
            self.store.save_relations(result["relations"], celex_id=celex_id)
        if self.config.processing.include_eurovoc:
            self.store.save_eurovoc(celex_id, result["eurovoc"])

    def _fetch_and_extract(self, celex_id: str, source) -> dict:
        """Core per-document pipeline, source-agnostic. No DB writes.

        Used by both sequential (source=self.source) and parallel
        (source=per-thread CellarSource). Returns a dict with metadata, units,
        relations, eurovoc ready to be persisted by the caller.
        """
        text_cfg = self.config.processing.text_extraction
        proc = self.config.processing

        metadata = source.fetch_metadata(celex_id)
        fetch_result = source.fetch_content(celex_id)
        raw_content, metadata = self._unpack_content(fetch_result, metadata)
        doc_type = metadata.get("document_type", "")

        # Full text (always, when content exists).
        full_text = None
        if raw_content:
            full_text = self._extract_full_text(raw_content, fetch_result)
        metadata["full_text"] = full_text

        # Drop raw HTML unless explicitly kept.
        if not text_cfg.store_raw_html:
            metadata["full_text_html"] = None

        units: list[dict] = []

        # Structural extraction for R/L/D.
        if raw_content and doc_type in STRUCTURAL_DOC_TYPES:
            language = fetch_result[2] if fetch_result and len(fetch_result) >= 3 else "eng"
            extract_meta: dict = {}
            units = self._extract_units(
                celex_id, raw_content, text_cfg, language=language,
                out_metadata=extract_meta,
            )

            # PDF fallback when HTML extraction is poor.
            content_type = fetch_result[1] if fetch_result else ""
            if content_type == "html" and self._should_retry_with_pdf(
                units,
                include_recitals=(
                    text_cfg.include_recitals
                    and not (
                        is_consolidated_celex(celex_id)
                        and proc.fetch_original_recitals_for_consolidated
                    )
                ),
                include_articles=text_cfg.include_articles,
                include_annexes=text_cfg.include_annexes,
            ):
                try:
                    pdf_result = source.fetch_pdf(celex_id)
                except TransientSourceError as exc:
                    logger.warning(
                        "Optional PDF quality retry failed for %s: %s; "
                        "keeping HTML extraction",
                        celex_id, exc,
                    )
                    pdf_result = None
                if pdf_result is not None:
                    pdf_raw, _, pdf_lang = pdf_result
                    pdf_meta: dict = {}
                    pdf_units = self._extract_units(
                        celex_id, pdf_raw, text_cfg, language=pdf_lang,
                        out_metadata=pdf_meta,
                    )
                    if pdf_units and len(pdf_units) > len(units):
                        logger.info(
                            "PDF fallback for %s: %d units (was %d from HTML)",
                            celex_id, len(pdf_units), len(units),
                        )
                        units = pdf_units
                        language = pdf_lang
                        extract_meta = pdf_meta
                        metadata["content_source"] = f"cellar_pdf_{pdf_lang}_fallback"
                        metadata["language"] = pdf_lang
                        # Keep full_text consistent with the adopted language —
                        # otherwise the translation phase would run a
                        # non-English model over the English HTML text.
                        metadata["full_text"] = self._extract_full_text(pdf_raw, pdf_result)
                        metadata["full_text_html"] = None

            # Translate-before-extract fallback for non-English legislative PDFs
            # where the source-language parser produced little or no structural
            # content (English-only markers don't fire on French/German/Italian PDFs).
            if (
                _should_run_translate_fallback(
                    units, doc_type, language,
                    include_recitals=text_cfg.include_recitals,
                    include_articles=text_cfg.include_articles,
                    include_annexes=text_cfg.include_annexes,
                )
                and extract_meta.get("markdown")
            ):
                translated = _safe_translate_markdown(
                    extract_meta["markdown"], language, celex_id,
                )
                if translated:
                    from eurlex_builder.extractors.pdf import (
                        _clean_pdf_artifacts,
                        _parse_legislative_markdown,
                    )
                    translated_units = _parse_legislative_markdown(
                        translated,
                        include_recitals=text_cfg.include_recitals,
                        include_articles=text_cfg.include_articles,
                        include_annexes=text_cfg.include_annexes,
                        article_granularity=text_cfg.article_granularity,
                    )
                    for u in translated_units:
                        u["text"] = _clean_pdf_artifacts(u["text"])
                        # Prefill text_translated so phase-2 doesn't re-translate.
                        u["text_translated"] = u["text"]
                    # Only adopt the translated parse when it actually found
                    # more structure — a garbled translation must not replace
                    # a partially successful source-language extraction.
                    if _translated_parse_is_better(
                        units,
                        translated_units,
                        include_recitals=text_cfg.include_recitals,
                        include_articles=text_cfg.include_articles,
                        include_annexes=text_cfg.include_annexes,
                    ):
                        source_profile = _structure_profile(units)
                        translated_profile = _structure_profile(translated_units)
                        logger.info(
                            "Translate-before-extract fallback for %s (%s → eng): "
                            "profile %s (was %s from source language).",
                            celex_id, language,
                            translated_profile,
                            source_profile,
                        )
                        units = translated_units
                        cs = metadata.get("content_source") or ""
                        metadata["content_source"] = f"{cs}__translated"
                    else:
                        logger.info(
                            "Translate-before-extract fallback for %s did not improve "
                            "extraction — keeping source-language units.", celex_id,
                        )

            # Consolidated text: fetch original recitals.
            if (
                is_consolidated_celex(celex_id)
                and proc.fetch_original_recitals_for_consolidated
                and text_cfg.include_recitals
            ):
                original_units = self._fetch_original_recitals_with_source(celex_id, source)
                units = self._merge_original_recitals(units, original_units)

        # Paragraph extraction for communications, proposals, and staff
        # working documents — all share the COM prose templates.
        elif raw_content and doc_type in COM_STYLE_DOC_TYPES:
            units = self._extract_com_units(celex_id, raw_content)

        # Strip boilerplate from last article, drop empty units.
        if text_cfg.strip_boilerplate and units:
            for i in range(len(units) - 1, -1, -1):
                if units[i].get("type") == "article":
                    units[i]["text"] = strip_boilerplate(units[i]["text"])
                    break
            units = [u for u in units if u.get("text", "").strip()]

        # Relations.
        relations: list[dict] = []
        if proc.include_relations:
            relations = source.fetch_relations(celex_id)
            if (
                is_consolidated_celex(celex_id)
                and proc.fetch_original_relations_for_consolidated
            ):
                original_celex = convert_consolidated_to_original(celex_id)
                if original_celex in self._selected_ids:
                    try:
                        source.fetch_metadata(original_celex)  # primes the relation cache
                    except TransientSourceError as exc:
                        logger.warning(
                            "Optional original-act relation lookup failed for %s: %s; "
                            "keeping relations for %s only",
                            original_celex, exc, celex_id,
                        )
                    else:
                        relations.extend(source.fetch_relations(original_celex))
                else:
                    logger.warning(
                        "Skipping original-act relations for %s because %s is "
                        "not part of the resolved dataset",
                        celex_id, original_celex,
                    )

        # EuroVoc.
        eurovoc: list[dict] = []
        if proc.include_eurovoc:
            eurovoc = source.fetch_eurovoc(celex_id)

        return {
            "celex_id": celex_id,
            "metadata": metadata,
            "units": units,
            "relations": relations,
            "eurovoc": eurovoc,
            # False on a fetch failure — the caller must then NOT clear the
            # document's existing text_units (empty units would mean "the
            # network failed", not "the document has no content").
            "fetched": raw_content is not None,
        }

    @staticmethod
    def _should_retry_with_pdf(
        units: list[dict],
        *,
        include_recitals: bool = True,
        include_articles: bool = True,
        include_annexes: bool = True,
    ) -> bool:
        """Check if HTML extraction produced poor enough results to justify PDF retry.

        Heuristic: retry if we got no structured units, only body fallback,
        or suspiciously few recitals for a document type that normally has many.
        Decisions (D) legitimately have 0-1 recitals, so we're lenient with those.
        """
        if not units:
            return include_recitals or include_articles or include_annexes
        if all(u["type"] == "body" for u in units):
            return include_articles

        recitals = [u for u in units if u["type"] == "recital"]
        articles = [u for u in units if u["type"] == "article"]

        # Regulations and directives should have >1 recital.
        # Decisions may legitimately have few, but zero recitals with
        # multiple articles often signals table-structured HTML we can't parse.
        if include_articles and not articles:
            return True
        if include_recitals and len(recitals) == 0 and len(articles) > 0:
            return True
        return False

    def _extract_com_units(self, celex_id: str, raw_content: bytes) -> list[dict]:
        """Extract paragraph-level units from a communication document."""
        from eurlex_builder.extractors.html import HtmlExtractor
        extractor = HtmlExtractor()
        if extractor.can_handle(raw_content):
            return extractor.extract_com(celex_id, raw_content)

        # PDF fallback: extract text, then split into paragraphs.
        from eurlex_builder.extractors.pdf import PdfExtractor
        pdf_extractor = PdfExtractor()
        if pdf_extractor.can_handle(raw_content):
            return self._extract_com_from_pdf(celex_id, raw_content)

        logger.warning("No HTML or PDF content for COM %s — paragraph extraction skipped", celex_id)
        return []

    def _extract_com_from_pdf(self, celex_id: str, raw_content: bytes) -> list[dict]:
        """Extract COM paragraphs from PDF via Docling (with pymupdf fallback)."""
        import tempfile
        from pathlib import Path
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
        from eurlex_builder.extractors.pdf import _get_converter, _PDF_TIMEOUT, extract_pdf_full_text
        from eurlex_builder.extractors.html import extract_com_from_text

        # Skip Docling for very large PDFs to avoid segfaults in native code.
        docling_size_limit = 50 * 1024 * 1024
        text = None

        if len(raw_content) > docling_size_limit:
            logger.info(
                "PDF for COM %s is %.0f MB — skipping Docling, using pymupdf",
                celex_id, len(raw_content) / 1024 / 1024,
            )
        else:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
                f.write(raw_content)
                tmp_path = f.name

            try:
                converter = _get_converter()
                pool = ThreadPoolExecutor(max_workers=1)
                future = pool.submit(converter.convert, tmp_path)
                try:
                    result = future.result(timeout=_PDF_TIMEOUT)
                    text = result.document.export_to_markdown()
                except FuturesTimeoutError:
                    logger.warning("Docling timed out for COM %s, falling back to pymupdf", celex_id)
                    future.cancel()
                    pool.shutdown(wait=False, cancel_futures=True)
                else:
                    pool.shutdown(wait=False)
            except Exception as exc:
                logger.warning("Docling failed for COM %s: %s, falling back to pymupdf", celex_id, exc)
            finally:
                Path(tmp_path).unlink(missing_ok=True)

        if not text:
            text = extract_pdf_full_text(raw_content)

        if not text:
            logger.warning("PDF text extraction returned nothing for COM %s", celex_id)
            return []

        units = extract_com_from_text(text)
        if units:
            logger.info("Extracted %d paragraph units from PDF for COM %s", len(units), celex_id)
        else:
            units = [{
                "type": "body",
                "subtype": None,
                "number": None,
                "title": None,
                "text": text.strip(),
            }]
            logger.info("PDF body fallback for COM %s", celex_id)
        return units

    def _extract_units(
        self, celex_id: str, raw_content: bytes, text_cfg, language: str = "eng",
        out_metadata: dict | None = None,
    ) -> list[dict]:
        """Run extractors on raw content, return text units."""
        for extractor in self.extractors:
            if extractor.can_handle(raw_content):
                return extractor.extract(
                    celex_id,
                    raw_content,
                    include_recitals=text_cfg.include_recitals,
                    include_articles=text_cfg.include_articles,
                    include_annexes=text_cfg.include_annexes,
                    article_granularity=text_cfg.article_granularity,
                    language=language,
                    out_metadata=out_metadata,
                )
        logger.warning(f"No extractor could handle content for {celex_id}")
        return []

    @staticmethod
    def _extract_full_text(raw_content: bytes, fetch_result) -> str | None:
        """Extract plain text from raw content for the full_text column."""
        content_type = fetch_result[1] if fetch_result else ""
        if content_type == "html":
            from eurlex_builder.extractors.html import extract_html_full_text
            return extract_html_full_text(raw_content)
        elif content_type == "pdf":
            from eurlex_builder.extractors.pdf import extract_pdf_full_text
            return extract_pdf_full_text(raw_content)
        return None

    def _fetch_original_recitals_with_source(
        self, consolidated_celex: str, source
    ) -> list[dict]:
        """Fetch recitals from the original act for a consolidated text."""
        original_celex = convert_consolidated_to_original(consolidated_celex)
        logger.debug(
            "Fetching original recitals for %s from %s",
            consolidated_celex, original_celex,
        )
        try:
            fetch_result = source.fetch_content(original_celex)
        except TransientSourceError as exc:
            logger.warning(
                "Optional original-act recital fetch failed for %s: %s; "
                "keeping consolidated extraction",
                original_celex, exc,
            )
            return []
        if fetch_result is None:
            logger.warning(
                "Could not fetch original act %s for recitals", original_celex
            )
            return []

        raw, _, _ = fetch_result
        for extractor in self.extractors:
            if extractor.can_handle(raw):
                extracted = extractor.extract(
                    consolidated_celex,
                    raw,
                    include_recitals=True,
                    include_articles=False,
                    include_annexes=False,
                )
                # The extractors fall back to a whole-document "body" unit when
                # they find no structure — that must not leak into the merge.
                return [u for u in extracted if u.get("type") == "recital"]
        return []

    @staticmethod
    def _merge_original_recitals(
        units: list[dict], original_units: list[dict],
    ) -> list[dict]:
        """Replace recitals while retaining translations for matching numbers."""
        if not original_units:
            return units
        translations = {
            unit.get("number"): unit.get("text_translated")
            for unit in units
            if unit.get("type") == "recital"
            and unit.get("number") is not None
            and unit.get("text_translated")
        }
        for unit in original_units:
            translated = translations.get(unit.get("number"))
            if translated:
                unit["text_translated"] = translated
        return original_units + [unit for unit in units if unit.get("type") != "recital"]

    def _report_missing_content(self) -> None:
        """Log and write a report of documents with no content or non-English content."""
        output_dir = Path(self.config.output.output_directory)

        try:
            rows = self.store.get_content_report()
        except Exception:
            return

        missing = [(r[0], r[1]) for r in rows if r[3] is None]
        non_eng = [(r[0], r[1], r[2]) for r in rows if r[2] and r[2] != "eng"]
        missing_path = output_dir / "missing_content.tsv"
        non_english_path = output_dir / "non_english_content.tsv"

        if missing:
            logger.info(f"{len(missing)} document(s) with no content in any language:")
            report_lines = ["celex_id\ttitle"]
            for cid, title in missing:
                logger.info(f"  {cid}")
                report_lines.append(f"{cid}\t{title}")

            missing_path.write_text("\n".join(report_lines), encoding="utf-8")
            logger.info(f"Missing content report: {missing_path}")
        else:
            missing_path.unlink(missing_ok=True)

        if non_eng:
            logger.info(f"{len(non_eng)} document(s) with non-English content:")
            report_lines = ["celex_id\tlanguage\ttitle"]
            for cid, title, lang in non_eng:
                logger.info(f"  {cid} ({lang})")
                report_lines.append(f"{cid}\t{lang}\t{title}")

            non_english_path.write_text("\n".join(report_lines), encoding="utf-8")
            logger.info(f"Non-English content report: {non_english_path}")
        else:
            non_english_path.unlink(missing_ok=True)

    def _report_extraction_stats(self) -> None:
        """Log detailed extraction statistics by failure mode."""
        if not hasattr(self.store, "conn"):
            return
        try:
            rows = self.store.conn.execute("""
                SELECT
                    w.celex_id, w.document_type, w.content_source, w.full_text,
                    COUNT(CASE WHEN tu.type != 'body' THEN 1 END) as structured_units,
                    COUNT(CASE WHEN tu.type = 'body' THEN 1 END) as body_units
                FROM works w
                LEFT JOIN text_units tu ON w.celex_id = tu.celex_id
                GROUP BY w.celex_id, w.document_type, w.content_source, w.full_text
            """).fetchall()
        except Exception:
            return

        no_full_text = sum(1 for r in rows if not r[3])
        no_structured = sum(
            1 for r in rows
            if r[1] in STRUCTURAL_DOC_TYPES and r[4] == 0 and r[3]
        )
        body_only = sum(
            1 for r in rows
            if r[1] in STRUCTURAL_DOC_TYPES and r[4] == 0 and r[5] > 0
        )

        logger.info("--- Extraction statistics ---")
        logger.info(f"  Total documents: {len(rows)}")
        logger.info(f"  Documents with no full text: {no_full_text}")
        logger.info(f"  R/L/D with full text but no structured units: {no_structured}")
        logger.info(f"  R/L/D with body-only fallback: {body_only}")

        # Break down by content source
        from collections import Counter
        no_text_sources = Counter(r[2] for r in rows if not r[3])
        if no_text_sources:
            logger.info("  No full text by source:")
            for src, n in no_text_sources.most_common():
                logger.info(f"    {src or 'NULL'}: {n}")

    def _setup_logging(self) -> None:
        """Configure file and console logging."""
        output_dir = Path(self.config.output.output_directory)
        output_dir.mkdir(parents=True, exist_ok=True)

        logger.setLevel(logging.DEBUG)

        if not logger.handlers:
            # File handler — verbose
            fh = RotatingFileHandler(
                output_dir / "pipeline.log",
                maxBytes=10 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            logger.addHandler(fh)

            # Console handler — info only
            ch = logging.StreamHandler()
            ch.setLevel(logging.INFO)
            ch.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(ch)
