"""Run eval cases: route the utterance against its fixture, execute on FakeExecutor, score.

Per case: build a fresh ``FixtureUIProvider``/``FakeExecutor``, ask the router, then execute whatever
it decided (a plan, or a taught flow resolved step by step). For planner routes, if the burst changed
the screen and the expected sequence is not finished yet, the planner is called again on the new
snapshot (up to ``max_bursts`` bursts) — the eval stands in for the pipeline's "is the goal done?"
check, so it measures whether the planner can carry a task across screens. ``Outcome.bursts`` reports it.

A route may carry ``rest`` ("open notes and <rest>" -> fast-path ``open_app`` + rest): like the pipeline,
the eval routes the rest again on the new screen and executes that too (``MAX_REST_HOPS``).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from brain.contracts import JSON, Action, Plan, Planner, Route, UISnapshot
from brain.fixtures import FakeExecutor, FixtureUIProvider
from eval.cases import Case
from eval.scoring import Outcome, Status, Step, Verdict, can_continue, describe_action, score, worst


class RouterLike(Protocol):
    def route(self, text: str, snapshot: UISnapshot) -> Route: ...


@dataclass(frozen=True)
class Components:
    """Everything the runner needs from the brain; tests inject stubs, run_eval builds the real ones."""

    make_router: Callable[[Sequence[JSON]], RouterLike]  # seeded flows -> router
    planner: Planner | None = None
    needs_confirm: Callable[[Plan, UISnapshot], bool] = lambda plan, snap: plan.confirm
    resolve_step: Callable[[JSON, UISnapshot], Action | None] | None = None  # flow step -> action
    notes: tuple[str, ...] = ()  # degradations to print up front


@dataclass(frozen=True)
class RunConfig:
    fixtures_dir: Path | None = None
    max_bursts: int = 3
    verbose: bool = False


@dataclass(frozen=True)
class CaseResult:
    case: Case
    runs: tuple[tuple[Outcome, Verdict], ...]
    mode: str = "router"

    @property
    def status(self) -> Status:
        return worst([v.status for _, v in self.runs])

    @property
    def passes(self) -> int:
        return sum(v.status == "PASS" for _, v in self.runs)


@dataclass
class _Session:
    """Mutable per-run state: provider, executor, executed steps."""

    provider: FixtureUIProvider
    executor: FakeExecutor
    steps: list[Step] = field(default_factory=list)
    plans: list[Plan] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    debug: list[str] = field(default_factory=list)
    confirm: bool = False
    failed: bool = False

    def run_action(self, snapshot_id: str, action: Action) -> bool:
        before = self.provider.snapshot()
        res = self.executor.run(snapshot_id, action)
        self.steps.append(Step(action, before, res.ok, res.changed, res.error))
        self.failed = self.failed or not res.ok
        return res.ok

    def run_plan(self, plan: Plan, comps: Components) -> bool:
        """Execute a burst; True if it changed which screen is showing."""
        start = self.provider.snapshot()
        self.plans.append(plan)
        self.confirm = self.confirm or plan.confirm or comps.needs_confirm(plan, start)
        for action in plan.actions:
            if not self.run_action(plan.snapshot_id, action):
                break
        end = self.provider.snapshot()
        return (end.app.name, end.snapshot_id) != (start.app.name, start.snapshot_id)


def _planner_debug(planner: Planner | None) -> list[str]:
    if planner is None:
        return []
    out: list[str] = []
    suffix = getattr(planner, "last_suffix", None) or getattr(planner, "last_prompt", None)
    if isinstance(suffix, str) and suffix:
        out.append("prompt suffix:\n    " + suffix.replace("\n", "\n    "))
    stats = getattr(planner, "last_stats", None)
    if isinstance(stats, dict) and stats:
        out.append(
            "stats: " + ", ".join(f"{k}={v:g}" for k, v in stats.items() if isinstance(v, int | float))
        )
    return out


def _replay_flow(case: Case, route: Route, sess: _Session, comps: Components) -> None:
    flow = next(
        (f for f in case.flows if str(f.get("name", "")).casefold() == (route.flow or "").casefold()), None
    )
    if flow is None:
        sess.problems.append(f"router chose flow {route.flow!r}, which this case did not seed")
        return
    if comps.resolve_step is None:
        sess.problems.append("brain.flows.resolve_step unavailable: cannot replay flows")
        return
    for i, raw in enumerate(flow.get("steps") or []):
        snap = sess.provider.snapshot()
        action = comps.resolve_step(raw, snap)
        if action is None:
            sess.problems.append(f"flow step {i} did not resolve against {snap.snapshot_id}")
            return
        if not sess.run_action(snap.snapshot_id, action):
            return


def _continue_hint(case: Case, steps: Sequence[Step]) -> str:
    done = ", ".join(describe_action(s.action) for s in steps)
    return (
        f"Continuing '{case.utterance}'. Already done: {done}. Plan only the remaining steps on this screen."
    )


def _continue(
    case: Case, sess: _Session, comps: Components, cfg: RunConfig, changed: bool
) -> tuple[int, float]:
    """Re-plan after a screen change while the expected sequence is unfinished. Returns (bursts, ms)."""
    bursts, ms = 1, 0.0
    seqs = case.expect.actions
    planner = comps.planner
    while (
        planner is not None and seqs is not None and changed and not sess.failed and bursts < cfg.max_bursts
    ):
        if not can_continue(seqs, sess.steps):
            break
        snap = sess.provider.snapshot()
        t0 = time.perf_counter()
        plan = planner.plan(case.utterance, snap, hint=_continue_hint(case, sess.steps))
        ms += (time.perf_counter() - t0) * 1000.0
        sess.debug += _planner_debug(planner)
        if not plan.actions:
            break
        bursts += 1
        changed = sess.run_plan(plan, comps)
    return bursts, ms


MAX_REST_HOPS = 3


def _execute(
    case: Case, route: Route, sess: _Session, comps: Components, cfg: RunConfig
) -> tuple[int, float]:
    """Run whatever ``route`` decided. Returns (bursts, extra planner ms)."""
    if route.plan is not None:
        changed = sess.run_plan(route.plan, comps)
        if route.kind == "planner":
            return _continue(case, sess, comps, cfg, changed)
        return 1, 0.0
    if route.kind == "flow" and not route.teach:
        _replay_flow(case, route, sess, comps)
    return 0, 0.0


def _follow_rest(
    case: Case, route: Route, router: RouterLike, sess: _Session, comps: Components, cfg: RunConfig
) -> tuple[int, float, str | None]:
    """Route each ``rest`` on the screen the previous step produced. Returns (bursts, ms, skip reason)."""
    bursts, ms = 0, 0.0
    rest = getattr(route, "rest", None)
    for _ in range(MAX_REST_HOPS):
        if not isinstance(rest, str) or not rest or sess.failed:
            break
        t0 = time.perf_counter()
        nxt = router.route(rest, sess.provider.snapshot())
        ms += (time.perf_counter() - t0) * 1000.0
        sess.debug.append(f"rest {rest!r} -> {nxt.kind} {nxt.reason}".rstrip())
        skip = _skip_reason(case, nxt, comps)
        if skip is not None:
            return bursts, ms, skip
        b, extra = _execute(case, nxt, sess, comps, cfg)
        bursts, ms = bursts + b, ms + extra
        rest = getattr(nxt, "rest", None)
    return bursts, ms, None


def _skip_reason(case: Case, route: Route, comps: Components) -> str | None:
    if route.kind == "none" and comps.planner is None and case.model_route_ok:
        return "needs the planner (no model)"
    return None


def run_case(case: Case, comps: Components, cfg: RunConfig | None = None) -> Outcome:
    cfg = cfg or RunConfig()
    try:  # a broken case (unknown fixture/state, malformed seeded flow) must not abort the whole run
        provider = FixtureUIProvider(case.fixture, cfg.fixtures_dir, state=case.state)
        router = comps.make_router(case.flows)
    except Exception as exc:
        return Outcome(route=None, error=f"case setup failed: {type(exc).__name__}: {exc}")
    sess = _Session(provider, FakeExecutor(provider))
    snap = provider.snapshot()
    t0 = time.perf_counter()
    try:
        route = router.route(case.utterance, snap)
    except Exception as exc:  # a crashing router is a result, not a harness failure
        return Outcome(route=None, error=f"router raised {type(exc).__name__}: {exc}")
    ms = (time.perf_counter() - t0) * 1000.0
    if route.kind == "planner":
        sess.debug += _planner_debug(comps.planner)
    skipped = _skip_reason(case, route, comps)
    error = route.reason if route.kind == "none" and route.reason.startswith("planner error") else None
    bursts, extra_ms = 0, 0.0
    try:
        if not skipped:
            bursts, extra_ms = _execute(case, route, sess, comps, cfg)
            more, rest_ms, skipped = _follow_rest(case, route, router, sess, comps, cfg)
            bursts, extra_ms = bursts + more, extra_ms + rest_ms
    except Exception as exc:
        error = f"{type(exc).__name__} during execution: {exc}"
    return Outcome(
        route=route,
        steps=tuple(sess.steps),
        plans=tuple(sess.plans),
        confirm=sess.confirm,
        bursts=bursts,
        ms=ms,
        total_ms=ms + extra_ms,
        error=error,
        skipped=skipped,
        problems=tuple(sess.problems),
        debug=tuple(sess.debug),
    )


def run_cases(
    cases: Sequence[Case],
    run_one: Callable[[Case], Outcome],
    *,
    mode: str = "router",
    repeat: int = 1,
    on_result: Callable[[CaseResult], None] | None = None,
) -> list[CaseResult]:
    """Run every case ``repeat`` times with ``run_one`` and score it for ``mode``."""
    results: list[CaseResult] = []
    for case in cases:
        runs: list[tuple[Outcome, Verdict]] = []
        for _ in range(max(1, repeat)):
            out = run_one(case)
            runs.append((out, score(case, out, mode)))
        res = CaseResult(case, tuple(runs), mode)
        results.append(res)
        if on_result is not None:
            on_result(res)
    return results
