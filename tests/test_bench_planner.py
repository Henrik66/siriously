"""Unit tests for scripts/bench/planner_bench.py (no Bonsai server needed)."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from brain.config import Settings

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "bench" / "planner_bench.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("planner_bench", _PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["planner_bench"] = mod
    spec.loader.exec_module(mod)
    return mod


pb = _load()


def _sse(*events: dict[str, Any] | str) -> list[bytes]:
    out = [b": keep-alive\n", b"\n"]
    for e in events:
        out.append(f"data: {e if isinstance(e, str) else json.dumps(e)}\n".encode())
    return out


def test_percentile_and_summarize() -> None:
    vals = [float(v) for v in range(1, 11)]
    assert pb.percentile(vals, 50) == 5.0
    assert pb.percentile(vals, 90) == 9.0
    assert pb.percentile(vals, 100) == 10.0 and pb.percentile([7.0], 90) == 7.0
    assert pb.percentile([], 90) == 0.0
    s = pb.summarize([3.0, 1.0, 2.0])
    assert s == {"n": 3, "median": 2.0, "p90": 3.0, "max": 3.0}
    assert pb.summarize([])["n"] == 0


def test_big_snapshot_has_exactly_n_items_and_stable_ids() -> None:
    snap = pb.big_snapshot(300)
    assert len(snap.items) == 300
    ids = [it.id for it in snap.items]
    assert len(ids) == len(set(ids))
    assert any(it.kind == "menu" and it.path == ("File", "New Note") for it in snap.items)


def test_first_token_ms_skips_non_content_chunks() -> None:
    lines = _sse(
        {"choices": [{"delta": {"role": "assistant"}}]},
        "not json",
        {"choices": [{"delta": {"content": "{"}}]},
        "[DONE]",
    )
    ticks = iter([10.25])
    assert pb.first_token_ms(lines, 10.0, clock=lambda: next(ticks)) == pytest.approx(250.0)


def test_first_token_ms_raises_without_tokens() -> None:
    with pytest.raises(RuntimeError):
        pb.first_token_ms(_sse({"choices": [{"delta": {}}]}, "[DONE]"), 0.0)


def test_decode_tps() -> None:
    assert pb.decode_tps({"completion_tokens": 20.0, "decode_ms": 1000.0}) == 20.0
    assert pb.decode_tps({"completion_tokens": 0.0, "decode_ms": 1000.0}) is None
    assert pb.decode_tps({}) is None


def test_verdicts_and_table() -> None:
    result = {
        "fastpath": {"ms": {"n": 10, "median": 2.0, "p90": 4.0, "max": 5.0}},
        "planner": {
            "ms": {"n": 5, "median": 1500.0, "p90": 1900.0, "max": 2100.0},
            "tokens": {"prompt": 1200.0, "cached": 1080.0, "completion": 19.0, "decode_tps": 15.2},
        },
        "ttft": {"ms": {"n": 5, "median": 300.0, "p90": 350.0, "max": 360.0}},
    }
    rows = pb.verdicts(result)
    by_metric = {r[0]: r for r in rows}
    assert by_metric["fast path, brain-side"][3] == "PASS"
    assert by_metric["fast path, speech end -> first action*"][3] == "PASS"
    assert by_metric["planner, speech end -> first action*"][3] == "FAIL"  # 150 + 1900 > 2000
    table = pb.format_table(rows)
    assert "planner call" in table and "15.2 tok/s" in table and "PRD 7" in table


def test_bench_fastpath_on_300_items() -> None:
    out = pb.bench_fastpath(["Notes", "Slack"], pb.big_snapshot(300), repeat=2)
    assert out["items"] == 300
    assert out["calls"] == 2 * len(pb.FASTPATH_UTTERANCES)
    assert 0 < out["hits"] < out["calls"]  # some utterances match, the long one falls through
    assert out["ms"]["n"] == out["calls"]


class _FakePlanner:
    def __init__(self) -> None:
        self.last_stats: dict[str, float] = {}
        self.calls = 0

    def plan(self, text: str, snapshot: Any) -> None:
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("server down")
        self.last_stats = {
            "prompt_tokens": 1200.0,
            "cached_tokens": 1080.0,
            "completion_tokens": 20.0,
            "prompt_ms": 500.0,
            "decode_ms": 1000.0,
        }


def test_bench_planner_records_rows_errors_and_tokens() -> None:
    cases = pb.planner_cases()[:3]
    assert len(cases) == 3
    out = pb.bench_planner(_FakePlanner(), cases)
    assert len(out["rows"]) == 3 and out["errors"] == 1
    assert out["ms"]["n"] == 2
    assert out["tokens"]["cached"] == 1080.0 and out["tokens"]["decode_tps"] == 20.0


def test_planner_cases_and_snapshots_come_from_the_eval() -> None:
    cases = pb.planner_cases()
    assert cases and all("planner" in c.expect.routes for c in cases)
    snap = pb.case_snapshot(cases[0])
    assert snap.items
    assert pb.planner_cases(["no-such-case-*"]) == []


def test_bench_ttft_with_fake_stream() -> None:
    sent: list[dict[str, Any]] = []

    def fake_stream(url: str, body: bytes, timeout: float) -> Iterable[bytes]:
        sent.append(json.loads(body))
        return _sse({"choices": [{"delta": {"content": "{"}}]})

    out = pb.bench_ttft(Settings(bonsai_url="http://127.0.0.1:9/v1"), runs=3, open_stream=fake_stream)
    assert out["runs"] == 3 and out["ms"]["n"] == 3  # warm-up run discarded
    assert len(sent) == 4
    body = sent[0]
    assert body["stream"] is True and body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["cache_prompt"] is True and body["max_tokens"] == 8


def test_main_fast_path_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert pb.main(["--no-model", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "fast path, brain-side" in out
    files = list(tmp_path.glob("bench-*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["fastpath"]["items"] == 300 and data["bonsai_url"] is None
