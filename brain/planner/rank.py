"""Element budget (PRD §5.3): pick the ~15 snapshot items most worth showing the model.

Score = fuzzy word overlap between the utterance and the item's name, plus small bonuses for
"always-useful" menus (New/Save/Find/Undo...), menus with shortcuts (call, don't click) and the
focused element (where ``type`` lands). Disabled items are never shown: they cannot be acted on.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from rapidfuzz import fuzz

from brain.contracts import UIItem, UISnapshot

LABEL_MAX = 40
_WORD_RE = re.compile(r"[a-z0-9]+")
# fmt: off
_STOP = frozenset({
    "a", "an", "the", "to", "and", "or", "of", "in", "on", "at", "for", "with", "my", "me", "please",
    "it", "this", "that", "then", "called", "named", "titled", "say", "saying", "is", "be", "can",
    "you", "i",
})
# fmt: on
ALWAYS_USEFUL = frozenset(
    {
        "new",
        "new note",
        "new window",
        "new tab",
        "new message",
        "new folder",
        "new event",
        "open",
        "save",
        "find",
        "search",
        "undo",
        "copy",
        "paste",
        "select all",
        "close window",
        "close tab",
    }
)
_ALWAYS_BONUS = 12.0
_SHORTCUT_BONUS = 3.0
_FOCUSED_BONUS = 15.0
_NOISE_FLOOR = 60.0  # fuzz.ratio of unrelated short words is often 25-40; treat that as no match
MIN_ELEMENTS = 5  # always-useful menus must not crowd every on-screen element out of the prompt

# Everyday words -> the macOS UI vocabulary they usually live under (System Settings panes, common
# menus). Only expands the utterance side of matching; the model still sees the real labels.
CONCEPTS: dict[str, tuple[str, ...]] = {
    "dark": ("appearance",),
    "light": ("appearance",),
    "theme": ("appearance",),
    "wifi": ("wi", "fi", "network"),
    "wireless": ("wi", "fi", "network"),
    "internet": ("wi", "fi", "network"),
    "volume": ("sound",),
    "mute": ("sound",),
    "audio": ("sound",),
    "brightness": ("displays", "display"),
    "resolution": ("displays", "display"),
    "headphones": ("bluetooth", "sound"),
    "airpods": ("bluetooth", "sound"),
    "wallpaper": ("wallpaper", "background"),
    "background": ("wallpaper",),
    "alerts": ("notifications",),
    "password": ("touch", "id", "password", "passwords"),
    "power": ("battery", "energy"),
    "bin": ("trash",),
    "garbage": ("trash",),
    "create": ("new", "add"),
    "make": ("new",),
    "start": ("new",),
    "remove": ("delete",),
    "erase": ("delete",),
    "look": ("find", "search"),
    "search": ("find",),
    "find": ("search",),
}


def words(text: str) -> list[str]:
    """Lowercased content words (stopwords dropped)."""
    return [w for w in _WORD_RE.findall(text.lower()) if w not in _STOP]


def utterance_words(text: str) -> list[str]:
    """Content words of the utterance plus their CONCEPTS expansions (deduplicated, in order)."""
    out = words(text)
    for w in list(out):
        out.extend(CONCEPTS.get(w, ()))
    return list(dict.fromkeys(out))


def _leaf_key(name: str) -> str:
    return name.lower().replace("…", "").replace("...", "").strip()


def match_score(text: str, name: str) -> float:
    """0..100 fuzzy overlap of ``name`` (an item label / menu leaf) with the utterance ``text``."""
    name_words = words(name)
    utt_words = utterance_words(text)
    if not name_words or not utt_words:
        return 0.0
    best = [_floor(max(fuzz.ratio(nw, uw) for uw in utt_words)) for nw in name_words]
    word_score = 0.5 * (sum(best) / len(best)) + 0.5 * max(best)
    phrase = _floor(fuzz.partial_ratio(" ".join(name_words), " ".join(words(text))))
    return 0.7 * word_score + 0.3 * phrase


def _floor(score: float) -> float:
    return score if score >= _NOISE_FLOOR else 0.0


def item_score(text: str, item: UIItem, focused_id: int | None) -> float:
    score = match_score(text, item.name)
    if item.kind == "menu":
        if _leaf_key(item.name) in ALWAYS_USEFUL:
            score += _ALWAYS_BONUS
        if item.shortcut:
            score += _SHORTCUT_BONUS
    elif item.id == focused_id:
        score += _FOCUSED_BONUS
    return score


def rank_items(
    text: str, snapshot: UISnapshot, k: int = 15, min_elements: int = MIN_ELEMENTS
) -> list[UIItem]:
    """Top-``k`` enabled items, best first. Ties keep snapshot order (stable sort).

    Reserves up to ``min_elements`` slots (at most a third of ``k``) for the best on-screen
    elements so a menu-heavy app, where "always useful" menus score on every utterance, still
    shows sidebar rows and buttons.
    """
    scored = [
        (item_score(text, it, snapshot.focused_id), idx, it)
        for idx, it in enumerate(snapshot.items)
        if it.enabled
    ]
    scored.sort(key=lambda t: (-t[0], t[1]))
    k = max(k, 0)
    top = scored[:k]
    n_elements = sum(1 for _, _, it in top if it.kind == "element")
    want = min(min_elements, k // 3, sum(1 for _, _, it in scored if it.kind == "element"))
    if n_elements < want:
        extra = [t for t in scored[k:] if t[2].kind == "element"][: want - n_elements]
        menus = [t for t in top if t[2].kind == "menu"]
        drop = {id(t) for t in menus[len(menus) - len(extra) :]}  # lowest-ranked menus make room
        top = sorted([t for t in top if id(t) not in drop] + extra, key=lambda t: (-t[0], t[1]))
    return [it for _, _, it in top]


def match_apps(text: str, apps: Sequence[str], limit: int = 5, cutoff: float = 80.0) -> list[str]:
    """Installed apps named in the utterance, best first (for the open_app enum).

    Case-insensitive; compares each app name with the utterance's word n-grams of the same length
    ("system settings" vs "open system settings"), so "minutes" does not match "Notes".
    """
    utt = _WORD_RE.findall(text.lower())
    scored: list[tuple[float, int, str]] = []
    for idx, app in enumerate(apps):
        name = " ".join(_WORD_RE.findall(app.lower()))
        n = len(name.split())
        if not name or len(utt) < n:
            continue
        grams = (" ".join(utt[i : i + n]) for i in range(len(utt) - n + 1))
        best = max(fuzz.ratio(name, g) for g in grams)
        if best >= cutoff:
            scored.append((best, idx, app))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [app for _, _, app in scored[:limit]]


def truncate(label: str, n: int = LABEL_MAX) -> str:
    label = " ".join(label.split())
    return label if len(label) <= n else label[: n - 1].rstrip() + "…"
