"""The parts of a search's LinkedIn syntax that every source needs: `NOT term` and `NOT "two words"`."""

import re
import unicodedata
from collections.abc import Sequence

NOT_TERM = re.compile(r'\bNOT\s+(?:"([^"]+)"|(\S+))')


def _plain(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def excluded_terms(keywords: str) -> list[str]:
    """The `NOT term` and `NOT "two words"` of a search, without accents."""
    return [_plain(phrase or word) for phrase, word in NOT_TERM.findall(keywords)]


def is_excluded(title: str, terms: Sequence[str]) -> bool:
    """LinkedIn's guest search does not always honor NOT, and matches "consultor" exactly where
    "Consultores" is the same role: a title with a word starting with an excluded term is dropped here."""
    plain = _plain(title)
    return any(re.search(rf"\b{re.escape(term)}", plain) for term in terms)
