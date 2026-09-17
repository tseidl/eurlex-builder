# PDF language regression fixtures

These are public EUR-Lex source texts downloaded on 16 September 2026, not
repaired dataset rows. URLs, PDF/text SHA-256 hashes, and excerpt boundaries
are recorded in `provenance.json` or the individual JSON fixtures. Extraction uses the package's unchanged
PyMuPDF text-layer helper, preserving its line and column order.

- `31982D0809-spa.md`: complete Spanish text of Council Decision 82/809/EEC and
  its attachments. The fresh extraction is byte-identical to the independently
  saved body-only failure case. The decision has two articles and annexes I/II;
  the tin agreement's articles and annexes A–G belong inside annex II. The text
  intentionally retains interleaved columns: introductory citations appear
  after article 1's heading, and the recitals appear after its signature.
  The layout tests pair it with original page geometry to check recovery while
  preserving this raw source unchanged.
- `gdpr-articles-1-2.json`: five verbatim selections from the official French,
  German, Italian, Dutch and Spanish GDPR PDFs. Each record contains three
  separate fragments: enacting formula; articles 1 and 2; signature and following
  lines. Tests concatenate these fragments deliberately; they are not complete
  documents. The fixtures check real heading forms, unchanged source words and
  the signature boundary.
- `preamble-layout-pages.json`: 14 complete public-source pages, with original
  text, page dimensions, line bounding boxes, span font sizes, PDF hashes and
  URLs. Geometry was read with PyMuPDF 1.27.2.3, `get_text("dict",
  flags=TEXTFLAGS_TEXT)`; unused fields were omitted. These cover all six GDPR
  first pages, Italian/Dutch footnotes split across blocks, a large footnote
  band in `32019R2088`, detached anchors in `32010R1095`, and the Spanish 1982
  and English 1983 column layouts. Tests replay the saved page API; the 1983
  page is a negative case that must remain unreordered. The final record is
  page 2 of `32014L0065`, read with PyMuPDF 1.28.2, covering slightly overlapping
  glyph boxes in a split footnote group.
- `gdpr-displaced-headings.json`: exact article 73–77 excerpts from four public
  GDPR PDFs and their successful Docling 2.107.0 conversions. Each record retains
  the URL, source PDF hash and complete Docling Markdown hash. The French,
  German, Italian and Dutch conversions displaced article 74's heading/title
  after article 76. Tests prepend synthetic empty headings 1–72 and an enacting
  formula to exercise the complete-sequence guard; these prefixes are test
  scaffolding, not claimed source text. The real excerpts check full article
  membership, unchanged surrounding units and preservation of Markdown words.

No test downloads documents or loads a translation model. Timeout and translation
rejection tests simulate those failures and exercise the real extraction and
pipeline control flow. The complete-document live comparisons and actual
Docling conversions are separate checks recorded in the
[follow-up review](../../../docs/pdf-preamble-review-2026-09-17.md).
