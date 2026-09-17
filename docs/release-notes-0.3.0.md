# eurlex-builder 0.3.0 — PDF structure recovery across languages

Improves article, recital and annex extraction in specific PDF layouts,
including fallback extraction after a Docling timeout.

- Recognises legislative headings and recital markers in French, German,
  Italian, Dutch and Spanish.
- Avoids false article and annex splits caused by cross-references and
  correlation tables; preserves ordinary prose mistaken for structural markers.
- Separates identified footnotes from recitals and repairs a narrowly defined
  two-column preamble layout.
- Corrects misplaced Docling headings only when the same PDF confirms the text
  belonging to each affected article. Original full text is preserved, and
  recovery steps are recorded.

**Validation:** 555 tests pass locally; 5,520 preservation comparisons are
unchanged. Additional checks cover 25 official PDF/language pairs across two
PyMuPDF versions and eight successful Docling conversions.

**Scope:** Fresh PDF runs may produce corrected counts and boundaries. HTML
extraction, configuration and the database schema are unchanged. Existing
datasets are not rebuilt; retain their original software version and DOI for
replication.

**Remaining limits:** General OCR and column reconstruction remain incomplete,
including three documented older PDFs. Correct counts alone do not establish
complete extraction.

[Detailed review](https://github.com/tseidl/eurlex-builder/blob/v0.3.0/docs/pdf-preamble-review-2026-09-17.md)
and [validation evidence](https://github.com/tseidl/eurlex-builder/blob/v0.3.0/docs/pdf-preamble-validation-2026-09-17.json).
