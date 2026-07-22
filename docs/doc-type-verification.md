# Document-type verification: smoke test of non-legislative types

**Date:** 2026-07-22 · **Package:** eurlex-builder v0.1.0 · **Scope:** fixed-mode communications (DC), proposals (PC), staff working documents (SC), and sector-6 case law (CJ/CC/CO). The validated regulation/directive/decision path was not re-tested here.

**Method:** 17 CELEX IDs spanning 1962–2021, one fixed-mode config per document type, run through the unmodified CLI (`eurlex-builder run … --fresh`, sequential). Executed by GPT-5 Codex (xhigh reasoning) under Claude Code supervision. The test sandbox blocked live Python sockets, so exact Cellar responses (SPARQL JSON, HTTP-300 choice pages, HTML/PDF streams) were pre-fetched with `curl` and replayed to the CLI; everything downstream of transport — metadata parsing, stream selection, language/PDF fallback, extraction, translation, storage, checkpointing, export, validation — ran live.

## Executive result

All 17 documents resolved in Cellar and produced a `works` row with title, date, language, and full text. The 12 DC/PC/SC documents produced 5,113 text units; all five court documents produced **zero** units because sector-6 type codes map to `unknown` and enter neither the legislative nor the COM-style extractor branch. Every run exited 0 and all four databases passed `eurlex-builder validate` — including the structurally empty court database.

The end-to-end storage/export path works, but extraction support is uneven: older/single-stream DC/PC/SC documents are generally usable; current proposal and impact-assessment templates lose core content; case law has no structural support.

Verdicts: **ok** = main selected representation substantially captured; **partial** = useful output with a known stream or granularity loss; **broken** = core content/structure absent even though the run reports success.

## Per-document outcomes

All rows have complete basic metadata and non-empty `works.full_text`. "R/EV" is relations/EuroVoc row count.

| Type | CELEX ID | Content | Extracted units | R/EV | Verdict | Main finding |
|---|---|---|---:|---:|---|---|
| Communication | `51985DC0310` | English PDF | 93 paragraph | 36/10 | partial | 95-page White Paper timed out in Docling, cleanly fell back to PyMuPDF; 93 chunks averaging 2,057 chars are page-scale, not paragraph-scale. |
| Communication | `52005DC0229` | English HTML | 87 paragraph | 0/5 | ok | 88.4% unit/full-text character ratio; section titles retained. |
| Communication | `52020DC0067` | English HTML | 95 para + 17 footnote | 5/9 | ok | 96.5% ratio, coherent prose; section-title context absent. |
| Communication | `52021DC0118` | English HTML | 158 para + 46 footnote | 30/10 | partial | Main stream 98.2% ratio, but the HTTP-300 `DOC_2` annex stream is ignored (10,585 source chars lost). |
| Proposal | `51995PC0375` | English PDF | 55 paragraph | 3/6 | ok | 14-page PDF via Docling; 94.9% ratio. Minor publication boilerplate remains. |
| Proposal | `51998PC0586` | English HTML | 258 paragraph | 4/5 | ok | 97.7% ratio with heading context and in-stream annex. |
| Proposal | `52020PC0825` (DSA) | English HTML | 452 para + 66 footnote | 48/9 | broken | Only 59.6% of source text captured. All 106 recitals (`li ManualConsidrant`), 74 article headings (`Titrearticle`), and 186 `li Point1` paragraphs use unhandled CSS classes and are skipped. |
| Proposal | `52021PC0206` (AI Act) | English HTML | 539 para + 81 footnote | 67/10 | broken | Only 63.4% of `DOC_1` captured. All 89 recitals and 85 article headings skipped; the 32,458-char annex stream `DOC_3` ignored. |
| Staff working doc | `52012SC0072` | English HTML | 2,142 paragraph | 0/14 | ok | 97.9% ratio; count consistent with the long impact-assessment source. |
| Staff working doc | `52016SC0110` | English HTML | 232 para + 103 footnote | 10/10 | ok | 96.5% ratio with coherent section context. |
| Staff working doc | `52020SC0348` | English HTML | 58 para + 138 footnote | 40/9 | broken | Only 29.8% of part 1 captured: 160,148 chars in `li Numbered-Para` skipped. HTTP-300 part 2 ignored (593,298 further chars). |
| Staff working doc | `52021SC0084` | English HTML | 198 para + 295 footnote | 62/10 | broken | Only 52.5% of part 1 captured (`li Numbered-Para` skipped); part 2 ignored (146,310 chars). |
| Judgment | `61962CJ0026` | English HTML | 0 | 5/0 | broken | Van Gend en Loos: source has Grounds / Decision on costs / Operative part; stored as `document_type='unknown'`, no units emitted. |
| Judgment | `62018CJ0311` | English HTML | 0 | 33/0 | broken | Schrems II: 171,992-char full text stored, no units. |
| Judgment | `62021CJ0252` | English HTML | 0 | 19/0 | broken | Full Grand Chamber ruling stored (113,198 chars), no units. |
| AG opinion | `62018CC0311` | English HTML | 0 | 86/0 | broken | Full 283,874-char opinion stored, no units. |
| Court order | `62020CO0026` | French HTML | 0 | 7/0 | broken | French-only source fetched and translated, no units. Full-text extraction is mojibake (`requÃ©rante`) even though the stored raw HTML is valid UTF-8. |

DC/PC/SC unit tables contain only `paragraph` and `footnote` rows — no `recital`/`article`/`annex`. That generic schema is by design for COM-style documents, but for the two modern proposals it coincides with losing the proposal's actual recitals, article headings, points, and annex stream.

## Bugs and reproducible failures

Reproduce any of these with a fixed-mode config containing the listed CELEX IDs.

1. **Sector-6 documents silently produce zero units** (`61962CJ0026`, `62018CJ0311`, `62021CJ0252`, `62018CC0311`, `62020CO0026`). Exit 0, `Processed: 5, Failed: 0`, checkpoints `processed`, `document_type='unknown'`, `text_units` empty. No unsupported-type error, no crash.
2. **Modern proposal CSS classes are skipped** (`52020PC0825`, `52021PC0206`). Recitals (`li ManualConsidrant`), article headings (`Titrearticle`), and points (`li Point0/Point1/Point2`) never reach the unit table while the documents are marked processed.
3. **Modern impact-assessment body class is skipped** (`52020SC0348`, `52021SC0084`). The ignored `li Numbered-Para` class accounts for ~160K missing characters per document.
4. **HTTP-300 multipart representations are truncated to one stream** (`52021DC0118`, `52021PC0206`, `52020SC0348`, `52021SC0084`). The selector takes only the first/ACT candidate (`DOC_1`); annex/part-2 streams are never fetched or merged (10,585 / 32,458 / 593,298 / 146,310 chars omitted, respectively).
5. **French court full text is mojibake before translation** (`62020CO0026`). `full_text_html` contains correct UTF-8 `requérante`; `full_text_original` contains `requÃ©rante`, and the translation inherits the artifacts.
6. **Document failures do not fail the CLI.** A run in which all documents failed at the metadata step reported `Processed: 0, Failed: 4` and still exited 0.

Additionally, at test time `validate` found no key/order/integrity violations in any of these databases — it did not flag unknown document types, non-empty full text with zero units, missing multipart streams, or low extraction coverage.

**Post-test remediation (same day):** bug 1's silence is now surfaced — `validate` reports `content_without_text_units` as an error (the court database above now exits 1) and `unextractable_document_type` as a warning, and the pipeline logs a run-time warning when a document has no extractor branch. Bugs 2–6 (coverage loss, multipart truncation, mojibake, exit codes) remain open; coverage cannot be validated post hoc because no source-text baseline is stored.

## Validation and output integrity

| Batch | Works | Text units | Relations | EuroVoc | `validate` |
|---|---:|---:|---:|---:|---|
| Communications | 4 | 496 | 71 | 34 | passed |
| Proposals | 4 | 1,451 | 122 | 30 | passed |
| Staff working documents | 4 | 3,166 | 112 | 43 | passed |
| Court cases | 5 | 0 | 150 | 0 | passed |

Relations populated for 15/17 documents (`52005DC0229` and `52012SC0072` returned none). Court metadata returned no EuroVoc descriptors.

## Limitations of this test

- Python's live HTTP transport itself was not exercised (sandbox restriction; see Method above).
- Sequential runs only; parallel mode and descriptive discovery were not tested for these types. Descriptive mode cannot request sector-6 documents at all — the `document_types` mapping has no case-law entry.
- Case-law coverage: three CJ judgments, one CC opinion, one CO order. General Court documents, other sector-6 suffixes, and languages beyond the one French order were not tested.
- ~4–6 documents per type is a smoke test, not a census; era coverage is indicative.
