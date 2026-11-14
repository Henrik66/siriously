"""Rehearse the 60-second demo (PRD §1), step by step, with timings and PASS/FAIL.

Two stages:

* fixtures (default): one in-process brain (``build_brain``) over ``FixtureUIProvider`` + ``FakeExecutor``,
  starting in Finder, with Bonsai if it answers (``--no-model`` to skip it). State carries across steps, so
  "call that standup" really saves what the first step did and "standup" really replays it.
* ``--live``: POSTs each line to a running brain server (``/utterance?wait=true``, JSON, ``X-Siriously-Token``
  when ``SIRIOUSLY_TOKEN`` is set) and reads ``/events/history`` — this drives the real Mac.

Exit code: 0 all steps passed, 1 any step failed, 2 none failed but some were skipped (e.g. no model).
Entry point: ``uv run python scripts/demo/rehearse.py [--live]`` (or ``python -m eval.rehearse``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from brain.contracts import JSON, Planner
from brain.fixtures import FakeExecutor, FixtureUIProvider, load_apps
from eval.components import PlannerOptions, build_planner
from eval.e2e import Recorder, TimedSink, real_brain_factory, take_turn
from eval.report import fmt_ms_text, utf8_stdio

TOKEN_ENV = "SIRIOUSLY_TOKEN"
TOKEN_HEADER = "X-Siriously-Token"
FIRST_RESPONSE = ("action", "confirm", "rejected")


@dataclass(frozen=True)
class DemoStep:
    key: str
    say: str
    audience: str  # what the room sees
    verified: bool = True
    routes: tuple[str, ...] = ()
    ends: str = "done"  # last event of the turn: done | confirm | rejected
    acts: bool | None = None  # must (True) / must not (False) run actions
    saved: str | None = None  # flow name the turn must save
    app: str | None = None  # fixtures only: frontmost app afterwards
    state: str | None = None  # fixtures only: frontmost snapshot afterwards
    needs: str | None = None  # skip unless this earlier step passed


DEMO: tuple[DemoStep, ...] = (
    DemoStep("task", "Open Notes and start a note called standup",
             "Notes comes forward and a new note titled 'standup' appears",
             routes=("fastpath", "planner"), acts=True, app="Notes", state="s-notes-1"),
    DemoStep("hijack", "Delete everything", "Nothing happens; the overlay shows 'rejected: not your voice'",
             verified=False, routes=("rejected",), ends="rejected", acts=False),
    DemoStep("teach", "Call that standup", "Overlay: saved flow 'standup'",
             routes=("flow",), acts=False, saved="standup", needs="task"),
    DemoStep("away", "Open Finder", "Finder comes forward", routes=("fastpath",), acts=True, app="Finder"),
    DemoStep("replay", "Standup", "Notes opens and a new 'standup' note appears instantly (no model)",
             routes=("flow",), acts=True, app="Notes", state="s-notes-1", needs="teach"),
    DemoStep("danger", "Delete this note", "Overlay asks 'Delete? say yes'; nothing is deleted yet",
             routes=("planner", "fastpath"), ends="confirm", acts=False),
    DemoStep("confirm", "Yes", "The note is deleted", routes=("control",), acts=True, state="s-notes-0",
             needs="danger"),
)  # fmt: skip


@dataclass(frozen=True)
class Heard:
    """What one utterance did, independent of where the brain runs."""

    route: str
    reason: str
    events: tuple[tuple[str, JSON], ...]  # (type, data)
    first_ms: float | None  # end of speech -> first action / confirm / rejected
    total_ms: float

    @property
    def types(self) -> list[str]:
        return [t for t, _ in self.events]

    def errors(self) -> list[str]:
        return [str(d.get("message", "")) for t, d in self.events if t == "error"]


class Stage(Protocol):
    name: str

    def say(self, uid: str, text: str, verified: bool) -> Heard: ...

    def screen(self) -> tuple[str, str] | None: ...  # (app, snapshot_id); None when unknown (live)


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


class FixtureStage:
    """One in-process brain over fixtures, starting in Finder."""

    def __init__(
        self, planner: Planner | None, fixtures_dir: Path | None = None, start: str = "finder"
    ) -> None:
        self.name = "fixtures"
        self.provider = FixtureUIProvider(start, fixtures_dir)
        self._tmp = tempfile.TemporaryDirectory(prefix="siriously-rehearse-")
        self.sink, self.rec = TimedSink(), Recorder(FakeExecutor(self.provider))
        factory = real_brain_factory(planner)
        self.brain = factory(self.provider, self.rec, self.sink, Path(self._tmp.name) / "flows.json", ())

    def say(self, uid: str, text: str, verified: bool) -> Heard:
        turn = take_turn(self.brain, self.sink, self.rec, uid, text, verified)
        firsts = [m for m in (turn.ms_to(t) for t in FIRST_RESPONSE) if m is not None]
        return Heard(
            route=turn.route.kind,
            reason=turn.route.reason,
            events=tuple((e.type, e.data) for e in turn.events),
            first_ms=min(firsts) if firsts else None,
            total_ms=(turn.t1 - turn.t0) * 1000.0,
        )

    def screen(self) -> tuple[str, str] | None:
        snap = self.provider.snapshot()
        return snap.app.name, snap.snapshot_id

    def close(self) -> None:
        self._tmp.cleanup()


class LiveStage:
    """A running brain server (the real Mac during rehearsal). Loopback by default; stdlib HTTP only."""

    def __init__(
        self, base_url: str, token: str | None = None, timeout_s: float = 15.0, wake: str = ""
    ) -> None:
        self.name = f"live {base_url}"
        self.wake = wake  # the Mac brain only listens to "Siriously, …" (brain.addressing)
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s

    def _request(self, path: str, body: JSON | None = None) -> JSON:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method="GET" if body is None else "POST")
        req.add_header("Accept", "application/json")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header(TOKEN_HEADER, self.token)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                out = json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:200]
            raise ConnectionError(f"{path}: HTTP {exc.code} {detail}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ConnectionError(f"{path}: {exc}") from exc
        return out if isinstance(out, dict) else {"value": out}

    def health(self) -> JSON:
        return self._request("/health")

    def say(self, uid: str, text: str, verified: bool) -> Heard:
        t0 = time.time()
        speaker = {"verified": verified, "score": 0.8 if verified else 0.05, "name": "rehearsal"}
        if self.wake:
            text = f"{self.wake}, {text}"
        body: JSON = {"id": uid, "text": text, "t_speech_end": t0, "stt_ms": 0, "speaker": speaker}
        route = self._request("/utterance?wait=true", body)
        total = (time.time() - t0) * 1000.0
        history = self._request("/events/history?n=200").get("events") or []
        mine = [e for e in history if isinstance(e, dict) and e.get("utterance_id") == uid]
        firsts = [float(e.get("ms_since_speech_end", 0)) for e in mine if e.get("type") in FIRST_RESPONSE]
        return Heard(
            route=str(route.get("kind", "?")),
            reason=str(route.get("reason") or ""),
            events=tuple((str(e.get("type")), dict(e.get("data") or {})) for e in mine),
            first_ms=firsts[0] if firsts else None,
            total_ms=total,
        )

    def screen(self) -> tuple[str, str] | None:
        return None


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    step: DemoStep
    status: str  # PASS | FAIL | SKIP
    heard: Heard | None = None
    problems: list[str] = field(default_factory=list)
    t_start: float = 0.0  # seconds since the rehearsal began


def check(step: DemoStep, heard: Heard, screen: tuple[str, str] | None) -> list[str]:
    problems: list[str] = []
    types = heard.types
    if step.routes and heard.route not in step.routes:
        problems.append(f"route {heard.route} (expected {'|'.join(step.routes)}) {heard.reason}".rstrip())
    if step.ends == "rejected":
        if types != ["rejected"]:
            problems.append(f"expected only 'rejected', got {types}")
    elif not types or types[-1] != step.ends:
        problems.append(f"turn ended with {types[-1] if types else 'nothing'}, expected {step.ends}")
    problems += [f"error: {m}" for m in heard.errors()]
    acted = sum(1 for t, d in heard.events if t == "action" and d.get("ok"))
    if step.acts is True and acted == 0:
        problems.append("no action ran")
    if step.acts is False and "action" in types:
        problems.append(f"{types.count('action')} action(s) ran but none should")
    if step.saved is not None:
        saved = next((d.get("saved") for t, d in heard.events if t == "done"), None)
        if str(saved or "").casefold() != step.saved.casefold():
            problems.append(f"flow not saved as {step.saved!r} (got {saved!r})")
    if screen is not None:
        if step.app is not None and screen[0].casefold() != step.app.casefold():
            problems.append(f"frontmost app {screen[0]}, expected {step.app}")
        if step.state is not None and screen[1] != step.state:
            problems.append(f"screen {screen[1]}, expected {step.state}")
    return problems


def _needs_model(heard: Heard) -> bool:
    return (heard.route == "none" and "no planner" in heard.reason) or any(
        "no planner" in m for m in heard.errors()
    )


def rehearse(
    stage: Stage, steps: Sequence[DemoStep] = DEMO, *, has_planner: bool = True, pause_s: float = 0.0
) -> list[StepResult]:
    results: list[StepResult] = []
    passed: set[str] = set()
    t0 = time.perf_counter()
    run_id = time.strftime("%H%M%S")
    for i, step in enumerate(steps):
        start = time.perf_counter() - t0
        if step.needs and step.needs not in passed:
            results.append(StepResult(step, "SKIP", None, [f"needs step '{step.needs}'"], start))
            continue
        try:
            heard = stage.say(f"rehearse-{run_id}-{i}-{step.key}", step.say, step.verified)
        except ConnectionError as exc:
            results.append(StepResult(step, "FAIL", None, [str(exc)], start))
            continue
        if not has_planner and _needs_model(heard):
            results.append(StepResult(step, "SKIP", heard, ["needs the planner (no model)"], start))
            continue
        problems = check(step, heard, stage.screen())
        results.append(StepResult(step, "FAIL" if problems else "PASS", heard, problems, start))
        if not problems:
            passed.add(step.key)
        if pause_s:
            time.sleep(pause_s)
    return results


def exit_code(results: Sequence[StepResult]) -> int:
    statuses = {r.status for r in results}
    return 1 if "FAIL" in statuses else 2 if "SKIP" in statuses else 0


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def timeline(results: Sequence[StepResult], stage_name: str, planner_desc: str) -> str:
    lines = [
        "",
        f"SIRIOUSLY demo rehearsal - {stage_name} - planner: {planner_desc}",
        f"{'#':>2}  {'t+':>6}  {'step':<8} {'you say':<46} {'route':<9} {'first resp.':>11}  result",
    ]
    for i, r in enumerate(results, 1):
        h = r.heard
        first = fmt_ms_text(h.first_ms) if h and h.first_ms is not None else "-"
        voice = "" if r.step.verified else " [other voice]"
        said = f'"{r.step.say}"{voice}'
        route = h.route if h else "-"
        lines.append(
            f"{i:>2}  {r.t_start:5.1f}s  {r.step.key:<8} {said:<46} {route:<9} {first:>11}  {r.status}"
        )
        lines.append(f"{'':>20}audience: {r.step.audience}")
        lines += [f"{'':>20}- {p}" for p in r.problems]
    counts = {s: sum(r.status == s for r in results) for s in ("PASS", "FAIL", "SKIP")}
    verdict = "READY" if counts["FAIL"] == counts["SKIP"] == 0 else "NOT READY"
    lines += ["", f"{verdict}: {counts['PASS']} pass, {counts['FAIL']} fail, {counts['SKIP']} skip"]
    return "\n".join(lines)


def results_json(results: Sequence[StepResult]) -> JSON:
    return {
        "steps": [
            {
                "key": r.step.key,
                "say": r.step.say,
                "status": r.status,
                "route": r.heard.route if r.heard else None,
                "first_ms": None
                if r.heard is None or r.heard.first_ms is None
                else round(r.heard.first_ms, 2),
                "total_ms": None if r.heard is None else round(r.heard.total_ms, 2),
                "events": r.heard.types if r.heard else [],
                "problems": r.problems,
            }
            for r in results
        ],
        "exit_code": exit_code(results),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="scripts/demo/rehearse.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--live", action="store_true", help="drive a running brain server instead of fixtures")
    host, port = os.environ.get("SIRIOUSLY_HOST", "127.0.0.1"), os.environ.get("SIRIOUSLY_PORT", "8765")
    p.add_argument("--url", default=f"http://{host}:{port}", help="server base URL for --live")
    p.add_argument("--timeout", type=float, default=15.0, help="per-request timeout for --live (s)")
    p.add_argument("--wake", default="Siriously", help="--live: wake word before each line")
    p.add_argument("--no-model", action="store_true", help="fixtures: run without the planner")
    p.add_argument("--bonsai-url", help="fixtures: Bonsai base URL (default SIRIOUSLY_BONSAI_URL)")
    p.add_argument("--pause", type=float, default=0.0, help="seconds between steps (pace a live run)")
    p.add_argument("--only", action="append", default=[], help="step keys to run (repeatable)")
    p.add_argument("--json", action="store_true", help="print results as JSON")
    return p.parse_args(argv)


def _stage(args: argparse.Namespace) -> tuple[Stage, str, bool]:
    """(stage, planner description, has planner). Raises ConnectionError for an unreachable live server."""
    if args.live:
        live = LiveStage(args.url, os.environ.get(TOKEN_ENV) or None, args.timeout, args.wake)
        health = live.health()
        return live, f"server reports bonsai={health.get('bonsai')}", True
    planner, desc = build_planner(
        load_apps(), PlannerOptions(use_model=not args.no_model, bonsai_url=args.bonsai_url)
    )
    return FixtureStage(planner), desc, planner is not None


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    utf8_stdio()
    steps = [s for s in DEMO if not args.only or s.key in args.only]
    try:
        stage, planner_desc, has_planner = _stage(args)
    except ConnectionError as exc:
        print(f"rehearse: cannot reach the brain server at {args.url}: {exc}", file=sys.stderr)
        return 1
    try:
        results = rehearse(stage, steps, has_planner=has_planner, pause_s=args.pause)
    finally:
        if isinstance(stage, FixtureStage):
            stage.close()
    print(
        json.dumps(results_json(results), indent=1)
        if args.json
        else timeline(results, stage.name, planner_desc)
    )
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
