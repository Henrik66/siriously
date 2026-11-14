"""Latency benchmark for the brain on whatever Bonsai server is configured (PRD §6, §7).

    uv run python scripts/bench/planner_bench.py                     # SIRIOUSLY_BONSAI_URL or :8080
    uv run python scripts/bench/planner_bench.py --bonsai-url http://127.0.0.1:8081/v1 --repeat 3
    uv run python scripts/bench/planner_bench.py --no-model          # fast path only

Measures:
  * warm TTFT: streamed planner-shaped request (cached static prefix + a typical suffix)
  * plan latency (median / p90) over the eval's planner cases, on their real fixture snapshots,
    with prompt / cached / completion tokens and decode tok/s from the server's timings
  * fast-path matcher latency on a synthetic 300-item snapshot
and prints a verdict table against PRD §6 (end of speech -> first action: fast path <= 1.0 s,
planner <= 2.0 s; STT time from PRD §7 is added). Writes eval/results/bench-<ts>.json.
For the real numbers run it on the Mac, plugged in, Low Power Mode off.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import math
import platform
import statistics
import sys
import time
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # `brain` / `eval` are repo packages; make them importable from here
    sys.path.insert(0, str(ROOT))

from brain.config import Settings  # noqa: E402
from brain.contracts import AppInfo, UIItem, UISnapshot  # noqa: E402

JSON = dict[str, Any]
STT_MS = 150.0  # PRD §7: Argmax base.en, 147 ms for a 6 s clip
TARGET_FASTPATH_E2E_MS = 1000.0  # PRD §6
TARGET_PLANNER_E2E_MS = 2000.0  # PRD §6
TARGET_FASTPATH_BRAIN_MS = 30.0  # PRD §5.2
DEFAULT_RESULTS = ROOT / "eval" / "results"
# (url, body, timeout) -> response lines. Raise urllib/socket errors like urlopen.
StreamOpener = Callable[[str, bytes, float], Iterable[bytes]]

# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..100); 0.0 for no values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(q / 100.0 * len(ordered))))
    return float(ordered[rank - 1])


def summarize(values: Sequence[float]) -> JSON:
    if not values:
        return {"n": 0, "median": 0.0, "p90": 0.0, "max": 0.0}
    return {
        "n": len(values),
        "median": round(float(statistics.median(values)), 2),
        "p90": round(percentile(values, 90), 2),
        "max": round(float(max(values)), 2),
    }


def big_snapshot(n: int = 300) -> UISnapshot:
    """A realistic-size snapshot: ~5/6 elements (rows, buttons, fields), ~1/6 menu items."""
    roles = ("row", "button", "textfield", "checkbox", "link", "cell")
    menus = ("File", "Edit", "View", "Window", "Help")
    n_menus = max(n // 6, 1)
    items: list[UIItem] = [
        UIItem(i + 1, "element", roles[i % len(roles)], f"Item {i + 1} {roles[i % len(roles)]}")
        for i in range(n - n_menus - 1)
    ]
    items += [
        UIItem(1000 + j, "menu", path=(menus[j % len(menus)], f"Command {j}"), shortcut=f"cmd+{j % 10}")
        for j in range(n_menus)
    ]
    items.append(UIItem(2000, "menu", path=("File", "New Note"), shortcut="cmd+n"))
    return UISnapshot("bench-300", AppInfo("Notes", "com.apple.Notes"), "Notes", items=tuple(items))


FASTPATH_UTTERANCES = (
    "new note",  # menu by name
    "scroll down",
    "open slack",
    "copy",
    "press return",
    "item 120 row",  # fuzzy against many labels
    "make the third paragraph of this document bold and blue",  # miss: falls through to the planner
)


def first_token_ms(
    lines: Iterable[bytes], t0: float, clock: Callable[[], float] = time.perf_counter
) -> float:
    """ms from ``t0`` to the first SSE chunk carrying generated text; raises if none arrives."""
    for raw in lines:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:") or line[5:].strip() == "[DONE]":
            continue
        try:
            event = json.loads(line[5:])
        except ValueError:
            continue
        for choice in event.get("choices") or ():
            delta = choice.get("delta") or {}
            if delta.get("content") or delta.get("reasoning_content"):
                return (clock() - t0) * 1000.0
    raise RuntimeError("stream ended without a token")


def decode_tps(stats: JSON) -> float | None:
    tokens, ms = float(stats.get("completion_tokens", 0.0)), float(stats.get("decode_ms", 0.0))
    return round(tokens / (ms / 1000.0), 2) if tokens > 0 and ms > 0 else None


def verdicts(result: JSON) -> list[tuple[str, str, str, str]]:
    """(metric, measured, target, PASS/FAIL/-) rows for the summary table."""
    rows: list[tuple[str, str, str, str]] = []
    fp = result.get("fastpath", {}).get("ms", {})
    if fp.get("n"):
        ok = fp["p90"] <= TARGET_FASTPATH_BRAIN_MS
        rows.append(
            ("fast path, brain-side", _ms(fp), f"<= {TARGET_FASTPATH_BRAIN_MS:.0f} ms (PRD 5.2)", _pf(ok))
        )
        e2e = STT_MS + fp["p90"]
        rows.append(
            (
                "fast path, speech end -> first action*",
                f"~{e2e:.0f} ms",
                f"<= {TARGET_FASTPATH_E2E_MS:.0f} ms (PRD 6)",
                _pf(e2e <= TARGET_FASTPATH_E2E_MS),
            )
        )
    plan = result.get("planner", {}).get("ms", {})
    if plan.get("n"):
        e2e = STT_MS + plan["p90"]
        rows.append(("planner call", _ms(plan), "", "-"))
        rows.append(
            (
                "planner, speech end -> first action*",
                f"~{STT_MS + plan['median']:.0f} / {e2e:.0f} ms",
                f"<= {TARGET_PLANNER_E2E_MS:.0f} ms (PRD 6)",
                _pf(e2e <= TARGET_PLANNER_E2E_MS),
            )
        )
    ttft = result.get("ttft", {}).get("ms", {})
    if ttft.get("n"):
        rows.append(("warm TTFT (planner request)", _ms(ttft), "", "-"))
    tok = result.get("planner", {}).get("tokens", {})
    if tok:
        rows.append(
            (
                "tokens prompt / cached / completion",
                f"{tok['prompt']:.0f} / {tok['cached']:.0f} / {tok['completion']:.0f}",
                "",
                "-",
            )
        )
        rows.append(("decode", f"{tok['decode_tps']} tok/s", "", "-"))
    return rows


def _ms(s: JSON) -> str:
    digits = 2 if s["p90"] < 10 else 0  # the fast path is sub-millisecond; don't print "0 / 0"
    return f"{s['median']:.{digits}f} / {s['p90']:.{digits}f} ms (n={s['n']})"


def _pf(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def format_table(rows: Sequence[tuple[str, str, str, str]]) -> str:
    head = ("metric", "median / p90", "target", "")
    widths = [max(len(r[i]) for r in [head, *rows]) for i in range(4)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in [head, *rows]]
    lines.insert(1, "  ".join("-" * w for w in widths))
    lines.append("* adds STT (~150 ms, PRD 7); excludes snapshot capture and executor time")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------


def bench_fastpath(apps: Sequence[str], snapshot: UISnapshot, repeat: int = 20) -> JSON:
    from brain.fastpath import FastPath

    fp = FastPath(apps)
    times: list[float] = []
    hits = 0
    for _ in range(repeat):
        for text in FASTPATH_UTTERANCES:
            t0 = time.perf_counter()
            plan = fp.match(text, snapshot)
            times.append((time.perf_counter() - t0) * 1000.0)
            hits += plan is not None
    return {"items": len(snapshot.items), "calls": len(times), "hits": hits, "ms": summarize(times)}


def urllib_stream(url: str, body: bytes, timeout: float) -> Iterable[bytes]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with opener.open(req, timeout=timeout) as resp:
        yield from resp


def bench_ttft(settings: Settings, runs: int = 5, open_stream: StreamOpener = urllib_stream) -> JSON:
    from brain.planner.client import BonsaiClient
    from brain.planner.prompt import build_messages

    client = BonsaiClient(settings.bonsai_url, settings.bonsai_model, settings.bonsai_timeout_s, max_tokens=8)
    suffix = (
        'APP: Notes | WINDOW: Notes\n1 button "New Note"\n110 menu "File › New Note" (cmd+n)\nUSER: new note'
    )
    body = {**client.body(build_messages(suffix), None), "stream": True}
    payload = json.dumps(body).encode("utf-8")
    times: list[float] = []
    for _ in range(runs + 1):  # first run warms the cache and is discarded
        t0 = time.perf_counter()
        times.append(first_token_ms(open_stream(client.url, payload, settings.bonsai_timeout_s), t0))
    return {"runs": runs, "ms": summarize(times[1:])}


def planner_cases(only: Sequence[str] = ()) -> list[Any]:
    from eval.cases import load_cases

    cases = [c for c in load_cases() if "planner" in c.expect.routes]
    if only:
        cases = [c for c in cases if any(fnmatch.fnmatch(c.id, pat) for pat in only)]
    return cases


def case_snapshot(case: Any) -> UISnapshot:
    from brain.fixtures import FixtureUIProvider

    provider = FixtureUIProvider(case.fixture)
    if case.state:
        provider.goto(case.state)
    return provider.snapshot()


def bench_planner(planner: Any, cases: Sequence[Any], repeat: int = 1) -> JSON:
    rows: list[JSON] = []
    for case in cases:
        snap = case_snapshot(case)
        for _ in range(repeat):
            t0 = time.perf_counter()
            error = None
            try:
                planner.plan(case.utterance, snap)
            except Exception as exc:  # a failed call is a data point, not a crash
                error = f"{type(exc).__name__}: {exc}"
            ms = (time.perf_counter() - t0) * 1000.0
            stats = dict(getattr(planner, "last_stats", {}) or {})
            rows.append({"id": case.id, "ms": round(ms, 1), "error": error, "stats": stats})
    return {"rows": rows, **_planner_summary(rows)}


def _planner_summary(rows: Sequence[JSON]) -> JSON:
    ok = [r for r in rows if r["error"] is None]
    if not ok:
        return {"ms": summarize([]), "errors": len(rows)}

    def mean(key: str) -> float:
        return round(statistics.fmean(float(r["stats"].get(key, 0.0)) for r in ok), 1)

    tps = [t for r in ok if (t := decode_tps(r["stats"])) is not None]
    tokens = {
        "prompt": mean("prompt_tokens"),
        "cached": mean("cached_tokens"),
        "completion": mean("completion_tokens"),
        "prompt_ms": mean("prompt_ms"),
        "decode_ms": mean("decode_ms"),
        "decode_tps": round(statistics.median(tps), 2) if tps else None,
    }
    return {"ms": summarize([r["ms"] for r in ok]), "errors": len(rows) - len(ok), "tokens": tokens}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="planner_bench", description=__doc__.split("\n\n")[0])
    p.add_argument("--bonsai-url", help="Bonsai base URL (default: SIRIOUSLY_BONSAI_URL)")
    p.add_argument("--no-model", action="store_true", help="fast path only (no Bonsai server needed)")
    p.add_argument("--repeat", type=int, default=1, help="planner calls per case")
    p.add_argument("--ttft-runs", type=int, default=5)
    p.add_argument("--only", action="append", default=[], help="case id glob (repeatable)")
    p.add_argument("--out", type=Path, default=DEFAULT_RESULTS)
    p.add_argument("--no-write", action="store_true")
    return p.parse_args(argv)


def run(args: argparse.Namespace) -> JSON:
    from brain.fixtures import load_apps

    settings = Settings()
    if args.bonsai_url:
        settings = Settings(bonsai_url=args.bonsai_url, bonsai_timeout_s=max(settings.bonsai_timeout_s, 15.0))
    apps = load_apps()
    result: JSON = {
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "machine": {
            "platform": platform.platform(),
            "processor": platform.processor(),
            "python": sys.version,
        },
        "bonsai_url": None if args.no_model else settings.bonsai_url,
        "fastpath": bench_fastpath(apps, big_snapshot(300)),
    }
    if not args.no_model:
        from brain.planner.bonsai import BonsaiPlanner

        planner = BonsaiPlanner(apps, settings)
        result["warmup_ms"] = round(planner.warmup(), 1)
        result["ttft"] = bench_ttft(settings, args.ttft_runs)
        result["planner"] = bench_planner(planner, planner_cases(args.only), args.repeat)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    result = run(args)
    print(format_table(verdicts(result)))
    if not args.no_write:
        args.out.mkdir(parents=True, exist_ok=True)
        path = args.out / f"bench-{dt.datetime.now():%Y%m%d-%H%M%S}.json"
        path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        print(f"\nresults: {path}")
    errors = result.get("planner", {}).get("errors", 0)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
