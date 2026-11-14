"""Flow memory (PRD 5.5): "teach once" sequences stored as descriptors, never ids."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path

from rapidfuzz import fuzz

from brain.contracts import JSON, Action, TargetDescriptor, UIItem, UISnapshot
from brain.control import parse_control

# Teach phrases only. "save this as X" is deliberately NOT a teach phrase: it is an ordinary
# Save As command in document apps (eval case pl-textedit-save).
_TEACH_RE = re.compile(
    r"^\s*(?:please\s+)?(?:(?:call|name)\s+(?:that|this|it)(?:\s+as)?|remember\s+(?:that|this|it)\s+as)"
    r"\s+(?P<name>.+?)\s*[.!?]*\s*$",
    re.IGNORECASE,
)
# Leading filler ("okay, ", "hey siriously, ", "please ") and trailing "please"; kept local so
# teach parsing stays independent of the control-word normaliser.
_LEAD_FILLER_RE = re.compile(
    r"^\W*(?:(?:hey|hi|okay|ok)\s+siri(?:ously)?|okay|ok|please|hey|hi)(?![a-z])", re.IGNORECASE
)
_TRAIL_FILLER_RE = re.compile(r"\s*,?\s+please\W*$", re.IGNORECASE)
_NOT_A_NAME = frozenset({"a day", "a night", "quits", "off"})  # "call it a day" is not a teach
_RUN_RE = re.compile(r"^(?:run|do|play)\s+(?:the\s+)?(?P<name>.+)$")
_PUNCT_RE = re.compile(r"[^\w\s]")
FUZZY_THRESHOLD = 90.0
MAX_FUZZY_WORDS = 5


def _strip_filler(text: str) -> str:
    prev = None
    while prev != text:
        prev = text
        text = _LEAD_FILLER_RE.sub("", text, count=1).lstrip(" ,.!?;:-")
    return text.strip()


def parse_teach(text: str) -> str | None:
    """ "call that standup" -> "standup"; None if the text is not a teach command."""
    m = _TEACH_RE.match(_strip_filler(text))
    if not m:
        return None
    name = _TRAIL_FILLER_RE.sub("", m.group("name")).strip().lower()
    if not name or name in _NOT_A_NAME or parse_control(name) is not None:
        return None  # "call that stop" would make a flow that shadows a control word
    return name


def _norm(text: str) -> str:
    return " ".join(_PUNCT_RE.sub(" ", text.lower()).split())


@dataclass(frozen=True)
class FlowStep:
    """An Action plus its target as a descriptor (action.target is informational only)."""

    action: Action
    target: TargetDescriptor | None = None

    def to_dict(self) -> JSON:
        return {"action": self.action.to_dict(), "target": self.target.to_dict() if self.target else None}

    @classmethod
    def from_dict(cls, d: JSON) -> FlowStep:
        t = d.get("target")
        return cls(Action.from_dict(d["action"]), TargetDescriptor.from_dict(t) if t else None)


@dataclass(frozen=True)
class Flow:
    name: str
    steps: tuple[FlowStep, ...]
    utterance: str = ""

    def to_dict(self) -> JSON:
        return {"name": self.name, "utterance": self.utterance, "steps": [s.to_dict() for s in self.steps]}

    @classmethod
    def from_dict(cls, d: JSON) -> Flow:
        return cls(
            name=str(d["name"]),
            steps=tuple(FlowStep.from_dict(s) for s in d.get("steps") or ()),
            utterance=str(d.get("utterance") or ""),
        )


class FlowStore:
    """Name -> Flow, persisted as JSON at ``path`` (None = in-memory)."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._flows: dict[str, Flow] = {}
        self._lossy = False  # the file on disk had content we could not load; back it up before writing
        self._load()

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            entries = raw["flows"]
            iter(entries)
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError):
            self._lossy = True
            return
        for entry in entries:
            try:
                flow = Flow.from_dict(entry)
            except (KeyError, TypeError, ValueError, AttributeError):
                self._lossy = True
                continue
            self._flows[flow.name.lower()] = flow

    def _backup_unreadable(self) -> None:
        """Move an unreadable/partially-readable flows file aside so a write cannot destroy it."""
        if self._path is None or not self._lossy:
            return
        bak = self._path.with_name(f"{self._path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        n = 0
        while bak.exists():
            n += 1
            bak = self._path.with_name(f"{self._path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}-{n}")
        try:
            os.replace(self._path, bak)
        except FileNotFoundError:
            pass
        except OSError:
            return  # keep _lossy so we retry; the atomic write below still protects the original
        self._lossy = False

    def names(self) -> list[str]:
        return sorted(self._flows)

    def get(self, name: str) -> Flow | None:
        return self._flows.get(name.strip().lower())

    def save(self, flow: Flow) -> None:
        flow = replace(flow, name=flow.name.strip().lower())
        self._flows[flow.name] = flow
        self._write()

    def _write(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._backup_unreadable()
        payload = json.dumps({"flows": [f.to_dict() for f in self._flows.values()]}, indent=2)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".flows-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self._path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def match(self, text: str) -> str | None:
        """Flow name for a short utterance: exact, "run <name>", or fuzzy (<= 5 words)."""
        norm = _norm(text)
        if not norm or not self._flows:
            return None
        by_norm = {_norm(n): n for n in self._flows}
        if norm in by_norm:
            return by_norm[norm]
        m = _RUN_RE.match(norm)
        candidate = m.group("name") if m else norm
        if candidate in by_norm:
            return by_norm[candidate]
        if len(norm.split()) > MAX_FUZZY_WORDS:
            return None
        best: tuple[float, str] | None = None
        for n, original in by_norm.items():
            score = max(fuzz.ratio(candidate, n), fuzz.ratio(norm, n))
            if score >= FUZZY_THRESHOLD and (best is None or score > best[0]):
                best = (score, original)
        return best[1] if best else None


def _find_menu(desc: TargetDescriptor, snapshot: UISnapshot) -> UIItem | None:
    menus = snapshot.menus
    if desc.path:
        for it in menus:
            if it.path == desc.path:
                return it
    leaf = desc.path[-1] if desc.path else desc.label
    if not leaf:
        return None
    return next((it for it in menus if it.name == leaf), None)


def _find_element(desc: TargetDescriptor, snapshot: UISnapshot) -> UIItem | None:
    elements = snapshot.elements
    for it in elements:
        if it.role == desc.role and it.label == desc.label:
            return it
    if not desc.label:
        return None
    same_label = [it for it in elements if it.label == desc.label]
    return same_label[0] if len(same_label) == 1 else None


def resolve_step(step: FlowStep, snapshot: UISnapshot) -> Action | None:
    """Re-bind a step to the ids of ``snapshot``; None if the descriptor does not resolve."""
    desc = step.target
    if desc is None:
        # No descriptor: fine for target-less ops, but a bare stale id cannot be trusted.
        return step.action if step.action.target is None else None
    if desc.bundle_id and snapshot.app.bundle_id and desc.bundle_id != snapshot.app.bundle_id:
        return None
    item = _find_menu(desc, snapshot) if desc.kind == "menu" else _find_element(desc, snapshot)
    if item is None:
        return None
    return replace(step.action, target=item.id)
