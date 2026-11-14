"""Build the real brain components for the eval, lazily and with graceful degradation.

Each brain module is imported only here, inside functions, so the harness still runs (with a clear
note) while a sibling workstream's module is missing or broken. Only the router (which brings the fast
path with it) is mandatory; flows, safety and the planner degrade.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Protocol, cast

from brain.config import load_settings
from brain.contracts import JSON, Action, Plan, Planner, UISnapshot
from eval.runner import Components, RouterLike

ConfirmFn = Callable[[Plan, UISnapshot], bool]
ResolveFn = Callable[[JSON, UISnapshot], Action | None]


class FlowMatcherLike(Protocol):
    """What the router needs from a flow store (mirrors brain.router.FlowMatcher)."""

    def names(self) -> list[str]: ...

    def match(self, text: str) -> str | None: ...


class ComponentError(RuntimeError):
    """A component the eval cannot run without is unavailable."""


@dataclass(frozen=True)
class PlannerOptions:
    use_model: bool = True
    bonsai_url: str | None = None
    sampling: str | None = None
    warmup: bool = True


class _NullFlowStore:
    """Stand-in for brain.flows.FlowStore when that module is unavailable."""

    def names(self) -> list[str]:
        return []

    def match(self, text: str) -> str | None:
        return None


def _unavailable(module: str, exc: BaseException) -> str:
    return f"{module} unavailable ({type(exc).__name__}: {exc})"


def build_planner(apps: Sequence[str], opts: PlannerOptions) -> tuple[Planner | None, str]:
    """(planner, description). The planner is None when disabled or unreachable; the text says why."""
    if not opts.use_model:
        return None, "disabled (--no-model)"
    try:
        from brain.planner.bonsai import BonsaiPlanner
        from brain.planner.client import SAMPLING, SamplingProfile
    except Exception as exc:
        return None, _unavailable("brain.planner.bonsai", exc)
    if opts.sampling and opts.sampling not in SAMPLING:
        return None, f"unknown sampling profile {opts.sampling!r} (have: {', '.join(SAMPLING)})"
    settings = load_settings()
    if opts.bonsai_url:
        settings = replace(settings, bonsai_url=opts.bonsai_url)
    try:
        sampling = cast(SamplingProfile, opts.sampling or "greedy")
        planner = BonsaiPlanner(apps, settings=settings, sampling=sampling)
        warm = f", warmup {planner.warmup():.0f} ms" if opts.warmup else ""
    except Exception as exc:  # unreachable server, bad sampling name, ...
        return None, f"Bonsai at {settings.bonsai_url} unavailable ({type(exc).__name__}: {exc})"
    return planner, f"Bonsai at {settings.bonsai_url} (sampling={sampling}{warm})"


def _flow_backend(notes: list[str]) -> tuple[Callable[[Sequence[JSON]], FlowMatcherLike], ResolveFn | None]:
    """(seeded store factory, step resolver). Degrades to an empty store without brain.flows."""
    try:
        from brain.flows import Flow, FlowStep, FlowStore, resolve_step
    except Exception as exc:
        notes.append(_unavailable("brain.flows", exc) + "; flow cases will fail")
        return (lambda seed: _NullFlowStore()), None

    def make_store(seed: Sequence[JSON]) -> FlowMatcherLike:
        store = FlowStore(None)
        for f in seed:
            store.save(Flow.from_dict(f))
        return store

    def resolve(raw: JSON, snap: UISnapshot) -> Action | None:
        return resolve_step(FlowStep.from_dict(raw), snap)

    return make_store, resolve


def _confirm_fn(notes: list[str]) -> ConfirmFn:
    try:
        from brain.safety import needs_confirm
    except Exception as exc:
        notes.append(_unavailable("brain.safety", exc) + "; confirm = plan.confirm only")
        return lambda plan, snap: plan.confirm
    return needs_confirm


def build_components(apps: Sequence[str], opts: PlannerOptions) -> tuple[Components, str]:
    """Real ``Router(FastPath(apps), FlowStore(None), planner)``. Raises ComponentError without a router."""
    try:
        from brain.fastpath import FastPath
        from brain.router import Router
    except Exception as exc:
        raise ComponentError(
            _unavailable("brain.router", exc) + ". The eval needs the router + fast path (W3)."
        ) from exc
    notes: list[str] = []
    fastpath = FastPath(apps)
    make_store, resolve = _flow_backend(notes)
    planner, planner_desc = build_planner(apps, opts)

    def make_router(seed: Sequence[JSON]) -> RouterLike:
        return Router(fastpath, make_store(seed), planner)

    comps = Components(
        make_router=make_router,
        planner=planner,
        needs_confirm=_confirm_fn(notes),
        resolve_step=resolve,
        notes=tuple(notes),
    )
    return comps, planner_desc
