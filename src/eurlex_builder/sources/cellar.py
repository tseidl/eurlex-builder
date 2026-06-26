"""Cellar data source — SPARQL queries + REST API for EUR-Lex documents."""

from __future__ import annotations

import logging
from datetime import date, datetime
from urllib.parse import quote

import requests
from lxml import etree, html
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from SPARQLWrapper import SPARQLWrapper

from eurlex_builder.utils import get_document_type, resolve_doc_type

logger = logging.getLogger("eurlex_builder")

CELLAR_SPARQL_ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"
CELLAR_RESOURCE_BASE = "https://publications.europa.eu/resource/celex/"

# Relation types returned by the metadata SPARQL query.
RELATION_TYPES = {"cites", "amends", "adopts", "based_on", "proposes_to_amend", "consolidates"}

# Keywords for HTTP 300 candidate scoring.
_INCLUDE_KEYWORDS = {"ACT"}
_EXCLUDE_KEYWORDS = {"annexe", "annex", "cover", "erratum", "corrigendum"}

DEFAULT_TIMEOUT = 20


import threading
import time

# Simple rate limiter: minimum seconds between requests per session.
_RATE_LIMIT_INTERVAL = 0.1  # ~10 requests/sec max

_rate_lock = threading.Lock()
_last_request_time = 0.0


def _sparql_query(query: str, *, retries: int = 3, label: str = "SPARQL") -> dict | None:
    """Execute a SPARQL query with retry on transient failures."""
    for attempt in range(retries):
        try:
            sparql = SPARQLWrapper(CELLAR_SPARQL_ENDPOINT)
            sparql.setQuery(query)
            sparql.setReturnFormat("json")
            return sparql.query().convert()
        except Exception as exc:
            if attempt < retries - 1:
                wait = 2 ** attempt
                logger.debug("%s attempt %d failed: %s — retrying in %ds", label, attempt + 1, exc, wait)
                time.sleep(wait)
            else:
                logger.error("%s failed after %d attempts: %s", label, retries, exc)
                return None


def _rate_limit() -> None:
    """Enforce a minimum interval between requests across all threads."""
    global _last_request_time
    with _rate_lock:
        now = time.monotonic()
        elapsed = now - _last_request_time
        if elapsed < _RATE_LIMIT_INTERVAL:
            time.sleep(_RATE_LIMIT_INTERVAL - elapsed)
        _last_request_time = time.monotonic()


def _create_session() -> requests.Session:
    """Create an HTTP session with retry logic."""
    session = requests.Session()
    retry_strategy = Retry(
        total=5,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _parse_value(value_obj: dict) -> str | int | float | bool | date | datetime:
    """Convert a SPARQL JSON binding value to a native Python type."""
    value_string = value_obj.get("value", "")
    value_type = value_obj.get("type", "uri")
    datatype = value_obj.get("datatype", "")

    if value_type == "literal" and datatype:
        if "boolean" in datatype:
            return value_string.lower() == "true" or value_string == "1"
        elif "integer" in datatype or "positiveInteger" in datatype:
            return int(value_string)
        elif "decimal" in datatype or "double" in datatype or "float" in datatype:
            return float(value_string)
        elif "date" in datatype and "dateTime" not in datatype:
            return datetime.strptime(value_string, "%Y-%m-%d").date()
        elif "dateTime" in datatype:
            try:
                return datetime.fromisoformat(value_string.replace("Z", "+00:00"))
            except ValueError:
                return value_string
        elif "gYear" in datatype:
            return int(value_string)

    return value_string


def _parse_300_response(html_content: bytes) -> list[tuple[str, str, int]]:
    """Parse HTTP 300 Multiple Choices response HTML to extract document alternatives.

    Returns a list of (url, stream_name, stream_order) tuples.
    """
    tree = html.fromstring(html_content)
    items = []

    for item in tree.xpath('//li[@title="item"]'):
        url_elements = item.xpath(".//a/@href")
        url = url_elements[0] if url_elements else None

        stream_name_elements = item.xpath('.//li[@title="stream_name"]/text()')
        stream_name = stream_name_elements[0].strip() if stream_name_elements else ""

        stream_order_elements = item.xpath('.//li[@title="stream_order"]/text()')
        try:
            stream_order = int(stream_order_elements[0].strip()) if stream_order_elements else 999
        except (ValueError, IndexError):
            stream_order = 999

        if url:
            items.append((url, stream_name, stream_order))

    return items


def _select_best_candidate(items: list[tuple[str, str, int]]) -> str | None:
    """Select the best document URL from HTTP 300 candidates using heuristics."""
    if not items:
        return None

    # Filter out excluded keywords.
    candidates = []
    for url, stream_name, stream_order in items:
        name_lower = stream_name.lower()
        if not any(kw.lower() in name_lower for kw in _EXCLUDE_KEYWORDS):
            candidates.append((url, stream_name, stream_order))

    if not candidates:
        logger.warning(
            "All 300-response candidates matched exclude keywords; "
            "falling back to first item: %s",
            items[0][1],
        )
        return items[0][0]

    # Score: start with stream_order, subtract 1000 for include-keyword matches.
    def _score(item: tuple[str, str, int]) -> int:
        _, name, order = item
        score = order
        name_upper = name.upper()
        for kw in _INCLUDE_KEYWORDS:
            if kw.upper() in name_upper:
                score -= 1000
                break
        return score

    best = min(candidates, key=_score)
    logger.debug("Selected document from 300 response: %s (order: %d)", best[1], best[2])
    return best[0]


# Max HTML size before truncation at ANNEX marker (50 MB).
_MAX_HTML_BYTES = 50 * 1024 * 1024


def _truncate_at_annex(html_bytes: bytes, celex_id: str) -> bytes:
    """For very large documents, truncate HTML before the first ANNEX to avoid OOM."""
    if len(html_bytes) <= _MAX_HTML_BYTES:
        return html_bytes
    # Find first ANNEX marker and truncate, closing tags to keep valid HTML.
    for marker in [b"<p>ANNEX", b">ANNEX<", b"ANNEX I", b"ANNEX</p>"]:
        idx = html_bytes.find(marker)
        if idx > 0:
            logger.info(
                "Truncating oversized HTML for %s at ANNEX marker (%.1f MB → %.1f MB)",
                celex_id, len(html_bytes) / 1e6, idx / 1e6,
            )
            return html_bytes[:idx] + b"</body></html>"
    # No ANNEX marker found — return as-is and hope for the best.
    logger.warning("Oversized HTML for %s (%.1f MB) but no ANNEX marker found", celex_id, len(html_bytes) / 1e6)
    return html_bytes


def _flatten_content_divs(html_bytes: bytes, celex_id: str) -> bytes:
    """Flatten <div class='content'> elements by moving their children to the parent."""
    html_bytes = _truncate_at_annex(html_bytes, celex_id)
    try:
        tree = etree.fromstring(html_bytes)
        for div in tree.xpath(".//*[local-name()='div' and @class='content']"):
            parent = div.getparent()
            if parent is not None:
                index = parent.index(div)
                for child in reversed(list(div)):
                    parent.insert(index, child)
                parent.remove(div)
        return etree.tostring(tree, encoding="utf-8")
    except Exception:
        try:
            tree = html.fromstring(html_bytes)
            for div in tree.xpath(".//*[local-name()='div' and @class='content']"):
                parent = div.getparent()
                if parent is not None:
                    index = parent.index(div)
                    for child in reversed(list(div)):
                        parent.insert(index, child)
                    parent.remove(div)
            return html.tostring(tree, encoding="utf-8")
        except Exception as exc:
            logger.warning(
                "Failed to flatten content divs for %s: %s", celex_id, exc
            )
            return html_bytes


def _build_metadata_query(celex_id: str) -> str:
    """Build the SPARQL query for fetching metadata and relations."""
    encoded = quote(celex_id, safe="")
    return f"""
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>

SELECT DISTINCT ?data_type ?value
WHERE {{
    ?work owl:sameAs <http://publications.europa.eu/resource/celex/{encoded}> .

    # titles
    {{
        ?expression cdm:expression_belongs_to_work ?work .
        ?expression cdm:expression_title ?value .
        FILTER EXISTS {{
            ?expression cdm:expression_uses_language ?lang .
            FILTER(STRENDS(STR(?lang), "ENG"))
        }}
        BIND("title" AS ?data_type)
    }}
    UNION
    # document date
    {{
        ?work cdm:work_date_document ?value .
        BIND("date" AS ?data_type)
    }}
    UNION
    # cites
    {{
        ?work cdm:work_cites_work ?w .
        ?w cdm:resource_legal_id_celex ?value .
        BIND("cites" AS ?data_type)
    }}
    UNION
    # amends
    {{
        ?work cdm:resource_legal_amends_resource_legal ?w .
        ?w cdm:resource_legal_id_celex ?value .
        BIND("amends" AS ?data_type)
    }}
    UNION
    # adopts
    {{
        ?work cdm:resource_legal_adopts_resource_legal ?w .
        ?w cdm:resource_legal_id_celex ?value .
        BIND("adopts" AS ?data_type)
    }}
    UNION
    # based on
    {{
        ?work cdm:resource_legal_based_on_resource_legal ?w .
        ?w cdm:resource_legal_id_celex ?value .
        BIND("based_on" AS ?data_type)
    }}
    UNION
    # proposes to amend
    {{
        ?work cdm:resource_legal_proposes_to_amend_resource_legal ?w .
        ?w cdm:resource_legal_id_celex ?value .
        BIND("proposes_to_amend" AS ?data_type)
    }}
    UNION
    # consolidates
    {{
        ?work cdm:act_consolidated_consolidates_resource_legal ?w .
        ?w cdm:resource_legal_id_celex ?value .
        BIND("consolidates" AS ?data_type)
    }}
    UNION
    # EuroVoc descriptors (URI|label format)
    {{
        ?work cdm:work_is_about_concept_eurovoc ?ev .
        OPTIONAL {{ ?ev <http://www.w3.org/2004/02/skos/core#prefLabel> ?evlabel .
                    FILTER(LANG(?evlabel) = "en") }}
        BIND("eurovoc" AS ?data_type)
        BIND(CONCAT(STR(?ev), "|", COALESCE(?evlabel, "")) AS ?value)
    }}
}}
"""


def _build_descriptive_query(
    *,
    document_types: list[str],
    start_date: date,
    end_date: date,
    eurovoc_uris: list[str] | None = None,
    include_corrigenda: bool = False,
    include_consolidated_texts: bool = False,
) -> str:
    """Build a SPARQL query that discovers CELEX IDs matching descriptive criteria."""
    # Map human-readable types to CELEX type codes and collect required sectors.
    type_conditions: list[str] = []
    sectors: set[str] = set()

    for dt in document_types:
        resolved = resolve_doc_type(dt)
        if resolved is None:
            logger.warning("Unknown document type '%s' — skipping in SPARQL filter", dt)
            continue
        code, sector = resolved
        type_conditions.append(f'?type = "{code}"^^xsd:string')
        sectors.add(sector)

    if not type_conditions:
        logger.error("No valid document types after mapping — query will return nothing")
        return ""

    type_filter = f"FILTER({' || '.join(type_conditions)})"
    sector_conditions = [f'?sector = "{s}"^^xsd:string' for s in sorted(sectors)]
    sector_filter = f"FILTER({' || '.join(sector_conditions)})"

    date_filters = (
        f'FILTER(?date >= "{start_date.isoformat()}"^^xsd:date)\n'
        f'        FILTER(?date <= "{end_date.isoformat()}"^^xsd:date)'
    )

    corrigenda_filter = ""
    if not include_corrigenda:
        corrigenda_filter = 'FILTER(!REGEX(STR(?celex), "R\\\\([0-9]+\\\\)$"))'

    consolidated_filter = ""
    if not include_consolidated_texts:
        consolidated_filter = 'FILTER(!STRSTARTS(STR(?celex), "0"))'

    # Optional EuroVoc filter.
    eurovoc_block = ""
    if eurovoc_uris:
        values = " ".join(f"<{uri}>" for uri in eurovoc_uris)
        eurovoc_block = (
            f"?work cdm:work_is_about_concept_eurovoc ?eurovoc .\n"
            f"        VALUES ?eurovoc {{ {values} }}"
        )

    return f"""
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>

SELECT DISTINCT ?celex
WHERE {{
    ?work cdm:work_date_document ?date .
    ?work cdm:resource_legal_type ?type .
    ?work cdm:resource_legal_id_celex ?celex .
    ?work cdm:resource_legal_id_sector ?sector .
    {eurovoc_block}
    {date_filters}
    {type_filter}
    {sector_filter}
    {corrigenda_filter}
    {consolidated_filter}
}}
"""


class CellarSource:
    """Fetches EU legislative data from the Cellar SPARQL endpoint and REST API."""

    def __init__(self):
        self.session = _create_session()
        # Cache relations and EuroVoc keyed by celex_id so fetch_* can return
        # them without a second SPARQL query.
        self._cached_relations: dict[str, list[dict]] = {}
        self._cached_eurovoc: dict[str, list[dict]] = {}

    def resolve_celex_ids(
        self,
        *,
        document_types: list[str],
        start_date: date,
        end_date: date,
        eurovoc_uris: list[str] | None = None,
        include_corrigenda: bool = False,
        include_consolidated_texts: bool = False,
    ) -> list[str]:
        """Discover CELEX IDs matching descriptive criteria via SPARQL."""
        query = _build_descriptive_query(
            document_types=document_types,
            start_date=start_date,
            end_date=end_date,
            eurovoc_uris=eurovoc_uris,
            include_corrigenda=include_corrigenda,
            include_consolidated_texts=include_consolidated_texts,
        )
        logger.debug("Descriptive SPARQL query:\n%s", query)

        result = _sparql_query(query, label=f"Descriptive SPARQL")
        if result is None:
            return []

        bindings = result.get("results", {}).get("bindings", [])
        ids = sorted(
            {_parse_value(b["celex"]) for b in bindings if b.get("celex")}
        )
        logger.info("Descriptive query returned %d CELEX IDs", len(ids))
        return ids

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    def fetch_metadata(self, celex_id: str) -> dict:
        """Fetch metadata and relations for a single CELEX ID via SPARQL.

        Relations are stored in self._cached_relations for later retrieval
        via fetch_relations.
        """
        query = _build_metadata_query(celex_id)

        result = _sparql_query(query, label=f"Metadata SPARQL for {celex_id}")
        if result is None:
            return self._empty_metadata(celex_id)

        bindings = result.get("results", {}).get("bindings", [])

        title = None
        date_adopted = None
        relations: list[dict] = []
        eurovoc: list[dict] = []

        for binding in bindings:
            data_type = _parse_value(binding.get("data_type", {}))
            value = _parse_value(binding.get("value", {}))

            if data_type == "title":
                title = value
            elif data_type == "date":
                date_adopted = value if isinstance(value, date) else None
            elif data_type == "eurovoc":
                # Value format: "URI|label"
                parts = str(value).split("|", 1)
                eurovoc.append({
                    "celex_id": celex_id,
                    "eurovoc_uri": parts[0],
                    "eurovoc_label": parts[1] if len(parts) > 1 else "",
                })
            elif data_type in RELATION_TYPES:
                relations.append(
                    {
                        "source_celex": celex_id,
                        "target_celex": value,
                        "relation_type": data_type,
                    }
                )

        # Cache relations and eurovoc for later retrieval.
        self._cached_relations[celex_id] = relations
        self._cached_eurovoc[celex_id] = eurovoc

        return {
            "celex_id": celex_id,
            "title": title,
            "date_adopted": date_adopted,
            "document_type": get_document_type(celex_id),
            "language": "eng",
            "full_text_html": None,
        }

    # ------------------------------------------------------------------
    # Content
    # ------------------------------------------------------------------

    # Languages to try, in priority order.
    LANGUAGE_CHAIN = ["eng", "fra", "deu", "ita", "nld", "spa"]

    def fetch_content(
        self, celex_id: str
    ) -> tuple[bytes, str, str] | None:
        """Fetch document content via Cellar REST API.

        Tries English content in any format first, then non-English:
        1. English HTML
        2. English PDF
        3. Non-English HTML (fra → deu → ...)
        4. Non-English PDF (fra → deu → ...)

        Returns (content_bytes, content_type, language) or None.
        content_type is "html" or "pdf".
        """
        encoded = quote(celex_id, safe="")
        url = f"{CELLAR_RESOURCE_BASE}{encoded}"

        # Per language: try HTML then PDF before moving to next language.
        # This ensures English PDF is preferred over French HTML.
        for lang in self.LANGUAGE_CHAIN:
            # Try HTML.
            for accept, ctype in [
                ("application/xhtml+xml", "html"),
                ("text/html", "html"),
            ]:
                headers = {"Accept": accept, "Accept-Language": lang}
                content = self._fetch_with_300_handling(url, headers, celex_id)
                if content is not None:
                    if lang != "eng":
                        logger.info(
                            "Content for %s found in %s (not English)",
                            celex_id, lang,
                        )
                    return (_flatten_content_divs(content, celex_id), ctype, lang)

            # Try PDF for this language before falling back to next language.
            pdf_result = self._fetch_pdf_for_lang(celex_id, lang)
            if pdf_result is not None:
                return pdf_result

        logger.warning("No content retrieved for %s in any language/format", celex_id)
        return None

    def fetch_pdf(self, celex_id: str) -> tuple[bytes, str, str] | None:
        """Try to fetch a PDF for a CELEX ID in any language (priority order)."""
        manifests = self._get_pdf_manifests(celex_id)
        if not manifests:
            return None
        for lang in self.LANGUAGE_CHAIN:
            result = self._try_pdf_manifest(celex_id, lang, manifests)
            if result:
                return result
        return None

    def _fetch_pdf_for_lang(
        self, celex_id: str, lang: str
    ) -> tuple[bytes, str, str] | None:
        """Try to fetch a PDF for a CELEX ID in a specific language."""
        manifests = self._get_pdf_manifests(celex_id)
        if not manifests:
            return None
        return self._try_pdf_manifest(celex_id, lang, manifests)

    def _get_pdf_manifests(self, celex_id: str) -> dict[str, str]:
        """Query SPARQL for PDF manifestation URIs, return {lang_code: manifest_uri}."""
        # Cache to avoid repeated SPARQL queries for the same doc.
        cache_key = f"_pdf_manifests_{celex_id}"
        if hasattr(self, cache_key):
            return getattr(self, cache_key)

        encoded = quote(celex_id, safe="")
        lang_map = {
            "eng": "ENG", "fra": "FRA", "deu": "DEU",
            "ita": "ITA", "nld": "NLD", "spa": "SPA",
        }

        query = f"""
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>

SELECT ?manifest ?lang WHERE {{
    ?work owl:sameAs <http://publications.europa.eu/resource/celex/{encoded}> .
    ?expression cdm:expression_belongs_to_work ?work .
    ?expression cdm:expression_uses_language ?lang .
    ?manifest cdm:manifestation_manifests_expression ?expression .
    ?manifest cdm:manifestation_type ?format .
    FILTER(CONTAINS(STR(?format), "pdf"))
}}
"""
        result = _sparql_query(query, label=f"PDF manifest SPARQL for {celex_id}")
        manifest_by_lang: dict[str, str] = {}
        if result:
            for b in result.get("results", {}).get("bindings", []):
                lang_uri = b.get("lang", {}).get("value", "")
                lang_suffix = lang_uri.split("/")[-1]
                manifest_uri = b.get("manifest", {}).get("value", "")
                for our_code, sparql_code in lang_map.items():
                    if lang_suffix == sparql_code:
                        manifest_by_lang[our_code] = manifest_uri

        setattr(self, cache_key, manifest_by_lang)
        return manifest_by_lang

    def _try_pdf_manifest(
        self, celex_id: str, lang: str, manifests: dict[str, str]
    ) -> tuple[bytes, str, str] | None:
        """Try to fetch a specific language's PDF from a manifest map."""
        manifest_uri = manifests.get(lang)
        if not manifest_uri:
            return None

        # Try DOC_1 through DOC_4 — some documents store the PDF at a higher index.
        for doc_n in range(1, 5):
            item_url = f"{manifest_uri}/DOC_{doc_n}"
            _rate_limit()
            try:
                resp = self.session.get(
                    item_url, headers={"Accept": "*/*"}, timeout=DEFAULT_TIMEOUT
                )
            except requests.RequestException as exc:
                logger.debug("PDF fetch failed for %s (%s): %s", celex_id, lang, exc)
                return None

            if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
                if lang != "eng":
                    logger.info(
                        "PDF for %s found in %s (not English)", celex_id, lang
                    )
                else:
                    logger.debug("PDF fetched for %s (English)", celex_id)
                return (resp.content, "pdf", lang)
            if resp.status_code == 404:
                continue
            # Non-404, non-200: stop trying higher doc numbers.
            break
        return None

    # ------------------------------------------------------------------
    # Relations
    # ------------------------------------------------------------------

    def fetch_relations(self, celex_id: str) -> list[dict]:
        """Return relations cached from the metadata fetch."""
        return self._cached_relations.get(celex_id, [])

    def fetch_eurovoc(self, celex_id: str) -> list[dict]:
        """Return EuroVoc descriptors cached from the metadata fetch."""
        return self._cached_eurovoc.get(celex_id, [])

    # ------------------------------------------------------------------
    # EuroVoc keyword resolution
    # ------------------------------------------------------------------

    def resolve_eurovoc_keywords(self, keywords: list[str]) -> dict[str, dict[str, set[str]]]:
        """Resolve keyword strings to EuroVoc concept URIs via SPARQL.

        For each keyword, searches both prefLabel and altLabel (English, case-insensitive
        CONTAINS match) in EuroVoc scheme eurovoc:100141.

        Returns nested dict: keyword -> {concept_uri -> set(matching_labels)}.
        """
        if not keywords:
            return {}

        values_clause = " ".join(f'"{kw}"' for kw in keywords)
        results: dict[str, dict[str, set[str]]] = {kw: {} for kw in keywords}

        for label_predicate in ("prefLabel", "altLabel"):
            query = f"""
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX eurovoc: <http://eurovoc.europa.eu/>

SELECT DISTINCT ?concept ?label ?keyword
WHERE {{
    VALUES ?keyword {{
        {values_clause}
    }}
    ?concept skos:inScheme eurovoc:100141 .
    ?concept skos:{label_predicate} ?label .
    FILTER(
        LANGMATCHES(LANG(?label), "en") &&
        CONTAINS(LCASE(STR(?label)), LCASE(?keyword))
    )
}}
ORDER BY ?keyword ?label
"""
            logger.debug("EuroVoc %s query:\n%s", label_predicate, query)

            result = _sparql_query(query, label=f"EuroVoc {label_predicate}")
            if result is None:
                continue

            bindings = result.get("results", {}).get("bindings", [])
            for binding in bindings:
                keyword = _parse_value(binding.get("keyword", {}))
                concept = _parse_value(binding.get("concept", {}))
                label = _parse_value(binding.get("label", {}))

                if keyword in results:
                    results[keyword].setdefault(concept, set()).add(label)

        for kw, concepts in results.items():
            if concepts:
                logger.info(
                    "Keyword '%s' matched %d EuroVoc concept(s)", kw, len(concepts)
                )
            else:
                logger.warning("Keyword '%s' matched no EuroVoc concepts", kw)

        return results

    def fetch_concept_info(self, concept_uri: str) -> dict:
        """Fetch definition, broader, narrower, and related labels for a EuroVoc concept.

        Returns dict with keys: definition, broader, narrower, related (all lists of strings).
        """
        query = f"""
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>

SELECT ?type ?text WHERE {{
    {{
        <{concept_uri}> skos:definition ?text .
        BIND("definition" AS ?type)
    }} UNION {{
        <{concept_uri}> skos:broader ?c .
        ?c skos:prefLabel ?text .
        BIND("broader" AS ?type)
    }} UNION {{
        <{concept_uri}> skos:narrower ?c .
        ?c skos:prefLabel ?text .
        BIND("narrower" AS ?type)
    }} UNION {{
        <{concept_uri}> skos:related ?c .
        ?c skos:prefLabel ?text .
        BIND("related" AS ?type)
    }}
    FILTER(LANGMATCHES(LANG(?text), "en"))
}}
ORDER BY ?type ?text
"""
        try:
            sparql = SPARQLWrapper(CELLAR_SPARQL_ENDPOINT)
            sparql.setQuery(query)
            sparql.setReturnFormat("json")
            result = sparql.query().convert()
        except Exception as exc:
            logger.error("Concept info query failed for %s: %s", concept_uri, exc)
            return {}

        info: dict[str, list[str]] = {
            "definition": [], "broader": [], "narrower": [], "related": [],
        }
        for b in result.get("results", {}).get("bindings", []):
            t = _parse_value(b.get("type", {}))
            text = _parse_value(b.get("text", {}))
            if t in info:
                info[t].append(text)

        return info

    # ------------------------------------------------------------------
    # Procedure number resolution
    # ------------------------------------------------------------------

    def resolve_procedure_numbers(self, procedure_numbers: list[str]) -> list[str]:
        """Resolve interinstitutional procedure numbers to CELEX IDs via SPARQL.

        Prefers adopted-work CELEX IDs; falls back to proposal CELEX IDs.
        Returns a sorted list of unique CELEX IDs.
        """
        if not procedure_numbers:
            return []

        values_clause = " ".join(f'"{pn}"' for pn in procedure_numbers)

        query = f"""
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>

SELECT DISTINCT ?procedure ?proposalCelex ?availableWorkCelex
WHERE {{
    VALUES ?procedure {{
        {values_clause}
    }}
    ?dossier cdm:procedure_code_interinstitutional_reference_procedure ?ref .
    ?proposal cdm:work_part_of_dossier ?dossier .
    ?proposal cdm:resource_legal_id_celex ?proposalCelex .

    OPTIONAL {{
        ?work cdm:resource_legal_adopts_resource_legal ?proposal .
        ?work cdm:resource_legal_id_celex ?workCelex .
    }}

    FILTER(CONTAINS(STR(?ref), ?procedure))
    FILTER(CONTAINS(STR(?proposalCelex), "PC"))
    FILTER(!CONTAINS(STR(?proposalCelex), "("))

    BIND(COALESCE(?workCelex, "") AS ?availableWorkCelex)
}}
"""
        logger.debug("Procedure resolution SPARQL query:\n%s", query)

        result = _sparql_query(query, label="Procedure resolution SPARQL")
        if result is None:
            return []

        bindings = result.get("results", {}).get("bindings", [])

        # Group results by procedure number: prefer adopted work CELEX over proposal.
        procedure_celex: dict[str, set[str]] = {pn: set() for pn in procedure_numbers}

        for binding in bindings:
            procedure = _parse_value(binding.get("procedure", {}))
            proposal = _parse_value(binding.get("proposalCelex", {}))
            work = _parse_value(binding.get("availableWorkCelex", {}))

            if procedure not in procedure_celex:
                continue

            if work:
                procedure_celex[procedure].add(work)
            elif proposal:
                procedure_celex[procedure].add(proposal)

        all_celex: set[str] = set()
        for pn, celex_ids in procedure_celex.items():
            if celex_ids:
                logger.info(
                    "Procedure '%s' resolved to %d CELEX ID(s): %s",
                    pn, len(celex_ids), ", ".join(sorted(celex_ids)),
                )
                all_celex.update(celex_ids)
            else:
                logger.warning("Procedure '%s' resolved to no CELEX IDs", pn)

        return sorted(all_celex)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_with_300_handling(
        self, url: str, headers: dict, celex_id: str
    ) -> bytes | None:
        """GET a URL, handling HTTP 300 Multiple Choices responses."""
        _rate_limit()
        try:
            response = self.session.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning("Request failed for %s: %s", celex_id, exc)
            return None

        if response.status_code == 300:
            logger.debug("Received 300 Multiple Choices for %s", celex_id)
            items = _parse_300_response(response.content)
            if not items:
                logger.warning("No alternatives in 300 response for %s", celex_id)
                return None

            selected_url = _select_best_candidate(items)
            if selected_url is None:
                return None

            logger.debug("Following 300 redirect to: %s", selected_url)
            _rate_limit()
            try:
                response = self.session.get(
                    selected_url, headers=headers, timeout=DEFAULT_TIMEOUT
                )
            except requests.RequestException as exc:
                logger.warning(
                    "Request for selected 300 candidate failed for %s: %s",
                    celex_id,
                    exc,
                )
                return None

        if response.status_code == 200:
            return response.content

        if response.status_code in (403, 404):
            logger.debug(
                "HTTP %d for %s (Accept: %s)",
                response.status_code,
                celex_id,
                headers.get("Accept"),
            )
            return None

        if response.status_code >= 400:
            logger.warning(
                "HTTP %d for %s: %s", response.status_code, celex_id, response.reason
            )
            return None

        # Unexpected status — return content anyway.
        return response.content

    @staticmethod
    def _empty_metadata(celex_id: str) -> dict:
        """Return a metadata dict with empty values."""
        return {
            "celex_id": celex_id,
            "title": None,
            "date_adopted": None,
            "document_type": get_document_type(celex_id),
            "language": "eng",
            "full_text_html": None,
        }
