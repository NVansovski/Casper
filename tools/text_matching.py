"""Fuzzy text matching for voice-driven lookups (umlaut- and case-insensitive)."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable
from difflib import SequenceMatcher
from typing import TypeVar

T = TypeVar("T")

_SEPARATORS = re.compile(r"[\s_\-.,;:/\\()\[\]]+")


def normalize(text: str) -> str:
    """Casefold, strip accents (ä -> a, ß -> ss) and unify separators."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _SEPARATORS.sub(" ", stripped).strip()


def _token_matches(query_token: str, candidate_token: str) -> bool:
    if query_token == candidate_token:
        return True
    if len(query_token) >= 3 and candidate_token.startswith(query_token):
        return True
    return SequenceMatcher(None, query_token, candidate_token).ratio() >= 0.85


def similarity(query: str, candidate: str) -> float:
    """Score in [0, 1] describing how well ``candidate`` matches ``query``."""
    q = normalize(query)
    c = normalize(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    if q in c:
        return min(1.0, 0.9 + 0.1 * (len(q) / len(c)))
    candidate_tokens = c.split()
    if all(
        any(_token_matches(query_token, token) for token in candidate_tokens)
        for query_token in q.split()
    ):
        return 0.85
    return SequenceMatcher(None, q, c).ratio()


def rank_matches(
    query: str, items: Iterable[T], key: Callable[[T], str]
) -> list[tuple[float, T]]:
    """Return all items with a positive score, best match first."""
    scored = [(similarity(query, key(item)), item) for item in items]
    return sorted(
        (pair for pair in scored if pair[0] > 0), key=lambda pair: pair[0], reverse=True
    )


def best_match(
    query: str,
    items: Iterable[T],
    key: Callable[[T], str],
    cutoff: float = 0.6,
) -> T | None:
    """Return the best-scoring item if its score reaches ``cutoff``, else ``None``."""
    ranked = rank_matches(query, items, key)
    if not ranked or ranked[0][0] < cutoff:
        return None
    return ranked[0][1]
