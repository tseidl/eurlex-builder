"""Tests for translation chunking, quality checks, and repair behavior."""

from __future__ import annotations

from eurlex_builder import translate as tr


class _WhitespaceTokenizer:
    def __call__(self, value, **kwargs):
        if isinstance(value, list):
            return {"input_ids": [item.split() for item in value]}
        return {"input_ids": value.split()}

    def decode(self, values, *, skip_special_tokens: bool = True) -> str:
        return " ".join(str(value) for value in values)


def test_translation_quality_rejects_repetition_and_extreme_lengths():
    assert tr.translation_quality_issue(
        "Texte juridique court.", "No, " * 20,
    ) == "repetitive_output"
    assert tr.translation_quality_issue("a" * 40, "word " * 100) == "excessive_expansion"
    assert tr.translation_quality_issue("Texte juridique " * 20, "Short") == "severe_contraction"
    assert tr.translation_quality_issue(
        "Texte juridique applicable.", "Applicable legal text.",
    ) is None


def test_translation_chunks_pack_short_paragraphs():
    chunks = tr._chunk_for_translation(
        ["one", "two", "three", "four", "five"],
        _WhitespaceTokenizer(),
        max_tokens=3,
    )

    assert chunks == ["one two three", "four five"]


def test_translation_hard_splits_one_oversized_unbroken_paragraph():
    tokenizer = _WhitespaceTokenizer()
    source = " ".join(f"token-{index}" for index in range(1_205))

    chunks = tr._chunk_for_translation([source], tokenizer, max_tokens=480)

    assert [len(tokenizer(chunk)["input_ids"]) for chunk in chunks] == [480, 480, 245]


def test_translate_text_retries_repetitive_generation(monkeypatch):
    calls: list[bool] = []

    def generate(chunk, tokenizer, model, *, retry):
        calls.append(retry)
        return "A sound legal translation." if retry else "No, " * 12

    monkeypatch.setattr(tr, "_get_model", lambda language: (_WhitespaceTokenizer(), object()))
    monkeypatch.setattr(tr, "_generate_translation", generate)

    result = tr.translate_text("Texte juridique court.", "fra")

    assert result == "A sound legal translation."
    assert calls == [False, True]


def test_translate_text_rejects_bad_retry(monkeypatch):
    monkeypatch.setattr(tr, "_get_model", lambda language: (_WhitespaceTokenizer(), object()))
    monkeypatch.setattr(
        tr,
        "_generate_translation",
        lambda chunk, tokenizer, model, *, retry: "No, " * 12,
    )

    assert tr.translate_text("Texte juridique court.", "fra") is None


def test_translation_failures_are_persisted_with_source_hash(store):
    tr._ensure_translation_failure_table(store.conn)
    tr._record_translation_failure(
        store.conn,
        scope="unit",
        record_id="42",
        celex_id="X1",
        source="Texte source.",
        reason="repetitive_output",
    )

    row = store.conn.execute(
        """SELECT scope, record_id, celex_id, length(source_sha256), reason,
                  policy_version
           FROM _translation_failures"""
    ).fetchone()
    assert row == (
        "unit", "42", "X1", 64, "repetitive_output",
        tr._TRANSLATION_POLICY_VERSION,
    )

    tr._clear_translation_failure(store.conn, scope="unit", record_id="42")
    assert store.conn.execute(
        "SELECT count(*) FROM _translation_failures"
    ).fetchone() == (0,)


def test_reset_rejected_translations_is_atomic_and_queues_translated_parse(store):
    store.save_work({
        "celex_id": "X1",
        "language": "fra",
        "full_text": "Texte source complet.",
        "content_source": "cellar_pdf_fra",
    })
    store.save_text_units("X1", [{
        "type": "article",
        "number": "1",
        "text": "Texte source.",
        "text_translated": "No, " * 12,
    }])
    store.conn.execute(
        "UPDATE works SET full_text_original = full_text, full_text = ? WHERE celex_id = 'X1'",
        ["No, " * 12],
    )
    store.save_work({
        "celex_id": "X2",
        "language": "fra",
        "full_text": "Texte source.",
        "content_source": "cellar_pdf_fra__translated",
    })
    store.save_text_units("X2", [{
        "type": "article",
        "number": "1",
        "text": "No, " * 12,
        "text_translated": "No, " * 12,
    }])
    store.mark_processed("X1")
    store.mark_processed("X2")

    works, units, fallback = tr._reset_rejected_translations(
        store.conn,
        translate_full_text=True,
        translate_text_units=True,
    )

    assert (works, units, fallback) == (1, 1, {"X2"})
    assert store.conn.execute(
        "SELECT full_text, full_text_original FROM works WHERE celex_id = 'X1'"
    ).fetchone() == ("Texte source complet.", None)
    assert store.conn.execute(
        "SELECT text_translated FROM text_units WHERE celex_id = 'X1'"
    ).fetchone() == (None,)
    assert store.conn.execute(
        "SELECT text_translated FROM text_units WHERE celex_id = 'X2'"
    ).fetchone()[0] == "No, " * 12
    assert store.is_processed("X1")
    assert not store.is_processed("X2")


def test_ordinary_resume_skips_rejected_but_explicit_retry_attempts_it(
    store, monkeypatch,
):
    store.save_work({
        "celex_id": "X1",
        "language": "fra",
        "full_text": "Texte source.",
        "content_source": "cellar_pdf_fra",
    })
    store.save_text_units("X1", [{
        "type": "article",
        "number": "1",
        "text": "Texte source.",
    }])
    unit_id = store.conn.execute(
        "SELECT id FROM text_units WHERE celex_id = 'X1'"
    ).fetchone()[0]
    tr._ensure_translation_failure_table(store.conn)
    tr._record_translation_failure(
        store.conn,
        scope="unit",
        record_id=str(unit_id),
        celex_id="X1",
        source="Texte source.",
        reason="repetitive_output",
    )
    calls = []
    monkeypatch.setattr(tr, "_get_model", lambda language: (object(), object()))
    monkeypatch.setattr(
        tr,
        "_translate_text_with_pair",
        lambda text, pair, *, document: calls.append(text) or ("Source text.", None),
    )

    tr._translate_database_inner(
        store.conn,
        translate_full_text=False,
        translate_text_units=True,
        max_full_text_chars=100_000,
    )
    assert calls == []

    tr._translate_database_inner(
        store.conn,
        translate_full_text=False,
        translate_text_units=True,
        max_full_text_chars=100_000,
        retry_rejected=True,
    )
    assert calls == ["Texte source."]
    assert store.conn.execute(
        "SELECT text_translated FROM text_units WHERE id = ?", [unit_id],
    ).fetchone() == ("Source text.",)
