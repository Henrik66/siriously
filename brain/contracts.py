"""Shared contracts between the speech daemon, brain, UIProvider/Executor and overlay (PRD §4).

Everything here is plain dataclasses + JSON Schema dicts, stdlib only, so the Mac side
(MacUIProvider / MacExecutor) and the brain can both import it unchanged.

All ids are integers scoped to one ``snapshot_id``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast, get_args, runtime_checkable

JSON = dict[str, Any]

# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------

Op = Literal["open_app", "press", "menu", "type", "keys", "scroll", "wait"]
OPS: tuple[str, ...] = get_args(Op)
TARGET_OPS: frozenset[str] = frozenset({"press", "menu"})  # target required
ItemKind = Literal["element", "menu"]
Direction = Literal["up", "down"]
EventType = Literal["heard", "rejected", "route", "plan", "action", "done", "error", "confirm"]
EVENT_TYPES: tuple[str, ...] = get_args(EventType)
RouteKind = Literal["control", "flow", "fastpath", "planner", "dictation", "rejected", "none"]
ControlWord = Literal["stop", "cancel", "undo", "yes", "no"]
PlanMode = Literal["actions", "dictation"]

MAX_ACTIONS = 6


class ContractError(ValueError):
    """Raised when a payload does not match a contract."""


def _req(d: JSON, key: str, typ: type | tuple[type, ...], ctx: str) -> Any:
    if key not in d:
        raise ContractError(f"{ctx}: missing '{key}'")
    v = d[key]
    if not isinstance(v, typ) or (typ is int and isinstance(v, bool)):
        raise ContractError(f"{ctx}: '{key}' has wrong type {type(v).__name__}")
    return v


# ---------------------------------------------------------------------------
# 4.1 Utterance (speech daemon -> brain)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Speaker:
    verified: bool
    score: float = 0.0
    name: str | None = None
    threshold: float | None = None  # voice-lock accept threshold: a short "yes" may pass slightly below it

    def to_dict(self) -> JSON:
        d: JSON = {"verified": self.verified, "score": self.score, "name": self.name}
        if self.threshold is not None:
            d["threshold"] = self.threshold
        return d

    @classmethod
    def from_dict(cls, d: JSON) -> Speaker:
        thr = d.get("threshold")
        return cls(
            verified=bool(_req(d, "verified", bool, "speaker")),
            score=float(d.get("score") or 0.0),
            name=d.get("name"),
            threshold=float(thr) if thr is not None else None,
        )


@dataclass(frozen=True)
class Utterance:
    id: str
    text: str
    t_speech_end: float = 0.0
    stt_ms: int = 0
    speaker: Speaker = field(default_factory=lambda: Speaker(verified=True, score=1.0))
    audio_s: float = 0.0  # clip length; t_speech_end - audio_s ~ when you started talking (follow-up window)

    def to_dict(self) -> JSON:
        d: JSON = {
            "id": self.id,
            "text": self.text,
            "t_speech_end": self.t_speech_end,
            "stt_ms": self.stt_ms,
            "speaker": self.speaker.to_dict(),
        }
        if self.audio_s:
            d["audio_s"] = self.audio_s
        return d

    @classmethod
    def from_dict(cls, d: JSON) -> Utterance:
        text = _req(d, "text", str, "utterance")
        if len(text) > MAX_UTTERANCE_CHARS:
            raise ContractError(f"utterance.text longer than {MAX_UTTERANCE_CHARS} characters")
        return cls(
            id=_req(d, "id", str, "utterance"),
            text=text,
            t_speech_end=float(d.get("t_speech_end", 0.0)),
            stt_ms=int(d.get("stt_ms", 0)),
            speaker=Speaker.from_dict(_req(d, "speaker", dict, "utterance")),
            audio_s=float(d.get("audio_s") or 0.0),
        )


# ---------------------------------------------------------------------------
# 4.2 UISnapshot (UIProvider -> brain)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AppInfo:
    name: str
    bundle_id: str = ""

    def to_dict(self) -> JSON:
        return {"name": self.name, "bundle_id": self.bundle_id}

    @classmethod
    def from_dict(cls, d: JSON) -> AppInfo:
        return cls(name=_req(d, "name", str, "app"), bundle_id=str(d.get("bundle_id", "")))


@dataclass(frozen=True)
class UIItem:
    """One actionable thing on screen: an AX element or a menu-bar item."""

    id: int
    kind: ItemKind
    role: str = ""
    label: str = ""
    value: str | None = None
    enabled: bool = True
    path: tuple[str, ...] = ()  # menus only: ("File", "New Note")
    shortcut: str | None = None  # menus only: "cmd+n"

    @property
    def display(self) -> str:
        """Human/model-facing name: menu path joined with ›, else the label."""
        if self.kind == "menu" and self.path:
            return " › ".join(self.path)
        return self.label

    @property
    def name(self) -> str:
        """Leaf name used for matching ("New Note" for File › New Note)."""
        if self.kind == "menu" and self.path:
            return self.path[-1]
        return self.label

    def to_dict(self) -> JSON:
        d: JSON = {"id": self.id, "kind": self.kind, "enabled": self.enabled}
        if self.kind == "menu":
            d["path"] = list(self.path)
            d["shortcut"] = self.shortcut
            if self.label:
                d["label"] = self.label
        else:
            d["role"] = self.role
            d["label"] = self.label
            if self.value is not None:
                d["value"] = self.value
        return d

    @classmethod
    def from_dict(cls, d: JSON) -> UIItem:
        kind = _req(d, "kind", str, "item")
        if kind not in ("element", "menu"):
            raise ContractError(f"item: bad kind {kind!r}")
        path = tuple(str(p) for p in d.get("path") or ())
        label = str(d.get("label") or (path[-1] if path else ""))
        return cls(
            id=_req(d, "id", int, "item"),
            kind=cast(ItemKind, kind),
            role=str(d.get("role") or ("menuitem" if kind == "menu" else "")),
            label=label,
            value=d.get("value"),
            enabled=bool(d.get("enabled", True)),
            path=path,
            shortcut=d.get("shortcut"),
        )


@dataclass(frozen=True)
class UISnapshot:
    snapshot_id: str
    app: AppInfo
    window: str = ""
    focused_id: int | None = None
    items: tuple[UIItem, ...] = ()
    truncated: bool = False

    def item(self, item_id: int) -> UIItem | None:
        for it in self.items:
            if it.id == item_id:
                return it
        return None

    @property
    def menus(self) -> tuple[UIItem, ...]:
        return tuple(i for i in self.items if i.kind == "menu")

    @property
    def elements(self) -> tuple[UIItem, ...]:
        return tuple(i for i in self.items if i.kind == "element")

    def to_dict(self) -> JSON:
        return {
            "snapshot_id": self.snapshot_id,
            "app": self.app.to_dict(),
            "window": self.window,
            "focused_id": self.focused_id,
            "items": [i.to_dict() for i in self.items],
            "truncated": self.truncated,
        }

    @classmethod
    def from_dict(cls, d: JSON) -> UISnapshot:
        return cls(
            snapshot_id=_req(d, "snapshot_id", str, "snapshot"),
            app=AppInfo.from_dict(_req(d, "app", dict, "snapshot")),
            window=str(d.get("window") or ""),
            focused_id=d.get("focused_id"),
            items=tuple(UIItem.from_dict(i) for i in d.get("items") or ()),
            truncated=bool(d.get("truncated", False)),
        )


# ---------------------------------------------------------------------------
# 4.3 Plan / Action (brain -> executor)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    """One op from the closed set. Only the fields relevant to ``op`` are set.

    open_app{app} | press{target} | menu{target} | type{text, target?} | keys{keys}
    | scroll{direction, amount} | wait{ms}
    """

    op: Op
    target: int | None = None
    text: str | None = None
    keys: str | None = None
    app: str | None = None
    direction: Direction | None = None
    amount: int | None = None
    ms: int | None = None

    def __post_init__(self) -> None:
        validate_action(self)

    def to_dict(self) -> JSON:
        d: JSON = {"op": self.op}
        for k in ("target", "text", "keys", "app", "direction", "amount", "ms"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        return d

    @classmethod
    def from_dict(cls, d: JSON) -> Action:
        op = _req(d, "op", str, "action")
        if op not in OPS:
            raise ContractError(f"action: unknown op {op!r}")
        return cls(
            op=cast(Op, op),
            target=d.get("target"),
            text=d.get("text"),
            keys=d.get("keys"),
            app=d.get("app"),
            direction=d.get("direction"),
            amount=d.get("amount"),
            ms=d.get("ms"),
        )

    # Convenience constructors -------------------------------------------------
    @classmethod
    def open_app(cls, app: str) -> Action:
        return cls(op="open_app", app=app)

    @classmethod
    def press(cls, target: int) -> Action:
        return cls(op="press", target=target)

    @classmethod
    def menu(cls, target: int) -> Action:
        return cls(op="menu", target=target)

    @classmethod
    def type_text(cls, text: str, target: int | None = None) -> Action:
        return cls(op="type", text=text, target=target)

    @classmethod
    def key(cls, keys: str) -> Action:
        return cls(op="keys", keys=keys)

    @classmethod
    def scroll(cls, direction: Direction, amount: int = 5) -> Action:
        return cls(op="scroll", direction=direction, amount=amount)

    @classmethod
    def wait(cls, ms: int) -> Action:
        return cls(op="wait", ms=ms)


def validate_action(a: Action) -> None:
    if a.op not in OPS:
        raise ContractError(f"action: unknown op {a.op!r}")
    if a.op in TARGET_OPS and a.target is None:
        raise ContractError(f"action {a.op}: target required")
    if a.op == "open_app" and not a.app:
        raise ContractError("action open_app: app required")
    if a.op == "type" and a.text is None:
        raise ContractError("action type: text required")
    if a.op == "keys" and not a.keys:
        raise ContractError("action keys: keys required")
    if a.op == "scroll" and a.direction not in ("up", "down"):
        raise ContractError("action scroll: direction must be up|down")
    if a.op == "wait" and (a.ms is None or a.ms < 0):
        raise ContractError("action wait: ms >= 0 required")


@dataclass(frozen=True)
class Plan:
    snapshot_id: str
    actions: tuple[Action, ...] = ()
    confirm: bool = False
    say: str = ""
    ask: tuple[int, ...] | None = None  # ambiguity: overlay numbers these ids for the user
    mode: PlanMode = "actions"  # "dictation": actions is a single type{text}

    def to_dict(self) -> JSON:
        d: JSON = {
            "snapshot_id": self.snapshot_id,
            "confirm": self.confirm,
            "say": self.say,
            "actions": [a.to_dict() for a in self.actions],
        }
        if self.ask is not None:
            d["ask"] = list(self.ask)
        if self.mode != "actions":
            d["mode"] = self.mode
        return d

    @classmethod
    def from_dict(cls, d: JSON) -> Plan:
        ask = d.get("ask")
        return cls(
            snapshot_id=_req(d, "snapshot_id", str, "plan"),
            actions=tuple(Action.from_dict(a) for a in d.get("actions") or ()),
            confirm=bool(d.get("confirm", False)),
            say=str(d.get("say") or ""),
            ask=tuple(int(x) for x in ask) if ask is not None else None,
            mode=cast(PlanMode, d.get("mode", "actions")),
        )


# ---------------------------------------------------------------------------
# 4.4 ActionResult (executor -> brain)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    changed: bool = False
    error: str | None = None
    snapshot: UISnapshot | None = None

    def to_dict(self) -> JSON:
        return {
            "ok": self.ok,
            "changed": self.changed,
            "error": self.error,
            "snapshot": self.snapshot.to_dict() if self.snapshot else None,
        }

    @classmethod
    def from_dict(cls, d: JSON) -> ActionResult:
        snap = d.get("snapshot")
        return cls(
            ok=bool(_req(d, "ok", bool, "result")),
            changed=bool(d.get("changed", False)),
            error=d.get("error"),
            snapshot=UISnapshot.from_dict(snap) if snap else None,
        )


# ---------------------------------------------------------------------------
# 4.6 Events (brain -> overlay)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    type: EventType
    utterance_id: str
    data: JSON = field(default_factory=dict)
    ms_since_speech_end: int = 0

    def to_dict(self) -> JSON:
        return {
            "type": self.type,
            "utterance_id": self.utterance_id,
            "data": self.data,
            "ms_since_speech_end": self.ms_since_speech_end,
        }


# ---------------------------------------------------------------------------
# Flow memory descriptor (PRD §5.5): resolve against a fresh snapshot, never store ids
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetDescriptor:
    kind: ItemKind
    role: str = ""
    label: str = ""
    path: tuple[str, ...] = ()
    bundle_id: str = ""

    @classmethod
    def of(cls, item: UIItem, bundle_id: str) -> TargetDescriptor:
        return cls(kind=item.kind, role=item.role, label=item.label, path=item.path, bundle_id=bundle_id)

    def to_dict(self) -> JSON:
        return {
            "kind": self.kind,
            "role": self.role,
            "label": self.label,
            "path": list(self.path),
            "bundle_id": self.bundle_id,
        }

    @classmethod
    def from_dict(cls, d: JSON) -> TargetDescriptor:
        return cls(
            kind=cast(ItemKind, d.get("kind", "element")),
            role=str(d.get("role", "")),
            label=str(d.get("label", "")),
            path=tuple(d.get("path") or ()),
            bundle_id=str(d.get("bundle_id", "")),
        )


# ---------------------------------------------------------------------------
# Router output (shared by router, eval harness and pipeline)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Route:
    kind: RouteKind
    plan: Plan | None = None
    control: ControlWord | None = None
    flow: str | None = None  # taught-flow name (route=flow) or name being taught
    teach: bool = False  # "call that <flow>"
    reason: str = ""
    ms: float = 0.0  # time spent deciding, in ms
    # Compound utterance: text still to handle after ``plan`` runs and the screen settles
    # ("open notes and start a note called standup" -> plan=open_app Notes, rest="start a note …").
    rest: str | None = None

    def to_dict(self) -> JSON:
        d: JSON = {
            "kind": self.kind,
            "plan": self.plan.to_dict() if self.plan else None,
            "control": self.control,
            "flow": self.flow,
            "teach": self.teach,
            "reason": self.reason,
            "ms": round(self.ms, 2),
        }
        if self.rest is not None:
            d["rest"] = self.rest
        return d


# ---------------------------------------------------------------------------
# 4.5 Python interfaces (in-process)
# ---------------------------------------------------------------------------


@runtime_checkable
class UIProvider(Protocol):
    def snapshot(self) -> UISnapshot: ...  # frontmost app

    def list_apps(self) -> list[str]: ...  # installed app names, for open_app matching


@runtime_checkable
class Executor(Protocol):
    def run(self, snapshot_id: str, action: Action) -> ActionResult: ...

    def undo(self) -> bool: ...  # best effort (cmd+z)


@runtime_checkable
class Planner(Protocol):
    """Bonsai (or a fake). Must only emit target ids present in ``snapshot``."""

    def plan(
        self,
        text: str,
        snapshot: UISnapshot,
        *,
        hint: str | None = None,
        error: str | None = None,
    ) -> Plan: ...


@runtime_checkable
class EventSink(Protocol):
    def emit(self, event: Event) -> None: ...


class NullSink:
    def emit(self, event: Event) -> None:
        return None


class ListSink:
    """Collects events in memory (tests, eval)."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]


# ---------------------------------------------------------------------------
# JSON Schemas (draft 2020-12) mirroring the dataclasses
# ---------------------------------------------------------------------------

SPEAKER_SCHEMA: JSON = {
    "type": "object",
    "required": ["verified"],
    "properties": {
        "verified": {"type": "boolean"},
        "score": {"type": "number"},
        "name": {"type": ["string", "null"]},
        "threshold": {"type": ["number", "null"]},
    },
}

MAX_UTTERANCE_CHARS = 2000  # longer "speech" is not a voice command; bounds regex/planner work

UTTERANCE_SCHEMA: JSON = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Utterance",
    "type": "object",
    "required": ["id", "text", "speaker"],
    "properties": {
        "id": {"type": "string"},
        "text": {"type": "string", "maxLength": MAX_UTTERANCE_CHARS},
        "t_speech_end": {"type": "number"},
        "stt_ms": {"type": "integer"},
        "speaker": SPEAKER_SCHEMA,
        "audio_s": {"type": "number"},
    },
}

UIITEM_SCHEMA: JSON = {
    "type": "object",
    "required": ["id", "kind"],
    "properties": {
        "id": {"type": "integer"},
        "kind": {"enum": ["element", "menu"]},
        "role": {"type": "string"},
        "label": {"type": "string"},
        "value": {"type": ["string", "null"]},
        "enabled": {"type": "boolean"},
        "path": {"type": "array", "items": {"type": "string"}},
        "shortcut": {"type": ["string", "null"]},
    },
}

UISNAPSHOT_SCHEMA: JSON = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "UISnapshot",
    "type": "object",
    "required": ["snapshot_id", "app", "items"],
    "properties": {
        "snapshot_id": {"type": "string"},
        "app": {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}, "bundle_id": {"type": "string"}},
        },
        "window": {"type": "string"},
        "focused_id": {"type": ["integer", "null"]},
        "items": {"type": "array", "items": UIITEM_SCHEMA},
        "truncated": {"type": "boolean"},
    },
}

ACTION_SCHEMA: JSON = {
    "type": "object",
    "required": ["op"],
    "properties": {
        "op": {"enum": list(OPS)},
        "target": {"type": "integer"},
        "text": {"type": "string"},
        "keys": {"type": "string"},
        "app": {"type": "string"},
        "direction": {"enum": ["up", "down"]},
        "amount": {"type": "integer"},
        "ms": {"type": "integer"},
    },
}

PLAN_SCHEMA: JSON = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Plan",
    "type": "object",
    "required": ["snapshot_id", "actions"],
    "properties": {
        "snapshot_id": {"type": "string"},
        "confirm": {"type": "boolean"},
        "say": {"type": "string"},
        "actions": {"type": "array", "maxItems": MAX_ACTIONS, "items": ACTION_SCHEMA},
        "ask": {"type": "array", "items": {"type": "integer"}},
        "mode": {"enum": ["actions", "dictation"]},
    },
}

ACTION_RESULT_SCHEMA: JSON = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "ActionResult",
    "type": "object",
    "required": ["ok"],
    "properties": {
        "ok": {"type": "boolean"},
        "changed": {"type": "boolean"},
        "error": {"type": ["string", "null"]},
        "snapshot": {"oneOf": [UISNAPSHOT_SCHEMA, {"type": "null"}]},
    },
}

EVENT_SCHEMA: JSON = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Event",
    "type": "object",
    "required": ["type", "utterance_id", "data", "ms_since_speech_end"],
    "properties": {
        "type": {"enum": list(EVENT_TYPES)},
        "utterance_id": {"type": "string"},
        "data": {"type": "object"},
        "ms_since_speech_end": {"type": "integer"},
    },
}

SCHEMAS: dict[str, JSON] = {
    "utterance": UTTERANCE_SCHEMA,
    "ui_snapshot": UISNAPSHOT_SCHEMA,
    "plan": PLAN_SCHEMA,
    "action_result": ACTION_RESULT_SCHEMA,
    "event": EVENT_SCHEMA,
}
