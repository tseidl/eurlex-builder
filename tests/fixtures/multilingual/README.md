# Official-language regression excerpts

These six excerpts come from the exact Cellar responses downloaded on
5 September 2026 for the multilingual feature check. `provenance.json` records
the resolved source URL, retrieval time, complete response hash, selected article
identifier, and excerpt hash for each file.

The original head and language/reference header are retained alongside the
selected article. XML serialization and the reduced document wrapper mean these
are structural excerpts, not byte-identical copies of the complete responses.

- GDPR Article 7 in English, German and Greek checks shared paragraph anchors
  and non-Latin text.
- DORA Article 60 checks that paragraph identifiers in quoted replacement law
  remain inside the amending article.
- Consolidated GDPR Article 7 in English and German checks the dated reference
  header and article-only coverage when supported paragraph anchors are absent.

Synthetic tests separately cover unavailable languages, legacy layouts,
conflicting language labels, duplicate identifiers, interruption-safe storage,
and preservation of the input database.
