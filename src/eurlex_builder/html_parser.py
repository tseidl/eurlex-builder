"""Parse source HTML bytes consistently, retaining declared encodings and namespaces."""

from __future__ import annotations

import logging

from lxml import etree, html

from eurlex_builder.utils import normalize_html_encoding_declaration

logger = logging.getLogger("eurlex_builder")


def parse_html(raw_content: bytes) -> etree._Element:
    """Try XML first, then HTML recovery on the same bytes, without serializing."""
    raw_content = normalize_html_encoding_declaration(raw_content)
    try:
        tree = etree.fromstring(raw_content)
        logger.debug("Parsed XHTML (%s)", tree.getroottree().docinfo.encoding)
        return tree
    except (etree.Error, ValueError):
        tree = html.fromstring(raw_content)
        logger.debug("Parsed plain HTML (%s)", tree.getroottree().docinfo.encoding)
        return tree


def decode_html(raw_content: bytes) -> str:
    """Decode optional source markup using the parser's encoding, without rewriting it.

    If parsing fails or reports no usable codec, retain the previous UTF-8
    replacement policy for this diagnostic column. Extraction still uses bytes.
    """
    try:
        encoding = parse_html(raw_content).getroottree().docinfo.encoding
        if encoding:
            return raw_content.decode(encoding, errors="replace")
    except (etree.Error, ValueError, LookupError):
        pass
    return raw_content.decode("utf-8", errors="replace")
