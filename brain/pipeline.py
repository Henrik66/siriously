"""Execution loop, safety gate and flow memory glue (PRD 5.4, 5.5, 5.6)."""

from __future__ import annotations

import contextlib
import functools
import inspect
import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal, Protocol, cast

from brain.choice import parse_choice
from brain.config import Settings
from brain.contracts import (
    JSON,
    Action,
    ActionResult,
    ControlWord,
    Event,
    EventSink,
    EventType,
    Executor,
    Plan,
    Planner,
    Route,
    TargetDescriptor,
    UIProvider,
    UISnapshot,
    Utterance,
)
from brain.control import normalize
from brain.flows import Flow, FlowStep, FlowStore, resolve_step
from brain.router import is_filler
from brain.safety import confirm_reason, normalize_keys

logger = logging.getLogger("brain.pipeline")


class RouterLike(Protocol):
    def route(self, text: str, snapshot: UISnapshot) -> Route: ...


Outcome = Literal["ok", "failed", "pending", "stopped"]

_PLAN_KINDS = ("fastpath", "planner", "dictation")
_PRESERVE_PENDING = frozenset(
    {"yes", "no", "cancel", "stop"}
)  # every other utterance drops a pending confirm
_RESOLVE_POLLS = 4  # flow step unresolved after earlier steps ran: wait for the UI to settle ...
_RESOLVE_INTERVAL_S = 0.25  # ... up to ~1 s before falling back to the planner


def _control_word(text: str) -> ControlWord | None:
    """Control word for ``text`` (None if not a control utterance). Pure regex, no UI access."""
    from brain.control import parse_control

    return parse_control(text)


def describe(action: Action, snapshot: UISnapshot) -> TargetDescriptor | None:
    """Descriptor of the item ``action`` targets in ``snapshot`` (None if no target / not found)."""
    if action.target is None:
        return None
    item = snapshot.item(action.target)
    return TargetDescriptor.of(item, snapshot.app.bundle_id) if item else None


# Menu leaves / shortcuts whose success is not visible in the UI tree (or that are state-dependent
# no-ops): a missing change must not count as a miss, or a re-plan would repeat them.
NO_VISIBLE_EFFECT_LABELS = frozenset(
    {"copy", "select all", "save", "find", "find next", "find previous", "undo", "redo"}
)
NO_VISIBLE_EFFECT_KEYS = frozenset(
    normalize_keys(k)
    for k in ("cmd+c", "cmd+a", "cmd+s", "cmd+f", "cmd+g", "cmd+shift+g", "cmd+z", "cmd+shift+z")
)


# A press on one of these that changes nothing means "already selected": success, not a miss.
SELECTION_ROLES = frozenset({"row", "cell", "outlinerow", "tab", "radiobutton", "sidebaritem", "listitem"})
# Text inputs commonly relabel ("Message #general" -> "Message Henrik") while keeping their id.
RELABELING_ROLES = frozenset({"textfield", "textarea", "searchfield", "combobox"})


def _label_key(label: str) -> str:
    return label.strip().rstrip("…").removesuffix("...").strip().lower()


def expects_change(action: Action, snapshot: UISnapshot | None = None) -> bool:
    """Ops that should visibly change the UI; a no-op result means the action probably missed.

    press/open_app always. menu/keys unless the resolved menu label or the shortcut is in the
    no-visible-effect set (copy, select all, save, find, undo/redo). ``snapshot`` lets us resolve the
    menu item behind a menu action or a shortcut; without it we assume a visible change.
    """
    if action.op in ("press", "open_app"):
        return True
    if action.op == "menu":
        item = snapshot.item(action.target) if snapshot is not None and action.target is not None else None
        return item is None or _label_key(item.name) not in NO_VISIBLE_EFFECT_LABELS
    if action.op != "keys" or "+" not in (action.keys or ""):
        return False
    combo = normalize_keys(action.keys or "")
    if combo in NO_VISIBLE_EFFECT_KEYS:
        return False
    if snapshot is not None:
        for item in snapshot.menus:
            if item.shortcut and normalize_keys(item.shortcut) == combo:
                return _label_key(item.name) not in NO_VISIBLE_EFFECT_LABELS
    return True


def _content(snap: UISnapshot) -> tuple[object, ...]:
    """Snapshot content without ids, for "did anything change" comparisons."""
    items = tuple((i.kind, i.role, i.label, i.value, i.enabled, i.path) for i in snap.items)
    focus = snap.item(snap.focused_id) if snap.focused_id is not None else None
    return (snap.app, snap.window, items, (focus.role, focus.label) if focus else None)


def summarize_steps(steps: tuple[FlowStep, ...]) -> str:
    """Compact text description of a flow for the planner hint."""
    parts: list[str] = []
    for s in steps:
        a, t = s.action, s.target
        if t is not None:
            name = " > ".join(t.path) if t.path else t.label
            what = f'{a.op} {t.role or t.kind} "{name}"'
            parts.append(f'type "{a.text}" into {t.role or t.kind} "{name}"' if a.op == "type" else what)
        elif a.op == "open_app":
            parts.append(f"open_app {a.app}")
        elif a.op == "type":
            parts.append(f'type "{a.text}"')
        elif a.op == "keys":
            parts.append(f"keys {a.keys}")
        else:
            parts.append(a.op)
    return "; ".join(parts)


@dataclass
class _Run:
    """Mutable state for one utterance's execution."""

    utt: Utterance
    text: str
    kind: str
    deadline: float
    replans: int = 0
    n_actions: int = 0
    steps: list[FlowStep] = field(default_factory=list)
    gen: int = 0  # stop generation at handle() entry; a later stop() aborts this run
    usage: dict[str, float] = field(default_factory=dict)  # planner tokens/ms summed over the run
    last_changed: bool = False  # the last executed action succeeded and changed the UI
    nav: bool = False  # the last executed burst ended on a navigation step (app/window changed)


@dataclass
class _Pending:
    """A confirmation awaiting "yes". ``resume`` gets a *fresh* snapshot and must re-resolve targets."""

    text: str
    kind: str
    resume: Callable[[_Run, UISnapshot], Outcome]
    created: float = field(default_factory=time.monotonic)
    done: tuple[FlowStep, ...] = ()  # steps already run in this utterance (for "call that X")
    n_actions: int = 0
    replans: int = 0


@dataclass
class _PendingAsk:
    """An ambiguity question (Plan.ask) awaiting "one" / "the second one" / a label."""

    text: str
    ids: tuple[int, ...]
    labels: tuple[str, ...]
    descs: tuple[TargetDescriptor, ...]
    created: float = field(default_factory=time.monotonic)


@functools.cache
def _accepts_timeout_cls(cls: type) -> bool:
    try:
        return "timeout_s" in inspect.signature(getattr(cls, "plan")).parameters  # noqa: B009
    except (TypeError, ValueError, AttributeError):
        return False


def _accepts_timeout(planner: Planner) -> bool:
    return _accepts_timeout_cls(type(planner))


class Brain:
    # Flow steps that stop resolving after earlier steps ran are re-polled this many times.
    resolve_polls = _RESOLVE_POLLS
    resolve_interval_s = _RESOLVE_INTERVAL_S

    def __init__(
        self,
        provider: UIProvider,
        executor: Executor,
        router: RouterLike,
        planner: Planner | None,
        flows: FlowStore,
        sink: EventSink,
        settings: Settings | None = None,
    ) -> None:
        self._provider = provider
        self._executor = executor
        self._router = router
        self._planner = planner
        self._flows = flows
        self._sink = sink
        self._settings = settings or Settings()
        self._plan_s_ema = 0.0  # measured planner seconds (EMA); 0 = no data yet
        self._stop_lock = threading.Lock()
        self._stop_gen = 0  # bumped by every stop(); runs/queued utterances compare against it
        self._stop_wall = 0.0  # time.time() of the latest stop (speech-end clock of Utterance)
        self._stop_mono = 0.0  # time.monotonic() of the latest stop (clock of ``received_at``)
        self._lock = threading.Lock()
        self._pending: _Pending | None = None
        self._ask: _PendingAsk | None = None
        self._last: tuple[tuple[FlowStep, ...], str] | None = None

    # ------------------------------------------------------------------ public

    @property
    def flows(self) -> FlowStore:
        return self._flows

    def warmup(self) -> float | None:
        """Prime the planner's prompt cache (static prefix). Returns ms, or None if skipped/failed.

        Call once at startup: the first uncached Bonsai call processes ~900 prefix tokens and can
        blow the per-utterance budget. Never raises.
        """
        warm = getattr(self._planner, "warmup", None)
        if not callable(warm):
            return None
        try:
            ms = float(warm())
        except Exception as exc:
            logger.warning("planner warmup failed: %s", exc)
            return None
        logger.info("planner warm (%.0f ms)", ms)
        return ms

    def stop(self) -> None:
        """Abort the current burst between actions and cancel anything queued before now (any thread)."""
        self._register_stop(time.time())

    def handle(self, utt: Utterance, received_at: float | None = None) -> Route:
        """Process one utterance to completion. Never raises.

        ``received_at`` (``time.monotonic()``) is when the caller queued the utterance; if omitted it
        is the time handle() is entered. An utterance that predates a stop() is cancelled.
        """
        try:
            gen = self._stop_gen  # captured before waiting on the utterance lock
            received = time.monotonic() if received_at is None else received_at
            if not utt.speaker.verified:
                self._emit("rejected", utt, {"text": utt.text, "score": utt.speaker.score})
                return Route(kind="rejected", reason="unverified speaker")
            word = _control_word(utt.text)
            if word == "stop":
                return self._handle_stop(utt)
            with self._lock:
                return self._handle_locked(utt, word, gen, received)
        except Exception as exc:
            return self._fail(utt, f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- plumbing

    def _emit(self, type_: EventType, utt: Utterance, data: JSON) -> None:
        ms = int((time.time() - utt.t_speech_end) * 1000) if utt.t_speech_end > 0 else 0
        self._sink.emit(Event(type=type_, utterance_id=utt.id, data=data, ms_since_speech_end=ms))

    def _fail(self, utt: Utterance, message: str) -> Route:
        with contextlib.suppress(Exception):  # a broken sink must not crash handle()
            self._emit("error", utt, {"message": message})
        return Route(kind="none", reason=message)

    def _register_stop(self, wall: float) -> None:
        with self._stop_lock:
            self._stop_gen += 1
            self._stop_wall = max(self._stop_wall, wall)
            self._stop_mono = time.monotonic()
            self._pending = None
            self._ask = None

    def _handle_stop(self, utt: Utterance) -> Route:
        """Stop must never wait for the utterance lock."""
        t0 = time.monotonic()
        # utterances spoken before "stop" ended (even if still queued) are cancelled
        self._register_stop(utt.t_speech_end if utt.t_speech_end > 0 else time.time())
        route = Route(kind="control", control="stop", reason="stop")
        self._emit("heard", utt, {"text": utt.text, "speaker": utt.speaker.to_dict()})
        self._emit("route", utt, {"kind": "control", "control": "stop", "reason": "stop", "ms": 0.0})
        self._emit("done", utt, self._done_data("control", 0, t0))
        return route

    def _cancelled_by_stop(self, utt: Utterance, gen: int, received: float) -> bool:
        with self._stop_lock:
            if self._stop_gen != gen or received <= self._stop_mono:
                return True
            return utt.t_speech_end > 0 and utt.t_speech_end <= self._stop_wall

    @staticmethod
    def _done_data(kind: str, n: int, t0: float) -> JSON:
        return {"route": kind, "n_actions": n, "ms": int((time.monotonic() - t0) * 1000)}

    # ---------------------------------------------------------------- dispatch

    def _handle_locked(self, utt: Utterance, word: ControlWord | None, gen: int, received: float) -> Route:
        t0 = time.monotonic()
        if word is None and self._cancelled_by_stop(utt, gen, received):
            self._emit("error", utt, {"message": "cancelled by stop"})
            return Route(kind="none", reason="cancelled by stop")
        if word is None and is_filler(utt.text):
            # Speech-to-text silence hallucinations ("Thank you.", "[BLANK_AUDIO]") are a no-op:
            # they must not cancel a pending confirm/question or produce an error.
            logger.info("ignoring filler utterance %s: %r", utt.id, utt.text)
            return Route(kind="none", reason="empty or filler utterance")
        if word not in _PRESERVE_PENDING:
            self._pending = None  # a new command/teach/undo invalidates any "are you sure?"
        ask, self._ask = self._ask, None  # any utterance consumes the pending question
        self._emit("heard", utt, {"text": utt.text, "speaker": utt.speaker.to_dict()})
        deadline = t0 + self._settings.utterance_timeout_s
        run = _Run(utt, utt.text, "control" if word else "none", deadline, gen=gen)
        if word is not None:  # control words never wait for (or fail on) a UI walk
            route = Route(kind="control", control=word)
            self._emit("route", utt, {"kind": "control", "reason": "", "ms": 0.0, "control": word})
            self._handle_control(run, route, t0)
            return route
        snapshot = self._provider.snapshot()
        if ask is not None and (answered := self._answer_ask(run, ask, snapshot, t0)) is not None:
            return answered
        route = self._router.route(utt.text, snapshot)
        data: JSON = {"kind": route.kind, "reason": route.reason, "ms": route.ms}
        if route.control:
            data["control"] = route.control
        if route.flow:
            data["flow"] = route.flow
        self._emit("route", utt, data)
        run.kind = route.kind
        if route.kind in ("planner", "dictation") and route.plan is not None:
            self._add_usage(run)  # the router already called the planner
        if route.kind == "control":
            self._handle_control(run, route, t0)
        elif route.kind == "flow" and route.teach:
            self._handle_teach(run, route, t0)
        elif route.kind == "flow":
            self._finish(run, self._start_flow(run, route, snapshot), t0)
        elif route.kind in _PLAN_KINDS and route.plan is not None:
            self._emit_plan(run, route.plan, route.kind, snapshot)
            outcome = self._start_plan(run, route.plan, snapshot)
            if outcome == "ok" and route.rest:
                outcome = self._run_rest(run, route, snapshot)
            self._finish(run, outcome, t0)
        else:
            self._emit("error", utt, {"message": route.reason or "no action"})
        return route

    def _finish(self, run: _Run, outcome: Outcome, t0: float) -> None:
        if outcome != "ok":
            return
        if run.steps:
            self._last = (tuple(run.steps), run.text)
        data = self._done_data(run.kind, run.n_actions, t0)
        if run.usage.get("calls"):
            data["usage"] = dict(run.usage)
        self._emit("done", run.utt, data)

    def _add_usage(self, run: _Run) -> None:
        """Fold the planner's ``last_stats`` (most recent call) into the run's usage totals."""
        stats = getattr(self._planner, "last_stats", None) or {}
        u = run.usage
        for key in ("prompt_tokens", "cached_tokens", "completion_tokens", "ms"):
            u[key] = u.get(key, 0) + float(stats.get(key, 0) or 0)
        u["calls"] = u.get("calls", 0) + 1

    # ----------------------------------------------------------------- control

    def _handle_control(self, run: _Run, route: Route, t0: float) -> None:
        utt, word = run.utt, route.control
        if word in ("cancel", "no"):
            self._pending = None
            self._emit("done", utt, self._done_data("control", 0, t0))
        elif word == "undo":
            if self._executor.undo():
                self._emit("done", utt, self._done_data("control", 0, t0))
            else:
                self._emit("error", utt, {"message": "nothing to undo"})
        elif word == "yes":
            self._confirm(run, t0)
        else:
            self._emit("error", utt, {"message": f"unknown control {word!r}"})

    def _confirm(self, run: _Run, t0: float) -> None:
        pending, self._pending = self._pending, None
        if pending is None or time.monotonic() - pending.created > self._settings.confirm_ttl_s:
            self._emit("error", run.utt, {"message": "nothing to confirm"})
            return
        fresh = self._provider.snapshot()  # never trust the snapshot from when we asked
        run.text, run.kind = pending.text, pending.kind
        run.steps, run.n_actions, run.replans = list(pending.done), pending.n_actions, pending.replans
        self._finish(run, pending.resume(run, fresh), t0)

    def _refuse(self, run: _Run, why: str) -> Outcome:
        self._emit("error", run.utt, {"message": f"cannot confirm: {why}; nothing was executed"})
        return "failed"

    def _handle_teach(self, run: _Run, route: Route, t0: float) -> None:
        name = route.flow or ""
        if self._last is None or not name:
            self._emit("error", run.utt, {"message": "nothing to save"})
            return
        steps, utterance = self._last
        self._flows.save(Flow(name=name, steps=steps, utterance=utterance))
        data = self._done_data("flow", 0, t0)
        data.update({"saved": name, "n_steps": len(steps)})
        self._emit("done", run.utt, data)

    # ---------------------------------------------------------------- execution

    def _gate(self, run: _Run) -> Outcome | None:
        if self._stop_gen != run.gen:
            self._emit("error", run.utt, {"message": "stopped"})
            return "stopped"
        if time.monotonic() > run.deadline:
            limit = self._settings.utterance_timeout_s
            self._emit("error", run.utt, {"message": f"timeout after {limit:g}s"})
            return "failed"
        return None

    def _ask_confirm(
        self, run: _Run, plan: Plan, reason: str, resume: Callable[[_Run, UISnapshot], Outcome]
    ) -> Outcome:
        self._pending = _Pending(
            run.text, run.kind, resume, done=tuple(run.steps), n_actions=run.n_actions, replans=run.replans
        )
        self._emit("confirm", run.utt, {"plan": plan.to_dict(), "reason": reason})
        return "pending"

    def _observe_change(self, before: UISnapshot, result: ActionResult) -> bool:
        """Re-snapshot once and compare content: trust the UI, not changed=False."""
        try:
            after = result.snapshot or self._provider.snapshot()
        except Exception:
            return False
        return _content(after) != _content(before)

    def _exec_one(
        self, run: _Run, action: Action, snapshot_id: str, snap: UISnapshot | None = None
    ) -> tuple[ActionResult, str | None]:
        try:
            result = self._executor.run(snapshot_id, action)
        except Exception as exc:
            result = ActionResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        failure: str | None = None
        changed = result.changed
        if not result.ok:
            failure = f"{action.op} failed: {result.error or 'unknown error'}"
        elif not changed and expects_change(action, snap):
            if snap is not None and self._observe_change(snap, result):
                changed = True
            elif action.op == "press" and self._is_selection(action, snap):
                pass  # already selected: nothing to change, nothing to retry
            else:
                failure = f"{action.op} had no effect (UI unchanged)"
        data: JSON = {
            "i": run.n_actions,
            "action": action.to_dict(),
            "ok": result.ok,
            "changed": changed,
            "error": result.error,
        }
        item = snap.item(action.target) if snap is not None and action.target is not None else None
        if item is not None:
            data["label"] = item.display  # human-readable target for overlays ("File › New Note")
        self._emit("action", run.utt, data)
        run.n_actions += 1
        run.last_changed = failure is None and changed
        return result, failure

    def _emit_plan(self, run: _Run, plan: Plan, source: str, snap: UISnapshot) -> None:
        data: JSON = {"plan": plan.to_dict(), "source": source}
        targets = {
            str(a.target): it.display
            for a in plan.actions
            if a.target is not None and (it := snap.item(a.target))
        }
        if targets:
            data["targets"] = targets  # {id: "File › New Note"} so overlays can show names, not ids
        if plan.ask is not None:
            data["ask"] = list(plan.ask)
            data["labels"] = [it.display if (it := snap.item(i)) else "" for i in plan.ask]
        self._emit("plan", run.utt, data)

    @staticmethod
    def _is_selection(action: Action, snap: UISnapshot | None) -> bool:
        item = snap.item(action.target) if snap is not None and action.target is not None else None
        return item is not None and item.role.lower() in SELECTION_ROLES

    def _start_plan(self, run: _Run, plan: Plan, snapshot: UISnapshot) -> Outcome:
        if not plan.actions:
            if plan.ask is None:
                self._emit("error", run.utt, {"message": "no actions planned"})
                return "failed"
            self._remember_ask(run, plan.ask, snapshot)
            return "ok"  # ambiguity: overlay numbers the candidates; the next utterance may pick one
        outcome = self._run_plan(run, plan, snapshot, confirmed=False)
        if outcome == "ok" and run.kind == "planner" and plan.mode == "actions" and run.nav:
            return self._continue(run, snapshot)
        return outcome

    def _remember_ask(self, run: _Run, ids: tuple[int, ...], snap: UISnapshot) -> None:
        found = [(i, it) for i in ids if (it := snap.item(i)) is not None]
        if not found:
            return
        self._ask = _PendingAsk(
            run.text,
            tuple(i for i, _ in found),
            tuple(it.display for _, it in found),
            tuple(TargetDescriptor.of(it, snap.app.bundle_id) for _, it in found),
        )

    def _answer_ask(self, run: _Run, ask: _PendingAsk, snap: UISnapshot, t0: float) -> Route | None:
        """If the utterance answers the pending question, run the chosen candidate (else None)."""
        if time.monotonic() - ask.created > self._settings.confirm_ttl_s:
            return None
        idx = parse_choice(run.text, ask.labels)
        if idx is None:
            return None
        route = Route(kind="planner", reason="ambiguity answer")
        self._emit("route", run.utt, {"kind": "planner", "reason": route.reason, "ms": 0.0})
        run.kind, run.text = "planner", ask.text
        desc, label = ask.descs[idx], ask.labels[idx]
        step = resolve_step(FlowStep(Action.press(ask.ids[idx]), desc), snap)
        if step is None or step.target is None:
            self._emit("error", run.utt, {"message": f"cannot resolve {label!r}: it is no longer on screen"})
            return route
        target = step.target
        if self._planner is not None:
            hint = f'User chose item {target} "{label}". Use it as the target.'
            plan = self._call_planner(run, ask.text, snap, hint=hint)
        else:
            action = Action.menu(target) if desc.kind == "menu" else Action.press(target)
            plan = Plan(snap.snapshot_id, (action,))
        self._emit_plan(run, plan, "choice", snap)
        self._finish(run, self._start_plan(run, plan, snap), t0)
        return route

    def _route_rest(self, text: str, snap: UISnapshot) -> Route:
        """Route compound tail text: never control/teach/flow; planner left to the caller."""
        route_rest = getattr(self._router, "route_rest", None)
        if callable(route_rest):
            return cast(Route, route_rest(text, snap, use_planner=False))
        route = self._router.route(text, snap)
        if route.kind in ("fastpath", "planner", "dictation") and route.plan is not None:
            return route
        return Route(kind="planner", reason="rest")

    def _run_rest(self, run: _Run, route: Route, base: UISnapshot) -> Outcome:
        """Second half of "open X and <rest>": route and run ``rest`` in the same utterance run."""
        assert route.plan is not None and route.rest is not None
        app = next((a.app for a in route.plan.actions if a.op == "open_app" and a.app), "")
        snap = self._await_app(run, base, app)
        if snap is None:
            self._emit("error", run.utt, {"message": f"{app or 'app'} did not come to the front"})
            return "failed"
        rest = self._route_rest(route.rest, snap)
        data: JSON = {"kind": rest.kind, "reason": rest.reason, "ms": rest.ms, "source": "rest"}
        self._emit("route", run.utt, data)
        run.kind, run.nav = rest.kind, False
        plan = rest.plan
        if plan is None:
            if self._planner is None:
                self._emit("error", run.utt, {"message": "no planner for the rest of the request"})
                return "failed"
            if not self._time_for_plan(run):
                self._emit("error", run.utt, {"message": "not enough time left to plan the rest"})
                return "failed"
            hint = (
                f"The app {app} was just opened and is now frontmost. Plan only what the user asked for next."
            )
            raw = self._call_planner(run, route.rest, snap, hint=hint)
            plan = self._drop_repeat(run, raw, snap)
            self._emit_plan(run, plan, "planner", snap)
            if raw.actions and not plan.actions:
                return "ok"
        else:
            self._emit_plan(run, plan, rest.kind, snap)
        return self._start_plan(run, plan, snap)

    def _await_app(self, run: _Run, base: UISnapshot, app: str) -> UISnapshot | None:
        """Fresh snapshot once ``app`` is frontmost (already frontmost is fine); None if it never is."""
        want = normalize(app)

        def is_app(s: UISnapshot) -> bool:
            have = normalize(s.app.name)
            return not want or have == want or want in have or have in want

        snap = self._provider.snapshot()
        if is_app(snap):
            return snap
        moved = self._await_navigation(run, base)
        return moved if moved is not None and is_app(moved) else None

    def _continue(self, run: _Run, base: UISnapshot) -> Outcome:
        """The burst ended by navigating (open_app / app or window changed): plan the rest there."""
        planner = self._planner
        if planner is None or run.replans >= self._settings.max_replans:
            return "ok"
        if not self._time_for_plan(run):
            return "ok"
        snap = self._await_navigation(run, base)
        if snap is None:
            return "ok"
        run.replans += 1
        hint = (
            f"Continuing the user's request. Already done: {summarize_steps(tuple(run.steps))}. "
            "Plan only the remaining steps; return no actions if nothing is left." + self._already_open(run)
        )
        plan = self._drop_repeat(run, self._call_planner(run, run.text, snap, hint=hint), snap)
        self._emit_plan(run, plan, "continue", snap)
        if not plan.actions and plan.ask is None:
            return "ok"  # nothing left
        run.nav = False
        return self._start_plan(run, plan, snap)

    # ----------------------------------------------------------------- planner budget

    def _expected_plan_s(self) -> float:
        """Seconds a planner call is expected to take: EMA of measured calls, floored."""
        return max(self._settings.min_plan_budget_s, self._plan_s_ema * 1.25)

    def _time_for_plan(self, run: _Run) -> bool:
        """Enough of the utterance budget left for one more planner call (re-plan/continuation)?"""
        return run.deadline - time.monotonic() >= self._expected_plan_s()

    def _call_planner(
        self, run: _Run, text: str, snap: UISnapshot, *, hint: str | None = None, error: str | None = None
    ) -> Plan:
        """planner.plan, capped to the remaining utterance budget when the planner supports it."""
        planner = self._planner
        if planner is None:
            raise RuntimeError("no planner")
        remaining = max(run.deadline - time.monotonic(), 0.5)
        t = time.monotonic()
        try:
            if _accepts_timeout(planner):
                plan_fn = cast(Callable[..., Plan], planner.plan)
                return plan_fn(text, snap, hint=hint, error=error, timeout_s=remaining)
            return planner.plan(text, snap, hint=hint, error=error)
        finally:
            took = time.monotonic() - t
            self._add_usage(run)
            self._plan_s_ema = took if self._plan_s_ema == 0.0 else 0.7 * self._plan_s_ema + 0.3 * took

    def _await_navigation(self, run: _Run, base: UISnapshot) -> UISnapshot | None:
        """Poll (~1 s) until the frontmost app/window differs from ``base``; None if it never does."""
        key = (base.app.bundle_id, base.app.name, base.window)
        for attempt in range(self.resolve_polls + 1):
            if attempt:
                if self._stop_gen != run.gen or time.monotonic() > run.deadline:
                    return None
                time.sleep(self.resolve_interval_s)
            snap = self._provider.snapshot()
            if (snap.app.bundle_id, snap.app.name, snap.window) != key:
                return snap
        return None

    def _run_plan(self, run: _Run, plan: Plan, base: UISnapshot, *, confirmed: bool) -> Outcome:
        descs = tuple(describe(a, base) for a in plan.actions)
        if not confirmed:
            reason = confirm_reason(plan, base)
            if reason:
                bundle = base.app.bundle_id
                return self._ask_confirm(
                    run, plan, reason, lambda r, fresh: self._resume_plan(r, plan, descs, bundle, fresh)
                )
        return self._exec_plan(run, plan, base, descs)

    def _resume_plan(
        self,
        run: _Run,
        plan: Plan,
        descs: tuple[TargetDescriptor | None, ...],
        bundle: str,
        fresh: UISnapshot,
    ) -> Outcome:
        """Run a confirmed plan against a fresh snapshot, re-resolving every target by descriptor."""
        if bundle and fresh.app.bundle_id != bundle:
            return self._refuse(run, f"frontmost app changed ({fresh.app.name})")
        actions = self._rebind(plan.actions, descs, fresh)
        if actions is None:
            return self._refuse(run, "a target is no longer on screen")
        fresh_plan = replace(plan, snapshot_id=fresh.snapshot_id, actions=actions)
        return self._exec_plan(run, fresh_plan, fresh, descs)

    @staticmethod
    def _rebind(
        actions: Sequence[Action],
        descs: Sequence[TargetDescriptor | None],
        snap: UISnapshot,
        *,
        keep_ids: bool = False,
    ) -> tuple[Action, ...] | None:
        """Re-bind targeted actions to ``snap`` ids by descriptor; None if any fails to resolve.

        ``keep_ids`` (mid-burst only): a type target, or a text-input role, whose id survives the UI
        change with the same kind+role is kept even if its label changed. Press/menu targets of any
        other role must still match by descriptor, so a destructive press is never redirected.
        """
        out: list[Action] = []
        for action, desc in zip(actions, descs, strict=True):
            if action.target is None:
                out.append(action)
                continue
            if keep_ids and desc is not None and Brain._same_slot(action, desc, snap):
                out.append(action)
                continue
            rebound = resolve_step(FlowStep(action, desc), snap) if desc is not None else None
            if rebound is None:
                return None
            out.append(rebound)
        return tuple(out)

    @staticmethod
    def _same_slot(action: Action, desc: TargetDescriptor, snap: UISnapshot) -> bool:
        item = snap.item(action.target) if action.target is not None else None
        if item is None or item.kind != desc.kind or item.role != desc.role:
            return False
        if desc.bundle_id and snap.app.bundle_id and desc.bundle_id != snap.app.bundle_id:
            return False
        return action.op == "type" or item.role.lower() in RELABELING_ROLES

    def _exec_plan(
        self, run: _Run, plan: Plan, base: UISnapshot, descs: tuple[TargetDescriptor | None, ...]
    ) -> Outcome:
        actions = list(plan.actions)
        cur, sid = base, plan.snapshot_id
        run.nav = False
        for i, action in enumerate(actions):
            if (stopped := self._gate(run)) is not None:
                return stopped
            result, failure = self._exec_one(run, action, sid, cur)
            if failure is not None:
                return self._replan(run, failure, result)
            run.steps.append(FlowStep(action, descs[i]))
            new = result.snapshot
            if i == len(actions) - 1 and run.kind == "planner":
                run.nav = self._is_navigation(action, cur, new)
            if result.changed and new is not None and new.snapshot_id != sid and i + 1 < len(actions):
                # the UI moved on: ids from the plan's snapshot are stale, re-bind the rest
                rest = self._rebind(actions[i + 1 :], descs[i + 1 :], new, keep_ids=True)
                if rest is None:
                    msg = "remaining targets no longer resolve after the UI changed"
                    return self._replan(run, msg, result)
                actions[i + 1 :] = rest
                cur, sid = new, new.snapshot_id
        return "ok"

    @staticmethod
    def _is_repeat(run: _Run, action: Action, snap: UISnapshot) -> bool:
        """Does ``action`` duplicate one already executed in this run?

        Targeted actions and open_app match any earlier step (same op + descriptor / app): the
        planner re-choosing an already-open sidebar row or menu is never useful. keys/type/scroll
        only match the immediately previous step, and only if it changed the UI.
        """
        if action.target is not None:
            desc = describe(action, snap)
            return desc is not None and any(
                st.action.op == action.op and st.target == desc for st in run.steps
            )
        if action.op == "open_app":
            return any(st.action.op == "open_app" and st.action.app == action.app for st in run.steps)
        if not run.steps or not run.last_changed:
            return False
        prev = run.steps[-1].action
        key = (action.op, action.keys, action.text, action.direction)
        return prev.target is None and key == (prev.op, prev.keys, prev.text, prev.direction)

    @classmethod
    def _drop_repeat(cls, run: _Run, plan: Plan, snap: UISnapshot) -> Plan:
        """Drop leading actions that repeat what this run already did (View > Appearance twice).

        Such a repeat would be reported as "no effect" and burn the re-plan budget; skipping it lets
        the remaining steps (if any) run, and an emptied plan means "nothing left".
        """
        actions = list(plan.actions)
        while actions and cls._is_repeat(run, actions[0], snap):
            logger.info("dropping repeated action %s", actions[0].op)
            actions.pop(0)
        return replace(plan, actions=tuple(actions)) if len(actions) != len(plan.actions) else plan

    @staticmethod
    def _already_open(run: _Run) -> str:
        names = [
            " > ".join(st.target.path) if st.target.path else st.target.label
            for st in run.steps
            if st.target is not None and st.action.op in ("press", "menu")
        ]
        names = [n for n in dict.fromkeys(names) if n]
        return "".join(f' Do NOT select "{n}" again, it is already open.' for n in names)

    def _is_navigation(self, action: Action, before: UISnapshot, after: UISnapshot | None) -> bool:
        if action.op == "open_app":
            return True
        if action.op not in ("press", "menu"):
            return False
        try:
            after = after or self._provider.snapshot()
        except Exception:
            return False
        return (after.app.bundle_id, after.window) != (before.app.bundle_id, before.window)

    def _replan(self, run: _Run, failure: str, result: ActionResult) -> Outcome:
        limit = self._settings.max_replans
        if self._planner is None or run.replans >= limit:
            suffix = f" (gave up after {run.replans} replans)" if run.replans else ""
            self._emit("error", run.utt, {"message": failure + suffix})
            return "failed"
        if (stopped := self._gate(run)) is not None:
            return stopped
        if not self._time_for_plan(run):
            self._emit("error", run.utt, {"message": f"{failure} (not enough time left to re-plan)"})
            return "failed"
        run.replans += 1
        snap = result.snapshot or self._provider.snapshot()
        error = failure
        if run.steps:
            error += f". Already done, do NOT repeat: {summarize_steps(tuple(run.steps))}"
            error += self._already_open(run)
        raw = self._call_planner(run, run.text, snap, error=error)
        plan = self._drop_repeat(run, raw, snap)
        self._emit_plan(run, plan, "replan", snap)
        if raw.actions and not plan.actions:
            return "ok"  # the planner only repeated what already worked: nothing left
        return self._start_plan(run, plan, snap)

    # -------------------------------------------------------------------- flows

    def _start_flow(self, run: _Run, route: Route, snapshot: UISnapshot) -> Outcome:
        flow = self._flows.get(route.flow or "")
        if flow is None:
            self._emit("error", run.utt, {"message": f"unknown flow {route.flow!r}"})
            return "failed"
        run.text = flow.utterance or flow.name
        return self._run_flow(run, flow, 0, snapshot, confirmed=False)

    def _await_resolution(
        self, run: _Run, step: FlowStep, snap: UISnapshot
    ) -> tuple[UISnapshot, Action | None]:
        """Poll briefly: after open_app/press the target app/window may still be coming forward."""
        for _ in range(self.resolve_polls):
            if self._stop_gen != run.gen or time.monotonic() > run.deadline:
                break
            time.sleep(self.resolve_interval_s)
            snap = self._provider.snapshot()
            if (action := resolve_step(step, snap)) is not None:
                return snap, action
        return snap, None

    def _run_flow(
        self, run: _Run, flow: Flow, start: int, snapshot: UISnapshot, *, confirmed: bool
    ) -> Outcome:
        snap = snapshot  # on resume this is already the fresh snapshot taken at "yes"
        for i in range(start, len(flow.steps)):
            if (stopped := self._gate(run)) is not None:
                return stopped
            step = flow.steps[i]
            action = resolve_step(step, snap)
            if action is None and i > 0:
                snap, action = self._await_resolution(run, step, snap)
            if action is None:
                return self._flow_fallback(run, flow, i, snap)
            if not (confirmed and i == start):
                plan = Plan(snap.snapshot_id, (action,))
                reason = confirm_reason(plan, snap)
                if reason:
                    return self._confirm_flow(run, flow, i, snap, plan, reason)
            result, failure = self._exec_one(run, action, snap.snapshot_id, snap)
            if failure is not None:
                return self._replan(run, failure, result)
            run.steps.append(FlowStep(action, step.target))
            snap = result.snapshot or self._provider.snapshot()
        return "ok"

    def _confirm_flow(
        self, run: _Run, flow: Flow, i: int, snap: UISnapshot, plan: Plan, reason: str
    ) -> Outcome:
        bundle = snap.app.bundle_id
        return self._ask_confirm(
            run, plan, reason, lambda r, fresh: self._resume_flow(r, flow, i, bundle, fresh)
        )

    def _resume_flow(self, run: _Run, flow: Flow, i: int, bundle: str, fresh: UISnapshot) -> Outcome:
        if bundle and fresh.app.bundle_id != bundle:
            return self._refuse(run, f"frontmost app changed ({fresh.app.name})")
        if resolve_step(flow.steps[i], fresh) is None:
            return self._refuse(run, "the confirmed step's target is no longer on screen")
        return self._run_flow(run, flow, i, fresh, confirmed=True)

    def _flow_fallback(self, run: _Run, flow: Flow, i: int, snap: UISnapshot) -> Outcome:
        """Step ``i`` no longer resolves: ask the planner for the *remaining* steps only."""
        if self._planner is None:
            self._emit("error", run.utt, {"message": f"flow {flow.name!r} no longer resolves; no planner"})
            return "failed"
        if i == 0:
            hint = f"taught flow: {summarize_steps(flow.steps)}"
        else:
            hint = (
                f"taught flow (remaining steps only): {summarize_steps(flow.steps[i:])}. "
                f"Already done, do NOT repeat: {summarize_steps(flow.steps[:i])}."
            )
        plan = self._call_planner(run, flow.utterance or flow.name, snap, hint=hint)
        self._emit_plan(run, plan, "flow-fallback", snap)
        return self._start_plan(run, plan, snap)


def build_brain(
    provider: UIProvider,
    executor: Executor,
    sink: EventSink,
    settings: Settings | None = None,
    planner: Planner | None = None,
    *,
    use_model: bool = True,
) -> Brain:
    """Wire FastPath, FlowStore, planner and Router around a provider/executor.

    ``planner=None`` builds a BonsaiPlanner unless ``use_model`` is False (fast path + flows only).
    """
    from brain.fastpath import FastPath
    from brain.router import Router

    settings = settings or Settings()
    apps = provider.list_apps()
    if not use_model:
        planner = None
    elif planner is None:
        try:
            from brain.planner.bonsai import BonsaiPlanner
            from brain.planner.client import SamplingProfile

            sampling: SamplingProfile = "card" if settings.bonsai_sampling == "card" else "greedy"
            planner = BonsaiPlanner(apps, settings, sampling=sampling)
        except ImportError:
            planner = None
    flows = FlowStore(settings.flows_path)
    router = Router(FastPath(apps), flows, planner)
    return Brain(provider, executor, router, planner, flows, sink, settings)
