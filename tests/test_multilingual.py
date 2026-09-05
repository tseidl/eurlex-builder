"""Official-language extraction, coverage, isolation and resumability checks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from eurlex_builder import cli, multilingual
from eurlex_builder.errors import TransientSourceError
from eurlex_builder.multilingual import (
    UnsupportedHtml,
    extract_multilingual_database,
    extract_official_units,
    normalize_languages,
)
from eurlex_builder.sources.cellar import CellarSource

CELEX = "32016R0679"
SECOND_CELEX = "32022R2554"
FIXTURES = Path(__file__).parent / "fixtures" / "multilingual"


def html(language="en", *, extra="", second_paragraph=True):
    """Build a document with titles, mixed text, tables, notes and hidden content."""
    second = (
        '<div id="016.007"><p>7. Keep the records.</p></div>'
        if second_paragraph
        else ""
    )
    return f'''<?xml version="1.0" encoding="UTF-8"?>
    <html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}">
    <body><div class="eli-subdivision" id="art_16">
    <p class="oj-ti-art">Article 16</p><div class="eli-title">Records</div>
    <div id="016.001">1. The <b>whole</b> obligation.
    <p>Including this continuation.</p><table><tr><td>(a)</td><td>Keep tables.</td></tr></table>
    <!-- Not article text. --><script>Not visible.</script></div>{extra}{second}
    </div><div id="art_17"><p class="oj-ti-art">Article 17</p>
    <p>Unnumbered article body.</p></div></body></html>'''.encode()


def source_database(tmp_path, celex_ids=(CELEX,)):
    """Create a disposable source with analytical data and checkpoints to preserve."""
    path = tmp_path / "source.duckdb"
    with duckdb.connect(str(path)) as conn:
        conn.execute(
            "CREATE TABLE works (celex_id VARCHAR PRIMARY KEY, full_text VARCHAR)"
        )
        conn.executemany(
            "INSERT INTO works VALUES (?, 'Original analytical text')",
            [[celex] for celex in celex_ids],
        )
        conn.execute("CREATE TABLE text_units AS SELECT 'Existing unit' AS text")
        conn.execute("CREATE TABLE _checkpoint AS SELECT 'processed' AS status")
    return path


def mock_source(monkeypatch, responses):
    """Provide deterministic responses while recording exact language requests."""
    calls = []
    closed = []

    def fetch(self, url, headers, celex_id, *, out_metadata):
        language = headers["Accept-Language"]
        calls.append((celex_id, language))
        response = responses[(celex_id, language)]
        out_metadata.update(
            source_url=f"https://example.test/{celex_id}/{language}",
            http_status=404 if response is None else 200,
        )
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr(
        multilingual,
        "CellarSource",
        lambda: SimpleNamespace(
            _fetch_with_300_handling=lambda *args, **kwargs: fetch(
                None, *args, **kwargs
            ),
            session=SimpleNamespace(close=lambda: closed.append(True)),
        ),
    )
    return calls, closed


def test_preserves_source_text_and_explicit_identifiers():
    units = extract_official_units(html(extra="Unanchored closing text."), CELEX, "eng")
    article, first, seventh, unnumbered = units
    assert [unit["source_id"] for unit in units] == [
        "art_16",
        "016.001",
        "016.007",
        "art_17",
    ]
    assert first["paragraph_num"] == "1"
    assert seventh["paragraph_num"] == "7"
    assert first["unit_key"] == f"{CELEX}:016.001"
    assert first["title"] == "Records"
    assert (
        first["text"]
        == "1. The whole obligation. Including this continuation. (a) Keep tables."
    )
    assert first["text"] in article["text"]
    assert "Unanchored closing text." in article["text"]
    assert "Not article text" not in article["text"]
    assert "Not visible" not in article["text"]
    assert "Article 16" not in article["text"]
    assert article["has_unanchored_text"]
    assert unnumbered["has_unanchored_text"]
    assert not first["has_unanchored_text"]
    assert not extract_official_units(html(), CELEX, "eng")[0]["has_unanchored_text"]


def test_consolidation_date_is_part_of_alignment_identity():
    first = extract_official_units(html(), "02016R0679-20160504", "eng")
    second = extract_official_units(html(), "02016R0679-20240101", "eng")
    assert {unit["unit_key"] for unit in first}.isdisjoint(
        unit["unit_key"] for unit in second
    )


@pytest.mark.parametrize("node", ["<!-- Hidden comment -->", "<?hidden instruction?>"])
def test_article_comments_and_processing_instructions_keep_their_tails(node):
    units = extract_official_units(html(extra=f"{node}Visible tail."), CELEX, "eng")
    assert "Visible tail." in units[0]["text"]
    assert "Hidden comment" not in units[0]["text"]
    assert "hidden instruction" not in units[0]["text"]


@pytest.mark.parametrize(
    "language,code", [("de", "deu"), ("el", "ell"), ("ga", "gle"), ("hr", "hrv")]
)
def test_language_evidence_accepts_official_codes(language, code):
    assert extract_official_units(html(language), CELEX, code)


def test_oj_header_can_verify_language_without_html_attribute():
    raw = (
        html()
        .replace(b' xml:lang="en"', b"")
        .replace(b"<body>", b'<body><p class="oj-hd-lg">DE</p>')
    )
    assert extract_official_units(raw, CELEX, "deu")


def test_http_header_can_verify_language_and_conflicts_are_rejected():
    raw = html().replace(b' xml:lang="en"', b"")
    assert extract_official_units(raw, CELEX, "eng", content_language="en")
    with pytest.raises(ValueError, match="language evidence"):
        extract_official_units(html(), CELEX, "eng", content_language="de")
    with pytest.raises(ValueError, match="language evidence"):
        extract_official_units(html(), CELEX, "fra")


def test_unverified_language_is_not_assumed_from_the_request():
    with pytest.raises(UnsupportedHtml, match="language"):
        extract_official_units(html().replace(b' xml:lang="en"', b""), CELEX, "eng")


@pytest.mark.parametrize(
    "fragment,message",
    [
        ('<div id="016.001"><p>Duplicate.</p></div>', "Duplicate"),
        ('<div id="017.003"><p>Wrong parent.</p></div>', "disagrees"),
        ('<div id="016.002"></div>', "no extractable text"),
    ],
)
def test_ambiguous_or_empty_anchors_do_not_create_alignment(fragment, message):
    with pytest.raises(UnsupportedHtml, match=message):
        extract_official_units(html(extra=fragment), CELEX, "eng")


def test_duplicate_articles_are_rejected():
    with pytest.raises(UnsupportedHtml, match="Duplicate"):
        extract_official_units(
            html().replace(b'id="art_17"', b'id="art_16"'), CELEX, "eng"
        )


def test_quoted_replacement_law_stays_in_its_enclosing_provision():
    replacement = (
        '<table><tr><td><div id="026.003"><p>Quoted paragraph 3.</p></div>'
        '<div id="art_18"><p>Quoted article 18.</p></div></td></tr></table>'
    )
    units = extract_official_units(html(extra=replacement), CELEX, "eng")
    assert [unit["source_id"] for unit in units] == [
        "art_16",
        "016.001",
        "016.007",
        "art_17",
    ]
    assert "Quoted paragraph 3." in units[0]["text"]
    assert "Quoted article 18." in units[0]["text"]
    assert units[0]["has_unanchored_text"]


def test_nested_paragraph_anchors_remain_in_the_parent_paragraph():
    replacement = '<div id="016.002"><p>2. Replace with:</p><div id="026.003"><p>Quoted law.</p></div></div>'
    units = extract_official_units(html(extra=replacement), CELEX, "eng")
    paragraph = next(unit for unit in units if unit["source_id"] == "016.002")
    assert "Quoted law." in paragraph["text"]
    assert not any(unit["source_id"] == "026.003" for unit in units)


def test_consolidated_reference_verifies_language_and_version():
    raw = (
        html()
        .replace(b' xml:lang="en"', b"")
        .replace(
            b"<body>",
            '<body><p class="reference">02016R0679 — DE — 04.05.2016 — 000.002</p>'.encode(),
        )
    )
    assert extract_official_units(raw, "02016R0679-20160504", "deu")
    with pytest.raises(ValueError, match="language evidence"):
        extract_official_units(raw, "02016R0679-20160504", "eng")
    with pytest.raises(ValueError, match="CELEX/date"):
        extract_official_units(raw, "02016R0679-20240101", "deu")


def test_inserted_paragraph_suffixes_are_retained():
    units = extract_official_units(
        html().replace(b"016.007", b"016.007a"), CELEX, "eng"
    )
    assert units[2]["paragraph_num"] == "7a"


def test_legacy_html_and_oversize_input_are_reported(monkeypatch):
    with pytest.raises(UnsupportedHtml, match="No supported article"):
        extract_official_units(
            b'<html lang="en"><body><p>Article 1</p><p>Body</p></body></html>',
            CELEX,
            "eng",
        )
    monkeypatch.setattr(multilingual, "_MAX_BYTES", 10)
    with pytest.raises(UnsupportedHtml, match="50 MB"):
        extract_official_units(html(), CELEX, "eng")


def test_language_validation():
    assert normalize_languages(["EN", "eng", "deu", "FR", "el", "ga"]) == [
        "eng",
        "deu",
        "fra",
        "ell",
        "gle",
    ]
    assert len(normalize_languages(list(multilingual.LANGUAGES))) == 24
    for values in ([], ["zz"], ["../../eng"]):
        with pytest.raises(ValueError):
            normalize_languages(values)


def test_live_gdpr_excerpts_align_without_losing_greek_text():
    by_language = {
        language: extract_official_units(
            (FIXTURES / f"gdpr-article-7-{language}.xhtml").read_bytes(),
            CELEX,
            language,
        )
        for language in ["eng", "deu", "ell"]
    }
    expected = ["art_7", "007.001", "007.002", "007.003", "007.004"]
    for units in by_language.values():
        assert [unit["source_id"] for unit in units] == expected
        assert not units[0]["has_unanchored_text"]
        assert all(unit["text"] in units[0]["text"] for unit in units[1:])
    assert any(
        "\u0370" <= character <= "\u03ff" for character in by_language["ell"][1]["text"]
    )


def test_live_dora_replacement_ids_are_not_new_act_paragraphs():
    units = extract_official_units(
        (FIXTURES / "dora-article-60-eng.xhtml").read_bytes(), SECOND_CELEX, "eng"
    )
    assert len(units) == 1
    assert units[0]["source_id"] == "art_60"
    assert units[0]["has_unanchored_text"]
    assert "A CCP shall maintain and operate" in units[0]["text"]


@pytest.mark.parametrize("language", ["eng", "deu"])
def test_live_consolidated_excerpts_report_article_only_coverage(language):
    units = extract_official_units(
        (FIXTURES / f"consolidated-gdpr-article-7-{language}.xhtml").read_bytes(),
        "02016R0679-20160504",
        language,
    )
    assert len(units) == 1
    assert units[0]["unit_key"] == "02016R0679-20160504:art_7"
    assert units[0]["has_unanchored_text"]


def test_live_fixture_provenance_hashes():
    for source in json.loads((FIXTURES / "provenance.json").read_text()):
        raw = (FIXTURES / source["fixture"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == source["fixture_sha256"]


def test_output_is_separate_and_missing_identifiers_are_visible(tmp_path, monkeypatch):
    path = source_database(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    calls, closed = mock_source(
        monkeypatch,
        {
            (CELEX, "eng"): html(),
            (CELEX, "deu"): html("de", second_paragraph=False),
            (CELEX, "gle"): None,
        },
    )
    result = extract_multilingual_database(
        path, languages=["eng", "deu", "gle"], formats=["parquet", "csv"]
    )
    assert (result.extracted, result.unavailable, result.failed) == (2, 1, 0)
    assert len(calls) == 3 and closed == [True]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert result.output_directory == tmp_path / "source-multilingual"
    with duckdb.connect(
        str(result.output_directory / "multilingual.duckdb"), read_only=True
    ) as conn:
        assert len(conn.execute("SELECT * FROM multilingual_documents").fetchall()) == 3
        assert (
            conn.execute("SELECT count(*) FROM text_units_multilingual").fetchone()[0]
            == 7
        )
        assert conn.execute(
            "SELECT languages_present, language_count, requested_language_count, languages_missing "
            "FROM multilingual_coverage WHERE source_id = '016.007'"
        ).fetchone() == ("eng", 1, 3, "deu,gle")
        assert (
            conn.execute(
                "SELECT raw_html FROM _multilingual_sources WHERE language = 'eng'"
            ).fetchone()[0]
            == html()
        )
        assert (
            conn.execute(
                "SELECT source_sha256 FROM multilingual_documents WHERE language = 'eng'"
            ).fetchone()[0]
            == hashlib.sha256(html()).hexdigest()
        )
    for table in multilingual.EXPORT_TABLES:
        assert (result.output_directory / f"{table}.parquet").is_file()
        assert (result.output_directory / f"{table}.csv").is_file()
    assert not (result.output_directory / "works.parquet").exists()


def test_resume_adds_languages_without_refetching_completed_expressions(
    tmp_path, monkeypatch
):
    path = source_database(tmp_path)
    responses = {
        (CELEX, "eng"): html(),
        (CELEX, "deu"): None,
        (CELEX, "fra"): html("fr"),
    }
    calls, _ = mock_source(monkeypatch, responses)
    extract_multilingual_database(path, languages=["eng", "deu"])
    calls.clear()
    result = extract_multilingual_database(path, languages=["eng", "deu", "fra"])
    assert calls == [(CELEX, "fra")]
    assert result.skipped == 2
    responses[(CELEX, "deu")] = html("de")
    calls.clear()
    result = extract_multilingual_database(
        path, languages=["eng", "deu", "fra"], retry_unavailable=True
    )
    assert calls == [(CELEX, "deu")]
    with duckdb.connect(
        str(result.output_directory / "multilingual.duckdb"), read_only=True
    ) as conn:
        assert conn.execute(
            "SELECT DISTINCT language_count, languages_missing FROM multilingual_coverage"
        ).fetchall() == [(3, None)]


def test_failures_retry_and_successful_other_languages_survive(tmp_path, monkeypatch):
    path = source_database(tmp_path)
    responses = {
        (CELEX, "eng"): TransientSourceError("temporary failure"),
        (CELEX, "deu"): html("de"),
    }
    calls, _ = mock_source(monkeypatch, responses)
    first = extract_multilingual_database(path, languages=["eng", "deu"])
    assert first.failed == 1 and first.extracted == 1
    responses[(CELEX, "eng")] = html()
    calls.clear()
    second = extract_multilingual_database(path, languages=["eng", "deu"])
    assert calls == [(CELEX, "eng")]
    assert second.failed == 0 and second.skipped == 1


def test_interruption_preserves_completed_pairs_and_resumes(tmp_path, monkeypatch):
    path = source_database(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    responses = {(CELEX, "eng"): html(), (CELEX, "deu"): KeyboardInterrupt()}
    calls, closed = mock_source(monkeypatch, responses)
    with pytest.raises(KeyboardInterrupt):
        extract_multilingual_database(path, languages=["eng", "deu"])
    assert closed == [True]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    responses[(CELEX, "deu")] = html("de")
    calls.clear()
    result = extract_multilingual_database(path, languages=["eng", "deu"])
    assert calls == [(CELEX, "deu")]
    assert result.extracted == result.skipped == 1


def test_failed_expression_save_rolls_back_old_units_and_source(tmp_path, monkeypatch):
    path = source_database(tmp_path)
    mock_source(monkeypatch, {(CELEX, "eng"): html()})
    result = extract_multilingual_database(path, languages=["eng"])
    with duckdb.connect(str(result.output_directory / "multilingual.duckdb")) as conn:
        original = conn.execute(
            "SELECT * FROM text_units_multilingual ORDER BY unit_order"
        ).fetchall()
        unit = extract_official_units(html(), CELEX, "eng")[0]
        with pytest.raises(duckdb.ConstraintException):
            multilingual._record_expression(
                conn, CELEX, "eng", "extracted", {}, b"replacement", [unit, unit], ""
            )
        assert (
            conn.execute(
                "SELECT * FROM text_units_multilingual ORDER BY unit_order"
            ).fetchall()
            == original
        )
        assert (
            conn.execute("SELECT raw_html FROM _multilingual_sources").fetchone()[0]
            == html()
        )
        assert (
            conn.execute("SELECT source_sha256 FROM multilingual_documents").fetchone()[
                0
            ]
            == hashlib.sha256(html()).hexdigest()
        )


def test_limit_is_resumable_across_all_requested_languages(tmp_path, monkeypatch):
    path = source_database(tmp_path, [CELEX, SECOND_CELEX])
    calls, _ = mock_source(
        monkeypatch,
        {
            (celex, code): html(language)
            for celex in [CELEX, SECOND_CELEX]
            for code, language in [("eng", "en"), ("deu", "de")]
        },
    )
    extract_multilingual_database(path, languages=["eng", "deu"], limit=1)
    assert calls == [(CELEX, "eng"), (CELEX, "deu")]
    calls.clear()
    extract_multilingual_database(path, languages=["eng", "deu"], limit=1)
    assert calls == [(SECOND_CELEX, "eng"), (SECOND_CELEX, "deu")]


def test_unsupported_layout_is_reported_and_not_retried_by_default(
    tmp_path, monkeypatch
):
    path = source_database(tmp_path)
    calls, _ = mock_source(
        monkeypatch,
        {(CELEX, "eng"): b'<html lang="en"><body>Legacy text</body></html>'},
    )
    result = extract_multilingual_database(path, languages=["eng"])
    assert result.unsupported == 1 and result.failed == 0
    calls.clear()
    extract_multilingual_database(path, languages=["eng"])
    assert not calls


def test_wrong_language_is_a_failure_not_an_official_translation(tmp_path, monkeypatch):
    path = source_database(tmp_path)
    mock_source(monkeypatch, {(CELEX, "deu"): html("en")})
    result = extract_multilingual_database(path, languages=["deu"])
    assert result.failed == 1
    with duckdb.connect(
        str(result.output_directory / "multilingual.duckdb"), read_only=True
    ) as conn:
        assert (
            conn.execute("SELECT count(*) FROM text_units_multilingual").fetchone()[0]
            == 0
        )


def test_rejects_output_aliasing_input_and_invalid_options(tmp_path):
    path = source_database(tmp_path)
    alias = tmp_path / "multilingual.duckdb"
    alias.symlink_to(path)
    with pytest.raises(ValueError, match="separate from"):
        extract_multilingual_database(
            path, languages=["eng"], output_directory=tmp_path
        )
    for kwargs in (
        {"languages": ["xx"]},
        {"languages": ["eng"], "formats": []},
        {"languages": ["eng"], "limit": 0},
    ):
        with pytest.raises(ValueError):
            extract_multilingual_database(path, **kwargs)
    assert not (tmp_path / "source-multilingual").exists()


def test_rejects_export_aliasing_source_and_unrelated_existing_output(tmp_path):
    path = source_database(tmp_path)
    alias = tmp_path / "multilingual_documents.parquet"
    alias.symlink_to(path)
    with pytest.raises(ValueError, match="separate from"):
        extract_multilingual_database(
            path, languages=["eng"], output_directory=tmp_path
        )
    out = tmp_path / "other-output"
    out.mkdir()
    with duckdb.connect(str(out / "multilingual.duckdb")) as conn:
        conn.execute("CREATE TABLE unrelated AS SELECT 'keep me' AS value")
    with pytest.raises(ValueError, match="not a multilingual dataset"):
        extract_multilingual_database(path, languages=["eng"], output_directory=out)
    with duckdb.connect(str(out / "multilingual.duckdb"), read_only=True) as conn:
        assert conn.execute("SELECT * FROM unrelated").fetchall() == [("keep me",)]


def test_different_extractor_version_requires_separate_output(tmp_path, monkeypatch):
    path = source_database(tmp_path)
    calls, _ = mock_source(monkeypatch, {(CELEX, "eng"): html()})
    result = extract_multilingual_database(path, languages=["eng"])
    calls.clear()
    monkeypatch.setattr(multilingual, "EXTRACTION_VERSION", 2)
    with pytest.raises(ValueError, match="different multilingual extractor version"):
        extract_multilingual_database(path, languages=["eng"])
    assert not calls
    with duckdb.connect(
        str(result.output_directory / "multilingual.duckdb"), read_only=True
    ) as conn:
        assert (
            conn.execute("SELECT count(*) FROM text_units_multilingual").fetchone()[0]
            == 4
        )


def test_cli_forwards_options_and_exits_nonzero_after_failed_export(
    tmp_path, monkeypatch
):
    path = source_database(tmp_path)
    received = {}

    def run(db_path, **kwargs):
        received.update(db_path=db_path, **kwargs)
        return multilingual.MultilingualResult(
            output_directory=Path("output"), failed=1
        )

    monkeypatch.setattr(multilingual, "extract_multilingual_database", run)
    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "multilingual",
                str(path),
                "--languages",
                "eng",
                "deu",
                "--limit",
                "1",
                "--retry-unavailable",
            ]
        )
    assert exc.value.code == 1
    assert received["languages"] == ["eng", "deu"]
    assert received["limit"] == 1
    assert received["retry_unavailable"]


def test_fetch_provenance_is_opt_in_and_does_not_change_returned_bytes():
    source = CellarSource()
    source.session.close()
    source.session = SimpleNamespace(
        get=lambda *args, **kwargs: SimpleNamespace(
            status_code=200,
            content=html(),
            url="https://example.test/resolved",
            headers={"Content-Language": "en"},
        )
    )
    metadata = {}
    assert source._fetch_with_300_handling("https://example.test", {}, CELEX) == html()
    assert (
        source._fetch_with_300_handling(
            "https://example.test", {}, CELEX, out_metadata=metadata
        )
        == html()
    )
    assert metadata == {
        "source_url": "https://example.test/resolved",
        "http_status": 200,
        "content_language": "en",
    }
