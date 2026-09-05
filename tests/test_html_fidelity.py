"""Protect source encodings, namespaces, and mixed HTML content from text loss."""

from __future__ import annotations

import codecs
from pathlib import Path
from unittest.mock import Mock

import pytest

from eurlex_builder.extractors.html import HtmlExtractor, extract_html_full_text
from eurlex_builder.html_parser import decode_html, parse_html
from eurlex_builder.pipeline import Pipeline
from eurlex_builder.sources.cellar import CellarSource

_TEXT = "Considérant le traité, l’Union établit des règles."
_LATIN_TEXT = "Considérant le traité."

_ENCODED_DOCUMENTS = [
    pytest.param(f'<?xml version="1.0" encoding="UTF-8"?><html><body><p>{_TEXT}</p></body></html>'.encode(), _TEXT, id="xml-utf8"),
    pytest.param(f'<html><head><meta charset="UTF-8"></head><body><p>{_TEXT}<br></p></body></html>'.encode(), _TEXT, id="html-utf8"),
    pytest.param(f'<?xml version="1.0" encoding="UTF-8"?><html><body><p>{_TEXT}<br></p></body></html>'.encode(), _TEXT, id="xml-declaration-html-recovery"),
    pytest.param(f'<html><head><meta charset="ISO-8859-1"></head><body><p>{_LATIN_TEXT}</p></body></html>'.encode("latin-1"), _LATIN_TEXT, id="html-latin1"),
    pytest.param(f'<?xml version="1.0" encoding="ISO-8859-1"?><html><body><p>{_LATIN_TEXT}</p></body></html>'.encode("latin-1"), _LATIN_TEXT, id="xml-latin1"),
    pytest.param(f'<html><head><meta charset="windows-1252"></head><body><p>{_TEXT}</p></body></html>'.encode("cp1252"), _TEXT, id="html-windows1252"),
    pytest.param(f'<html><head><meta http-equiv="Content-Type" content="text/html; charset=UNICODE-1-1-UTF-8"></head><body><p>{_TEXT}</p></body></html>'.encode(), _TEXT, id="legacy-utf8-alias"),
    pytest.param(codecs.BOM_UTF8 + f'<html><body><p>{_TEXT}<br></p></body></html>'.encode(), _TEXT, id="html-bom"),
    pytest.param(codecs.BOM_UTF8 + f'<html><head><meta charset="ISO-8859-1"></head><body><p>{_TEXT}</p></body></html>'.encode(), _TEXT, id="bom-over-meta"),
    pytest.param(f'<?xml version="1.0" encoding="UTF-8"?><html><head><meta charset="ISO-8859-1"/></head><body><p>{_TEXT}</p></body></html>'.encode(), _TEXT, id="xml-declaration-over-meta"),
    pytest.param(b'<html><body><p>Consid&#233;rant le trait&#233;.</p></body></html>', _LATIN_TEXT, id="entities"),
]


@pytest.mark.parametrize("raw, expected", _ENCODED_DOCUMENTS)
def test_source_to_extraction_preserves_declared_encoding(raw, expected):
    source = CellarSource()
    source.session.close()
    source.session = Mock()
    source.session.get.return_value = Mock(status_code=200, content=raw)

    fetched = source.fetch_content("32020R0001")

    assert fetched == (raw, "html", "eng")
    assert extract_html_full_text(fetched[0]) == expected
    assert HtmlExtractor().extract("32020R0001", fetched[0])[0]["text"] == expected
    assert HtmlExtractor().extract_com("52020DC0001", fetched[0])[0]["text"] == expected
    _, metadata = Pipeline._unpack_content(fetched, {})
    assert expected in metadata["full_text_html"] or b"&#" in raw
    assert metadata["full_text_html"] == decode_html(raw)


@pytest.mark.parametrize("namespace", ["", ' xmlns="http://www.w3.org/1999/xhtml"'])
@pytest.mark.parametrize("malformed", [False, True])
def test_namespace_and_html_recovery_preserve_structural_coordinates(namespace, malformed):
    raw = f'''<html{namespace}><body><div class="content">
    <div id="art_1"><p>1. States shall:</p><div><p>(a) collect information;</p>
    <p>(b) report annually.{'<br>' if malformed else ''}</p></div></div>
    </div></body></html>'''.encode()
    tree = parse_html(raw)
    assert tree.tag == ("{http://www.w3.org/1999/xhtml}html" if namespace and not malformed else "html")
    units = HtmlExtractor().extract("32020R0001", raw, article_granularity="point")
    assert [(u["number"], u["paragraph_num"], u["point_letter"]) for u in units] == [
        ("1", "1", None), ("1", "1", "a"), ("1", "1", "b"),
    ]


@pytest.mark.parametrize("raw", [b"", b"\xff\xfe", b"<html><body>\xff<br></body></html>"])
def test_diagnostic_raw_storage_has_a_defined_fallback(raw):
    assert isinstance(decode_html(raw), str)
    _, metadata = Pipeline._unpack_content((raw, "html", "eng"), {}, store_raw_html=False)
    assert metadata["full_text_html"] is None


def test_raw_storage_falls_back_when_parser_reports_no_encoding(monkeypatch):
    monkeypatch.setattr("eurlex_builder.html_parser.parse_html", lambda raw: Mock(getroottree=lambda: Mock(docinfo=Mock(encoding=None))))
    assert decode_html(b"invalid \xff") == "invalid \ufffd"


@pytest.mark.parametrize("namespace", ["", ' xmlns="http://www.w3.org/1999/xhtml"'])
def test_full_text_preserves_text_outside_paragraphs_without_duplicates(namespace):
    raw = f'''<html{namespace}><head><title>Page title</title></head><body>
    <div id="banner"><div class="bglang"><p>Language selector</p></div>
    <div class="bgtool"><em>|</em></div></div><h1>32020R0001</h1>
    <p class="hd-oj">Banner</p><div class="content">Introduction.
    <h2>Heading</h2><p>Main <b>body</b> text.</p>
    <table><tr><td>(a)</td><td>Table content.</td></tr></table>
    <ul><li>List <em>content.</em></li></ul>Conclusion.</div>
    <script>Hidden script</script><style>Hidden style</style>
    <!-- Hidden comment -->Final tail.</body></html>'''.encode()
    assert extract_html_full_text(raw) == (
        "Introduction. Heading Main body text. (a) Table content. "
        "List content. Conclusion. Final tail."
    )


def test_full_text_preserves_outer_text_of_nested_paragraphs():
    raw = b'<html><body><p>Outer start.<p>Inner text.</p>Outer end.</p></body></html>'
    assert extract_html_full_text(raw) == "Outer start. Inner text. Outer end."


def test_article_preserves_mixed_text_comments_and_block_boundaries():
    raw = b'''<html><body><div id="art_1">
    <p class="eli-title">Article 1</p>
    1. Member States <!-- Hidden -->shall <span>report</span> annually.
    <div><p>(a) Collect information.</p><p>(b) Publish findings.</p></div>
    <div>2. Reporting <b>shall</b> include <i>all</i> findings.</div>
    <script>Hidden script</script><?notice hidden?>
    <div>3. Final obligation.</div></div></body></html>'''
    units = HtmlExtractor().extract("32020R0001", raw, article_granularity="point")
    assert [(u["paragraph_num"], u["point_letter"], u["text"]) for u in units] == [
        ("1", None, "Member States shall report annually."),
        ("1", "a", "Collect information."),
        ("1", "b", "Publish findings."),
        ("2", None, "Reporting shall include all findings."),
        ("3", None, "Final obligation."),
    ]


def test_standalone_inline_point_markers_keep_existing_boundaries():
    raw = b'''<html><body><div id="art_1"><span>1. States shall:</span>
    <span>(a) collect information;</span><span>(b) report annually.</span>
    </div></body></html>'''
    units = HtmlExtractor().extract("32020R0001", raw, article_granularity="point")
    assert [(u["paragraph_num"], u["point_letter"]) for u in units] == [
        ("1", None), ("1", "a"), ("1", "b"),
    ]


@pytest.mark.parametrize("prefix", ["", "oj-"])
def test_annex_prefix_variants_preserve_multiple_annex_boundaries(prefix):
    raw = f'''<html><body><p class="{prefix}ti-art">Article 1</p>
    <p>Complete provision.</p><p class="{prefix}ti-grseq-1">ANNEX I</p>
    <p>First annex.</p><p class="{prefix}ti-grseq-1">ANNEX II</p>
    <p>Second annex.</p><p class="{prefix}doc-end">End marker</p></body></html>'''.encode()
    units = HtmlExtractor().extract("32020R0001", raw)
    assert [(u["number"], u["text"]) for u in units if u["type"] == "annex"] == [
        ("I", "First annex."), ("II", "Second annex."),
    ]
    assert [u["text"] for u in units if u["type"] == "article"] == ["Complete provision."]


@pytest.mark.parametrize("body", ["<p>Substantive body.</p>", "<div>Substantive body.</div>", "<table><tr><td>Substantive body.</td></tr></table>"])
@pytest.mark.parametrize("footnote", ["", '<dl><dd id="footnote1"><p><a class="footnote">(1)</a>Footnote text.</p></dd></dl>'])
def test_com_body_fallback_is_independent_of_footnotes(body, footnote):
    raw = f'<html><body>{body}{footnote}</body></html>'.encode()
    units = HtmlExtractor().extract_com("52020DC0001", raw)
    assert units[0]["type"] == "body"
    assert units[0]["text"] == "Substantive body."
    assert len(units) == (2 if footnote else 1)
    if footnote:
        assert units[1]["type"] == "footnote"
        assert units[1]["text"] == "Footnote text."
        assert extract_html_full_text(raw) == "Substantive body. (1) Footnote text."


@pytest.mark.parametrize("granularity", ["article", "paragraph", "point"])
@pytest.mark.parametrize("filename, article, phrases", [
    ("consolidated-copyright-article-6.xhtml", "6", [
        "For the purposes of this Directive, the expression ‘technological measures’ means any technology",
        "sui generis right provided for in Chapter III of Directive 96/9/EC.",
        "which achieves the protection objective.",
    ]),
    ("consolidated-gdpr-article-99.xhtml", "99", [
        "This Regulation shall enter into force on the twentieth day",
        "Official Journal of the European Union .", "It shall apply from 25 May 2018.",
    ]),
    ("consolidated-safety-article-8.xhtml", "8", [
        "Pursuant to paragraph 1, the employer shall, inter alia, for first aid",
        "designate the workers required to implement such measures.",
    ]),
    ("copyright-directive-article-24.xhtml", "24", [
        "where there is use for the sole purpose of illustration for teaching or scientific research",
        "in the case of extraction for the purposes of illustration for teaching or scientific research",
    ]),
])
def test_live_excerpts_retain_substantive_text_across_extraction_apis(filename, article, phrases, granularity):
    raw = (Path(__file__).parent / "fixtures" / "html" / filename).read_bytes()
    units = HtmlExtractor().extract("32020R0001", raw, article_granularity=granularity)
    assert {u["number"] for u in units} == {article}
    texts = [" ".join(u["text"] for u in units), extract_html_full_text(raw)]
    for phrase in phrases:
        for text in texts:
            assert phrase in text


def test_recovered_consolidated_subparagraphs_keep_their_parent():
    raw = (Path(__file__).parent / "fixtures" / "html" / "consolidated-safety-article-8.xhtml").read_bytes()
    units = HtmlExtractor().extract("01989L0391-20081211", raw, article_granularity="point")
    paragraph = [u for u in units if u["paragraph_num"] == "2"]
    assert [u["subparagraph_num"] for u in paragraph] == ["1", "2"]
    assert paragraph[0]["text"].startswith("Pursuant to paragraph 1, the employer shall,")
    assert paragraph[1]["text"].startswith("The number of such workers,")
