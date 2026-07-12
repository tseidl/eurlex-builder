"""Translation of non-English text units using Helsinki-NLP Opus-MT models."""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger("eurlex_builder")

# Opus-MT model names for each source language.
MODEL_MAP: dict[str, str] = {
    "fra": "Helsinki-NLP/opus-mt-fr-en",
    "deu": "Helsinki-NLP/opus-mt-de-en",
    "ita": "Helsinki-NLP/opus-mt-it-en",
    "nld": "Helsinki-NLP/opus-mt-nl-en",
    "spa": "Helsinki-NLP/opus-mt-es-en",
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

        model_name = MODEL_MAP.get(lang)
        if not model_name:
            logger.warning("No translation model available for language: %s", lang)
            return None

        logger.info("Loading translation model: %s", model_name)
        from transformers import MarianMTModel, MarianTokenizer

        tokenizer = MarianTokenizer.from_pretrained(model_name)
        model = MarianMTModel.from_pretrained(model_name)
        # Marian ships max_length=512 while every call below explicitly uses
        # max_new_tokens=512. Transformers already gives the latter precedence;
        # clearing the superseded default avoids a warning for every chunk.
        model.generation_config.max_length = None
        _models[lang] = (tokenizer, model)
        return (tokenizer, model)


# Token budget per chunk — stay below the model's 512-token hard limit to
# leave room for special tokens and avoid silent truncation.
_MAX_INPUT_TOKENS = 480


def translate_text(text: str, source_lang: str) -> str | None:
    """Translate a single text string to English.

    Splits long texts into paragraphs, then sentence-chunks using the actual
    tokenizer (not char length) so no chunk silently exceeds the 512-token
    input limit.
    """
    pair = _get_model(source_lang)
    if pair is None:
        return None

    tokenizer, model = pair

    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    if not paragraphs:
        paragraphs = [text]

    chunks = _chunk_for_translation(paragraphs, tokenizer)

    translated_chunks: list[str] = []
    for chunk in chunks:
        inputs = tokenizer(
            [chunk], return_tensors="pt", padding=True, truncation=True, max_length=512
        )
        outputs = model.generate(**inputs, max_new_tokens=512)
        translated = tokenizer.decode(outputs[0], skip_special_tokens=True)
        translated_chunks.append(translated)

    return " ".join(translated_chunks)


def _chunk_for_translation(paragraphs: list[str], tokenizer) -> list[str]:
    """Build chunks that stay within _MAX_INPUT_TOKENS using the real tokenizer.

    Splits paragraphs by sentence when they exceed the budget, then packs
    sentences greedily. If a single sentence exceeds the budget (rare: very
    long URLs or unbreakable strings), it falls back to a hard token split.
    """
    def n_tokens(s: str) -> int:
        return len(tokenizer(s, add_special_tokens=False)["input_ids"])

    chunks: list[str] = []
    for para in paragraphs:
        if n_tokens(para) <= _MAX_INPUT_TOKENS:
            chunks.append(para)
            continue
        # Paragraph exceeds budget — split on sentences, pack greedily.
        buf: list[str] = []
        buf_toks = 0
        for sent in _split_sentences(para):
            sent_toks = n_tokens(sent)
            if sent_toks > _MAX_INPUT_TOKENS:
                # Sentence alone exceeds budget — flush buffer then hard-split.
                if buf:
                    chunks.append(" ".join(buf))
                    buf, buf_toks = [], 0
                chunks.extend(_hard_split_by_tokens(sent, tokenizer))
                continue
            if buf_toks + sent_toks > _MAX_INPUT_TOKENS and buf:
                chunks.append(" ".join(buf))
                buf, buf_toks = [], 0
            buf.append(sent)
            buf_toks += sent_toks
        if buf:
            chunks.append(" ".join(buf))
    return chunks


def _hard_split_by_tokens(text: str, tokenizer) -> list[str]:
    """Last-resort: split a single oversized sentence on token boundaries."""
    ids = tokenizer(text, add_special_tokens=False)["input_ids"]
    parts = []
    for i in range(0, len(ids), _MAX_INPUT_TOKENS):
        parts.append(tokenizer.decode(ids[i : i + _MAX_INPUT_TOKENS], skip_special_tokens=True))
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
        out.append(translated if translated is not None else line)
    return "\n".join(out)


def translate_database(
    db_path: str,
    *,
    translate_full_text: bool = True,
    translate_text_units: bool = True,
    max_full_text_chars: int = 100_000,
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
        )
    finally:
        conn.close()


def _translate_database_inner(
    conn,
    *,
    translate_full_text: bool,
    translate_text_units: bool,
    max_full_text_chars: int,
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
            for celex_id, text, lang in tqdm(translatable, desc="Translating full texts"):
                translated = translate_text(text, lang)
                if translated:
                    conn.execute(
                        "UPDATE works SET full_text_original = full_text, full_text = ? WHERE celex_id = ?",
                        [translated, celex_id],
                    )
                    work_count += 1
            logger.info("Translated full text for %d of %d work(s).", work_count, len(translatable))
    elif translate_full_text:
        logger.info("No untranslated full texts found.")

    # --- Phase 2: Translate text_units ---
    if not translate_text_units:
        logger.info("Skipping text_units translation (translate_text_units=False).")
        return

    unit_rows = conn.execute("""
        SELECT tu.id, tu.text, w.language
        FROM text_units tu
        JOIN works w ON tu.celex_id = w.celex_id
        WHERE w.language != 'eng'
          AND tu.text_translated IS NULL
          AND tu.text IS NOT NULL
          AND tu.text != ''
    """).fetchall()

    if unit_rows:
        logger.info("Translating %d text unit(s)...", len(unit_rows))
        unit_count = 0
        for unit_id, text, lang in tqdm(unit_rows, desc="Translating units"):
            translated = translate_text(text, lang)
            if translated:
                conn.execute(
                    "UPDATE text_units SET text_translated = ? WHERE id = ?",
                    [translated, unit_id],
                )
                unit_count += 1
        logger.info("Translated %d of %d text unit(s).", unit_count, len(unit_rows))
    else:
        logger.info("No untranslated text units found.")
