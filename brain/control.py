"""Control-word parser (PRD 5.1 step 1): regex/dict only, never the model, well under 1 ms.

Only whole-utterance matches count. "undo" and "undo that" are control; longer sentences such as
"undo the last two edits in notes" are NOT, and fall through to the fast path / planner.

Confirmations may carry a short benign tail ("okay", "yes, delete it", "yeah do it"), and stops may be
repeated or padded ("stop stop", "stop everything", "wait", "hold on") but stay at most three words, so
"stop the music in spotify" is a command, not a control word.
"""

from __future__ import annotations

import re

from brain.contracts import ControlWord

_NON_WORD = re.compile(r"[^a-z0-9\s]+")
_SPACES = re.compile(r"\s+")
_APOS = re.compile(r"['’]")
_FILLER = re.compile(
    r"^(?:(?:hey|hi|okay|ok)\s+)?(?:siriously|siri)\b\s*|\b(?:please|okay|ok|just|now|thanks|thank you)\b"
)
_WAKE = re.compile(r"^(?:(?:hey|hi|okay|ok)\s+)?(?:siriously|siri)\b\s*")


def fold(text: str) -> str:
    """Lowercase, drop apostrophes and punctuation, collapse spaces (no filler removal)."""
    t = _APOS.sub("", text.lower())
    t = _NON_WORD.sub(" ", t)
    return _SPACES.sub(" ", t).strip()


def normalize(text: str) -> str:
    """Lowercase, drop punctuation and filler ("please", "hey siriously", "okay")."""
    t = _FILLER.sub(" ", fold(text))
    return _SPACES.sub(" ", t).strip()


_WORDS: dict[ControlWord, tuple[str, ...]] = {
    "stop": ("stop", "stop it", "stop that", "halt", "abort"),
    "cancel": ("cancel", "cancel that", "never mind", "nevermind", "forget it"),
    "undo": ("undo", "undo that", "take that back"),
    "yes": ("yes", "yeah", "yep", "yup", "confirm", "do it", "go ahead", "sure"),
    "no": ("no", "nope", "dont", "do not"),
}
_LOOKUP: dict[str, ControlWord] = {p: w for w, ps in _WORDS.items() for p in ps}

# "yes"/"okay" + an optional short, benign tail. Checked on the folded text (normalize() eats "okay").
_YES_HEAD = r"(?:yes|yeah|yep|yup|ok|okay|sure)"
_YES_TAIL = (
    r"(?:do it|go ahead|go for it|delete it|do that|please|please do|please do it|confirm|proceed|"
    r"thats fine|thats ok|thats okay|its fine|fine|sounds good|i am sure|im sure|absolutely|sure)"
)
_YES_RE = re.compile(rf"{_YES_HEAD}(?: {_YES_HEAD})*(?: {_YES_TAIL})?")

# Short stop phrasings (<= 3 words after normalize()).
_STOP_HEAD = r"(?:stop|halt|abort|wait|hold on|hold it|freeze)"
_STOP_TAIL = r"(?:stop|it|that|everything|all|right(?: now| there)?|a second|a sec|a moment|wait)"
_STOP_RE = re.compile(rf"{_STOP_HEAD}(?: {_STOP_TAIL})*")
_STOP_MAX_WORDS = 3


def parse_control(text: str) -> ControlWord | None:
    """Return the control word if the whole utterance is one, else None."""
    norm = normalize(text)
    word = _LOOKUP.get(norm)
    if word is not None:
        return word
    if norm and len(norm.split()) <= _STOP_MAX_WORDS and _STOP_RE.fullmatch(norm):
        return "stop"
    folded = _WAKE.sub("", fold(text)).strip()
    if folded and (_YES_RE.fullmatch(folded) or _YES_RE.fullmatch(norm)):
        return "yes"
    return None
