"""Scoring: does what the brain did match what a case expects?

Matching runs over the *executed* steps (actions plus the snapshot each ran against), so shortcut
equivalence can be checked: a ``menu`` pattern for File › New Note (cmd+n) accepts ``keys cmd+n``.
``wait`` actions are ignored. Sequences are matched exactly, with ``optional`` steps skippable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from brain.contracts import Action, Plan, Route, UISnapshot
from brain.fixtures import normalize_keys
from eval.cases import ActionPattern, Case, SequencePattern, StepPattern, normalize_text

Status = Literal["PASS", "FAIL", "SKIP", "ERROR"]
STATUS_ORDER: dict[str, int] = {"PASS": 0, "SKIP": 1, "FAIL": 2, "ERROR": 3}


@dataclass(frozen=True)
class Step:
    """One executed action and the snapshot it ran against."""

    action: Action
    before: UISnapshot
    ok: bool = True
    changed: bool = False
    error: str | None = None

    def describe(self) -> str:
        return describe_action(self.action, self.before)


@dataclass(frozen=True)
class Outcome:
    """What happened for one run of one case."""

    route: Route | None
    steps: tuple[Step, ...] = ()
    plans: tuple[Plan, ...] = ()
    confirm: bool = False
    bursts: int = 0
    ms: float = 0.0  # first routing decision, wall clock
    total_ms: float = 0.0  # including continuation planner calls
    error: str | None = None
    skipped: str | None = None
    problems: tuple[str, ...] = ()  # runner-detected failures (e.g. flow step did not resolve)
    debug: tuple[str, ...] = ()  # verbose-only details (prompt, stats)
    first_action_ms: float | None = None  # e2e: handle() start -> first action event
    events: tuple[str, ...] = ()  # e2e: event types of the scored turn


@dataclass(frozen=True)
class Verdict:
    status: Status
    reasons: tuple[str, ...] = ()


def describe_action(a: Action, snap: UISnapshot | None = None) -> str:
    if a.op in ("press", "menu") and a.target is not None:
        item = snap.item(a.target) if snap else None
        if item is None:
            return f"{a.op}({a.target})"
        # ASCII only ("›" -> ">", "…" -> "..."): Windows consoles mangle them when output is piped.
        name = " > ".join(item.path) if item.kind == "menu" and item.path else item.label
        name = name.replace("…", "...")
        return f"{a.op}({a.target} {name!r})"
    if a.op == "type":
        suffix = f" -> {a.target}" if a.target is not None else ""
        return f"type({a.text!r}{suffix})"
    arg = {"keys": a.keys, "open_app": a.app, "wait": a.ms}.get(a.op)
    if a.op == "scroll":
        arg = f"{a.direction},{a.amount}"
    return f"{a.op}({arg})"


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _shortcut(snap: UISnapshot, target: int | None) -> str | None:
    item = snap.item(target) if target is not None else None
    if item is None or item.kind != "menu" or not item.shortcut or not item.enabled:
        return None
    return normalize_keys(item.shortcut)


def _fields_match(p: ActionPattern, a: Action, snap: UISnapshot) -> bool:
    if a.op == "type":  # text lands in the explicit target, else the focused item, else nowhere
        lands = a.target if a.target is not None else snap.focused_id
        if lands is None or (p.targets and lands not in p.targets):
            return False
    elif p.targets and a.target not in p.targets:
        return False
    if p.texts and normalize_text(a.text or "") not in p.texts:
        return False
    if p.keys and normalize_keys(a.keys or "") not in p.keys:
        return False
    if p.apps and (a.app or "").removesuffix(".app").casefold() not in p.apps:
        return False
    return p.direction is None or a.direction == p.direction


def action_matches(p: ActionPattern, step: Step) -> bool:
    a, snap = step.action, step.before
    if p.op == a.op:
        return _fields_match(p, a, snap)
    if p.op == "menu" and a.op == "press":  # pressing a menu item is the same as choosing it
        return a.target in p.targets
    if p.op == "menu" and a.op == "keys":  # the menu item's shortcut
        combo = normalize_keys(a.keys or "")
        return any(_shortcut(snap, t) == combo for t in p.targets)
    if p.op == "keys" and a.op in ("menu", "press"):  # a menu item whose shortcut is expected
        sc = _shortcut(snap, a.target)
        return sc is not None and sc in p.keys
    return False


def _step_matches(sp: StepPattern, step: Step) -> bool:
    return any(action_matches(p, step) for p in sp.alts)


def _closure(seq: SequencePattern, positions: set[int]) -> set[int]:
    out = set(positions)
    frontier = list(positions)
    while frontier:
        i = frontier.pop()
        if i < len(seq) and seq[i].optional and i + 1 not in out:
            out.add(i + 1)
            frontier.append(i + 1)
    return out


def reachable(seq: SequencePattern, steps: Sequence[Step]) -> set[int]:
    """Pattern positions reachable after consuming ``steps`` (NFA over optional steps)."""
    positions = _closure(seq, {0})
    for step in steps:
        if step.action.op == "wait":
            continue
        nxt = {i + 1 for i in positions if i < len(seq) and _step_matches(seq[i], step)}
        positions = _closure(seq, nxt)
        if not positions:
            break
    return positions


def sequence_matches(seq: SequencePattern, steps: Sequence[Step]) -> bool:
    return len(seq) in reachable(seq, steps)


def any_sequence_matches(seqs: Sequence[SequencePattern], steps: Sequence[Step]) -> bool:
    return any(sequence_matches(s, steps) for s in seqs)


def can_continue(seqs: Sequence[SequencePattern], steps: Sequence[Step]) -> bool:
    """True if ``steps`` is a proper prefix of some acceptable sequence (more work still expected)."""
    if any_sequence_matches(seqs, steps):
        return False
    return any(reachable(s, steps) for s in seqs)


def concrete_action(p: ActionPattern, snap: UISnapshot) -> Action:
    """A concrete action satisfying ``p`` (first value per field; ``type`` picks a listed field on screen)."""
    if p.op in ("menu", "press"):
        return Action.menu(p.targets[0]) if p.op == "menu" else Action.press(p.targets[0])
    if p.op == "type":
        target = next((t for t in p.targets if snap.item(t) is not None), None)
        return Action.type_text(p.texts[0] if p.texts else "", target=target)
    if p.op == "keys":
        return Action.key(p.keys[0])
    if p.op == "open_app":
        return Action.open_app(p.apps[0])
    if p.op == "scroll":
        return Action.scroll("up" if p.direction == "up" else "down")
    return Action.wait(0)


def describe_sequences(seqs: Sequence[SequencePattern]) -> str:
    return " || ".join("[" + ", ".join(sp.describe() for sp in seq) + "]" for seq in seqs)


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------


def _route_reasons(case: Case, route: Route) -> list[str]:
    exp, reasons = case.expect, []
    if exp.routes and route.kind not in exp.routes:
        why = f" ({route.reason})" if route.reason else ""
        reasons.append(f"route: expected {'|'.join(exp.routes)}, got {route.kind}{why}")
    if exp.control is not None and route.control != exp.control:
        reasons.append(f"control: expected {exp.control}, got {route.control}")
    if exp.teach is not None and route.teach != exp.teach:
        reasons.append(f"teach: expected {exp.teach}, got {route.teach}")
    if exp.flow is not None and (route.flow or "").casefold() != exp.flow.casefold():
        reasons.append(f"flow: expected {exp.flow!r}, got {route.flow!r}")
    return reasons


def _action_reasons(case: Case, out: Outcome, mode: str) -> list[str]:
    reasons = [f"action failed: {s.describe()}: {s.error}" for s in out.steps if not s.ok]
    seqs = case.actions_for(mode)
    if seqs is not None and not any_sequence_matches(seqs, out.steps):
        got = ", ".join(s.describe() for s in out.steps if s.action.op != "wait") or "nothing"
        reasons.append(f"actions: got [{got}]; expected {describe_sequences(seqs)}")
    if case.expect.confirm is not None and out.confirm != case.expect.confirm:
        reasons.append(f"confirm: expected {case.expect.confirm}, got {out.confirm}")
    return reasons


def score(case: Case, out: Outcome, mode: str = "router") -> Verdict:
    if out.skipped:
        return Verdict("SKIP", (out.skipped,))
    if out.error:
        return Verdict("ERROR", (out.error,))
    if out.route is None:
        return Verdict("ERROR", ("no route",))
    reasons = _route_reasons(case, out.route) + list(out.problems) + _action_reasons(case, out, mode)
    return Verdict("FAIL", tuple(reasons)) if reasons else Verdict("PASS")


def worst(statuses: Sequence[Status]) -> Status:
    """Aggregate status over repeated runs: the worst one wins (PASS < SKIP < FAIL < ERROR)."""
    return max(statuses, key=lambda s: STATUS_ORDER[s]) if statuses else "SKIP"
