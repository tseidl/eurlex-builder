"""Evaluate generated discovery SPARQL against RDF terms used by Cellar."""

from __future__ import annotations

from datetime import date

import pytest
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import XSD

from eurlex_builder.sources.cellar import _build_descriptive_query


@pytest.mark.parametrize("document_types", [["regulation"], ["communication"], ["regulation", "communication"]])
@pytest.mark.parametrize("consolidated", [False, True])
@pytest.mark.parametrize("corrigenda", [False, True])
def test_discovery_respects_type_sector_date_and_optional_filters(document_types, consolidated, corrigenda):
    graph = Graph()
    cdm = Namespace("http://publications.europa.eu/ontology/cdm#")
    records = [
        ("32016R0679", "3", "R", "2016-05-04", True),
        ("02016R0679-20160504", "0", "R", "2016-05-04", True),
        ("32016R0679R(01)", "3", "R", "2016-05-04", True),
        ("32016L0001", "3", "L", "2016-05-04", True),
        ("02016L0001-20160504", "0", "L", "2016-05-04", True),
        ("52016DC0001", "5", "DC", "2016-05-04", True),
        ("02016DC0001-20160504", "0", "DC", "2016-05-04", True),
        ("32015R0001", "3", "R", "2015-01-01", True),
        ("32016R0002", "3", "R", "2016-05-04", False),
    ]
    concept = URIRef("http://eurovoc.europa.eu/1")
    for celex, sector, code, day, has_concept in records:
        work = URIRef(f"http://example.test/{celex}")
        for predicate, value in [
            (cdm.resource_legal_id_celex, Literal(celex, datatype=XSD.string)),
            (cdm.resource_legal_id_sector, Literal(sector, datatype=XSD.string)),
            (cdm.resource_legal_type, Literal(code, datatype=XSD.string)),
            (cdm.work_date_document, Literal(date.fromisoformat(day))),
        ]:
            graph.add((work, predicate, value))
        if has_concept:
            graph.add((work, cdm.work_is_about_concept_eurovoc, concept))
    query = _build_descriptive_query(
        document_types=document_types, start_date=date(2016, 1, 1), end_date=date(2016, 12, 31),
        eurovoc_uris=[str(concept)], include_consolidated_texts=consolidated,
        include_corrigenda=corrigenda,
    )
    found = {str(row[0]) for row in graph.query(query)}
    expected = set()
    if "regulation" in document_types:
        expected.add("32016R0679")
        if consolidated:
            expected.add("02016R0679-20160504")
        if corrigenda:
            expected.add("32016R0679R(01)")
    if "communication" in document_types:
        expected.add("52016DC0001")
    assert found == expected
