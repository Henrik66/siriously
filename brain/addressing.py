"""Who is the brain talking to, and how much may it do without asking.

Ported from the Mac-side mini brain (``mac/mini_brain.py``), which was tuned live on the M3:

* **Wake word.** Only "Siriously, …" counts (Whisper usually writes "seriously"). It also counts at the start
  of a later sentence in the same clip ("Right. Seriously, text Omar hello."), never mid-sentence.
* **Follow-ups.** Speech that *starts* within ``follow_up_seconds`` of the last command *finishing* needs no
  wake word, but only if it sounds like a command ("close it", "undo that"). "Oh, it worked that time" is
  you talking about the screen: a live test planned it as a reply and sent it to a chat.
* **Permissions.** Per-category allow / ask / block from the menu bar (``~/.siriously/settings.json``, shared
  with the menu bar app and the speech daemon): sending messages, deleting, purchases, shut down/restart,
  quitting apps, moving to Trash.

The Brain only uses this when it is given ``Prefs`` (``--mac`` does). Without it, behaviour is unchanged:
every verified utterance is a command and ``brain.safety`` decides what needs a "yes".
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from brain.contracts import Action, Plan, UISnapshot
from brain.safety import confirm_reason, normalize_keys

Mode = Literal["allow", "ask", "block"]

SETTINGS_PATH = Path(os.environ.get("SIRIOUSLY_SETTINGS", "~/.siriously/settings.json")).expanduser()
DEFAULTS: dict[str, Any] = {
    "listening": True,
    "wake_word": "siriously",
    "wake_word_required": True,
    "follow_up_seconds": 10,
    "speak_answers": False,
    "permissions": {
        "send_messages": "ask",
        "delete": "ask",
        "purchases": "block",
        "system": "ask",
        "quit_apps": "allow",
        "trash": "allow",
    },
}
CATEGORY_LABELS = {
    "send_messages": "Sending messages",
    "delete": "Deleting",
    "purchases": "Purchases",
    "system": "Shut down, restart, log out",
    "quit_apps": "Quitting apps",
    "trash": "Moving to Trash",
}
_STRICTNESS: dict[str, int] = {"allow": 0, "ask": 1, "block": 2}


class Prefs:
    """``settings.json``, re-read when its mtime changes (the menu bar app writes it)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or SETTINGS_PATH
        self._mtime: float | None = None
        self._value: dict[str, Any] = json.loads(json.dumps(DEFAULTS))

    def get(self) -> dict[str, Any]:
        try:
            mtime: float | None = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime is not None and mtime != self._mtime:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return self._value
            merged = json.loads(json.dumps(DEFAULTS))
            merged.update({k: v for k, v in raw.items() if k != "permissions"})
            merged["permissions"].update(raw.get("permissions") or {})
            self._value, self._mtime = merged, mtime
        return self._value

    @property
    def wake_required(self) -> bool:
        return bool(self.get().get("wake_word_required", True))

    @property
    def follow_up_s(self) -> float:
        return float(self.get().get("follow_up_seconds", 10))

    def wake_rx(self) -> re.Pattern[str]:
        return wake_rx(str(self.get().get("wake_word") or "siriously"))

    def mode(self, category: str) -> Mode:
        m = str(self.get()["permissions"].get(category, "ask"))
        return m if m in ("allow", "ask", "block") else "ask"  # type: ignore[return-value]


# ---------------------------------------------------------------------------------------------- wake word

WAKE_VARIANTS = {
    "siriously": r"(?:siriously|seriously|siri ?ously|sirius ?ly|sirious|cereusly|serious ?ly|seriosly)",
}


def wake_rx(word: str) -> re.Pattern[str]:
    w = word.lower().strip()
    core = WAKE_VARIANTS.get(w, r"\s+".join(map(re.escape, w.split())))
    return re.compile(r"^\W*(?:(?:hey|hi|ok|okay|yo)\W+)?" + core + r"\b\W*", re.IGNORECASE)


_LEAD_FILLER = re.compile(r"^\W*(?:(?:like|so|um+|uh+|and|but|ok|okay|alright|well)\W+){1,2}", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?]\s+")
WAKE_BODY_MAX_WORDS = 25  # longer than this after a mid-clip "Seriously." is conversation, not a command


def _wake_at(seg: str, rx: re.Pattern[str]) -> int | None:
    """End of the wake word when ``seg`` starts with it, optionally after one or two fillers ("Like, …")."""
    if m := rx.match(seg):
        return m.end()
    if (lead := _LEAD_FILLER.match(seg)) and (m := rx.match(seg[lead.end() :])):
        return lead.end() + m.end()
    return None


def _until_next_wake(body: str, rx: re.Pattern[str]) -> str:
    """'open Safari. Seriously open Safari.' -> 'open Safari.' (a repeated command runs once)."""
    for b in _SENTENCE_END.finditer(body):
        if _wake_at(body[b.end() :], rx) is not None:
            return body[: b.start() + 1].strip()
    return body.strip()


def strip_wake(text: str, rx: re.Pattern[str]) -> tuple[bool, str]:
    """(woke, text without the wake word). Accepts it at the start, or at the start of a later sentence
    (also after "like"/"so"/"um") when a command follows, since the mic often glues earlier chatter onto
    the command: "It's a good idea. Like seriously open Safari" -> "open Safari"."""
    if m := rx.match(text):
        return True, _until_next_wake(text[m.end() :], rx)
    for start in [0, *(b.end() for b in _SENTENCE_END.finditer(text))]:
        seg = text[start:]
        if (end := _wake_at(seg, rx)) is not None:
            body = seg[end:]
            body = _until_next_wake(body, rx)
            # mid-clip, "Seriously, he never listens." is just talk: only a command counts
            if len(body.split()) <= WAKE_BODY_MAX_WORDS and sounds_like_command(body):
                return True, body
    return False, text


# A follow-up without the wake word only counts if it starts like a command.
COMMAND_VERBS = frozenset(
    [
        "open",
        "close",
        "quit",
        "launch",
        "start",
        "switch",
        "go",
        "show",
        "hide",
        "type",
        "write",
        "dictate",
        "text",
        "message",
        "send",
        "reply",
        "scroll",
        "undo",
        "redo",
        "delete",
        "remove",
        "clear",
        "click",
        "press",
        "tap",
        "select",
        "copy",
        "paste",
        "cut",
        "save",
        "new",
        "make",
        "create",
        "add",
        "rename",
        "move",
        "find",
        "search",
        "play",
        "pause",
        "stop",
        "skip",
        "mute",
        "unmute",
        "turn",
        "set",
        "change",
        "zoom",
        "minimize",
        "maximize",
        "mark",
        "pin",
        "unpin",
        "archive",
        "forward",
        "empty",
        "duplicate",
        "sort",
        "bookmark",
        "refresh",
        "reload",
        "print",
        "check",
        "uncheck",
        "toggle",
        "increase",
        "decrease",
        "raise",
        "lower",
        "put",
        "bring",
        "navigate",
        "use",
        "enter",
        "accept",
        "decline",
        "approve",
        "compose",
        "attach",
        "upload",
        "download",
        "install",
        "sign",
        "log",
        "lock",
        "restart",
        "shut",
        "sleep",
        "call",
        "facetime",
        "email",
        "reopen",
        "edit",
        "format",
        "bold",
        "italic",
        "underline",
        "highlight",
        "insert",
        "view",
        "expand",
        "collapse",
        "focus",
        "cancel",
    ]
)
_LEAD_INS = frozenset(
    [
        "and",
        "also",
        "then",
        "now",
        "next",
        "ok",
        "okay",
        "so",
        "please",
        "can",
        "could",
        "would",
        "will",
        "you",
        "just",
        "let's",
        "lets",
    ]
)
_POLITE_Q = re.compile(r"^\W*(?:(?:hey|ok|okay)\W+)?(?:can|could|would|will) you\b", re.IGNORECASE)


def _words(text: str) -> list[str]:
    return re.sub(r"[^\w\s']", " ", text.lower()).split()


def sounds_like_command(text: str) -> bool:
    t = text.strip()
    if not t or (t.endswith("?") and not _POLITE_Q.match(t)):
        return False
    words = _words(t)
    i = 0
    while i < len(words) - 1 and words[i] in _LEAD_INS:
        i += 1
    return bool(words) and words[i] in COMMAND_VERBS


@dataclass
class FollowUp:
    """When the last command finished (wall clock, same as Utterance.t_speech_end)."""

    last_active: float = 0.0

    def touch(self) -> None:
        self.last_active = time.time()

    def open_for(self, t_speech_end: float, audio_s: float, window_s: float) -> bool:
        start = (t_speech_end - audio_s) if t_speech_end > 0 else time.time()
        return start - self.last_active < window_s


# -------------------------------------------------------------------------------------------- permissions

_TEXT_RISK: tuple[tuple[str, str], ...] = (
    ("purchases", r"\b(buy|purchase|pay|order|checkout|check out)\b"),
    ("system", r"\b(shut ?down|restart|reboot|log ?out|sign ?out)\b"),
    ("delete", r"\b(delete|erase|empty|discard|remove)\b"),
    ("send_messages", r"\b(send|post|reply|forward)\b"),
    ("quit_apps", r"\bquit\b"),
    ("trash", r"\btrash\b"),
)
_KEY_RISK: dict[frozenset[str], str] = {
    normalize_keys(k): c
    for k, c in {
        "cmd+shift+delete": "delete",
        "cmd+option+shift+delete": "delete",
        "cmd+option+delete": "delete",
        "cmd+shift+q": "system",
        "cmd+option+shift+q": "system",
        "cmd+q": "quit_apps",
        "cmd+option+esc": "quit_apps",
        "cmd+delete": "trash",
        "cmd+backspace": "trash",
    }.items()
}
CHAT_APPS = frozenset(
    {"Slack", "Messages", "Mail", "Discord", "WhatsApp", "Microsoft Teams", "Telegram", "Signal", "Outlook"}
)


def _text_cats(text: str) -> set[str]:
    t = text.lower()
    return {c for c, rx in _TEXT_RISK if re.search(rx, t)}


def risk_categories(text: str, actions: Iterable[Action], snapshot: UISnapshot) -> set[str]:
    acts = list(actions)
    cats: set[str] = set()
    if any(a.op != "type" for a in acts):  # dictating "send me the file" is not sending
        cats |= _text_cats(text)
    apps = {snapshot.app.name} | {a.app or "" for a in acts if a.op == "open_app"}
    for n, a in enumerate(acts):
        if a.op in ("press", "menu") and a.target is not None and (item := snapshot.item(a.target)):
            cats |= _text_cats(f"{item.name} {item.label}")
        if a.op == "keys" and a.keys:
            combo = normalize_keys(a.keys)
            if combo in _KEY_RISK:
                cats.add(_KEY_RISK[combo])
            typed_before = any(b.op == "type" for b in acts[:n])
            if combo == normalize_keys("return") and apps & CHAT_APPS and typed_before:
                cats.add("send_messages")
    if "delete" in cats and "trash" in cats and "empty" not in text.lower():
        cats.discard("delete")  # "delete the file" in Finder = Move to Trash
    return cats


@dataclass(frozen=True)
class Verdict:
    mode: Mode
    reason: str | None = None  # why it asks / is blocked (for the confirm card or the error)


def verdict(plan: Plan, snapshot: UISnapshot, text: str, prefs: Prefs) -> Verdict:
    """allow / ask / block for ``plan``: the strictest setting among its risk categories. A plan the
    planner flagged, or that ``brain.safety`` finds destructive but no category covers, still asks."""
    cats = risk_categories(text, plan.actions, snapshot)
    if cats:
        cat = max(cats, key=lambda c: _STRICTNESS[prefs.mode(c)])
        mode = prefs.mode(cat)
        label = CATEGORY_LABELS.get(cat, cat)
        if mode == "block":
            return Verdict("block", f"{label} is blocked in your settings")
        if mode == "ask":
            return Verdict("ask", confirm_reason(plan, snapshot) or f"{label.lower()} asks first")
        if plan.confirm:
            return Verdict("ask", plan.say or "plan requires confirmation")
        return Verdict("allow")
    reason = confirm_reason(plan, snapshot)
    return Verdict("ask", reason) if reason else Verdict("allow")
