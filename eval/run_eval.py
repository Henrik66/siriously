"""Eval harness CLI (PRD §5.7): ``uv run python -m eval.run_eval [options]``.

Replays every case in ``eval/commands.yaml`` as text against its fixture and prints per-case
PASS/FAIL/SKIP/ERROR, the pass rate (with and without skipped cases, per split and category), and median /
p90 latency per route. Two modes:

* ``router`` (default): ``Router(FastPath(apps), FlowStore(None), planner)`` decides, the eval executes the
  plan on ``FakeExecutor`` (re-planning across screens itself) and scores the actions.
* ``e2e``: the full pipeline (``brain.pipeline.build_brain``) handles each utterance; scored on events,
  effective actions and the final screen; latency = end of speech -> first action. See ``eval/e2e.py``.

``--split dev`` is for prompt tuning; report ``--split holdout`` separately so tuning can't overfit.
Results go to ``eval/results/<timestamp>.json``. Fully offline: the only network use is the local Bonsai
server, and ``--no-model`` avoids even that (planner cases are then reported as SKIP).

Examples::

    uv run python -m eval.run_eval --no-model
    uv run python -m eval.run_eval --bonsai-url http://127.0.0.1:8080/v1 --repeat 3
    uv run python -m eval.run_eval --only 'pl-*' --verbose
    uv run python -m eval.run_eval --filter fastpath --filter control
    uv run python -m eval.run_eval --e2e --split holdout      # router-only vs e2e, held-out cases
    uv run python -m eval.run_eval --mode e2e --filter demo
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import platform
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from brain.contracts import JSON
from brain.fixtures import DEFAULT_FIXTURES_DIR, FixtureError, load_apps
from eval.cases import DEFAULT_CASES, MODES, Case, CaseError, load_cases, select_cases
from eval.components import ComponentError, PlannerOptions, build_components
from eval.report import case_line, comparison_text, results_json, summarize, summary_text
from eval.runner import CaseResult, Components, RunConfig, run_case, run_cases
from eval.scoring import Outcome

DEFAULT_RESULTS = Path(__file__).resolve().parent / "results"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m eval.run_eval", description=__doc__.split("\n\n")[0])
    p.add_argument("--no-model", action="store_true", help="run without the planner; planner cases SKIP")
    p.add_argument("--bonsai-url", help="Bonsai OpenAI-compatible base URL (default: SIRIOUSLY_BONSAI_URL)")
    p.add_argument("--sampling", help="planner sampling profile, e.g. greedy | card (A/B per PRD §5.3)")
    p.add_argument("--no-warmup", action="store_true", help="skip priming the planner's prompt cache")
    p.add_argument(
        "--only", action="append", default=[], help="case id or glob (repeatable, comma-separated)"
    )
    p.add_argument("--filter", action="append", default=[], metavar="ROUTE",
                   help="keep cases whose expected route, category or tag matches (repeatable)")  # fmt: skip
    p.add_argument(
        "--split", choices=("dev", "holdout", "all"), default="all", help="case split (default all)"
    )
    p.add_argument("--mode", choices=(*MODES, "both"), default="router", help="router | e2e | both")
    p.add_argument("--e2e", action="store_true", help="same as --mode both: router-only vs end-to-end")
    p.add_argument("--repeat", type=int, default=1, help="run each case N times (latency stats use all runs)")
    p.add_argument("--bursts", type=int, default=3, help="max planner bursts per case (default 3)")
    p.add_argument("--verbose", "-v", action="store_true", help="show plans, prompt suffix and planner stats")
    p.add_argument("--cases", type=Path, default=DEFAULT_CASES, help="commands YAML")
    p.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES_DIR, help="fixtures directory")
    p.add_argument("--out", type=Path, default=DEFAULT_RESULTS, help="results directory")
    p.add_argument("--no-write", action="store_true", help="do not write a results JSON file")
    p.add_argument("--min-pass", type=float, default=None,
                   help="exit 1 if the pass rate over cases that ran is below this (0..1)")  # fmt: skip
    return p.parse_args(argv)


def _split(values: Sequence[str]) -> list[str]:
    return [v.strip() for raw in values for v in raw.split(",") if v.strip()]


def write_results(by_mode: dict[str, list[CaseResult]], meta: JSON, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"{stamp}.json"
    n = 1
    while path.exists():
        n += 1
        path = out_dir / f"{stamp}-{n}.json"
    path.write_text(
        json.dumps(results_json(by_mode, meta), indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path


def _meta(args: argparse.Namespace, planner_desc: str, n_cases: int) -> JSON:
    return {
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "planner": planner_desc,
        "no_model": bool(args.no_model),
        "bonsai_url": args.bonsai_url,
        "sampling": args.sampling,
        "repeat": args.repeat,
        "max_bursts": args.bursts,
        "cases_file": str(args.cases),
        "n_cases": n_cases,
        "only": _split(args.only),
        "filter": list(args.filter),
        "split": args.split,
        "modes": _modes(args),
        "python": platform.python_version(),
        "platform": platform.platform(),
    }


def _utf8_stdout() -> None:
    """Piped output on Windows defaults to cp1252, which mangles menu glyphs (›, …)."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper) and (stream.encoding or "").lower() != "utf-8":
            stream.reconfigure(encoding="utf-8", errors="replace")


def _modes(args: argparse.Namespace) -> list[str]:
    return list(MODES) if args.e2e or args.mode == "both" else [str(args.mode)]


def _runner(mode: str, comps: Components, cfg: RunConfig) -> Callable[[Case], Outcome]:
    if mode == "router":
        return lambda c: run_case(c, comps, cfg)
    try:
        from eval.e2e import real_brain_factory, run_case_e2e

        factory = real_brain_factory(comps.planner)
    except Exception as exc:
        raise ComponentError(f"e2e mode needs brain.pipeline ({type(exc).__name__}: {exc})") from exc
    has_planner = comps.planner is not None
    return lambda c: run_case_e2e(c, factory, has_planner, cfg)


def _setup(args: argparse.Namespace) -> tuple[list[Case], Components, str]:
    """Load cases + components. Raises CaseError / FixtureError / ComponentError."""
    cases = select_cases(load_cases(args.cases), _split(args.only), args.filter, split=args.split)
    if not cases:
        raise CaseError("no cases selected")
    opts = PlannerOptions(
        use_model=not args.no_model,
        bonsai_url=args.bonsai_url,
        sampling=args.sampling,
        warmup=not args.no_warmup,
    )
    comps, planner_desc = build_components(load_apps(args.fixtures), opts)
    if not args.no_model and comps.planner is None:
        print(f"eval: WARNING planner unavailable: {planner_desc}. Planner cases will SKIP.", file=sys.stderr)
    for note in comps.notes:
        print(f"eval: note: {note}", file=sys.stderr)
    return cases, comps, planner_desc


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    _utf8_stdout()
    try:
        cases, comps, planner_desc = _setup(args)
        cfg = RunConfig(fixtures_dir=args.fixtures, max_bursts=args.bursts, verbose=args.verbose)
        runners = {mode: _runner(mode, comps, cfg) for mode in _modes(args)}
    except (CaseError, FixtureError, ComponentError) as exc:
        print(f"eval: {exc}", file=sys.stderr)
        return 2
    by_mode: dict[str, list[CaseResult]] = {}
    for mode, run_one in runners.items():
        mode_cases = [c for c in cases if mode in c.modes]
        print(f"\n== {mode}: {len(mode_cases)} cases (split {args.split}), planner: {planner_desc}")
        by_mode[mode] = run_cases(
            mode_cases,
            run_one,
            mode=mode,
            repeat=args.repeat,
            on_result=lambda r: print(case_line(r, args.verbose), flush=True),
        )
        print(summary_text(by_mode[mode], planner_desc, mode))
    print(comparison_text(by_mode))
    if not args.no_write:
        print(f"results: {write_results(by_mode, _meta(args, planner_desc, len(cases)), args.out)}")
    if args.min_pass is not None and any(summarize(r).rate_run < args.min_pass for r in by_mode.values()):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
