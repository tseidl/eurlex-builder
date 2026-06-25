# eurlex-builder: Text extraction approach

## How we get documents

We fetch documents from the EU Cellar API (not scraping EUR-Lex). Metadata comes from a single SPARQL query per document (title, date, all relations). Content comes via REST content negotiation on `/resource/celex/{CELEX_ID}`, trying XHTML then HTML across 6 languages (eng → fra → deu → ita → nld → spa). If no HTML exists (common for pre-1980s docs), we fall back to PDF via a SPARQL manifest lookup.

## How we extract text from HTML

We detect which of four HTML structures the document uses, based on CSS classes and div IDs:

- **Standard OJ** (modern docs): articles in `<div id="art_1">`, recitals in `<div id="rct_1">`, annexes in `<div id="anx_I">`
- **Manual CSS** (pre-2010): articles marked by `<p class="Titrearticle">`, recitals by `<p class="li ManualConsidrant">`
- **Class-based OJ** (1970s-1990s formatted): articles in `<p class="ti-art">`, recitals as `<p class="normal">` starting with "Whereas"
- **Text-only** (oldest): all content in a `<div id="TexteOnly">`, parsed sequentially — "Whereas" lines are recitals, "Article N" headings start articles. Handles both old-style (`Whereas [text]`) and modern (`Whereas:` followed by `(1)`, `(2)`) recital formats.

If no structure is recognized or extraction yields zero recitals + articles, we fall back to PDF (if available) or extract the full body as a single text unit.

## How we extract text from PDF

PDF bytes go through Docling (IBM's layout-aware document parser), which outputs markdown. We then parse the markdown for `## Article N` headings and `Whereas` lines, similar to the text-only HTML parser but adapted for markdown conventions. Known limitation: Docling sometimes merges separate "Whereas" paragraphs into one line on scanned two-column PDFs, losing ~3-5% of recitals.

## Key design choices

- **Structural parsing over regex**: we rely on HTML DOM structure (XPath, div IDs, CSS classes) rather than regex on flattened text. This is more robust for modern documents but can't handle structures we haven't seen.
- **Heading detection heuristic**: in text-only HTML, we distinguish "Article 1" (a heading) from "Article 2(2) of Regulation..." (inline reference) by checking whether the text after "Article N" looks like prose continuation (starts with "of", "to", "the", "(", etc.).
- **No sentence-level splitting**: we extract recitals, articles, and annexes as whole units. Sentence tokenization is left to downstream analysis.

## Current accuracy (100-document comparison against reference JSON)

Dates: 100%. Recitals: ~95% true accuracy. Articles: ~98%. Most remaining errors are Docling paragraph merging on scanned PDFs — our HTML extraction is near-perfect on documents that have HTML.
