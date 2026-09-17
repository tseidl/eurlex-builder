"""Source-language markers for the PDF fetch fallback languages."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class LegislativeMarkers:
    article_word: str
    article: str
    annex_word: str
    signature: str
    enacting: str
    recital_marker: str
    recital_start: str

    def compile(self, pattern: str) -> re.Pattern[str]:
        """Accept a marker at the start of a plain or Markdown heading line."""
        return re.compile(r"^(?:#{1,3}\s+)?(?:" + pattern + r")", re.IGNORECASE)


# The parser requires standalone markers except for already-supported French
# same-line bodies, where it also checks the trailing text for references.
_NUMBER = r"(?P<number>\d+[a-z]*)"
LEGISLATIVE_MARKERS = {
    "spa": LegislativeMarkers(
        article_word="Artículo",
        article=rf"Artículo\s+(?:{_NUMBER}|(?P<sole>único))",
        annex_word="ANEXO",
        signature=r"Hech[oa]\s+en\s+.+\b\d",
        enacting=r"(?:HA[N]?\s+ADOPTADO\b|DECIDE\s*:|HAN\s+CONVENIDO\b)",
        recital_marker=r"Considerando\s+lo\s+siguiente",
        recital_start=r"Considerando\s+que\b",
    ),
    "fra": LegislativeMarkers(
        article_word="Article",
        article=rf"Article\s+(?:(?P<first>premier|1er)|{_NUMBER}|(?P<sole>unique))",
        annex_word="ANNEXE",
        signature=r"Fait[e]?\s+à\s+.+\b\d",
        enacting=r"(?:(?:A|ONT)\s+ADOPTÉ\b|(?:A|ONT)\s+DÉCIDÉ\b|DÉCIDE\s*:)",
        recital_marker=r"considérant\s+ce\s+qui\s+suit",
        recital_start=r"considérant\s+que\b",
    ),
    "deu": LegislativeMarkers(
        article_word="Artikel",
        article=rf"(?:Artikel\s+{_NUMBER}|(?P<sole>Einziger\s+Artikel))",
        annex_word="ANHANG",
        signature=r"Geschehen\s+zu\s+.+\b\d",
        enacting=r"(?:HAT|HABEN)\s+(?:FOLGENDE[NRS]?\b|BESCHLOSSEN\b)",
        recital_marker=r"in\s+Erwägung\s+nachstehender\s+Gründe",
        recital_start=r"in\s+Erwägung,?\s+(?:dass|daß)\b",
    ),
    "ita": LegislativeMarkers(
        article_word="Articolo",
        article=rf"Articolo\s+(?:{_NUMBER}|(?P<sole>unico))",
        annex_word="ALLEGATO",
        signature=r"Fatto\s+a\s+.+\b\d",
        enacting=r"(?:HA|HANNO)\s+(?:ADOTTATO|DECISO)\b",
        recital_marker=r"considerando\s+quanto\s+segue",
        recital_start=r"considerando\s+che\b",
    ),
    "nld": LegislativeMarkers(
        article_word="Artikel",
        article=rf"(?:Artikel\s+{_NUMBER}|(?P<sole>Enig\s+artikel))",
        annex_word="BIJLAGE",
        signature=r"Gedaan\s+te\s+.+\b\d",
        enacting=r"(?:HEEFT|HEBBEN)\s+(?:DE\s+VOLGENDE\b|BESLOTEN\b)|BESLUIT\s*:",
        recital_marker=r"Overwegende\s+hetgeen\s+volgt",
        recital_start=r"Overwegende\s+dat\b",
    ),
}


def is_next_annex(current: str | None, candidate: str | None) -> bool:
    """Require a consecutive outer sequence before splitting an existing annex.

    Attached agreements can contain their own annexes. In particular, C and D
    must not be read as Roman annexes 100 and 500 after the decision's annex II.
    Ambiguous headings remain in the enclosing annex's text.
    """
    if not current or not candidate:
        return False
    if current.isdigit() or candidate.isdigit():
        return (
            current.isdigit() and candidate.isdigit()
            and int(candidate) == int(current) + 1
        )

    def roman_value(number: str) -> int:
        values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
        total = previous = 0
        for letter in reversed(number.upper()):
            value = values[letter]
            total += -value if value < previous else value
            previous = max(value, previous)
        return total

    return roman_value(candidate) == roman_value(current) + 1
