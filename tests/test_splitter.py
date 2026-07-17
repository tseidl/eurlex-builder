"""Unit tests for the sub-article splitter."""

from __future__ import annotations

from eurlex_builder.extractors.splitter import (
    _find_quoted_regions,
    is_amending_article,
    split_article,
)


# ---------------------------------------------------------------------------
# Article-level (default) — must reproduce current behavior bit-for-bit.
# ---------------------------------------------------------------------------

def test_article_level_single_block():
    parts = ["This Regulation shall enter into force on 28 August 2003."]
    units = split_article(parts, number="20", title=None, granularity="article")
    assert len(units) == 1
    assert units[0]["type"] == "article"
    assert units[0]["paragraph_num"] is None
    assert units[0]["point_letter"] is None
    assert units[0]["text"] == "This Regulation shall enter into force on 28 August 2003."


def test_article_level_multi_block_joins_with_space():
    parts = ["1. First paragraph.", "2. Second paragraph.", "3. Third."]
    units = split_article(parts, number="5", title="Some title", granularity="article")
    assert len(units) == 1
    assert units[0]["text"] == "1. First paragraph. 2. Second paragraph. 3. Third."
    assert units[0]["title"] == "Some title"
    assert units[0]["paragraph_num"] is None


def test_article_level_empty():
    units = split_article([], number="1", title=None, granularity="article")
    assert len(units) == 1
    assert units[0]["text"] == ""


# ---------------------------------------------------------------------------
# Paragraph-level — clean modern-OJ case (one paragraph per body_part).
# ---------------------------------------------------------------------------

def test_paragraph_level_clean_split():
    parts = [
        "1. The first paragraph says X.",
        "2. The second paragraph says Y.",
        "3. The third paragraph says Z.",
    ]
    units = split_article(parts, number="5", title="My Article", granularity="paragraph")
    assert len(units) == 3
    assert [u["paragraph_num"] for u in units] == ["1", "2", "3"]
    assert units[0]["text"] == "The first paragraph says X."
    assert units[1]["text"] == "The second paragraph says Y."
    assert units[2]["text"] == "The third paragraph says Z."
    # Title attaches to first row only.
    assert units[0]["title"] == "My Article"
    assert units[1]["title"] is None
    assert units[2]["title"] is None


def test_paragraph_level_preamble_gets_paragraph_zero():
    parts = [
        "Member States shall ensure the following.",  # preamble (no number)
        "1. First obligation.",
        "2. Second obligation.",
    ]
    units = split_article(parts, number="10", title="Obligations", granularity="paragraph")
    assert len(units) == 3
    assert units[0]["paragraph_num"] == "0"
    assert units[0]["text"] == "Member States shall ensure the following."
    assert units[0]["title"] == "Obligations"  # title on preamble
    assert units[1]["paragraph_num"] == "1"
    assert units[1]["title"] is None  # title NOT repeated on first numbered paragraph


def test_paragraph_level_letter_suffix():
    parts = [
        "1. First paragraph.",
        "1a. Amendment-inserted paragraph.",
        "2. Second paragraph.",
        "2a. Another amendment.",
    ]
    units = split_article(parts, number="5", title=None, granularity="paragraph")
    assert [u["paragraph_num"] for u in units] == ["1", "1a", "2", "2a"]


def test_paragraph_level_inline_smushed_text():
    """Older HTML / PDF can produce one body_part with multiple paragraphs inline."""
    parts = [
        "1. First. 2. Second. 3. Third.",
    ]
    units = split_article(parts, number="5", title=None, granularity="paragraph")
    # Lenient fallback should find all three.
    assert [u["paragraph_num"] for u in units] == ["1", "2", "3"]
    assert units[0]["text"] == "First."
    assert units[1]["text"] == "Second."
    assert units[2]["text"] == "Third."


def test_paragraph_level_handles_pdf_ocr_artifact():
    """1980s PDF extraction can produce '1 .' with a space between digit and period."""
    parts = [
        "1 . The column headed 'EXAA8a' is replaced. 2. The column headed 'UK' is replaced.",
    ]
    units = split_article(parts, number="1", title=None, granularity="paragraph")
    assert [u["paragraph_num"] for u in units] == ["1", "2"]
    assert "EXAA8a" in units[0]["text"]
    assert "UK" in units[1]["text"]


def test_paragraph_level_no_numbered_structure_falls_back_to_single_unit():
    """An article that has no numbered paragraphs stays as one row."""
    parts = ["The contractual liability of the Centre shall be governed by the law applicable."]
    units = split_article(parts, number="18", title=None, granularity="paragraph")
    assert len(units) == 1
    assert units[0]["paragraph_num"] is None  # no split detected
    assert units[0]["text"] == parts[0]


def test_paragraph_level_does_not_falsely_split_on_cross_references():
    """'Article 5(2)(a)' contains digits but no period after — must not match."""
    parts = [
        "The matters referred to in Article 5(2)(a) shall be governed by this Regulation.",
    ]
    units = split_article(parts, number="3", title=None, granularity="paragraph")
    assert len(units) == 1


# ---------------------------------------------------------------------------
# Point-level — lettered (a), (b), ...
# ---------------------------------------------------------------------------

def test_point_level_splits_lettered_points():
    # Realistic HTML structure: each point comes as its own <p> element,
    # so each is a separate body_part. Joined with newlines internally.
    parts = [
        "1. Member States shall ensure that the following are protected:",
        "(a) personal data;",
        "(b) sensitive data;",
        "(c) biometric data.",
        "2. The Commission shall report annually.",
    ]
    units = split_article(parts, number="5", title=None, granularity="point")
    # Expect: paragraph 1 stem + 3 points, then paragraph 2 as single unit (no points).
    paragraph_nums = [u["paragraph_num"] for u in units]
    point_letters = [u["point_letter"] for u in units]
    assert "1" in paragraph_nums
    assert point_letters.count("a") == 1
    assert point_letters.count("b") == 1
    assert point_letters.count("c") == 1
    # Paragraph 2 stays as one unit (no points).
    p2 = [u for u in units if u["paragraph_num"] == "2"]
    assert len(p2) == 1
    assert p2[0]["point_letter"] is None


def test_point_level_trailing_subparagraph_emitted_separately():
    """A non-point line after the last point gets its own row (GDPR Art 6(1) pattern)."""
    parts = [
        "1. Processing shall be lawful if at least one applies:",
        "(a) consent;",
        "(b) contract;",
        "(c) legal obligation;",
        "Point (c) of the first subparagraph shall not apply to processing carried out by public authorities.",
    ]
    units = split_article(parts, number="6", title=None, granularity="point")
    # Expect: stem, (a), (b), (c), and trailing subparagraph — 5 rows, all paragraph_num=1.
    assert len(units) == 5
    point_letters = [u["point_letter"] for u in units]
    assert point_letters.count("a") == 1
    assert point_letters.count("b") == 1
    assert point_letters.count("c") == 1
    # Trailing subparagraph has point_letter=None and contains the qualifier text.
    trailing = [u for u in units if u["point_letter"] is None and "shall not apply" in u["text"]]
    assert len(trailing) == 1
    assert trailing[0]["paragraph_num"] == "1"
    # The (c) point's text does NOT contain the trailing subparagraph.
    c_point = [u for u in units if u["point_letter"] == "c"][0]
    assert "shall not apply" not in c_point["text"]


def test_point_level_continuation_line_between_points_belongs_to_preceding():
    """Regression for bug #4 from external review: a non-point line between
    two points must be absorbed into the preceding point, not dropped."""
    parts = [
        "1. The following apply:",
        "(a) first line",
        "continued line for point a",
        "(b) second line",
    ]
    units = split_article(parts, number="5", title=None, granularity="point")
    # Expect stem + (a) (with continuation) + (b)
    assert len(units) == 3
    a_point = [u for u in units if u["point_letter"] == "a"][0]
    assert "first line" in a_point["text"]
    assert "continued line for point a" in a_point["text"]
    b_point = [u for u in units if u["point_letter"] == "b"][0]
    assert "second line" in b_point["text"]
    # Continuation must NOT be its own row.
    assert all(u["point_letter"] is not None or u["paragraph_num"] == "1" for u in units)


def test_paragraph_level_partially_smushed_recovers_all_three():
    """Regression for bug #5 from external review: when strict finds some
    markers and lenient finds more siblings (smushed onto the same line),
    we must use lenient — not return early on strict>=2."""
    parts = ["1. First. 2. Second.", "3. Third."]
    units = split_article(parts, number="5", title=None, granularity="paragraph")
    nums = [u["paragraph_num"] for u in units]
    assert nums == ["1", "2", "3"]
    assert "First." in units[0]["text"]
    assert "Second." in units[1]["text"]
    assert "Third." in units[2]["text"]


def test_point_level_ignores_inline_cross_references():
    """'point (f) of paragraph 1' must NOT match as a point marker."""
    parts = [
        "1. Processing shall be lawful if at least one applies:",
        "(a) consent;",
        "(b) contract;",
        "Point (f) of the first subparagraph shall not apply to processing carried out by public authorities.",
    ]
    units = split_article(parts, number="6", title=None, granularity="point")
    point_letters = [u["point_letter"] for u in units]
    # Only one (a) and one (b) — no false (f) from the cross-reference.
    assert point_letters.count("a") == 1
    assert point_letters.count("b") == 1
    assert "f" not in point_letters


def test_point_level_paragraph_without_points_emits_one_row():
    parts = ["1. A simple paragraph with no lettered points.", "2. Another simple paragraph."]
    units = split_article(parts, number="5", title=None, granularity="point")
    assert len(units) == 2
    assert all(u["point_letter"] is None for u in units)


# ---------------------------------------------------------------------------
# Amending-article handling.
# ---------------------------------------------------------------------------

def test_is_amending_article_by_title():
    assert is_amending_article("Amendments to Regulation (EU) No 575/2013", "")
    assert is_amending_article("Amending Directive 2009/138/EC", "")
    assert is_amending_article("Modifications to Decision 2014/415", "")
    assert not is_amending_article("Definitions", "")
    assert not is_amending_article(None, "")


def test_amending_article_mechanical_edits_get_amendment_item_subtype():
    parts = [
        "1. Article 5 is deleted.",
        "2. In Article 6, paragraph 2, the words 'X' are replaced by 'Y'.",
        "3. The following Article is inserted after Article 8: 'Article 8a — ...'.",
    ]
    units = split_article(
        parts, number="1", title="Amendments to Regulation (EU) No 575/2013",
        granularity="paragraph",
    )
    # First two are mechanical (no quoted block), third has a quoted block (substantive).
    assert units[0]["subtype"] == "amendment_item"
    assert units[1]["subtype"] == "amendment_item"
    assert units[2]["subtype"] is None  # substantive replacement


def test_amending_article_substantive_replacement_detected_via_quote():
    parts = [
        "1. Article 10 is replaced by the following: 'Article 10 — New scope: 1. The scope shall be...'.",
    ]
    units = split_article(
        parts, number="1", title="Amendments to Directive 2009/138/EC",
        granularity="paragraph",
    )
    # Regression for bug #3 from external review: the inner "1." inside the
    # quoted replacement text must NOT trigger a second paragraph split.
    assert len(units) == 1
    assert units[0]["paragraph_num"] == "1"
    assert units[0]["subtype"] is None
    # Full quoted replacement text must be preserved.
    assert "The scope shall be" in units[0]["text"]


def test_amending_article_does_not_split_inside_quoted_smart_quote_block():
    """Smart-quote variant: ‘ ... ’ used by modern OJ HTML."""
    parts = [
        "1. Article 5 is replaced by the following: ‘Article 5\n1. First requirement.\n2. Second requirement.’",
    ]
    units = split_article(
        parts, number="1", title="Amendments to Regulation (EU) No 575/2013",
        granularity="paragraph",
    )
    assert len(units) == 1
    assert "First requirement" in units[0]["text"]
    assert "Second requirement" in units[0]["text"]


def test_non_amending_article_never_gets_amendment_item_subtype():
    parts = ["1. The Commission shall report.", "2. Reports shall be public."]
    units = split_article(parts, number="20", title="Reporting", granularity="paragraph")
    assert all(u["subtype"] is None for u in units)


# ---------------------------------------------------------------------------
# Recital semantics — splitter is NEVER called on recitals; sanity check that
# the function only emits type='article' to prevent accidental misuse.
# ---------------------------------------------------------------------------

def test_emits_article_type_only():
    parts = ["1. text.", "2. more text."]
    for granularity in ("article", "paragraph", "point"):
        units = split_article(parts, number="5", title=None, granularity=granularity)
        assert all(u["type"] == "article" for u in units)


# ---------------------------------------------------------------------------
# Quoted-region regressions (2026-07 review): apostrophes inside quoted
# replacement law must not end the region; unterminated quotes cover the rest.
# ---------------------------------------------------------------------------

def test_quoted_block_with_apostrophe_is_not_split():
    """An apostrophe (’) inside the quoted replacement text must not close
    the quoted region — the inner '1.' / '2.' markers stay unsplit."""
    parts = [
        "Article 5 is replaced by the following:",
        "‘Article 5",
        "Member States’ obligations",
        "1. Each Member State shall designate an authority.",
        "2. The authority shall report annually.’.",
    ]
    units = split_article(
        parts, number="1", title="Amendments to Directive X", granularity="paragraph",
    )
    assert len(units) == 1
    assert units[0]["paragraph_num"] is None
    assert "report annually" in units[0]["text"]
    # The quoted law must never be tagged as a mechanical edit.
    assert units[0]["subtype"] is None


def test_unterminated_quote_treats_rest_as_quoted():
    parts = [
        "Article 5 is replaced by the following:",
        "‘Article 5",
        "1. Each Member State shall designate an authority.",
        "2. The authority shall report annually.",
    ]
    units = split_article(
        parts, number="1", title="Amendments to Directive X", granularity="paragraph",
    )
    assert len(units) == 1
    assert "report annually" in units[0]["text"]


def test_straight_apostrophe_inside_quoted_block_is_not_split():
    parts = [
        "1. Article 10 is replaced by the following: 'Article 10 Member States' obligations: 1. The scope shall be broad.'.",
    ]
    units = split_article(
        parts, number="1", title="Amendments to Regulation X", granularity="paragraph",
    )
    assert len(units) == 1
    assert "The scope shall be broad" in units[0]["text"]


def test_legacy_backtick_closes_straight_apostrophe_quoted_block():
    text = (
        "Article 2 is replaced by the following: 'Article 2 France's "
        "obligations apply.` Article 2 This Decision is addressed to France."
    )

    regions = _find_quoted_regions(text)

    inner = text.index("Article 2 France")
    outer = text.rindex("Article 2 This Decision")
    assert any(start <= inner < end for start, end in regions)
    assert not any(start <= outer < end for start, end in regions)


# ---------------------------------------------------------------------------
# Point-sequence regressions (2026-07 review): double-letter insertions,
# roman sub-points, and wrapped continuations of the last point.
# ---------------------------------------------------------------------------

def test_point_level_double_letter_insertion():
    """Amendment-inserted points like (aa) between (a) and (b) get own rows."""
    parts = [
        "1. The following categories apply:",
        "(a) first point;",
        "(aa) inserted point;",
        "(b) second point.",
    ]
    units = split_article(parts, number="3", title=None, granularity="point")
    letters = [u["point_letter"] for u in units if u["point_letter"]]
    assert letters == ["a", "aa", "b"]
    aa = [u for u in units if u["point_letter"] == "aa"][0]
    assert aa["text"] == "inserted point;"


def test_point_level_roman_subpoints_stay_inside_parent_point():
    """Roman sub-points (i), (ii) subdivide a point — they must stay inside
    it, not become points 'i' etc. or dangling fragments."""
    parts = [
        "1. The following categories apply:",
        "(a) first point;",
        "(b) second point, including:",
        "(i) first sub-point;",
        "(ii) second sub-point.",
    ]
    units = split_article(parts, number="3", title=None, granularity="point")
    letters = [u["point_letter"] for u in units if u["point_letter"]]
    assert letters == ["a", "b"]
    b_point = [u for u in units if u["point_letter"] == "b"][0]
    assert "first sub-point" in b_point["text"]
    assert "second sub-point" in b_point["text"]


def test_point_level_letter_i_after_h_is_a_real_point():
    """(i) following (h) is the expected successor, not a roman sub-point."""
    parts = ["1. List:"] + [f"({c}) point {c};" for c in "abcdefghi"]
    units = split_article(parts, number="3", title=None, granularity="point")
    letters = [u["point_letter"] for u in units if u["point_letter"]]
    assert letters == list("abcdefghi")


def test_point_level_sequence_not_starting_at_a_falls_back():
    """Markers that don't start at (a) (e.g. quoted fragments) don't split."""
    parts = [
        "1. As set out below:",
        "(c) some text that looks like a point;",
        "(d) some more text.",
    ]
    units = split_article(parts, number="3", title=None, granularity="point")
    assert all(u["point_letter"] is None for u in units)


def test_point_level_wrapped_continuation_of_last_point_absorbed():
    """A lowercase-starting wrapped line after the last point belongs to that
    point; only uppercase-starting lines are trailing subparagraphs."""
    parts = [
        "1. Processing shall be lawful only if:",
        "(a) the data subject has given consent;",
        "(b) processing is necessary for compliance",
        "with a legal obligation to which the controller is subject.",
    ]
    units = split_article(parts, number="6", title=None, granularity="point")
    assert len(units) == 3  # stem, (a), (b) — no fragment row
    b_point = [u for u in units if u["point_letter"] == "b"][0]
    assert b_point["text"].endswith("controller is subject.")


# ---------------------------------------------------------------------------
# Second-round regressions (2026-07 xhigh review of the first fix round).
# ---------------------------------------------------------------------------

def test_inline_quoted_term_does_not_suppress_later_paragraphs():
    """An inline quoted term whose close quote is followed by a space must
    close there — not swallow the rest of the article as 'quoted'."""
    parts = [
        "1. The programme shall be known as: ‘Erasmus+’ and shall cover the period.",
        "2. Member States shall participate.",
        "3. The Commission shall report.",
    ]
    units = split_article(parts, number="1", title=None, granularity="paragraph")
    assert [u["paragraph_num"] for u in units] == ["1", "2", "3"]


def test_point_sequence_tolerates_deleted_points():
    """Consolidated text with point (c) deleted: (d)/(e) keep their own rows."""
    parts = ["1. List:", "(a) point a;", "(b) point b;", "(d) point d;", "(e) point e."]
    units = split_article(parts, number="1", title=None, granularity="point")
    letters = [u["point_letter"] for u in units if u["point_letter"]]
    assert letters == ["a", "b", "d", "e"]


def test_point_sequence_starts_at_b_when_a_is_inline_in_stem():
    """(a) smushed into the stem line: points (b) onward still split."""
    parts = [
        "1. Lawful only if: (a) consent is given;",
        "(b) contract;",
        "(c) legal obligation;",
        "(d) vital interests.",
    ]
    units = split_article(parts, number="1", title=None, granularity="point")
    letters = [u["point_letter"] for u in units if u["point_letter"]]
    assert letters == ["b", "c", "d"]


def test_point_level_capitalized_wrapped_continuation_absorbed():
    """A wrapped continuation starting with a capitalized word (no sentence
    punctuation before the break) stays inside the last point."""
    parts = [
        "1. The following apply:",
        "(a) consent;",
        "(b) processing is necessary in accordance with",
        "Regulation (EU) 2016/679 of the European Parliament.",
    ]
    units = split_article(parts, number="1", title=None, granularity="point")
    assert len(units) == 3
    b_point = [u for u in units if u["point_letter"] == "b"][0]
    assert "Regulation (EU) 2016/679" in b_point["text"]


def test_pdf_style_wrapped_lines_do_not_create_unnumbered_subparagraphs():
    parts = [
        "1. This provision applies to every controller established in the Union",
        "Regulation requirements continue on this visually wrapped source line",
    ]
    units = split_article(parts, number="1", title=None, granularity="point")
    assert len(units) == 1
    assert "visually wrapped" in units[0]["text"]
    assert units[0]["subparagraph_num"] is None


def test_html_boundaries_can_emit_numbered_subparagraphs():
    parts = [
        "1. This is a complete first subparagraph with enough substantive legal text.",
        "This is a complete second subparagraph with enough substantive legal text.",
    ]
    units = split_article(
        parts,
        number="1",
        title=None,
        granularity="point",
        split_unnumbered_subparagraphs=True,
    )
    assert [unit["subparagraph_num"] for unit in units] == ["1", "2"]


def test_trailing_subparagraph_gets_structural_number():
    parts = [
        "1. The following apply:",
        "(a) first point;",
        "(b) second point.",
        "Point (b) shall not apply to public authorities.",
    ]
    units = split_article(parts, number="1", title=None, granularity="point")
    primary = [unit for unit in units if unit.get("point_letter")]
    trailing = [unit for unit in units if "public authorities" in unit["text"]]
    assert all(unit["subparagraph_num"] == "1" for unit in primary)
    assert trailing[0]["subparagraph_num"] == "2"
