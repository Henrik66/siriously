"""End-to-end eval mode: every case through the real pipeline (``brain.pipeline.build_brain``).

Router-only mode asks "did the router decide well?". This mode asks "did the brain get the job done?":
the execution loop, re-plans, confirm-before-act, control words, teach + replay and the voice lock all
run for real against ``FixtureUIProvider`` + ``FakeExecutor``. Each case is scored on

* the event sequence of the scored turn: ``heard → route → plan → action… → done`` (``confirm`` first for
  dangerous plans, after which the eval answers "yes" itself; only ``rejected`` for an unverified voice);
* the *effective* actions (failed / no-effect attempts that the pipeline recovered from are dropped)
  against the case's acceptable sequences (``expect.e2e_actions`` overrides ``expect.actions``);
* the final screen: ``expect.app`` / ``expect.state``, or else the end states the case's own reference
  sequences reach on the fixture.

``Outcome.first_action_ms`` is "end of speech → first action": from ``handle()`` start to the first
``action`` event of the scored turn (the speech daemon's STT time is not included).
"""

from __future__ import annotations

import itertools
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

from brain.config import load_settings
from brain.contracts import (
    JSON,
    Action,
    ActionResult,
    Event,
    EventSink,
    Executor,
    Planner,
    Route,
    Speaker,
    Utterance,
)
from brain.fixtures import FakeExecutor, FixtureUIProvider
from brain.pipeline import expects_change
from eval.cases import Case, SequencePattern
from eval.runner import RunConfig
from eval.scoring import Outcome, Step, concrete_action, describe_action

MAX_REFERENCE_PATHS = 512


class BrainLike(Protocol):
    def handle(self, utt: Utterance) -> Route: ...


# (provider, executor, sink, flows_path, seeded flows) -> brain
BrainFactory = Callable[[FixtureUIProvider, Executor, EventSink, Path, Sequence[JSON]], BrainLike]


def real_brain_factory(planner: Planner | None) -> BrainFactory:
    """``build_brain`` with ``planner`` (None = fast path + flows only) and a throwaway flows file."""
    from brain.flows import Flow
    from brain.pipeline import build_brain

    def make(
        provider: FixtureUIProvider,
        executor: Executor,
        sink: EventSink,
        flows_path: Path,
        seed: Sequence[JSON],
    ) -> BrainLike:
        settings = replace(load_settings(), flows_path=flows_path)  # never touch ~/.siriously
        brain = build_brain(
            provider, executor, sink, settings, planner=planner, use_model=planner is not None
        )
        for f in seed:
            brain.flows.save(Flow.from_dict(f))
        return brain

    return make


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


@dataclass
class Recorder:
    """Executor wrapper that records each action with the snapshot it ran against."""

    inner: FakeExecutor
    steps: list[Step] = field(default_factory=list)

    def run(self, snapshot_id: str, action: Action) -> ActionResult:
        before = self.inner.provider.snapshot()
        res = self.inner.run(snapshot_id, action)
        self.steps.append(Step(action, before, res.ok, res.changed, res.error))
        return res

    def undo(self) -> bool:
        return self.inner.undo()


class TimedSink:
    def __init__(self) -> None:
        self.events: list[tuple[float, Event]] = []

    def emit(self, event: Event) -> None:
        self.events.append((time.perf_counter(), event))


@dataclass(frozen=True)
class Turn:
    text: str
    route: Route
    events: tuple[Event, ...]
    times: tuple[float, ...]
    steps: tuple[Step, ...]
    t0: float
    t1: float

    @property
    def types(self) -> list[str]:
        return [e.type for e in self.events]

    def ms_to(self, type_: str) -> float | None:
        for t, e in zip(self.times, self.events, strict=True):
            if e.type == type_:
                return (t - self.t0) * 1000.0
        return None

    def errors(self) -> list[str]:
        return [str(e.data.get("message", "")) for e in self.events if e.type == "error"]

    def describe(self) -> str:
        parts = []
        for e in self.events:
            if e.type == "route":
                parts.append(f"route({e.data.get('kind')})")
            elif e.type == "action":
                ok = "ok" if e.data.get("ok") else "FAILED"
                act = e.data.get("action")
                op = act.get("op", "?") if isinstance(act, dict) else "?"  # tolerate malformed events
                parts.append(f"action({op} {ok})")
            elif e.type == "plan":
                parts.append(f"plan({e.data.get('source')})")
            else:
                parts.append(e.type)
        return f"{self.text!r}: " + " ".join(parts)


def take_turn(brain: BrainLike, sink: TimedSink, rec: Recorder, uid: str, text: str, verified: bool) -> Turn:
    n_ev, n_steps = len(sink.events), len(rec.steps)
    speaker = Speaker(verified=verified, score=0.8 if verified else 0.05)
    t0 = time.perf_counter()
    route = brain.handle(Utterance(id=uid, text=text, t_speech_end=time.time(), speaker=speaker))
    t1 = time.perf_counter()
    new = sink.events[n_ev:]
    return Turn(
        text=text,
        route=route,
        events=tuple(e for _, e in new),
        times=tuple(t for t, _ in new),
        steps=tuple(rec.steps[n_steps:]),
        t0=t0,
        t1=t1,
    )


# ---------------------------------------------------------------------------
# Expected end states
# ---------------------------------------------------------------------------


def _paths(seq: SequencePattern) -> list[list[int | None]]:
    """Every (alternative, skip-if-optional) choice per step, as indices into ``alts`` (None = skipped)."""
    choices = [list(range(len(sp.alts))) + ([None] if sp.optional else []) for sp in seq]
    return [list(c) for c in itertools.islice(itertools.product(*choices), MAX_REFERENCE_PATHS)]


def reference_end_states(
    case: Case, seqs: Sequence[SequencePattern], fixtures_dir: Path | None = None
) -> set[tuple[str, str]]:
    """(app, snapshot_id) reached by executing every acceptable path through ``seqs`` on the fixture."""
    out: set[tuple[str, str]] = set()
    for seq in seqs:
        for path in _paths(seq):
            provider = FixtureUIProvider(case.fixture, fixtures_dir, state=case.state)
            executor = FakeExecutor(provider)
            ok = True
            for sp, choice in zip(seq, path, strict=True):
                if choice is None:
                    continue
                snap = provider.snapshot()
                if not executor.run(snap.snapshot_id, concrete_action(sp.alts[choice], snap)).ok:
                    ok = False
                    break
            if ok:
                front = provider.snapshot()
                out.add((front.app.name, front.snapshot_id))
    return out


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def effective(steps: Sequence[Step]) -> tuple[Step, ...]:
    """Steps that did something: drop failures and no-effect attempts the pipeline re-planned around."""
    return tuple(s for s in steps if s.ok and not (expects_change(s.action, s.before) and not s.changed))


def _unverified_problems(main: Turn) -> list[str]:
    problems = []
    if main.types != ["rejected"]:
        problems.append(f"unverified speaker: expected only 'rejected', got {main.types}")
    if main.steps:
        problems.append(f"unverified speaker executed {len(main.steps)} action(s)")
    return problems


def _confirm_problems(main: Turn, yes: Turn | None) -> list[str]:
    types = main.types
    if "confirm" not in types:
        return ["expected a confirm event before acting"]
    if "action" in types[: types.index("confirm")]:
        return ["acted before asking for confirmation"]
    if yes is None or not yes.types or yes.types[-1] != "done":
        return [f"after 'yes' the plan did not finish: {yes.types if yes else None}"]
    return [f"error after 'yes': {m}" for m in yes.errors()]


def event_problems(case: Case, main: Turn, yes: Turn | None) -> list[str]:
    """Check the scored turn's event grammar."""
    if not case.verified:
        return _unverified_problems(main)
    types = main.types
    problems = [] if types[:2] == ["heard", "route"] else [f"turn must start heard, route; got {types[:2]}"]
    problems += [f"error event: {m}" for m in main.errors()]
    if case.expect.confirm:
        problems += _confirm_problems(main, yes)
    elif "confirm" in types:
        problems.append("unexpected confirm event")
    elif types and types[-1] != "done":
        problems.append(f"turn did not finish with done (last event: {types[-1]})")
    planned = main.route.kind in ("fastpath", "planner", "dictation")
    if planned and "action" in types and "plan" not in types[: types.index("action")]:
        problems.append("action before any plan event")
    if case.expect.teach:
        saved = next((e.data.get("saved") for e in main.events if e.type == "done"), None)
        if str(saved or "").casefold() != (case.expect.flow or str(saved or "")).casefold():
            problems.append(f"flow not saved (done.saved={saved!r})")
    return problems


def state_problems(
    case: Case, provider: FixtureUIProvider, start: tuple[str, str], cfg: RunConfig
) -> list[str]:
    front = provider.snapshot()
    got = (front.app.name, front.snapshot_id)
    exp = case.expect
    if not case.verified:
        return [] if got == start else [f"unverified speaker changed the screen: {start} -> {got}"]
    problems = []
    if exp.app is not None and got[0].casefold() != exp.app.casefold():
        problems.append(f"final app: expected {exp.app}, got {got[0]}")
    if exp.state is not None and got[1] != exp.state:
        problems.append(f"final state: expected {exp.state}, got {got[1]}")
    seqs = case.actions_for("e2e")
    if exp.app is None and exp.state is None and not case.before and seqs:
        ends = reference_end_states(case, seqs, cfg.fixtures_dir)
        if ends and got not in ends:
            problems.append(f"final screen {got} not among expected {sorted(ends)}")
    return problems


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def _skip(case: Case, main: Turn, has_planner: bool) -> str | None:
    if has_planner:
        return None
    if case.needs_model:
        return "needs the planner (no model)"
    # the router fell through to the planner, or a fast-path open_app left a `rest` only the planner can do
    model_needed = main.route.kind == "none" or any("no planner" in m for m in main.errors())
    if model_needed and case.model_route_ok:
        return "needs the planner (no model)"
    return None


def _setup_problems(turns: Sequence[Turn]) -> list[str]:
    return [f"setup turn {t.text!r} failed: {m}" for t in turns for m in t.errors()]


def _outcome(case: Case, main: Turn, yes: Turn | None, problems: list[str], has_planner: bool) -> Outcome:
    route = main.route
    error = route.reason if route.kind == "none" and route.reason.startswith("planner error") else None
    steps = main.steps + (yes.steps if yes else ())
    turns = [main] + ([yes] if yes else [])
    return Outcome(
        route=route,
        steps=effective(steps),
        confirm="confirm" in main.types,
        bursts=sum(t.types.count("plan") for t in turns),
        ms=(main.t1 - main.t0) * 1000.0,
        total_ms=sum((t.t1 - t.t0) * 1000.0 for t in turns),
        first_action_ms=main.ms_to("action"),
        error=error,
        skipped=_skip(case, main, has_planner),
        problems=tuple(problems),
        debug=tuple(t.describe() for t in turns)
        + tuple(
            f"attempted: {describe_action(s.action, s.before)} ok={s.ok} changed={s.changed}" for s in steps
        ),
        events=tuple(main.types),
    )


def run_case_e2e(
    case: Case, factory: BrainFactory, has_planner: bool, cfg: RunConfig | None = None
) -> Outcome:
    cfg = cfg or RunConfig()
    if case.needs_model and not has_planner:
        return Outcome(route=None, skipped="needs the planner (no model)")
    with tempfile.TemporaryDirectory(prefix="siriously-eval-") as td:
        try:
            provider = FixtureUIProvider(case.fixture, cfg.fixtures_dir, state=case.state)
            rec, sink = Recorder(FakeExecutor(provider)), TimedSink()
            brain = factory(provider, rec, sink, Path(td) / "flows.json", case.flows)
        except Exception as exc:
            return Outcome(route=None, error=f"case setup failed: {type(exc).__name__}: {exc}")
        before = [take_turn(brain, sink, rec, f"{case.id}-b{i}", t, True) for i, t in enumerate(case.before)]
        start = (provider.snapshot().app.name, provider.snapshot().snapshot_id)
        main = take_turn(brain, sink, rec, case.id, case.utterance, case.verified)
        yes = None
        if case.expect.confirm and "confirm" in main.types:
            yes = take_turn(brain, sink, rec, f"{case.id}-yes", "yes", True)
        problems = _setup_problems(before) + event_problems(case, main, yes)
        problems += state_problems(case, provider, start, cfg)
    return _outcome(case, main, yes, problems, has_planner)
