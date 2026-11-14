"""Eval case model and ``commands.yaml`` loader.

A case::

    - id: pl-notes-standup
      category: planner              # control | fastpath | planner | safety | flow (reporting only)
      fixture: notes                 # eval/fixtures/<fixture>.json
      state: s-notes-0               # optional start state (default: the fixture's initial)
      utterance: start a new note called standup
      expect:
        route: planner               # or a list of acceptable routes
        control: undo                # control cases
        teach: true                  # "call that X" cases
        flow: standup                # flow name (teach or replay)
        confirm: true                # plan must ask for confirmation (false: must not)
        actions:                     # acceptable sequences; any one must match exactly
          - - {op: open_app, app: Notes, optional: true}
            - any: [{op: menu, target: 110}, {op: press, target: 1}]
            - {op: type, text: standup}
            - {op: keys, keys: return, optional: true}
      setup:
        flows: [{name: standup, steps: [{action: {...}, target: {...}}]}]   # seeded taught flows
      split: holdout                 # dev (default) | holdout: never tune prompts on holdout
      tags: [demo]
      # e2e mode (full pipeline) extras:
      modes: [e2e]                   # default [router, e2e]
      before: [new note]             # turns handled first, e.g. something to undo / teach / confirm
      speaker: {verified: false}     # the scored utterance comes from an unverified voice
      needs_model: true              # SKIP without a planner
      # expect.state / expect.app: final frontmost snapshot / app (e2e); expect.e2e_actions overrides
      # expect.actions in e2e (e.g. control "yes" executes the pending plan there).

Action patterns: ``op`` plus optional ``target`` / ``text`` / ``keys`` / ``app`` / ``direction``, each a
value or a list of acceptable values. ``text``/``app`` compare case-insensitively, ``keys`` after
normalization. A ``menu`` pattern also accepts pressing that item's shortcut (and vice versa).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import yaml

from brain.contracts import JSON, OPS
from brain.fixtures import normalize_keys

DEFAULT_CASES = Path(__file__).resolve().parent / "commands.yaml"
ROUTES = frozenset({"control", "flow", "fastpath", "planner", "dictation", "rejected", "none"})
MODES = ("router", "e2e")
SPLITS = frozenset({"dev", "holdout"})
CONTROL_WORDS = frozenset({"stop", "cancel", "undo", "yes", "no"})
_PATTERN_KEYS = frozenset({"op", "target", "text", "keys", "app", "direction", "optional", "any"})


class CaseError(ValueError):
    """commands.yaml is malformed."""


def normalize_text(s: str) -> str:
    """Comparison form for typed text: casefold, straight quotes, collapsed spaces, no trailing . ! ?"""
    t = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return " ".join(t.casefold().split()).rstrip(".!?").strip()


@dataclass(frozen=True)
class ActionPattern:
    op: str
    targets: tuple[int, ...] = ()
    texts: tuple[str, ...] = ()  # normalized
    keys: tuple[str, ...] = ()  # normalized
    apps: tuple[str, ...] = ()  # casefolded
    direction: str | None = None

    def describe(self) -> str:
        parts = [str(t) for t in self.targets] + [repr(t) for t in self.texts]
        parts += list(self.keys) + list(self.apps) + ([self.direction] if self.direction else [])
        return f"{self.op}({'|'.join(parts)})"


@dataclass(frozen=True)
class StepPattern:
    """One expected step: any of ``alts`` matches; ``optional`` steps may be skipped."""

    alts: tuple[ActionPattern, ...]
    optional: bool = False

    def describe(self) -> str:
        body = " / ".join(a.describe() for a in self.alts)
        return f"[{body}]?" if self.optional else body


SequencePattern = tuple[StepPattern, ...]


@dataclass(frozen=True)
class Expect:
    routes: tuple[str, ...] = ()
    actions: tuple[SequencePattern, ...] | None = None  # None = not checked
    confirm: bool | None = None
    control: str | None = None
    flow: str | None = None
    teach: bool | None = None
    # e2e only (the full pipeline on FakeExecutor):
    e2e_actions: tuple[SequencePattern, ...] | None = None  # overrides `actions` in e2e mode
    state: str | None = None  # final snapshot_id of the frontmost app
    app: str | None = None  # final frontmost app


@dataclass(frozen=True)
class Case:
    id: str
    fixture: str
    utterance: str
    expect: Expect
    category: str = ""
    state: str | None = None
    flows: tuple[JSON, ...] = ()
    note: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)
    split: str = "dev"  # dev: tune prompts on these; holdout: report only
    modes: tuple[str, ...] = MODES  # which eval modes run this case
    before: tuple[str, ...] = ()  # e2e: utterances handled first (not scored unless they error)
    verified: bool = True  # e2e: speaker verification of the scored utterance
    needs_model: bool = False  # SKIP when there is no planner (e.g. a `before` turn needs it)

    def actions_for(self, mode: str) -> tuple[SequencePattern, ...] | None:
        if mode == "e2e" and self.expect.e2e_actions is not None:
            return self.expect.e2e_actions
        return self.expect.actions

    @property
    def model_route_ok(self) -> bool:
        """True if a model route (planner/dictation) is an acceptable outcome for this case."""
        return bool({"planner", "dictation"} & set(self.expect.routes))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _as_list(v: object) -> list[object]:
    if v is None:
        return []
    return list(v) if isinstance(v, list | tuple) else [v]


def _strs(v: object, ctx: str) -> list[str]:
    out = _as_list(v)
    if not all(isinstance(x, str) for x in out):
        raise CaseError(f"{ctx}: expected string(s), got {v!r}")
    return [str(x) for x in out]


def _ints(v: object, ctx: str) -> tuple[int, ...]:
    out = _as_list(v)
    if not all(isinstance(x, int) and not isinstance(x, bool) for x in out):
        raise CaseError(f"{ctx}: target must be int(s), got {v!r}")
    return tuple(cast(int, x) for x in out)


def parse_action_pattern(d: object, ctx: str) -> ActionPattern:
    if not isinstance(d, dict):
        raise CaseError(f"{ctx}: action pattern must be a mapping, got {d!r}")
    if "any" in d:
        raise CaseError(f"{ctx}: 'any' is only allowed at step level")
    op = d.get("op")
    if op not in OPS:
        raise CaseError(f"{ctx}: unknown op {op!r}")
    unknown = set(d) - _PATTERN_KEYS
    if unknown:
        raise CaseError(f"{ctx}: unknown pattern keys {sorted(unknown)}")
    if "target" in d and not _as_list(d["target"]):
        raise CaseError(f"{ctx}: empty target list")
    direction = d.get("direction")
    if direction not in (None, "up", "down"):
        raise CaseError(f"{ctx}: direction must be up|down")
    return ActionPattern(
        op=str(op),
        targets=_ints(d.get("target"), ctx),
        texts=tuple(normalize_text(t) for t in _strs(d.get("text"), ctx)),
        keys=tuple(normalize_keys(k) for k in _strs(d.get("keys"), ctx)),
        apps=tuple(a.casefold() for a in _strs(d.get("app"), ctx)),
        direction=cast(str | None, direction),
    )


def parse_step(d: object, ctx: str) -> StepPattern:
    if not isinstance(d, dict):
        raise CaseError(f"{ctx}: step must be a mapping, got {d!r}")
    optional = bool(d.get("optional", False))
    if "any" in d:
        alts = _as_list(d["any"])
        if not alts or set(d) - {"any", "optional"}:
            raise CaseError(f"{ctx}: 'any' needs a non-empty list and only 'optional' beside it")
        if any(isinstance(a, dict) and "optional" in a for a in alts):
            raise CaseError(f"{ctx}: put 'optional' on the step, not inside 'any'")
        return StepPattern(tuple(parse_action_pattern(a, ctx) for a in alts), optional)
    return StepPattern((parse_action_pattern(d, ctx),), optional)


def _parse_actions(v: object, ctx: str) -> tuple[SequencePattern, ...] | None:
    if v is None:
        return None
    if not isinstance(v, list) or not all(isinstance(s, list) for s in v):
        raise CaseError(f"{ctx}: expect.actions must be a list of sequences (lists)")
    return tuple(tuple(parse_step(st, f"{ctx}[{i}]") for st in seq) for i, seq in enumerate(v))


def parse_expect(d: object, ctx: str) -> Expect:
    if not isinstance(d, dict):
        raise CaseError(f"{ctx}: expect must be a mapping")
    routes = tuple(_strs(d.get("route"), ctx))
    bad = set(routes) - ROUTES
    if bad:
        raise CaseError(f"{ctx}: unknown route(s) {sorted(bad)}")
    control = d.get("control")
    if control is not None and control not in CONTROL_WORDS:
        raise CaseError(f"{ctx}: unknown control word {control!r}")
    confirm, teach, flow = d.get("confirm"), d.get("teach"), d.get("flow")
    return Expect(
        routes=routes,
        actions=_parse_actions(d.get("actions"), f"{ctx}.actions"),
        confirm=None if confirm is None else bool(confirm),
        control=cast(str | None, control),
        flow=None if flow is None else str(flow),
        teach=None if teach is None else bool(teach),
        e2e_actions=_parse_actions(d.get("e2e_actions"), f"{ctx}.e2e_actions"),
        state=None if d.get("state") is None else str(d["state"]),
        app=None if d.get("app") is None else str(d["app"]),
    )


def _parse_meta(d: JSON, cid: str) -> JSON:
    """split / modes / before / speaker / needs_model."""
    split = str(d.get("split") or "dev")
    if split not in SPLITS:
        raise CaseError(f"{cid}: split must be one of {sorted(SPLITS)}")
    modes = tuple(_strs(d.get("modes"), cid)) or MODES
    if set(modes) - set(MODES):
        raise CaseError(f"{cid}: modes must be a subset of {list(MODES)}")
    speaker = d.get("speaker") or {}
    if not isinstance(speaker, dict):
        raise CaseError(f"{cid}: speaker must be a mapping like {{verified: false}}")
    return {
        "split": split,
        "modes": modes,
        "before": tuple(_strs(d.get("before"), cid)),
        "verified": bool(speaker.get("verified", True)),
        "needs_model": bool(d.get("needs_model", False)),
    }


def parse_case(d: object, index: int) -> Case:
    if not isinstance(d, dict):
        raise CaseError(f"case #{index}: must be a mapping")
    cid = d.get("id")
    if not isinstance(cid, str) or not cid:
        raise CaseError(f"case #{index}: missing id")
    for key in ("fixture", "utterance"):
        if not isinstance(d.get(key), str) or not d[key]:
            raise CaseError(f"{cid}: missing {key}")
    if "expect" not in d:
        raise CaseError(f"{cid}: missing expect")
    setup = d.get("setup") or {}
    flows = (setup.get("flows") or []) if isinstance(setup, dict) else None
    if not isinstance(flows, list) or not all(isinstance(f, dict) for f in flows):
        raise CaseError(f"{cid}: setup.flows must be a list of flow mappings")
    state = d.get("state")
    return Case(
        id=cid,
        fixture=str(d["fixture"]),
        utterance=str(d["utterance"]),
        expect=parse_expect(d["expect"], cid),
        category=str(d.get("category") or ""),
        state=None if state is None else str(state),
        flows=tuple(cast(JSON, f) for f in flows),
        note=str(d.get("note") or ""),
        tags=tuple(_strs(d.get("tags"), cid)),
        **_parse_meta(d, cid),
    )


def parse_cases(data: object) -> list[Case]:
    raw = data.get("cases") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        raise CaseError("commands file must be a list of cases (or {cases: [...]})")
    cases = [parse_case(d, i) for i, d in enumerate(raw)]
    seen: set[str] = set()
    for c in cases:
        if c.id in seen:
            raise CaseError(f"duplicate case id {c.id!r}")
        seen.add(c.id)
    return cases


def load_cases(path: Path | None = None) -> list[Case]:
    p = path or DEFAULT_CASES
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CaseError(f"{p}: {exc}") from exc
    return parse_cases(data)


def select_cases(
    cases: Sequence[Case],
    only: Sequence[str] = (),
    routes: Sequence[str] = (),
    split: str = "all",
    mode: str | None = None,
) -> list[Case]:
    """Filter by id (exact or fnmatch glob), expected route / category / tag, split and mode."""
    from fnmatch import fnmatchcase

    out = list(cases)
    if only:
        out = [c for c in out if any(c.id == o or fnmatchcase(c.id, o) for o in only)]
    if routes:
        want = set(routes)
        out = [c for c in out if want & set(c.expect.routes) or c.category in want or want & set(c.tags)]
    if split != "all":
        out = [c for c in out if c.split == split]
    if mode is not None:
        out = [c for c in out if mode in c.modes]
    return out
