"""Answer parser for ambiguity questions: "one", "the second one", "the last one", or a label."""

from __future__ import annotations

import re
from collections.abc import Sequence

from rapidfuzz import fuzz

_ORDINALS: dict[str, int] = {
    "1": 0,
    "one": 0,
    "first": 0,
    "2": 1,
    "two": 1,
    "second": 1,
    "3": 2,
    "three": 2,
    "third": 2,
    "4": 3,
    "four": 3,
    "fourth": 3,
    "5": 4,
    "five": 4,
    "fifth": 4,
    "6": 5,
    "six": 5,
    "sixth": 5,
    "7": 6,
    "seven": 6,
    "seventh": 6,
    "8": 7,
    "eight": 7,
    "eighth": 7,
    "9": 8,
    "nine": 8,
    "ninth": 8,
}
_LAST = frozenset({"last", "final"})
_NOISE = frozenset(
    {"the", "a", "number", "no", "option", "item", "choice", "one", "pick", "choose", "select", "please"}
)
_NON_WORD = re.compile(r"[^a-z0-9\s]+")
FUZZY_THRESHOLD = 85.0


def _norm(text: str) -> str:
    return " ".join(_NON_WORD.sub(" ", text.lower().replace("'", "")).split())


def _ordinal(words: list[str], n: int) -> int | None:
    """ "one", "number two", "the second one", "first one", "the last one"."""
    core = [w for w in words if w not in _NOISE]
    if not core and "one" in words:
        core = ["one"]  # bare "one" is candidate 1
    if len(core) != 1:
        return None
    word = core[0]
    idx = n - 1 if word in _LAST else _ORDINALS.get(word)
    return idx if idx is not None and 0 <= idx < n else None


def parse_choice(text: str, labels: Sequence[str]) -> int | None:
    """0-based index of the candidate the user picked, or None if ``text`` is not an answer."""
    words = _norm(text).split()
    if not words or not labels:
        return None
    if (idx := _ordinal(words, len(labels))) is not None:
        return idx
    spoken = " ".join(w for w in words if w not in {"the", "please", "pick", "choose", "select"})
    scored = [(fuzz.ratio(spoken, _norm(lb)), i) for i, lb in enumerate(labels)]
    scored.sort(reverse=True)
    best = scored[0]
    if best[0] < FUZZY_THRESHOLD:
        return None
    if len(scored) > 1 and scored[1][0] >= best[0]:
        return None  # ambiguous between labels
    return best[1]
