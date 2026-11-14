"""Fixture-backed fakes for developing and evaluating the brain off-Mac (PRD §4.5, BUILD_PLAN W1).

``FixtureUIProvider`` serves UISnapshots from ``eval/fixtures/<name>.json`` and tracks the current
state; ``FakeExecutor`` applies the fixture's scripted transitions. File format::

    {"name": "notes", "initial": "s-notes-0",
     "snapshots": {"s-notes-0": {UISnapshot}, ...},
     "transitions": [{"from": "s-notes-0", "action": {"op": "menu", "target": 110}, "to": "s-notes-1"}]}

A transition ``action`` is a pattern: ``op`` plus any subset of Action fields. ``keys`` compare after
:func:`normalize_keys`, ``text``/``app`` case-insensitively.

Executor semantics (code against these; they mirror what a real Mac does as closely as a fake can):

* ``press``/``menu`` on an id missing from the current snapshot, or on a disabled item -> ``ok=False``.
  With a scripted transition -> moves state, ``changed=True``; without one -> ``ok=True, changed=False``.
* ``keys`` without a transition fires the enabled menu item whose shortcut matches (``cmd+n`` behaves
  like File › New Note); otherwise ``ok=True, changed=True`` on the same snapshot (text entry).
* ``type`` (and ``scroll``) without a transition -> ``ok=True, changed=True`` on the same snapshot, except an
  untargeted ``type`` with nothing focused -> ``changed=False``. ``type`` with a target types into it.
* Edit › Paste / Paste and Match Style / Cut change the focused text: ``changed`` iff something is focused.
* App › Quit X (or ``cmd+q``) quits the frontmost app; the previously active app, else Finder, comes forward.
* Edit › Undo… / ``cmd+z`` and :meth:`FakeExecutor.undo` pop the current app's state history. Typing is not
  recorded in history (it never changes state), so undo after typing reverts the last *state* change.
* ``open_app`` must name an app from ``list_apps()`` (case-insensitive). It switches to the fixture
  whose initial snapshot has that app name, or to a minimal synthetic snapshot for other installed apps.
* Ids are stable within a fixture, so a burst planned on an earlier snapshot of the same fixture keeps
  working; an action with a target planned on a different app's snapshot fails as stale.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from brain.contracts import JSON, OPS, Action, ActionResult, AppInfo, ContractError, UIItem, UISnapshot

DEFAULT_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "eval" / "fixtures"
APPS_FILE = "apps.json"


_TEXT_EDIT_ITEMS = frozenset({"Paste", "Paste and Match Style", "Cut"})


class FixtureError(ValueError):
    """A fixture file is missing or malformed."""


# ---------------------------------------------------------------------------
# Key combos
# ---------------------------------------------------------------------------

_MOD_ALIASES = {
    "cmd": "cmd", "command": "cmd", "meta": "cmd", "super": "cmd", "⌘": "cmd",
    "opt": "opt", "option": "opt", "alt": "opt", "⌥": "opt",
    "ctrl": "ctrl", "control": "ctrl", "ctl": "ctrl", "⌃": "ctrl",
    "shift": "shift", "⇧": "shift",
}  # fmt: skip
_MOD_ORDER = ("ctrl", "opt", "shift", "cmd")  # macOS menu display order
_KEY_ALIASES = {
    "enter": "return", "esc": "escape", "backspace": "delete", "del": "delete", "⌫": "delete",
    "spacebar": "space", "plus": "=", "minus": "-", "up arrow": "up", "down arrow": "down",
    "left arrow": "left", "right arrow": "right", "↩": "return", "⎋": "escape",
}  # fmt: skip


def normalize_keys(keys: str) -> str:
    """Canonical key combo: lowercase, aliases folded, modifiers in macOS order.

    ``"Command+Shift+N"`` and ``"shift+cmd+n"`` both become ``"shift+cmd+n"``; ``"alt+cmd+L"`` becomes
    ``"opt+cmd+l"``. Unknown tokens are kept as-is so nothing is silently dropped.
    """
    raw = keys.strip().lower()
    if raw.endswith("++"):
        parts, key = [p for p in raw[:-2].split("+") if p.strip()], "+"
    else:
        tokens = [p.strip() for p in raw.split("+") if p.strip()]
        parts, key = tokens[:-1], (tokens[-1] if tokens else "")
    mods = {_MOD_ALIASES.get(p, p) for p in parts}
    if key in _MOD_ALIASES and not parts:
        return _MOD_ALIASES[key]
    key = _KEY_ALIASES.get(key, key)
    ordered = [m for m in _MOD_ORDER if m in mods] + sorted(mods - set(_MOD_ORDER))
    return "+".join([*ordered, key])


def _fold(s: str) -> str:
    return " ".join(s.casefold().split())


# ---------------------------------------------------------------------------
# Fixture model
# ---------------------------------------------------------------------------


def pattern_matches(pattern: Mapping[str, object], action: Action) -> bool:
    """True if ``action`` has every field ``pattern`` specifies (op + any subset of Action fields)."""
    for key, want in pattern.items():
        got = getattr(action, key, None)
        if key == "keys":
            if not isinstance(got, str) or normalize_keys(got) != normalize_keys(str(want)):
                return False
        elif key in ("text", "app"):
            if not isinstance(got, str) or _fold(got) != _fold(str(want)):
                return False
        elif got != want:
            return False
    return True


@dataclass(frozen=True)
class Transition:
    src: str
    pattern: Mapping[str, object]
    dst: str

    def to_dict(self) -> JSON:
        return {"from": self.src, "action": dict(self.pattern), "to": self.dst}


@dataclass(frozen=True)
class Fixture:
    name: str
    initial: str
    snapshots: Mapping[str, UISnapshot]
    transitions: tuple[Transition, ...] = ()
    note: str = ""

    @property
    def app(self) -> AppInfo:
        return self.snapshots[self.initial].app

    def next_state(self, state: str, action: Action) -> str | None:
        for t in self.transitions:
            if t.src == state and pattern_matches(t.pattern, action):
                return t.dst
        return None

    def to_dict(self) -> JSON:
        return {
            "name": self.name,
            "note": self.note,
            "initial": self.initial,
            "snapshots": {k: s.to_dict() for k, s in self.snapshots.items()},
            "transitions": [t.to_dict() for t in self.transitions],
        }

    @classmethod
    def from_dict(cls, d: JSON, source: str = "fixture") -> Fixture:
        try:
            snaps = {str(k): UISnapshot.from_dict(v) for k, v in dict(d["snapshots"]).items()}
            trans = tuple(_transition(t) for t in d.get("transitions") or ())
            fx = cls(
                name=str(d["name"]),
                initial=str(d["initial"]),
                snapshots=snaps,
                transitions=trans,
                note=str(d.get("note") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:  # ContractError is a ValueError
            raise FixtureError(f"{source}: {exc!r}") from exc
        problems = validate_fixture(fx)
        if problems:
            raise FixtureError(f"{source}: " + "; ".join(problems))
        return fx


_ACTION_FIELDS = frozenset({"op", "target", "text", "keys", "app", "direction", "amount", "ms"})


def _transition(t: JSON) -> Transition:
    pattern = dict(t["action"])
    if pattern.get("op") not in OPS:
        raise ContractError(f"transition: unknown op {pattern.get('op')!r}")
    unknown = set(pattern) - _ACTION_FIELDS
    if unknown:  # a typo like "targt" would otherwise make the transition silently never fire
        raise ContractError(f"transition: unknown action fields {sorted(unknown)}")
    return Transition(src=str(t["from"]), pattern=pattern, dst=str(t["to"]))


def validate_fixture(fx: Fixture) -> list[str]:
    """Structural problems in a fixture (empty list = valid)."""
    problems: list[str] = []
    if fx.initial not in fx.snapshots:
        problems.append(f"initial {fx.initial!r} is not a snapshot")
    for key, snap in fx.snapshots.items():
        if snap.snapshot_id != key:
            problems.append(f"snapshot key {key!r} != snapshot_id {snap.snapshot_id!r}")
        ids = [i.id for i in snap.items]
        if len(ids) != len(set(ids)):
            problems.append(f"{key}: duplicate item ids")
        if snap.focused_id is not None and snap.item(snap.focused_id) is None:
            problems.append(f"{key}: focused_id {snap.focused_id} is not an item")
    problems += _transition_problems(fx)
    problems += _stability_problems(fx)
    return problems


def _transition_problems(fx: Fixture) -> list[str]:
    problems: list[str] = []
    for t in fx.transitions:
        if t.src not in fx.snapshots or t.dst not in fx.snapshots:
            problems.append(f"transition {t.src}->{t.dst} references an unknown snapshot")
            continue
        target = t.pattern.get("target")
        if isinstance(target, int) and fx.snapshots[t.src].item(target) is None:
            problems.append(f"transition from {t.src}: target {target} is not in that snapshot")
    return problems


def _stability_problems(fx: Fixture) -> list[str]:
    """Ids are stable within a fixture: one id = one kind/role (menus: one path) in every state."""
    seen: dict[int, tuple[str, str, tuple[str, ...]]] = {}
    problems: list[str] = []
    for snap in fx.snapshots.values():
        for it in snap.items:
            sig = (it.kind, it.role, it.path)
            if seen.setdefault(it.id, sig) != sig:
                problems.append(f"id {it.id} changes identity across states ({seen[it.id]} vs {sig})")
    return problems


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def fixture_names(fixtures_dir: Path | None = None) -> list[str]:
    """Names of all fixtures in ``fixtures_dir`` (``apps.json`` excluded)."""
    d = fixtures_dir or DEFAULT_FIXTURES_DIR
    return sorted(p.stem for p in d.glob("*.json") if p.name != APPS_FILE)


def _fixture_path(fixture: str | Path, fixtures_dir: Path | None) -> Path:
    p = Path(fixture)
    if p.suffix == ".json" and (p.is_absolute() or p.exists()):
        return p
    d = fixtures_dir or DEFAULT_FIXTURES_DIR
    path = d / f"{fixture}.json"
    if not path.exists():
        raise FixtureError(f"no fixture {str(fixture)!r} in {d} (have: {', '.join(fixture_names(d))})")
    return path


@lru_cache(maxsize=64)
def _load_cached(path: Path, mtime_ns: int) -> Fixture:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise FixtureError(f"{path}: top level must be an object")
    return Fixture.from_dict(data, source=str(path))


def load_fixture(fixture: str | Path, fixtures_dir: Path | None = None) -> Fixture:
    """Load (and validate) a fixture by name (``"notes"``) or by path to its JSON file."""
    path = _fixture_path(fixture, fixtures_dir).resolve()
    if not path.exists():
        raise FixtureError(f"fixture file not found: {path}")
    return _load_cached(path, path.stat().st_mtime_ns)


def load_apps(fixtures_dir: Path | None = None) -> list[str]:
    """Installed app names from ``apps.json`` (a JSON list, or ``{"apps": [...]}``)."""
    path = (fixtures_dir or DEFAULT_FIXTURES_DIR) / APPS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureError(f"{path}: {exc}") from exc
    apps = data.get("apps") if isinstance(data, dict) else data
    if not isinstance(apps, list) or not all(isinstance(a, str) for a in apps):
        raise FixtureError(f"{path}: expected a list of app names")
    return [str(a) for a in apps]


def synthetic_fixture(app: str) -> Fixture:
    """Minimal one-state fixture for an installed app that has no fixture file."""
    slug = re.sub(r"[^a-z0-9]+", "-", app.lower()).strip("-") or "app"
    menus = [
        (100, (app, f"About {app}"), None),
        (101, (app, f"Hide {app}"), "cmd+h"),
        (102, (app, f"Quit {app}"), "cmd+q"),
        (110, ("Edit", "Undo"), "cmd+z"),
        (111, ("Edit", "Copy"), "cmd+c"),
        (112, ("Edit", "Paste"), "cmd+v"),
        (113, ("Edit", "Select All"), "cmd+a"),
        (120, ("Window", "Minimize"), "cmd+m"),
    ]
    items = tuple(
        UIItem(id=i, kind="menu", role="menuitem", label=p[-1], path=p, shortcut=s) for i, p, s in menus
    )
    sid = f"s-{slug}-0"
    snap = UISnapshot(snapshot_id=sid, app=AppInfo(name=app), window=app, items=items)
    return Fixture(name=f"app:{slug}", initial=sid, snapshots={sid: snap}, note="synthetic")


# ---------------------------------------------------------------------------
# UIProvider
# ---------------------------------------------------------------------------


@dataclass
class _AppState:
    fixture: Fixture
    state: str
    history: list[str] = field(default_factory=list)


class FixtureUIProvider:
    """UIProvider over fixture files. Tracks the frontmost app and each app's current state."""

    def __init__(
        self,
        fixture: str | Path,
        fixtures_dir: Path | None = None,
        *,
        state: str | None = None,
        apps: Sequence[str] | None = None,
    ) -> None:
        if fixtures_dir is None and Path(fixture).suffix == ".json":
            fixtures_dir = Path(fixture).resolve().parent
        self.fixtures_dir = fixtures_dir or DEFAULT_FIXTURES_DIR
        fx = load_fixture(fixture, self.fixtures_dir)
        if state is not None and state not in fx.snapshots:
            raise FixtureError(f"fixture {fx.name!r} has no state {state!r}")
        self._first = (fx, state or fx.initial)
        self._apps_override = list(apps) if apps is not None else None
        self._apps: list[str] | None = None
        self._by_app: dict[str, Fixture] | None = None
        self.reset()

    # -- UIProvider -----------------------------------------------------------
    def snapshot(self) -> UISnapshot:
        cur = self._cur
        return cur.fixture.snapshots[cur.state]

    def list_apps(self) -> list[str]:
        if self._apps is None:
            self._apps = self._compute_apps()
        return list(self._apps)

    # -- state ------------------------------------------------------------------
    @property
    def fixture(self) -> Fixture:
        return self._cur.fixture

    @property
    def state(self) -> str:
        return self._cur.state

    def reset(self) -> None:
        fx, state = self._first
        self._open: dict[str, _AppState] = {fx.name: _AppState(fx, state)}  # running apps, by fixture
        self._order: list[str] = [fx.name]  # activation order, frontmost last
        self._front = fx.name

    def goto(self, state: str) -> None:
        """Move the frontmost app to ``state`` (pushes history for undo)."""
        cur = self._cur
        if state not in cur.fixture.snapshots:
            raise FixtureError(f"fixture {cur.fixture.name!r} has no state {state!r}")
        cur.history.append(cur.state)
        cur.state = state

    def pop(self) -> bool:
        """Undo the frontmost app's last state change. False if there is nothing to undo."""
        cur = self._cur
        if not cur.history:
            return False
        cur.state = cur.history.pop()
        return True

    def resolve_app(self, name: str) -> str | None:
        """Installed app name matching ``name`` case-insensitively (``.app`` suffix ignored)."""
        want = _fold(name.removesuffix(".app"))
        return next((a for a in self.list_apps() if _fold(a) == want), None)

    def activate(self, app: str) -> bool:
        """Bring ``app`` (an installed app name) to the front. Returns True if the frontmost app changed."""
        fx = self._fixture_for(app)
        if fx.name == self._front:
            return False
        self._open.setdefault(fx.name, _AppState(fx, fx.initial))
        self._front = fx.name
        if fx.name in self._order:
            self._order.remove(fx.name)
        self._order.append(fx.name)
        return True

    def quit_front(self) -> str:
        """Quit the frontmost app; the previously active app (else Finder) comes forward. Returns its name.

        A quit app forgets its state: reopening it starts from the fixture's initial snapshot.
        """
        quitting = self._front
        self._open.pop(quitting, None)
        self._order = [n for n in self._order if n != quitting]
        if not self._order:  # Finder is always running on macOS
            finder = self._fixture_for(self.resolve_app("Finder") or "Finder")
            self._open[finder.name] = _AppState(finder, finder.initial)
            self._order = [finder.name]
        self._front = self._order[-1]
        return self._cur.fixture.app.name

    # -- internals --------------------------------------------------------------
    @property
    def _cur(self) -> _AppState:
        return self._open[self._front]

    def _fixture_index(self) -> dict[str, Fixture]:
        if self._by_app is None:
            first = self._first[0]
            self._by_app = {_fold(first.app.name): first}
            for name in fixture_names(self.fixtures_dir):
                fx = load_fixture(name, self.fixtures_dir)
                self._by_app.setdefault(_fold(fx.app.name), fx)
        return self._by_app

    def _fixture_for(self, app: str) -> Fixture:
        index = self._fixture_index()
        key = _fold(app)
        if key not in index:
            index[key] = synthetic_fixture(app)
        return index[key]

    def _compute_apps(self) -> list[str]:
        if self._apps_override is not None:
            return self._apps_override
        try:
            apps = load_apps(self.fixtures_dir)
        except FixtureError:
            apps = []
        known = {_fold(a) for a in apps}
        for fx in self._fixture_index().values():
            if _fold(fx.app.name) not in known:
                apps.append(fx.app.name)
                known.add(_fold(fx.app.name))
        return apps


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class FakeExecutor:
    """Executor that applies a fixture's scripted transitions to a :class:`FixtureUIProvider`."""

    def __init__(self, provider: FixtureUIProvider) -> None:
        self.provider = provider
        self.log: list[Action] = []
        self.results: list[ActionResult] = []
        self.undos = 0

    def run(self, snapshot_id: str, action: Action) -> ActionResult:
        self.log.append(action)
        result = self._apply(snapshot_id, action)
        self.results.append(result)
        return result

    def undo(self) -> bool:
        self.undos += 1
        return self.provider.pop()

    # -- dispatch -----------------------------------------------------------------
    def _apply(self, snapshot_id: str, action: Action) -> ActionResult:
        if action.target is not None and snapshot_id not in self.provider.fixture.snapshots:
            front = self.provider.snapshot()
            return self._fail(
                f"stale snapshot {snapshot_id!r}: {front.app.name} is frontmost ({front.snapshot_id})"
            )
        if action.op == "open_app":
            return self._open_app(action)
        if action.op in ("press", "menu"):
            return self._activate_item(action)
        if action.op == "keys":
            return self._keys(action)
        if action.op == "type":
            return self._type(action)
        if action.op == "wait":
            return self._ok(changed=False)
        return self._transition_or(action, changed=True)  # scroll

    def _ok(self, changed: bool) -> ActionResult:
        return ActionResult(ok=True, changed=changed, snapshot=self.provider.snapshot())

    def _fail(self, error: str) -> ActionResult:
        return ActionResult(ok=False, changed=False, error=error, snapshot=self.provider.snapshot())

    def _transition_or(self, action: Action, changed: bool) -> ActionResult:
        dst = self.provider.fixture.next_state(self.provider.state, action)
        if dst is not None:
            self.provider.goto(dst)
            return self._ok(changed=True)
        return self._ok(changed=changed)

    # -- ops ----------------------------------------------------------------------
    def _open_app(self, action: Action) -> ActionResult:
        app = self.provider.resolve_app(action.app or "")
        if app is None:
            return self._fail(f"app {action.app!r} is not installed")
        return self._ok(changed=self.provider.activate(app))

    def _activate_item(self, action: Action) -> ActionResult:
        snap = self.provider.snapshot()
        item = snap.item(action.target) if action.target is not None else None
        if item is None:
            return self._fail(f"no item {action.target} in {snap.snapshot_id}")
        if not item.enabled:
            return self._fail(f"{item.display!r} is disabled")
        if action.op == "menu" and item.kind != "menu":
            return self._fail(f"item {item.id} ({item.display!r}) is not a menu item")
        leaf = item.path[-1] if item.kind == "menu" and item.path else ""
        if leaf.startswith("Undo"):  # also "Undo Typing" etc. in real captures
            return self._ok(changed=self.provider.pop())
        if leaf in _TEXT_EDIT_ITEMS and self.provider.fixture.next_state(self.provider.state, action) is None:
            return self._ok(changed=self.provider.snapshot().focused_id is not None)  # edits focused text
        if leaf.startswith("Quit ") and self.provider.fixture.next_state(self.provider.state, action) is None:
            self.provider.quit_front()
            return self._ok(changed=True)
        dst = self.provider.fixture.next_state(self.provider.state, action)
        if dst is None and item.kind == "menu":  # pressing a menu item == choosing it
            dst = self.provider.fixture.next_state(self.provider.state, Action.menu(item.id))
        if dst is None:
            return self._ok(changed=False)
        self.provider.goto(dst)
        return self._ok(changed=True)

    def _keys(self, action: Action) -> ActionResult:
        dst = self.provider.fixture.next_state(self.provider.state, action)
        if dst is not None:
            self.provider.goto(dst)
            return self._ok(changed=True)
        combo = normalize_keys(action.keys or "")
        for item in self.provider.snapshot().menus:
            if item.shortcut and normalize_keys(item.shortcut) == combo:
                if not item.enabled:
                    return self._ok(changed=False)
                return self._activate_item(Action.menu(item.id))
        return self._ok(changed=True)

    def _type(self, action: Action) -> ActionResult:
        if action.target is not None:
            item = self.provider.snapshot().item(action.target)
            if item is None:
                return self._fail(f"no item {action.target} in {self.provider.snapshot().snapshot_id}")
            if not item.enabled:
                return self._fail(f"{item.display!r} is disabled")
        elif self.provider.snapshot().focused_id is None:
            # Nothing focused: keystrokes go nowhere useful on a real Mac. Not an error, but no change.
            return self._transition_or(action, changed=False)
        return self._transition_or(action, changed=True)
