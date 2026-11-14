"""Eval reporting: per-case lines, pass rates (overall / category / split), latency per route, results JSON.

Latency per route means different things per mode:

* ``router``: the routing decision (fast path match, or the planner call), wall clock.
* ``e2e``: "end of speech → first action": ``Brain.handle()`` start to the first ``action`` event.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from brain.contracts import JSON
from eval.runner import CaseResult
from eval.scoring import Outcome, Verdict, describe_action

LATENCY_LABEL = {
    "router": "routing decision",
    "e2e": "end of speech -> first action",
}


@dataclass(frozen=True)
class Summary:
    total: int
    passed: int
    failed: int
    errors: int
    skipped: int

    @property
    def rate_all(self) -> float:
        """Pass rate over every case (skips count as not passed)."""
        return self.passed / self.total if self.total else 0.0

    @property
    def rate_run(self) -> float:
        """Pass rate over the cases that actually ran (skips excluded)."""
        ran = self.total - self.skipped
        return self.passed / ran if ran else 0.0

    def to_dict(self) -> JSON:
        return {
            "total": self.total,
            "pass": self.passed,
            "fail": self.failed,
            "error": self.errors,
            "skip": self.skipped,
            "pass_rate_all": round(self.rate_all, 4),
            "pass_rate_excluding_skip": round(self.rate_run, 4),
        }


def summarize(results: Sequence[CaseResult]) -> Summary:
    counts = defaultdict(int, {k: 0 for k in ("PASS", "FAIL", "ERROR", "SKIP")})
    for r in results:
        counts[r.status] += 1
    return Summary(len(results), counts["PASS"], counts["FAIL"], counts["ERROR"], counts["SKIP"])


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..100)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100.0 * len(ordered)))
    return ordered[rank - 1]


def _latency(r: CaseResult, out: Outcome) -> float | None:
    return out.first_action_ms if r.mode == "e2e" else out.ms


def latency_by_route(results: Sequence[CaseResult]) -> dict[str, JSON]:
    """Median / p90 / max latency (see module doc), grouped by the route actually taken."""
    groups: dict[str, list[float]] = defaultdict(list)
    for r in results:
        for out, verdict in r.runs:
            ms = _latency(r, out)
            if out.route is not None and ms is not None and verdict.status != "SKIP":
                groups[out.route.kind].append(ms)
    return {
        kind: {
            "n": len(ms),
            "median_ms": round(statistics.median(ms), 2),
            "p90_ms": round(percentile(ms, 90), 2),
            "max_ms": round(max(ms), 2),
        }
        for kind, ms in sorted(groups.items())
    }


def _group(results: Sequence[CaseResult], key: str) -> dict[str, JSON]:
    groups: dict[str, list[CaseResult]] = defaultdict(list)
    for r in results:
        groups[str(getattr(r.case, key)) or "-"].append(r)
    return {k: summarize(v).to_dict() for k, v in sorted(groups.items())}


def by_category(results: Sequence[CaseResult]) -> dict[str, JSON]:
    return _group(results, "category")


def by_split(results: Sequence[CaseResult]) -> dict[str, JSON]:
    return _group(results, "split")


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


def _actions_text(out: Outcome) -> str:
    return ", ".join(s.describe() for s in out.steps) or "-"


def case_line(r: CaseResult, verbose: bool = False) -> str:
    out, verdict = r.runs[-1]
    kind = out.route.kind if out.route else "-"
    runs = f" {r.passes}/{len(r.runs)}" if len(r.runs) > 1 else ""
    bursts = f" x{out.bursts}" if out.bursts > 1 else ""
    ms = _latency(r, out)
    ms_text = f"{ms:8.1f} ms" if ms is not None else "       - ms"
    head = f"{r.status:<5}{runs} {r.case.id:<36} {kind:<9}{ms_text}{bursts}  {_actions_text(out)}"
    lines = [head]
    failing = [v for _, v in r.runs if v.status != "PASS"]
    for reason in failing[-1].reasons if failing else ():
        lines.append(f"      - {reason}")
    if verbose:
        lines += _verbose_lines(r, out, verdict)
    return "\n".join(lines)


def _verbose_lines(r: CaseResult, out: Outcome, verdict: Verdict) -> list[str]:
    c = r.case
    lines = [
        f"      utterance: {c.utterance!r}  fixture: {c.fixture} ({c.state or 'initial'})  split: {c.split}"
    ]
    if out.route is not None and out.route.reason:
        lines.append(f"      reason: {out.route.reason}")
    for i, plan in enumerate(out.plans):
        acts = ", ".join(describe_action(a) for a in plan.actions)
        lines.append(
            f"      plan[{i}] on {plan.snapshot_id}: [{acts}] confirm={plan.confirm} say={plan.say!r}"
        )
    lines += [f"      {d}" for d in out.debug]
    return lines


def _rate_line(label: str, d: Mapping[str, object]) -> str:
    ran = int(str(d["total"])) - int(str(d["skip"]))
    return f"{label} {d['pass']}/{ran}" + (f" (+{d['skip']} skip)" if d["skip"] else "")


def summary_text(results: Sequence[CaseResult], planner_desc: str, mode: str = "router") -> str:
    s = summarize(results)
    lines = [
        "",
        f"[{mode}] planner: {planner_desc}",
        f"cases: {s.total}  pass {s.passed}  fail {s.failed}  error {s.errors}  skip {s.skipped}",
        f"pass rate (all cases):      {s.passed}/{s.total} = {s.rate_all:.1%}",
        f"pass rate (excluding skip): {s.passed}/{s.total - s.skipped} = {s.rate_run:.1%}",
        "by split:    " + "  ".join(_rate_line(k, v) for k, v in by_split(results).items()),
        "by category: " + "  ".join(_rate_line(k, v) for k, v in by_category(results).items()),
        f"latency by route ({LATENCY_LABEL.get(mode, mode)}):",
    ]
    for kind, st in latency_by_route(results).items():
        lines.append(
            f"  {kind:<9} n={st['n']:<3} median {st['median_ms']:8.1f} ms   p90 {st['p90_ms']:8.1f} ms"
            f"   max {st['max_ms']:8.1f} ms"
        )
    return "\n".join(lines)


def comparison_text(by_mode: Mapping[str, Sequence[CaseResult]]) -> str:
    """Router-only vs e2e pass rates, overall and on the cases both modes ran."""
    if len(by_mode) < 2:
        return ""
    lines = ["", "router-only vs e2e (pass / ran, skips excluded):"]
    for mode, results in by_mode.items():
        s = summarize(results)
        lines.append(f"  {mode:<7} all {s.passed}/{s.total - s.skipped} = {s.rate_run:.1%}")
    ids = set.intersection(*({r.case.id for r in res} for res in by_mode.values()))
    for mode, results in by_mode.items():
        s = summarize([r for r in results if r.case.id in ids])
        lines.append(f"  {mode:<7} shared {s.passed}/{s.total - s.skipped} = {s.rate_run:.1%}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def _run_json(out: Outcome, verdict: Verdict) -> JSON:
    return {
        "status": verdict.status,
        "reasons": list(verdict.reasons),
        "route": out.route.to_dict() if out.route else None,
        "ms": round(out.ms, 2),
        "total_ms": round(out.total_ms, 2),
        "first_action_ms": None if out.first_action_ms is None else round(out.first_action_ms, 2),
        "bursts": out.bursts,
        "confirm": out.confirm,
        "events": list(out.events),
        "actions": [
            {
                **s.action.to_dict(),
                "on": s.before.snapshot_id,
                "ok": s.ok,
                "changed": s.changed,
                "error": s.error,
            }
            for s in out.steps
        ],
        "plans": [p.to_dict() for p in out.plans],
        "debug": list(out.debug),  # e2e: per-turn event trace + every attempted action
    }


def mode_json(results: Sequence[CaseResult]) -> JSON:
    return {
        "summary": {
            **summarize(results).to_dict(),
            "by_category": by_category(results),
            "by_split": by_split(results),
        },
        "latency_by_route": latency_by_route(results),
        "cases": [
            {
                "id": r.case.id,
                "category": r.case.category,
                "split": r.case.split,
                "fixture": r.case.fixture,
                "utterance": r.case.utterance,
                "status": r.status,
                "runs": [_run_json(o, v) for o, v in r.runs],
            }
            for r in results
        ],
    }


def results_json(by_mode: Mapping[str, Sequence[CaseResult]], meta: JSON) -> JSON:
    return {"meta": meta, "modes": {mode: mode_json(results) for mode, results in by_mode.items()}}
