"""A search in LinkedIn syntax, read for every source: `NOT term`, `NOT "two words"` and the OR alternatives."""

import re
import unicodedata
from collections.abc import Sequence

NOT_TERM = re.compile(r'\bNOT\s+(?:"([^"]+)"|(\S+))')
_OR = re.compile(r"\bOR\b")


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


def search_terms(keywords: str) -> list[str]:
    """The plain terms of a search, for a source that does not understand OR, NOT or quotes: each OR
    alternative becomes its own search, and NOT is applied to the titles afterwards (is_excluded)."""
    text = NOT_TERM.sub(" ", keywords).replace("(", " ").replace(")", " ")
    terms = (" ".join(part.replace('"', " ").split()) for part in _OR.split(text))
    return list(dict.fromkeys(term for term in terms if term))
