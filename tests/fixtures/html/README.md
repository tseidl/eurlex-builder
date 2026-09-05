# HTML regression excerpts

These excerpts retain the article elements from English Cellar HTML downloaded
on 5 September 2026. `provenance.json` records the source URLs, original-response
SHA-256 hashes, selected element IDs, and fixture hashes. The selected XML
elements were serialized as UTF-8 and enclosed in an XHTML body; these are
structural excerpts, not byte-identical copies of the complete HTTP responses.

The three consolidated articles exercise substantive text before and after
inline spans, including a paragraph followed by an unnumbered subparagraph.
Copyright Directive Article 24 exercises replacement text inside table cells
that was present in text units but absent from `works.full_text`.

Synthetic fixtures in `test_html_fidelity.py` separately test raw response-byte
preservation, charset declarations, HTML recovery, comments, and body fallback.
