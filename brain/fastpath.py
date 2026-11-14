"""Fast-path matcher (PRD 5.2): no model, <= 30 ms. Conservative: a miss is cheap, a false positive is not.

Anything with extra payload ("new note called standup") or several clauses ("open notes and ...")
returns None so the planner handles it. Emitted target ids always come from the given snapshot.

Standard commands ("copy", "new tab") prefer the matching enabled menu item. The raw shortcut is only a
fallback when the snapshot has no menu item that could make it mean something else: if the matching item
is disabled, or another item owns the shortcut (Notes' cmd+n is New Note, Calendar's cmd+t is Go to
Today), the planner decides instead.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from rapidfuzz import fuzz, process

from brain.contracts import Action, Direction, Plan, UIItem, UISnapshot
from brain.control import normalize
from brain.fixtures import normalize_keys

APP_CUTOFF = 85.0
MENU_CUTOFF = 88.0
MENU_MARGIN = 5.0
MAX_WORDS = 8

_CLAUSE = re.compile(r"\b(?:and|then|also|after that|afterwards)\b")
_OPEN = re.compile(
    r"^(?:open|launch|start|switch to|go to|bring up)\s+(?:the\s+)?(.+?)(?:\s+(?:app|application))?$"
)
_CREATE = re.compile(
    r"^(?:(?:start|create|make|add)\s+(?:(?:a|an)\s+)?(?:new\s+)?|new\s+)"
    r"([a-z]+(?:\s[a-z]+)?)\s+(?:called|named|titled)\s+(.+)$",
    re.IGNORECASE,
)
_NOUN_CUTOFF = 92.0
_SKIP_NOUNS = frozenset({"tab", "window"})
_DESTROY = re.compile(
    r"^(?:delete|remove|trash|get rid of|throw away|discard)\s+"
    r"(?:this|the|that|current|selected)(?:\s+(?:current|selected))?\s+([a-z]+(?:\s[a-z]+)?)$"
)
_NOT_AN_OBJECT = frozenset(
    {"last", "first", "next", "previous", "all", "every", "entire", "whole", "rest", "above", "below"}
)
_SEARCH = re.compile(r"^(search the web for|search for|look up|google)\s+(.+)$", re.IGNORECASE)
_DOMAIN = re.compile(
    r"^(?:go to|open|visit)\s+(?:the\s+)?((?:https?://)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:/\S*)?)$",
    re.IGNORECASE,
)
BROWSER_NAMES = frozenset({"safari", "google chrome", "chrome", "firefox", "arc", "microsoft edge", "edge"})
BROWSER_BUNDLES = frozenset(
    {
        "com.apple.safari",
        "com.google.chrome",
        "org.mozilla.firefox",
        "company.thebrowser.browser",
        "com.microsoft.edgemac",
    }
)
_PRESS = re.compile(r"^(?:press|hit|push|tap)\s+(.+)$")
_CLICK = re.compile(r"^(?:click|press|hit|tap|push)\s+(?:on\s+)?(?:the\s+)?(.+?)(?:\s+button)?$")
_SCROLL = re.compile(r"^scroll\s+(up|down)(?:\s+(a lot|a little|a bit|lot|little|bit|way))?$")
_MENU_VERB = re.compile(r"^(?:show|open|choose|select|run|click|go to|do|press|hit)\s+")
_STD_VERBS = ("open ", "go to ", "switch to ", "show ", "make ", "create ", "do ")
_DETERMINERS = frozenset({"a", "an", "the", "this", "current", "my", "that"})

APP_ALIASES: dict[str, str] = {
    "settings": "system settings",
    "system preferences": "system settings",
    "preferences": "system settings",
    "vs code": "visual studio code",
    "vscode": "visual studio code",
    "code": "visual studio code",
    "chrome": "google chrome",
    "word": "microsoft word",
    "excel": "microsoft excel",
    "powerpoint": "microsoft powerpoint",
}

_SCROLL_AMOUNT: dict[str | None, int] = {
    None: 5,
    "a lot": 15,
    "lot": 15,
    "way": 30,
    "a little": 2,
    "little": 2,
    "a bit": 2,
    "bit": 2,
}


@dataclass(frozen=True)
class _Std:
    """A standard command: prefer a menu item with one of these leaf names, else the shortcut."""

    menu: frozenset[str]
    keys: str
    say: str


def _std(keys: str, say: str, *menu: str) -> _Std:
    return _Std(frozenset(menu), keys, say)


_STD: dict[str, _Std] = {
    "new tab": _std("cmd+t", "New tab", "new tab"),
    "close tab": _std("cmd+w", "Closing tab", "close tab"),
    "next tab": _std("ctrl+tab", "Next tab", "show next tab", "next tab", "select next tab"),
    "previous tab": _std(
        "ctrl+shift+tab", "Previous tab", "show previous tab", "previous tab", "select previous tab"
    ),
    "new window": _std("cmd+n", "New window", "new window", "new finder window"),
    "close window": _std("cmd+w", "Closing window", "close window", "close"),
    "copy": _std("cmd+c", "Copy", "copy"),
    "paste": _std("cmd+v", "Paste", "paste"),
    "cut": _std("cmd+x", "Cut", "cut"),
    "select all": _std("cmd+a", "Select all", "select all"),
    "save": _std("cmd+s", "Saving", "save"),
    "find": _std("cmd+f", "Find", "find"),
    "print": _std("cmd+p", "Print", "print"),
    "redo": _std("cmd+shift+z", "Redo", "redo"),
}
_STD_ALIASES = {
    "last tab": "previous tab",
    "prev tab": "previous tab",
    "paste it": "paste",
    "copy it": "copy",
}

_KEY_NAMES = {
    "return": "return",
    "enter": "return",
    "escape": "escape",
    "esc": "escape",
    "tab": "tab",
    "space": "space",
    "spacebar": "space",
    "delete": "delete",
    "backspace": "delete",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "up arrow": "up",
    "down arrow": "down",
    "left arrow": "left",
    "right arrow": "right",
}
_MODIFIERS = {
    "command": "cmd",
    "cmd": "cmd",
    "control": "ctrl",
    "ctrl": "ctrl",
    "option": "alt",
    "alt": "alt",
    "shift": "shift",
}
_MOD_ORDER = ("cmd", "ctrl", "alt", "shift")


def parse_keys(spoken: str) -> str | None:
    """Normalized spoken key combo ("command shift n") -> "cmd+shift+n", or None if not a key."""
    words = spoken.split()
    mods: list[str] = []
    while len(words) > 1 and words[0] in _MODIFIERS:
        mods.append(_MODIFIERS[words.pop(0)])
    rest = " ".join(words)
    key = _KEY_NAMES.get(rest)
    if key is None and len(rest) == 1:  # single letter or digit (normalize() removed punctuation)
        key = rest
    if key is None or len(set(mods)) != len(mods):
        return None
    return "+".join([*(m for m in _MOD_ORDER if m in mods), key])


def _shortcut_taken(std: _Std, snap: UISnapshot) -> bool:
    """Does a menu item other than the standard command's own own its fallback shortcut?"""
    combo = normalize_keys(std.keys)
    return any(
        i.shortcut and normalize_keys(i.shortcut) == combo and normalize(i.name) not in std.menu
        for i in snap.menus
    )


# Things a person deletes as a whole. The generic tiers (Move to Trash, Edit > Delete) only fire for
# these, so "remove this formatting" / "delete this line" go to the planner instead of deleting
# the current selection. An exact "Delete <Noun>" menu leaf still matches any noun.
_OBJECT_NOUNS = frozenset(
    {
        "note", "file", "folder", "item", "document", "doc", "event", "message", "email", "mail",
        "conversation", "chat", "photo", "picture", "image", "page", "reminder", "contact",
        "entry", "card", "draft", "attachment", "bookmark", "recording", "memo",
    }
)  # fmt: skip


class FastPath:
    """Pattern matcher over the utterance and the current snapshot. No model."""

    def __init__(self, apps: Sequence[str]) -> None:
        self._apps: dict[str, str] = {}
        for app in apps:
            n = normalize(app)
            if n:
                self._apps.setdefault(n, app)
        self._app_keys = list(self._apps)

    def match(self, text: str, snapshot: UISnapshot) -> Plan | None:
        """Return a one-action Plan for an unambiguous, payload-free command, else None."""
        t = normalize(text)
        if not t or len(t.split()) > MAX_WORDS or _CLAUSE.search(t):
            return None
        created = self._create(text, snapshot)
        if created is not None:
            return created
        if (web := self._web(snapshot, text)) is not None:
            return web
        for fn in (
            self._destroy,
            self._std,
            self._scroll,
            self._keys,
            self._open_app,
            self._button,
            self._menu,
        ):
            plan = fn(t, snapshot)
            if plan is not None:
                return plan
        return None

    @staticmethod
    def _plan(snap: UISnapshot, action: Action, say: str) -> Plan:
        return Plan(snapshot_id=snap.snapshot_id, actions=(action,), say=say)

    def _destroy(self, t: str, snap: UISnapshot) -> Plan | None:
        """ "delete this note" -> the one enabled Delete <Noun> / Move to Trash / Edit > Delete leaf.

        Requires a demonstrative/article, so "delete everything" never matches. The pipeline's
        safety gate still asks for confirmation before the menu action runs.
        """
        m = _DESTROY.match(t)
        if not m:
            return None
        words = m.group(1).split()
        if any(w in _NOT_AN_OBJECT for w in words):
            return None
        noun = " ".join(words)
        singular = noun[:-3] + "y" if noun.endswith("ies") else noun.removesuffix("s")
        leaves = [(i, normalize(i.name)) for i in snap.menus if i.enabled and i.path]
        tiers = (
            [i for i, leaf in leaves if leaf in (f"delete {noun}", f"delete {singular}")],
            [i for i, leaf in leaves if leaf == "move to trash" and singular in _OBJECT_NOUNS],
            [
                i
                for i, leaf in leaves
                if leaf == "delete" and normalize(i.path[0]) == "edit" and singular in _OBJECT_NOUNS
            ],
        )
        for hits in tiers:
            if hits:
                if len({i.path for i in hits}) != 1:
                    return None
                return self._plan(snap, Action.menu(hits[0].id), hits[0].name)
        return None

    def _web(self, snap: UISnapshot, text: str) -> Plan | None:
        """In a browser: "search for X" / "go to github.com" -> focus the address bar, type, return."""
        if (
            normalize(snap.app.name) not in BROWSER_NAMES
            and snap.app.bundle_id.lower() not in BROWSER_BUNDLES
        ):
            return None
        raw = text.strip().rstrip(".!?,;").strip()
        if m := _SEARCH.match(raw):
            query = m.group(2).strip().strip("\"'“”").strip()
            if not query or (m.group(1).lower() == "google" and self._resolve_app(normalize(query))):
                return None  # "google chrome" is an app name, not a query
        elif m := _DOMAIN.match(raw):
            query = m.group(1)
        else:
            return None
        bar = [i for i in snap.menus if i.enabled and i.path and normalize(i.name) == "open location"]
        focus = Action.menu(bar[0].id) if len(bar) == 1 else Action.key("cmd+l")
        actions = (focus, Action.type_text(query), Action.key("return"))
        return Plan(snapshot_id=snap.snapshot_id, actions=actions, say=f"Searching {query}")

    def _std(self, t: str, snap: UISnapshot) -> Plan | None:
        phrase = " ".join(w for w in t.split() if w not in _DETERMINERS)
        for prefix in _STD_VERBS:
            if phrase.startswith(prefix):
                rest = phrase[len(prefix) :]
                if rest in _STD or rest in _STD_ALIASES:
                    phrase = rest
                    break
        std = _STD.get(_STD_ALIASES.get(phrase, phrase))
        if std is None:
            return None
        named = [i for i in snap.menus if i.path and normalize(i.name) in std.menu]
        hits = [i for i in named if i.enabled]
        if len({i.path for i in hits}) == 1:
            return self._plan(snap, Action.menu(hits[0].id), std.say)
        if (named and not hits) or _shortcut_taken(std, snap):
            return None  # greyed out, or the shortcut means something else here: planner decides
        return self._plan(snap, Action.key(std.keys), std.say)

    def _scroll(self, t: str, snap: UISnapshot) -> Plan | None:
        m = _SCROLL.match(t)
        if not m:
            return None
        d: Direction = "up" if m.group(1) == "up" else "down"
        return self._plan(snap, Action.scroll(d, _SCROLL_AMOUNT[m.group(2)]), f"Scrolling {d}")

    def _keys(self, t: str, snap: UISnapshot) -> Plan | None:
        m = _PRESS.match(t)
        keys = parse_keys(m.group(1)) if m else None
        return self._plan(snap, Action.key(keys), f"Pressing {keys}") if keys else None

    def resolve_app(self, spoken: str) -> str | None:
        """Installed app name for a spoken name (aliases + fuzzy), or None."""
        return self._resolve_app(normalize(spoken))

    def _create(self, text: str, snap: UISnapshot) -> Plan | None:
        """ "create a folder called demo" -> menu(New Folder), type(demo), return."""
        m = _CREATE.match(text.strip())
        if not m:
            return None
        noun = normalize(m.group(1))
        name = m.group(2).strip().strip("\"'“”").rstrip(".!?,;").strip()
        if not noun or not name or noun.split()[-1].rstrip("s") in _SKIP_NOUNS:
            return None
        singular = noun[:-3] + "y" if noun.endswith("ies") else noun.removesuffix("s")
        scored: list[tuple[float, UIItem]] = []
        for item in snap.menus:
            leaf = normalize(item.name)
            if not item.enabled or not item.path or not leaf.startswith("new "):
                continue
            kind = leaf[4:]
            if any(w in _SKIP_NOUNS for w in kind.split()) and kind != singular:
                continue
            scored.append((max(fuzz.ratio(singular, kind), fuzz.ratio(noun, kind)), item))
        scored = sorted((x for x in scored if x[0] >= _NOUN_CUTOFF), key=lambda x: -x[0])
        if not scored or any(o.path != scored[0][1].path and sc == scored[0][0] for sc, o in scored[1:]):
            return None
        item = scored[0][1]
        actions = (Action.menu(item.id), Action.type_text(name), Action.key("return"))
        return Plan(snapshot_id=snap.snapshot_id, actions=actions, say=f"{item.name}: {name}")

    def _resolve_app(self, spoken: str) -> str | None:
        spoken = normalize(APP_ALIASES.get(spoken, spoken))
        if spoken in self._apps:
            return self._apps[spoken]
        if len(spoken) < 3:
            return None
        hit = process.extractOne(spoken, self._app_keys, scorer=fuzz.ratio, score_cutoff=APP_CUTOFF)
        return self._apps[hit[0]] if hit else None

    def _open_app(self, t: str, snap: UISnapshot) -> Plan | None:
        m = _OPEN.match(t)
        app = self._resolve_app(m.group(1)) if m else None
        return self._plan(snap, Action.open_app(app), f"Opening {app}") if app else None

    def _button(self, t: str, snap: UISnapshot) -> Plan | None:
        m = _CLICK.match(t)
        if not m:
            return None
        hits = [i for i in snap.elements if i.enabled and i.label and normalize(i.label) == m.group(1)]
        if len(hits) != 1:
            return None
        return self._plan(snap, Action.press(hits[0].id), f"Pressing {hits[0].label}")

    def _menu(self, t: str, snap: UISnapshot) -> Plan | None:
        queries = {t}
        stripped = _MENU_VERB.sub("", t)
        if stripped:
            queries.add(stripped)
        scored: list[tuple[float, UIItem]] = []
        for item in snap.menus:
            if not item.enabled or not item.path:
                continue
            cands = (normalize(item.name), normalize(" ".join(item.path)))
            best = max(fuzz.ratio(q, c) for q in queries for c in cands)
            if best >= MENU_CUTOFF:
                scored.append((best, item))
        if not scored:
            return None
        scored.sort(key=lambda s: -s[0])
        top_score, top = scored[0]
        for score, other in scored[1:]:
            if other.path != top.path and top_score - score < MENU_MARGIN:
                return None  # ambiguous: leave it to the planner
        return self._plan(snap, Action.menu(top.id), top.name)
