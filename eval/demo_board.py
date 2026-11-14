"""Demo scoreboard (PRD §5.7 "show the number in the demo"): ``uv run python -m eval.demo_board``.

Reads the newest ``eval/results/*.json`` (or ``--file``) and prints a big, projector-friendly board: the
pass rate in block digits, the holdout rate, router-only vs end-to-end, and median / p90 latency per route.
Stdlib only, no network. Colour when the terminal supports it (``NO_COLOR`` disables); ``--ascii`` for
consoles without block glyphs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from brain.contracts import JSON
from eval.report import headline, utf8_stdio

DEFAULT_RESULTS = Path(__file__).resolve().parent / "results"
TARGET = 0.85  # PRD §6: eval pass rate >= 85%

# 3x5 glyphs; "#" becomes a double-width block when rendered.
_FONT: dict[str, tuple[str, ...]] = {
    "0": ("###", "# #", "# #", "# #", "###"),
    "1": (" # ", "## ", " # ", " # ", "###"),
    "2": ("###", "  #", "###", "#  ", "###"),
    "3": ("###", "  #", "###", "  #", "###"),
    "4": ("# #", "# #", "###", "  #", "  #"),
    "5": ("###", "#  ", "###", "  #", "###"),
    "6": ("###", "#  ", "###", "# #", "###"),
    "7": ("###", "  #", "  #", "  #", "  #"),
    "8": ("###", "# #", "###", "# #", "###"),
    "9": ("###", "# #", "###", "  #", "###"),
    ".": (" ", " ", " ", " ", "#"),
    "%": ("# #", "  #", " # ", "#  ", "# #"),
    "-": ("   ", "   ", "###", "   ", "   "),
    " ": (" ", " ", " ", " ", " "),
}
_COLORS = {"good": "\x1b[1;32m", "ok": "\x1b[1;33m", "bad": "\x1b[1;31m", "dim": "\x1b[2m", "end": "\x1b[0m"}


def big(text: str, *, ascii_only: bool = False) -> list[str]:
    """``text`` (digits . % - space) as 5 rows of block characters."""
    block = "##" if ascii_only else "██"
    rows = ["", "", "", "", ""]
    for ch in text:
        glyph = _FONT.get(ch, _FONT[" "])
        for i in range(5):
            rows[i] += glyph[i].replace("#", block).replace(" ", "  ") + "  "
    return [r.rstrip() for r in rows]


def fmt_ms(ms: float) -> str:
    if ms >= 1000:
        return f"{ms / 1000:.1f} s"
    return f"{ms:.0f} ms" if ms >= 10 else f"{ms:.1f} ms"


def _pct(rate: object) -> str:
    return "-" if not isinstance(rate, int | float) else f"{rate * 100:.1f}%"


def latest_results(results_dir: Path) -> Path | None:
    """Newest results file (names are timestamps, so lexical order is chronological)."""
    files = sorted(results_dir.glob("*.json"), key=lambda p: (p.stat().st_mtime, p.name))
    return files[-1] if files else None


class _Paint:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, tone: str) -> str:
        return f"{_COLORS[tone]}{text}{_COLORS['end']}" if self.enabled else text


def _tone(rate: object) -> str:
    if not isinstance(rate, int | float):
        return "dim"
    return "good" if rate >= TARGET else "ok" if rate >= TARGET - 0.15 else "bad"


def _latency_lines(mode: Mapping[str, object], paint: _Paint) -> list[str]:
    lat = mode.get("latency")
    if not isinstance(lat, dict) or not lat:
        return []
    lines = [paint(f"  {mode.get('latency_label', 'latency')}  (median / p90)", "dim")]
    for route, st in lat.items():
        if route == "none":
            continue
        lines.append(
            f"    {route:<10}{fmt_ms(float(st['median_ms'])):>9} / {fmt_ms(float(st['p90_ms'])):<9}"
            + paint(f"  n={st['n']}", "dim")
        )
    return lines


def _split_line(mode: Mapping[str, object]) -> str:
    splits = mode.get("split")
    if not isinstance(splits, dict):
        return ""
    parts = [f"{k} {v['pass']}/{v['ran']} ({_pct(v['rate'])})" for k, v in sorted(splits.items())]
    return "  " + "    ".join(parts)


def render_board(head: Mapping[str, object], mode: str | None = None, *, ascii_only: bool = False,
                 color: bool = False) -> str:  # fmt: skip
    """The scoreboard for ``mode`` (default: e2e if present, else router) of a :func:`headline`."""
    modes = head.get("modes")
    if not isinstance(modes, dict) or not modes:
        raise ValueError("headline has no modes")
    mode = mode or ("e2e" if "e2e" in modes else next(iter(modes)))
    if mode not in modes:
        raise ValueError(f"mode {mode!r} not in results (have: {', '.join(modes)})")
    m, paint = modes[mode], _Paint(color)
    title = "end to end" if mode == "e2e" else "router"
    lines = [
        "",
        paint(f"  SIRIOUSLY  -  offline voice remote  -  eval ({title})", "dim"),
        paint(f"  {head.get('timestamp') or ''}   planner: {head.get('planner') or '-'}", "dim"),
        "",
        *(paint("  " + row, _tone(m["rate"])) for row in big(_pct(m["rate"]), ascii_only=ascii_only)),
        "",
        f"  {m['pass']} / {m['total']} commands pass" + (f"   ({m['skip']} skipped)" if m["skip"] else ""),
        _split_line(m),
    ]
    if len(modes) > 1:
        lines.append("  " + "    ".join(f"{k}: {_pct(v['rate'])}" for k, v in modes.items()))
    return "\n".join([*lines, "", *_latency_lines(m, paint), ""])


def _use_color(flag: str) -> bool:
    if flag != "auto":
        return flag == "always"
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m eval.demo_board", description=__doc__.split("\n\n")[0])
    p.add_argument("--file", type=Path, help="results JSON (default: newest in eval/results)")
    p.add_argument("--dir", type=Path, default=DEFAULT_RESULTS, help="results directory")
    p.add_argument("--mode", choices=("router", "e2e"), help="which mode to show (default: e2e if present)")
    p.add_argument("--ascii", action="store_true", help="ASCII digits instead of block glyphs")
    p.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    p.add_argument("--json", action="store_true", help="print the headline JSON instead of the board")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    utf8_stdio()
    path = args.file or latest_results(args.dir)
    if path is None:
        print(
            f"demo_board: no results in {args.dir}; run `python -m eval.run_eval --e2e` first",
            file=sys.stderr,
        )
        return 2
    try:
        data: JSON = json.loads(Path(path).read_text(encoding="utf-8"))
        head = headline(data, str(path))
        out = json.dumps(head, indent=1) if args.json else render_board(
            head, args.mode, ascii_only=args.ascii, color=_use_color(args.color)
        )  # fmt: skip
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"demo_board: cannot read {path}: {exc}", file=sys.stderr)
        return 2
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
