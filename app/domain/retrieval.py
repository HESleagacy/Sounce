"""Deterministic lexical relevance scoring for assistant context.

The assistant used to hand Gemini the 30 most recent memories regardless of what
the user just said. That is fine with 30 memories and useless with 3,000: the
relevant fact gets pushed out by recency long before the prompt gets large.

This module scores candidates against the incoming message with plain lexical
overlap. It is deliberately not embeddings: lexical retrieval is debuggable,
costs nothing, needs no extra service, and for a single user's own phrasing it
is a strong baseline. Swap it for embeddings only when a measurement says this
is the thing that is failing.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any, TypeVar

TOKEN = re.compile(r"[a-z0-9]+")

# Hinglish and English filler that appears in almost every message and would
# otherwise dominate the overlap score.
STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "that",
        "this",
        "with",
        "from",
        "you",
        "your",
        "are",
        "was",
        "have",
        "has",
        "had",
        "not",
        "but",
        "can",
        "will",
        "would",
        "should",
        "what",
        "when",
        "where",
        "which",
        "who",
        "how",
        "why",
        "please",
        "hai",
        "hain",
        "tha",
        "they",
        "them",
        "their",
        "then",
        "than",
        "there",
        "here",
        "some",
        "any",
        "all",
        "thi",
        "kya",
        "kaise",
        "mera",
        "meri",
        "mere",
        "aap",
        "aapka",
        "aapki",
        "main",
        "mujhe",
        "koi",
        "kuch",
        "karo",
        "kar",
        "karna",
        "karta",
        "karti",
        "diya",
        "raha",
        "rahi",
        "hoon",
        "hona",
        "gaya",
        "gayi",
        "bhi",
        "par",
        "aur",
        "yeh",
        "woh",
    }
)


def tokenize(text: str | None) -> set[str]:
    if not text:
        return set()
    return {token for token in TOKEN.findall(text.lower()) if len(token) > 2 and token not in STOPWORDS}


T = TypeVar("T")


def score(query_tokens: set[str], candidate_tokens: set[str]) -> float:
    """Overlap weighted so that matching a rare-ish long token counts for more.

    Normalised by candidate length so a rambling memory cannot outrank a precise
    one purely by containing more words.
    """
    if not query_tokens or not candidate_tokens:
        return 0.0
    overlap = query_tokens & candidate_tokens
    if not overlap:
        return 0.0
    weight = sum(1.0 + math.log(len(token)) for token in overlap)
    return weight / math.sqrt(len(candidate_tokens))


def rank(
    query: str | None,
    candidates: Sequence[T],
    text_of: Callable[[T], str],
    limit: int,
    keep_recent: int = 3,
) -> list[T]:
    """Return the most relevant candidates, always keeping a few recent ones.

    ``candidates`` must arrive newest-first. ``keep_recent`` guarantees the
    assistant still sees what was just discussed even when the current message
    shares no vocabulary with it, which is what makes follow-up turns work.
    """
    if limit <= 0 or not candidates:
        return []
    query_tokens = tokenize(query)
    if not query_tokens:
        return list(candidates[:limit])

    pinned = list(candidates[:keep_recent])
    pinned_ids = {id(item) for item in pinned}
    scored = [
        (score(query_tokens, tokenize(text_of(item))), -index, item)
        for index, item in enumerate(candidates)
        if id(item) not in pinned_ids
    ]
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    relevant = [item for value, _, item in scored if value > 0]
    selected = pinned + relevant[: max(0, limit - len(pinned))]
    if len(selected) < limit:
        remaining = [item for _, _, item in scored if item not in selected]
        selected += remaining[: limit - len(selected)]
    # Restore newest-first ordering so the prompt reads chronologically.
    order = {id(item): index for index, item in enumerate(candidates)}
    return sorted(selected[:limit], key=lambda item: order.get(id(item), 0))


def trim_to_budget(
    sections: Iterable[list[Any]],
    serialized_length: Callable[[], int],
    budget: int,
) -> None:
    """Drop the lowest-priority entries in place until the prompt fits.

    ``sections`` is consumed lowest-priority-first, so callers decide what gets
    sacrificed: documents before memories, memories before the pending action.
    """
    for section in sections:
        while section and serialized_length() > budget:
            section.pop()
        if serialized_length() <= budget:
            return
