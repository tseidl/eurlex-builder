"""Translation of non-English text units using Helsinki-NLP Opus-MT models."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from collections import Counter

logger = logging.getLogger("eurlex_builder")

# Opus-MT model names and immutable Hugging Face revisions.
MODEL_MAP: dict[str, tuple[str, str]] = {
    "fra": (
        "Helsinki-NLP/opus-mt-fr-en",
        "c4aed37b318c763fd177aa449b44e3b783cc6c02",
    ),
    "deu": (
        "Helsinki-NLP/opus-mt-de-en",
        "1a922f3b32a8e809e17a47d4b32142d8105924e5",
    ),
    "ita": (
        "Helsinki-NLP/opus-mt-it-en",
        "42556a0848fc726f4d27399f20b19ff6f01afe11",
    ),
    "nld": (
        "Helsinki-NLP/opus-mt-nl-en",
        "48af999f2c59b10c05ca6e008dcedc07677a9b15",
    ),
    "spa": (
        "Helsinki-NLP/opus-mt-es-en",
        "c96e2c5399ebfae4fc43d9669556b9afa74bb69d",
    ),
}

# Lazy-loaded models and tokenizers, guarded by a lock for thread safety.
_models: dict[str, tuple] = {}
_model_lock = threading.Lock()


def _get_model(lang: str):
    """Load and cache an Opus-MT model + tokenizer for a language pair."""
    if lang in _models:
        return _models[lang]

    with _model_lock:
        # Double-check after acquiring lock (another thread may have loaded it).
        if lang in _models:
            return _models[lang]

        model_spec = MODEL_MAP.get(lang)
        if not model_spec:
            logger.warning("No translation model available for language: %s", lang)
            return None
        model_name, revision = model_spec

        logger.info("Loading translation model: %s@%s", model_name, revision[:12])
        from transformers import MarianMTModel, MarianTokenizer

        tokenizer = MarianTokenizer.from_pretrained(model_name, revision=revision)
        model = MarianMTModel.from_pretrained(model_name, revision=revision)
        # Marian ships max_length=512 while every call below explicitly uses
        # max_new_tokens=512. Transformers already gives the latter precedence;
        # clearing the superseded default avoids a warning for every chunk.
        model.generation_config.max_length = None
        _models[lang] = (tokenizer, model)
        return (tokenizer, model)


# Token budget per chunk — stay below the model's 512-token hard limit to
# leave room for special tokens and avoid silent truncation.
_MAX_INPUT_TOKENS = 480
_RETRY_INPUT_TOKENS = 160
_TRANSLATION_POLICY_VERSION = 1
_WORD_RE = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*", re.UNICODE)


def _ensure_translation_failure_table(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS _translation_failures (
               scope VARCHAR NOT NULL,
               record_id VARCHAR NOT NULL,
               celex_id VARCHAR NOT NULL,
               source_sha256 VARCHAR NOT NULL,
               reason VARCHAR NOT NULL,
               policy_version INTEGER NOT NULL DEFAULT 1,
               attempted_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
               PRIMARY KEY (scope, record_id)
           )"""
    )
    try:
        conn.execute(
            "ALTER TABLE _translation_failures ADD COLUMN policy_version INTEGER"
        )
    except Exception:
        pass
    conn.execute(
        """UPDATE _translation_failures SET policy_version = 0
           WHERE policy_version IS NULL"""
    )
    conn.execute(
        """DELETE FROM _translation_failures f
           WHERE policy_version != ?
              OR (scope = 'unit' AND NOT EXISTS (
                      SELECT 1 FROM text_units t
                      WHERE cast(t.id AS VARCHAR) = f.record_id
                  ))
              OR (scope = 'full_text' AND NOT EXISTS (
                      SELECT 1 FROM works w WHERE w.celex_id = f.celex_id
                  ))
              OR (scope = 'unit' AND EXISTS (
                      SELECT 1 FROM text_units t
                      WHERE cast(t.id AS VARCHAR) = f.record_id
                        AND t.text_translated IS NOT NULL
                  ))
              OR (scope = 'unit' AND EXISTS (
                      SELECT 1 FROM text_units t
                      WHERE cast(t.id AS VARCHAR) = f.record_id
                        AND sha256(t.text) != f.source_sha256
                  ))
              OR (scope = 'full_text' AND EXISTS (
                      SELECT 1 FROM works w WHERE w.celex_id = f.celex_id
                        AND w.full_text_original IS NOT NULL
                  ))
              OR (scope = 'full_text' AND EXISTS (
                      SELECT 1 FROM works w WHERE w.celex_id = f.celex_id
                        AND w.full_text_original IS NULL
                        AND sha256(w.full_text) != f.source_sha256
                  ))""",
        [_TRANSLATION_POLICY_VERSION],
    )


def _record_translation_failure(
    conn,
    *,
    scope: str,
    record_id: str,
    celex_id: str,
    source: str,
    reason: str,
) -> None:
    source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    conn.execute(
        """INSERT INTO _translation_failures
               (scope, record_id, celex_id, source_sha256, reason,
                policy_version, attempted_at)
           VALUES (?, ?, ?, ?, ?, ?, current_timestamp)
           ON CONFLICT (scope, record_id) DO UPDATE SET
               celex_id = excluded.celex_id,
               source_sha256 = excluded.source_sha256,
               reason = excluded.reason,
               policy_version = excluded.policy_version,
               attempted_at = excluded.attempted_at""",
        [
            scope, record_id, celex_id, source_sha256, reason,
            _TRANSLATION_POLICY_VERSION,
        ],
    )


def _clear_translation_failure(conn, *, scope: str, record_id: str) -> None:
    conn.execute(
        "DELETE FROM _translation_failures WHERE scope = ? AND record_id = ?",
        [scope, record_id],
    )


def translation_quality_issue(
    source: str,
    translated: str | None,
    *,
    document: bool = False,
) -> str | None:
    """Return a reason when a model output is unsafe to store as a translation."""
    if translated is None or not translated.strip():
        return "empty_output"

    source_length = len(source.strip())
    translated_length = len(translated.strip())
    if source_length:
        ratio = translated_length / source_length
        if source_length >= 20 and translated_length >= 200 and ratio > 4:
            return "excessive_expansion"
        if (
            source_length >= 100
            and sum(character.isalpha() for character in source) >= 40
            and ratio < 0.25
        ):
            return "severe_contraction"
        if document and source_length >= 1_000:
            if ratio > 2.5:
                return "document_expansion"
            if ratio < 0.4:
                return "document_contraction"

    tokens = [token.casefold() for token in _WORD_RE.findall(translated)]
    source_tokens = [token.casefold() for token in _WORD_RE.findall(source)]
    if _has_excessive_repetition(tokens, source_tokens):
        return "repetitive_output"
    return None


def _has_excessive_repetition(tokens: list[str], source_tokens: list[str]) -> bool:
    if len(tokens) < 10:
        return False

    run = 1
    for previous, current in zip(tokens, tokens[1:]):
        run = run + 1 if current == previous else 1
        if run >= 10:
            return True

    if len(tokens) >= 30:
        output_token, most_common = Counter(tokens).most_common(1)[0]
        output_share = most_common / len(tokens)
        if most_common >= 20 and output_share >= 0.4:
            source_common = Counter(source_tokens).most_common(1)
            source_token, source_count = source_common[0] if source_common else ("", 0)
            source_share = source_count / len(source_tokens) if source_tokens else 0
            if output_token != source_token or output_share >= source_share + 0.15:
                return True
        for width in (2, 3, 4):
            counts = Counter(
                tuple(tokens[index : index + width])
                for index in range(len(tokens) - width + 1)
            )
            repetitions = counts.most_common(1)[0][1]
            output_coverage = repetitions * width / len(tokens)
            if repetitions >= 8 and output_coverage >= 0.4:
                source_counts = Counter(
                    tuple(source_tokens[index : index + width])
                    for index in range(len(source_tokens) - width + 1)
                )
                source_repetitions = (
                    source_counts.most_common(1)[0][1] if source_counts else 0
                )
                source_coverage = (
                    source_repetitions * width / len(source_tokens)
                    if source_tokens else 0
                )
                if output_coverage >= source_coverage + 0.15:
                    return True
    return False


def _low_information_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    alphabetic = sum(character.isalpha() for character in stripped)
    return alphabetic < 3 or (len(stripped) >= 20 and alphabetic / len(stripped) < 0.08)


def translate_text(text: str, source_lang: str) -> str | None:
    """Translate a single text string to English.

    Splits long texts into paragraphs, then sentence-chunks using the actual
    tokenizer (not char length) so no chunk silently exceeds the 512-token
    input limit.
    """
    pair = _get_model(source_lang)
    if pair is None:
        return None

    return _translate_text_with_pair(text, pair, document=False)[0]


def _translate_text_with_pair(
    text: str,
    pair: tuple,
    *,
    document: bool,
) -> tuple[str | None, str | None]:
    tokenizer, model = pair

    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    if not paragraphs:
        paragraphs = [text]

    chunks = _chunk_for_translation(paragraphs, tokenizer)

    translated_chunks: list[str] = []
    for chunk in chunks:
        translated, issue = _translate_chunk(chunk, tokenizer, model)
        if translated is None:
            return None, issue
        translated_chunks.append(translated)

    result = " ".join(translated_chunks)
    issue = translation_quality_issue(text, result, document=document)
    if issue:
        return None, issue
    return result, None


def _translate_chunk(chunk: str, tokenizer, model) -> tuple[str | None, str | None]:
    if _low_information_text(chunk):
        return chunk, None

    translated = _generate_translation(chunk, tokenizer, model, retry=False)
    issue = translation_quality_issue(chunk, translated)
    if not issue:
        return translated, None

    retry_chunks = _chunk_for_translation(
        [chunk], tokenizer, max_tokens=_RETRY_INPUT_TOKENS,
    )
    retried: list[str] = []
    for retry_chunk in retry_chunks:
        if _low_information_text(retry_chunk):
            retried.append(retry_chunk)
            continue
        retry_translation = _generate_translation(
            retry_chunk, tokenizer, model, retry=True,
        )
        retry_issue = translation_quality_issue(retry_chunk, retry_translation)
        if retry_issue:
            return None, f"{issue}; retry_{retry_issue}"
        retried.append(retry_translation)

    result = " ".join(retried)
    retry_issue = translation_quality_issue(chunk, result)
    if retry_issue:
        return None, f"{issue}; retry_{retry_issue}"
    return result, None


def _generate_translation(chunk: str, tokenizer, model, *, retry: bool) -> str:
    inputs = tokenizer(
        [chunk], return_tensors="pt", padding=True, truncation=True, max_length=512
    )
    generation_options: dict[str, int | float] = {"max_new_tokens": 512}
    if retry:
        generation_options.update({
            "no_repeat_ngram_size": 3,
            "repetition_penalty": 1.1,
        })
    outputs = model.generate(**inputs, **generation_options)
    return tokenizer.decode(outputs[0], skip_special_tokens=True)


def _chunk_for_translation(
    paragraphs: list[str],
    tokenizer,
    *,
    max_tokens: int = _MAX_INPUT_TOKENS,
) -> list[str]:
    """Build chunks that stay within _MAX_INPUT_TOKENS using the real tokenizer.

    Splits paragraphs by sentence when they exceed the budget, then packs
    sentences greedily. If a single sentence exceeds the budget (rare: very
    long URLs or unbreakable strings), it falls back to a hard token split.
    """
    def n_tokens(s: str) -> int:
        return len(
            tokenizer(s, add_special_tokens=False, verbose=False)["input_ids"]
        )

    segments: list[str] = []
    for para in paragraphs:
        if n_tokens(para) <= max_tokens:
            segments.append(para)
            continue
        buf: list[str] = []
        buf_toks = 0
        for sent in _split_sentences(para):
            sent_toks = n_tokens(sent)
            if sent_toks > max_tokens:
                if buf:
                    segments.append(" ".join(buf))
                    buf, buf_toks = [], 0
                segments.extend(
                    _hard_split_by_tokens(sent, tokenizer, max_tokens=max_tokens)
                )
                continue
            if buf_toks + sent_toks > max_tokens and buf:
                segments.append(" ".join(buf))
                buf, buf_toks = [], 0
            buf.append(sent)
            buf_toks += sent_toks
        if buf:
            segments.append(" ".join(buf))

    chunks: list[str] = []
    packed: list[str] = []
    for segment in segments:
        candidate = " ".join([*packed, segment])
        if packed and n_tokens(candidate) > max_tokens:
            chunks.append(" ".join(packed))
            packed = [segment]
        else:
            packed.append(segment)
    if packed:
        chunks.append(" ".join(packed))
    return chunks


def _hard_split_by_tokens(
    text: str,
    tokenizer,
    *,
    max_tokens: int = _MAX_INPUT_TOKENS,
) -> list[str]:
    """Last-resort: split a single oversized sentence on token boundaries."""
    ids = tokenizer(text, add_special_tokens=False, verbose=False)["input_ids"]
    parts = []
    for i in range(0, len(ids), max_tokens):
        parts.append(tokenizer.decode(ids[i : i + max_tokens], skip_special_tokens=True))
    return parts


def _split_sentences(text: str) -> list[str]:
    """Rough sentence splitting for EU legal text."""
    import re
    # Split on period/semicolon followed by space and uppercase letter.
    parts = re.split(r"(?<=[.;])\s+(?=[A-Z(])", text)
    return [p for p in parts if p.strip()]


def translate_markdown(text: str, source_lang: str) -> str | None:
    """Translate Docling markdown to English line-by-line.

    Preserves all newline boundaries — critical for the downstream
    legislative-markdown parser which decides recital / article / annex
    boundaries based on line position. `translate_text` itself splits on
    newlines and rejoins chunks with spaces, so calling it on a multi-line
    string would collapse single-newline structure. We avoid that here by
    translating each non-blank source line independently and preserving the
    exact newline layout.

    Blank lines pass through unchanged. Lines that look like footnote refs
    (e.g. "(1) OJ No L...") are left untranslated since translation tends to
    mangle them.
    """
    if source_lang == "eng":
        return text
    pair = _get_model(source_lang)
    if pair is None:
        return None

    import re
    footnote_ref = re.compile(r"^\s*\(\d+\)\s+OJ\s+", re.IGNORECASE)

    out: list[str] = []
    for line in text.split("\n"):
        if not line.strip():
            out.append(line)
            continue
        if footnote_ref.match(line):
            out.append(line)
            continue
        translated = translate_text(line, source_lang)
        if translated is None:
            logger.warning("Rejected translation of a markdown line in %s", source_lang)
            return None
        out.append(translated)
    result = "\n".join(out)
    issue = translation_quality_issue(text, result, document=len(text) >= 1_000)
    if issue:
        logger.warning("Rejected translated markdown: %s", issue)
        return None
    return result


def _reset_rejected_translations(
    conn,
    *,
    translate_full_text: bool,
    translate_text_units: bool,
) -> tuple[int, int, set[str]]:
    work_repairs: list[tuple[str, str]] = []
    if translate_full_text:
        for celex_id, source, translated in conn.execute(
            """SELECT celex_id, full_text_original, full_text
               FROM works WHERE full_text_original IS NOT NULL"""
        ).fetchall():
            issue = translation_quality_issue(source, translated, document=True)
            if issue:
                work_repairs.append((celex_id, issue))

    unit_repairs: list[tuple[int, str]] = []
    fallback_repairs: set[str] = set()
    if translate_text_units:
        rows = conn.execute(
            """SELECT t.id, t.celex_id, t.text, t.text_translated,
                      w.content_source
               FROM text_units t JOIN works w USING (celex_id)
               WHERE w.language != 'eng'
                 AND t.text IS NOT NULL AND t.text_translated IS NOT NULL"""
        ).fetchall()
        for unit_id, celex_id, source, translated, content_source in rows:
            issue = translation_quality_issue(source, translated)
            if not issue:
                continue
            if content_source and "__translated" in content_source:
                fallback_repairs.add(celex_id)
            else:
                unit_repairs.append((unit_id, issue))

    if work_repairs or unit_repairs or fallback_repairs:
        conn.execute("BEGIN TRANSACTION")
        try:
            if work_repairs:
                conn.executemany(
                    """UPDATE works
                       SET full_text = full_text_original, full_text_original = NULL
                       WHERE celex_id = ?""",
                    [(celex_id,) for celex_id, _ in work_repairs],
                )
            if unit_repairs:
                conn.executemany(
                    "UPDATE text_units SET text_translated = NULL WHERE id = ?",
                    [(unit_id,) for unit_id, _ in unit_repairs],
                )
            if fallback_repairs:
                conn.executemany(
                    "DELETE FROM _checkpoint WHERE celex_id = ?",
                    [(celex_id,) for celex_id in sorted(fallback_repairs)],
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    if work_repairs:
        logger.warning(
            "Reset %d existing full-text translation(s) rejected by quality checks.",
            len(work_repairs),
        )
    if unit_repairs:
        logger.warning(
            "Reset %d existing text-unit translation(s) rejected by quality checks.",
            len(unit_repairs),
        )
    if fallback_repairs:
        logger.warning(
            "Queued %d rejected translated-extraction work(s) for reprocessing: %s",
            len(fallback_repairs), ", ".join(sorted(fallback_repairs)),
        )
    return len(work_repairs), len(unit_repairs), fallback_repairs


def translate_database(
    db_path: str,
    *,
    translate_full_text: bool = True,
    translate_text_units: bool = True,
    max_full_text_chars: int = 100_000,
    retry_rejected: bool = False,
) -> None:
    """Translate all non-English content in a DuckDB database.

    Translates both works.full_text and text_units.text sequentially
    to avoid thread contention with PyTorch inference.

    Args:
        translate_full_text: Translate works.full_text (doc-level).
        translate_text_units: Translate text_units.text (per-unit).
        max_full_text_chars: Skip full_text translation for documents longer
            than this (default 100,000 chars, ~50 pages). Set to 0 to disable.
    """
    import duckdb

    conn = duckdb.connect(db_path)
    try:
        _translate_database_inner(
            conn,
            translate_full_text=translate_full_text,
            translate_text_units=translate_text_units,
            max_full_text_chars=max_full_text_chars,
            retry_rejected=retry_rejected,
        )
    finally:
        conn.close()


def _translate_database_inner(
    conn,
    *,
    translate_full_text: bool,
    translate_text_units: bool,
    max_full_text_chars: int,
    retry_rejected: bool = False,
) -> None:
    """Inner implementation — conn lifetime managed by caller."""
    from tqdm import tqdm

    # Ensure columns exist (for databases created before translation support).
    try:
        conn.execute("ALTER TABLE text_units ADD COLUMN text_translated VARCHAR")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE works ADD COLUMN full_text_original VARCHAR")
    except Exception:
        pass

    _ensure_translation_failure_table(conn)
    if retry_rejected:
        scopes = []
        if translate_full_text:
            scopes.append("full_text")
        if translate_text_units:
            scopes.append("unit")
        if scopes:
            conn.execute(
                "DELETE FROM _translation_failures WHERE scope = ANY(?)",
                [scopes],
            )

    _reset_rejected_translations(
        conn,
        translate_full_text=translate_full_text,
        translate_text_units=translate_text_units,
    )

    # --- Phase 1: Translate works.full_text ---
    if not translate_full_text:
        logger.info("Skipping full_text translation (translate_full_text=False).")
        work_rows = []
    else:
        work_rows = conn.execute("""
            SELECT celex_id, full_text, language
            FROM works
            WHERE language != 'eng'
              AND full_text_original IS NULL
              AND full_text IS NOT NULL
              AND full_text != ''
              AND NOT EXISTS (
                  SELECT 1 FROM _translation_failures f
                  WHERE f.scope = 'full_text'
                    AND f.record_id = works.celex_id
              )
        """).fetchall()

    if work_rows:
        # Split into translatable and too-large.
        translatable = []
        skipped = []
        for row in work_rows:
            celex_id, text, lang = row
            if max_full_text_chars and len(text) > max_full_text_chars:
                skipped.append((celex_id, len(text), lang))
            else:
                translatable.append(row)

        if skipped:
            logger.info(
                "Skipping full_text translation for %d document(s) exceeding %d chars "
                "(text_units still translated):",
                len(skipped), max_full_text_chars,
            )
            for celex_id, chars, lang in skipped:
                logger.info("  %s: %s, %s chars", celex_id, lang, f"{chars:,}")

        if translatable:
            logger.info("Translating full text for %d work(s)...", len(translatable))
            work_count = 0
            rejected_count = 0
            for celex_id, text, lang in tqdm(translatable, desc="Translating full texts"):
                pair = _get_model(lang)
                if pair is None:
                    continue
                translated, issue = _translate_text_with_pair(
                    text, pair, document=True,
                )
                if translated:
                    conn.execute(
                        "UPDATE works SET full_text_original = full_text, full_text = ? WHERE celex_id = ?",
                        [translated, celex_id],
                    )
                    _clear_translation_failure(
                        conn, scope="full_text", record_id=celex_id,
                    )
                    work_count += 1
                elif issue:
                    _record_translation_failure(
                        conn,
                        scope="full_text",
                        record_id=celex_id,
                        celex_id=celex_id,
                        source=text,
                        reason=issue,
                    )
                    rejected_count += 1
                    logger.warning(
                        "Rejected full-text translation for %s: %s", celex_id, issue,
                    )
            logger.info("Translated full text for %d of %d work(s).", work_count, len(translatable))
            if rejected_count:
                logger.warning(
                    "Left %d rejected full-text translation(s) in the source language.",
                    rejected_count,
                )
    elif translate_full_text:
        logger.info("No untranslated full texts found.")

    # --- Phase 2: Translate text_units ---
    if not translate_text_units:
        logger.info("Skipping text_units translation (translate_text_units=False).")
        return

    unit_rows = conn.execute("""
        SELECT tu.id, tu.celex_id, tu.text, w.language
        FROM text_units tu
        JOIN works w ON tu.celex_id = w.celex_id
        WHERE w.language != 'eng'
          AND tu.text_translated IS NULL
          AND tu.text IS NOT NULL
          AND tu.text != ''
          AND NOT EXISTS (
              SELECT 1 FROM _translation_failures f
              WHERE f.scope = 'unit'
                AND f.record_id = cast(tu.id AS VARCHAR)
          )
    """).fetchall()

    if unit_rows:
        logger.info("Translating %d text unit(s)...", len(unit_rows))
        unit_count = 0
        rejected_count = 0
        for unit_id, celex_id, text, lang in tqdm(unit_rows, desc="Translating units"):
            pair = _get_model(lang)
            if pair is None:
                continue
            translated, issue = _translate_text_with_pair(
                text, pair, document=False,
            )
            if translated:
                conn.execute(
                    "UPDATE text_units SET text_translated = ? WHERE id = ?",
                    [translated, unit_id],
                )
                _clear_translation_failure(
                    conn, scope="unit", record_id=str(unit_id),
                )
                unit_count += 1
            elif issue:
                _record_translation_failure(
                    conn,
                    scope="unit",
                    record_id=str(unit_id),
                    celex_id=celex_id,
                    source=text,
                    reason=issue,
                )
                rejected_count += 1
                logger.warning("Rejected text-unit translation %s: %s", unit_id, issue)
        logger.info("Translated %d of %d text unit(s).", unit_count, len(unit_rows))
        if rejected_count:
            logger.warning(
                "Left %d rejected text-unit translation(s) pending.", rejected_count,
            )
    else:
        logger.info("No untranslated text units found.")
