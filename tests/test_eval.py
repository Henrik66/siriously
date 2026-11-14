"""Tests for the eval harness: case loading, pattern matching, scoring, runner (with stubs), report, CLI."""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

from brain.contracts import JSON, Action, Event, EventSink, Executor, Plan, Route, UISnapshot, Utterance
from brain.fixtures import FakeExecutor, FixtureUIProvider, fixture_names, load_fixture
from eval import demo_board, run_eval
from eval.cases import (
    ActionPattern,
    Case,
    CaseError,
    Expect,
    load_cases,
    normalize_text,
    parse_action_pattern,
    parse_cases,
    parse_step,
    select_cases,
)
from eval.demo_board import big, render_board
from eval.e2e import BrainFactory, run_case_e2e
from eval.report import (
    comparison_text,
    headline,
    latency_by_route,
    percentile,
    results_json,
    summarize,
    summary_text,
)
from eval.report import fmt_ms_text as fmt_ms
from eval.runner import CaseResult, Components, RouterLike, RunConfig, run_case, run_cases
from eval.scoring import (
    Outcome,
    Step,
    Verdict,
    action_matches,
    can_continue,
    reachable,
    score,
    sequence_matches,
    worst,
)

NOTES = load_fixture("notes")
NOTES0 = NOTES.snapshots["s-notes-0"]
NOTES1 = NOTES.snapshots["s-notes-1"]


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------


class StubRouter:
    def __init__(self, fn: object) -> None:
        self.fn = fn
        self.seen: list[tuple[str, str]] = []

    def route(self, text: str, snapshot: UISnapshot) -> Route:
        self.seen.append((text, snapshot.snapshot_id))
        assert callable(self.fn)
        result = self.fn(text, snapshot)
        assert isinstance(result, Route)
        return result


class StubPlanner:
    """Returns scripted action lists, one per call, planned on whatever snapshot it is given."""

    def __init__(self, bursts: Sequence[Sequence[Action]]) -> None:
        self.bursts = list(bursts)
        self.calls: list[tuple[str, str, str | None]] = []
        self.last_stats = {"ms": 12.0}

    def plan(
        self, text: str, snapshot: UISnapshot, *, hint: str | None = None, error: str | None = None
    ) -> Plan:
        self.calls.append((text, snapshot.snapshot_id, hint))
        actions = self.bursts.pop(0) if self.bursts else []
        return Plan(snapshot_id=snapshot.snapshot_id, actions=tuple(actions))


def comps_for(route_fn: object, planner: StubPlanner | None = None, **kw: object) -> Components:
    def make(seed: Sequence[JSON]) -> RouterLike:
        return StubRouter(route_fn)

    return Components(make_router=make, planner=planner, **kw)  # type: ignore[arg-type]


def planner_route(planner: StubPlanner) -> object:
    return lambda text, snap: Route(kind="planner", plan=planner.plan(text, snap))


def case(yaml_like: JSON) -> Case:
    return parse_cases([{"id": "t", "fixture": "notes", "utterance": "x", **yaml_like}])[0]


def step(action: Action, snap: UISnapshot = NOTES0, ok: bool = True) -> Step:
    return Step(action, snap, ok=ok)


# ---------------------------------------------------------------------------
# commands.yaml
# ---------------------------------------------------------------------------


def test_shipped_commands_load_with_required_mix() -> None:
    cases = load_cases()
    assert len(cases) >= 60
    cats = [c.category for c in cases]
    assert cats.count("control") >= 5
    assert cats.count("fastpath") >= 12
    assert cats.count("planner") >= 14
    assert cats.count("safety") >= 2
    assert cats.count("flow") >= 2
    assert all(c.expect.confirm or not c.verified for c in cases if c.category == "safety")
    holdout = [c for c in cases if c.split == "holdout"]
    assert len(holdout) >= 15 and {c.category for c in holdout} >= {"planner", "fastpath", "safety"}
    demo = {c.id for c in cases if "demo" in c.tags}
    assert {"demo-hijack", "demo-teach-replay", "pl-notes-standup-from-finder"} <= demo


def test_shipped_commands_reference_real_fixture_ids() -> None:
    """Every expected target must exist in a fixture the case can reach, or the case can never pass."""
    by_app = {load_fixture(n).app.name.casefold(): load_fixture(n) for n in fixture_names()}
    for c in load_cases():
        fx = load_fixture(c.fixture)
        assert c.state is None or c.state in fx.snapshots, c.id
        patterns = [p for seq in c.expect.actions or () for sp in seq for p in sp.alts]
        reached = [fx] + [by_app[a] for p in patterns if p.op == "open_app" for a in p.apps if a in by_app]
        all_ids = {i.id for f in reached for s in f.snapshots.values() for i in s.items}
        for p in patterns:
            assert set(p.targets) <= all_ids, (c.id, p.targets)


def test_parse_pattern_normalizes() -> None:
    p = parse_action_pattern({"op": "keys", "keys": ["Command+Shift+N", "enter"]}, "ctx")
    assert p.keys == ("shift+cmd+n", "return")
    t = parse_action_pattern({"op": "type", "text": "I’m Late!", "target": 7}, "ctx")
    assert t.texts == ("i'm late",) and t.targets == (7,)
    sp = parse_step(
        {"any": [{"op": "menu", "target": 1}, {"op": "press", "target": 2}], "optional": True}, "c"
    )
    assert sp.optional and len(sp.alts) == 2


@pytest.mark.parametrize(
    ("data", "needle"),
    [
        ([{"id": "a", "fixture": "f", "utterance": "u", "expect": {"route": "teleport"}}], "unknown route"),
        (
            [{"id": "a", "fixture": "f", "utterance": "u", "expect": {"actions": [[{"op": "fly"}]]}}],
            "unknown op",
        ),
        (
            [{"id": "a", "fixture": "f", "utterance": "u", "expect": {"actions": [{"op": "keys"}]}}],
            "list of seq",
        ),
        ([{"id": "a", "fixture": "f", "utterance": "u", "expect": {"control": "maybe"}}], "control word"),
        ([{"id": "a", "fixture": "f", "expect": {}}], "utterance"),
        ([{"fixture": "f", "utterance": "u", "expect": {}}], "missing id"),
        ([{"id": "a", "fixture": "f", "utterance": "u"}], "missing expect"),
        ([{"id": "a", "fixture": "f", "utterance": "u", "expect": {}}] * 2, "duplicate"),
        ({"nope": 1}, "list of cases"),
        (
            [
                {
                    "id": "a",
                    "fixture": "f",
                    "utterance": "u",
                    "expect": {"actions": [[{"op": "keys", "zap": 1}]]},
                }
            ],
            "zap",
        ),
        ([{"id": "a", "fixture": "f", "utterance": "u", "expect": {"actions": [[{"any": []}]]}}], "any"),
    ],
)
def test_parse_errors(data: object, needle: str) -> None:
    with pytest.raises(CaseError, match=needle):
        parse_cases(data)


def test_load_cases_reports_bad_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "c.yaml"
    bad.write_text("- id: [unclosed", encoding="utf-8")
    with pytest.raises(CaseError):
        load_cases(bad)


def test_select_cases() -> None:
    cases = load_cases()
    assert [c.id for c in select_cases(cases, only=["ct-stop"])] == ["ct-stop"]
    assert all(c.id.startswith("fp-") for c in select_cases(cases, only=["fp-*"]))
    assert {c.category for c in select_cases(cases, routes=["control"])} == {"control"}
    safety = select_cases(cases, routes=["safety"])
    assert safety and all(c.category == "safety" for c in safety)


def test_normalize_text() -> None:
    assert normalize_text("  I’m  Running Late. ") == "i'm running late"
    assert normalize_text("Standup") == normalize_text("standup")


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def test_menu_pattern_accepts_its_shortcut_and_press() -> None:
    menu = parse_action_pattern({"op": "menu", "target": 110}, "c")  # File > New Note, cmd+n
    assert action_matches(menu, step(Action.menu(110)))
    assert action_matches(menu, step(Action.key("Command+N")))
    assert action_matches(menu, step(Action.press(110)))
    assert not action_matches(menu, step(Action.key("cmd+t")))
    assert not action_matches(menu, step(Action.menu(111)))


def test_keys_pattern_accepts_menu_with_that_shortcut() -> None:
    keys = parse_action_pattern({"op": "keys", "keys": "cmd+n"}, "c")
    assert action_matches(keys, step(Action.menu(110)))
    assert not action_matches(keys, step(Action.menu(111)))
    assert not action_matches(keys, step(Action.press(1)))  # element, no shortcut


def test_type_pattern() -> None:
    typ = parse_action_pattern({"op": "type", "text": "standup", "target": 30}, "c")
    assert action_matches(typ, step(Action.type_text("Standup", target=30), NOTES1))
    assert action_matches(typ, step(Action.type_text("standup."), NOTES1))  # no target: focused 30
    assert not action_matches(typ, step(Action.type_text("standup"), NOTES0))  # focus is 20 there
    assert not action_matches(typ, step(Action.type_text("stand up", target=30), NOTES1))


def test_other_fields() -> None:
    app = parse_action_pattern({"op": "open_app", "app": "System Settings"}, "c")
    assert action_matches(app, step(Action.open_app("system settings")))
    assert not action_matches(app, step(Action.open_app("Settings")))
    scroll = parse_action_pattern({"op": "scroll", "direction": "down"}, "c")
    assert action_matches(scroll, step(Action.scroll("down", 15)))
    assert not action_matches(scroll, step(Action.scroll("up")))


SEQ = (
    parse_step({"op": "open_app", "app": "Notes", "optional": True}, "c"),
    parse_step({"any": [{"op": "menu", "target": 110}, {"op": "press", "target": 1}]}, "c"),
    parse_step({"op": "type", "text": "standup"}, "c"),
    parse_step({"op": "keys", "keys": "return", "optional": True}, "c"),
)


@pytest.mark.parametrize(
    ("actions", "full", "more"),
    [
        ([Action.menu(110), Action.type_text("standup")], True, False),
        (
            [Action.open_app("Notes"), Action.press(1), Action.type_text("standup"), Action.key("return")],
            True,
            False,
        ),
        ([Action.key("cmd+n"), Action.wait(100), Action.type_text("Standup")], True, False),
        ([Action.open_app("Notes")], False, True),
        ([Action.menu(110)], False, True),
        ([Action.menu(111)], False, False),
        (
            [Action.menu(110), Action.type_text("standup"), Action.key("return"), Action.key("return")],
            False,
            False,
        ),
        ([], False, True),
    ],
)
def test_sequence_matching(actions: list[Action], full: bool, more: bool) -> None:
    steps = [step(a) for a in actions]
    assert sequence_matches(SEQ, steps) is full
    assert can_continue([SEQ], steps) is more


def test_reachable_handles_optional_tail() -> None:
    assert 4 in reachable(SEQ, [step(Action.menu(110)), step(Action.type_text("standup"))])


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_score_pass_and_failures() -> None:
    c = case({"expect": {"route": "planner", "confirm": True, "actions": [[{"op": "menu", "target": 137}]]}})
    good = Outcome(route=Route(kind="planner"), steps=(step(Action.menu(137)),), confirm=True)
    assert score(c, good) == Verdict("PASS")
    no_confirm = score(c, Outcome(route=Route(kind="planner"), steps=good.steps))
    assert no_confirm.status == "FAIL" and "confirm" in no_confirm.reasons[0]
    wrong_route = score(c, Outcome(route=Route(kind="fastpath"), steps=good.steps, confirm=True))
    assert "route" in wrong_route.reasons[0]
    failed = Outcome(route=Route(kind="planner"), steps=(step(Action.menu(137), ok=False),), confirm=True)
    assert any("action failed" in r for r in score(c, failed).reasons)


def test_score_control_flow_and_special_statuses() -> None:
    c = case({"expect": {"route": "control", "control": "undo"}})
    assert score(c, Outcome(route=Route(kind="control", control="undo"))).status == "PASS"
    assert score(c, Outcome(route=Route(kind="control", control="stop"))).status == "FAIL"
    t = case({"expect": {"route": "flow", "teach": True, "flow": "Standup"}})
    assert score(t, Outcome(route=Route(kind="flow", teach=True, flow="standup"))).status == "PASS"
    assert score(t, Outcome(route=None, skipped="no model")).status == "SKIP"
    assert score(t, Outcome(route=None, error="boom")).status == "ERROR"
    assert (
        score(t, Outcome(route=Route(kind="flow", teach=True, flow="standup"), problems=("x",))).status
        == "FAIL"
    )


def test_worst() -> None:
    assert worst(["PASS", "PASS"]) == "PASS"
    assert worst(["PASS", "FAIL", "SKIP"]) == "FAIL"
    assert worst(["ERROR", "FAIL"]) == "ERROR"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


STANDUP = {
    "fixture": "finder",
    "utterance": "open notes and start a note called standup",
    "expect": {
        "route": "planner",
        "actions": [
            [
                {"op": "open_app", "app": "Notes"},
                {"op": "menu", "target": 110},
                {"op": "type", "text": "standup"},
            ]
        ],
    },
}


def test_fastpath_case_passes_with_stub() -> None:
    c = case(
        {
            "utterance": "new note",
            "expect": {"route": "fastpath", "actions": [[{"op": "menu", "target": 110}]]},
        }
    )
    route = Route(kind="fastpath", plan=Plan(snapshot_id="s-notes-0", actions=(Action.key("cmd+n"),)))
    out = run_case(c, comps_for(lambda t, s: route))
    assert score(c, out).status == "PASS"
    assert out.bursts == 1 and out.steps[0].before.snapshot_id == "s-notes-0"


def test_planner_continues_across_screens() -> None:
    planner = StubPlanner([[Action.open_app("Notes")], [Action.menu(110), Action.type_text("standup")]])
    c = case(STANDUP)
    out = run_case(c, comps_for(planner_route(planner), planner))
    assert score(c, out).status == "PASS", score(c, out).reasons
    assert out.bursts == 2
    assert planner.calls[1][1] == "s-notes-0" and "open_app(Notes)" in (planner.calls[1][2] or "")
    assert out.debug and "ms=12" in out.debug[0]


def test_continuation_respects_burst_limit_and_stops_when_done() -> None:
    planner = StubPlanner([[Action.open_app("Notes")], [Action.menu(110), Action.type_text("standup")]])
    out = run_case(case(STANDUP), comps_for(planner_route(planner), planner), RunConfig(max_bursts=1))
    assert out.bursts == 1 and len(planner.calls) == 1
    done = StubPlanner([[Action.open_app("Notes"), Action.key("cmd+n"), Action.type_text("standup")]])
    out2 = run_case(case(STANDUP), comps_for(planner_route(done), done))
    assert out2.bursts == 1 and len(done.calls) == 1 and score(case(STANDUP), out2).status == "PASS"


def test_stale_ids_after_open_app_fail_the_case() -> None:
    # Planned on Finder's snapshot: menu 110 is Finder's File > New Finder Window, stale once Notes is up.
    bad = StubPlanner([[Action.open_app("Notes"), Action.menu(110), Action.type_text("standup")]])
    out = run_case(case(STANDUP), comps_for(planner_route(bad), bad))
    verdict = score(case(STANDUP), out)
    assert verdict.status == "FAIL" and any("stale" in r for r in verdict.reasons)


def test_confirm_comes_from_safety_fn() -> None:
    c = case({"expect": {"confirm": True}})
    route = Route(kind="planner", plan=Plan(snapshot_id="s-notes-0", actions=(Action.menu(137),)))
    out = run_case(c, comps_for(lambda t, s: route, needs_confirm=lambda p, s: True))
    assert out.confirm and score(c, out).status == "PASS"


def test_no_model_skips_planner_cases_but_not_others() -> None:
    none = Route(kind="none", reason="no planner")
    assert run_case(case(STANDUP), comps_for(lambda t, s: none)).skipped
    fp = case({"expect": {"route": "fastpath"}})
    assert score(fp, run_case(fp, comps_for(lambda t, s: none))).status == "FAIL"


def test_planner_error_and_router_crash_are_errors() -> None:
    err = Route(kind="none", reason="planner error: timeout")
    planner = StubPlanner([])
    assert (
        score(case(STANDUP), run_case(case(STANDUP), comps_for(lambda t, s: err, planner))).status == "ERROR"
    )

    def boom(text: str, snap: UISnapshot) -> Route:
        raise RuntimeError("kaput")

    out = run_case(case(STANDUP), comps_for(boom, planner))
    assert out.error and "kaput" in out.error


FLOW = {
    "utterance": "standup",
    "setup": {"flows": [{"name": "standup", "steps": [{"action": {"op": "menu", "target": 110}}]}]},
    "expect": {"route": "flow", "flow": "standup", "actions": [[{"op": "menu", "target": 110}]]},
}


def test_flow_replay_uses_resolver() -> None:
    c = case(FLOW)
    resolved: list[str] = []

    def resolve(raw: JSON, snap: UISnapshot) -> Action | None:
        resolved.append(snap.snapshot_id)
        return Action.from_dict(raw["action"])

    out = run_case(c, comps_for(lambda t, s: Route(kind="flow", flow="standup"), resolve_step=resolve))
    assert score(c, out).status == "PASS" and resolved == ["s-notes-0"]


def test_flow_replay_problems() -> None:
    c = case(FLOW)
    unresolved = run_case(
        c, comps_for(lambda t, s: Route(kind="flow", flow="standup"), resolve_step=lambda r, s: None)
    )
    assert any("did not resolve" in p for p in unresolved.problems)
    no_resolver = run_case(c, comps_for(lambda t, s: Route(kind="flow", flow="standup")))
    assert any("unavailable" in p for p in no_resolver.problems)
    unknown = run_case(
        c, comps_for(lambda t, s: Route(kind="flow", flow="other"), resolve_step=lambda r, s: None)
    )
    assert any("did not seed" in p for p in unknown.problems)


def test_run_cases_repeat_and_callback() -> None:
    c = case({"expect": {"route": "control", "control": "stop"}})
    seen: list[CaseResult] = []
    comps = comps_for(lambda t, s: Route(kind="control", control="stop"))
    results = run_cases([c], lambda k: run_case(k, comps), repeat=3, on_result=seen.append)
    assert seen == results and results[0].passes == 3 and results[0].status == "PASS"
    assert results[0].mode == "router"


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _result(
    status: str, kind: str, ms: float, category: str = "planner", mode: str = "router", split: str = "dev"
) -> CaseResult:
    c = Case(
        id=f"{status}-{ms}", fixture="notes", utterance="u", expect=Expect(), category=category, split=split
    )
    out = Outcome(route=Route(kind=kind), ms=ms, first_action_ms=ms / 2)  # type: ignore[arg-type]
    return CaseResult(c, ((out, Verdict(status)),), mode)  # type: ignore[arg-type]


def test_percentile() -> None:
    assert percentile([], 90) == 0.0
    assert percentile([5.0], 90) == 5.0
    assert percentile([float(i) for i in range(1, 11)], 90) == 9.0
    assert percentile([float(i) for i in range(1, 11)], 50) == 5.0


def test_summary_rates_and_latency() -> None:
    results = [
        _result("PASS", "fastpath", 1.0, "fastpath"),
        _result("PASS", "fastpath", 3.0, "fastpath"),
        _result("FAIL", "planner", 900.0),
        _result("SKIP", "none", 0.5),
    ]
    s = summarize(results)
    assert (s.total, s.passed, s.failed, s.skipped) == (4, 2, 1, 1)
    assert s.rate_all == 0.5 and s.rate_run == pytest.approx(2 / 3)
    lat = latency_by_route(results)
    assert lat["fastpath"]["median_ms"] == 2.0 and lat["planner"]["n"] == 1
    assert "none" not in lat  # skipped runs don't count toward latency
    text = summary_text(results, "stub")
    assert "2/4 = 50.0%" in text and "2/3 = 66.7%" in text and "fastpath" in text
    js = results_json({"router": results}, {"k": "v"})
    summary = js["modes"]["router"]["summary"]
    assert summary["pass_rate_excluding_skip"] == pytest.approx(0.6667)
    assert (
        summary["by_category"]["fastpath"]["pass"] == 2 and summary["by_category"]["fastpath"]["total"] == 2
    )
    assert summary["by_split"]["dev"]["total"] == 4
    json.dumps(js)  # serializable


def test_e2e_latency_uses_first_action_and_comparison() -> None:
    router = [_result("PASS", "fastpath", 10.0), _result("FAIL", "planner", 900.0, split="holdout")]
    e2e = [_result("PASS", "fastpath", 10.0, mode="e2e"), _result("PASS", "planner", 900.0, mode="e2e")]
    assert latency_by_route(e2e)["planner"]["median_ms"] == 450.0  # first_action_ms
    assert "end of speech -> first action" in summary_text(e2e, "stub", "e2e")
    assert "holdout 0/1" in summary_text(router, "stub")
    text = comparison_text({"router": router, "e2e": e2e})
    assert "router  all 1/2 = 50.0%" in text and "e2e     all 2/2 = 100.0%" in text
    assert comparison_text({"router": router}) == ""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_with_stub_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_build(apps: Sequence[str], opts: object) -> tuple[Components, str]:
        return comps_for(lambda t, s: Route(kind="control", control="stop")), "stub planner"

    monkeypatch.setattr(run_eval, "build_components", fake_build)
    code = run_eval.main(["--only", "ct-stop,ct-cancel", "--out", str(tmp_path), "--min-pass", "0.4"])
    out = capsys.readouterr().out
    assert code == 0
    assert "PASS  ct-stop" in out and "FAIL  ct-cancel" in out and "1/2 = 50.0%" in out
    [written] = list(tmp_path.glob("*.json"))
    data = json.loads(written.read_text(encoding="utf-8"))
    assert data["modes"]["router"]["summary"]["total"] == 2 and data["meta"]["planner"] == "stub planner"
    assert run_eval.main(["--only", "ct-stop,ct-cancel", "--no-write", "--min-pass", "0.9"]) == 1


def test_cli_rejects_empty_selection_and_bad_cases(tmp_path: Path) -> None:
    assert run_eval.main(["--only", "no-such-case", "--no-write", "--no-model"]) == 2
    bad = tmp_path / "bad.yaml"
    bad.write_text("{}", encoding="utf-8")
    assert run_eval.main(["--cases", str(bad), "--no-write", "--no-model"]) == 2


def test_cli_smoke_with_real_router_no_model(capsys: pytest.CaptureFixture[str]) -> None:
    """End to end with the real router/fast path, offline: control cases must route correctly."""
    pytest.importorskip("brain.router")
    assert run_eval.main(["--no-model", "--no-write", "--filter", "control"]) == 0
    out = capsys.readouterr().out
    assert "planner: disabled (--no-model)" in out and "pass rate" in out


# ---------------------------------------------------------------------------
# Review regressions + self-consistency of commands.yaml
# ---------------------------------------------------------------------------


def _concrete(p: ActionPattern, snap: UISnapshot) -> Action:
    """A concrete action satisfying ``p`` (first alternative; for type, a listed field on screen)."""
    if p.op in ("menu", "press"):
        return Action(op=p.op, target=p.targets[0])  # type: ignore[arg-type]
    if p.op == "type":
        target = next((t for t in p.targets if snap.item(t) is not None), None)
        return Action.type_text(p.texts[0], target=target)
    if p.op == "keys":
        return Action.key(p.keys[0])
    if p.op == "open_app":
        return Action.open_app(p.apps[0])
    if p.op == "scroll":
        return Action.scroll("up" if p.direction == "up" else "down")
    return Action.wait(0)


@pytest.mark.parametrize("c", [c for c in load_cases() if c.expect.actions], ids=lambda c: c.id)
def test_every_expected_sequence_runs_on_its_fixture(c: Case) -> None:
    """Each alternative of each expected sequence (optional steps included) must execute ok and PASS."""
    for seq in c.expect.actions or ():
        for alt_idx in range(max(len(sp.alts) for sp in seq)):
            provider = FixtureUIProvider(c.fixture, state=c.state)
            ex = FakeExecutor(provider)
            steps: list[Step] = []
            for sp in seq:
                before = provider.snapshot()
                action = _concrete(sp.alts[min(alt_idx, len(sp.alts) - 1)], before)
                res = ex.run(before.snapshot_id, action)
                steps.append(Step(action, before, res.ok, res.changed, res.error))
            assert all(s.ok for s in steps), (c.id, [(s.describe(), s.error) for s in steps])
            assert sequence_matches(seq, steps), (c.id, [s.describe() for s in steps])


def test_every_expected_type_names_its_field() -> None:
    for c in load_cases():
        for seq in c.expect.actions or ():
            for sp in seq:
                assert all(p.targets for p in sp.alts if p.op == "type"), c.id


def test_typing_into_the_wrong_field_fails() -> None:
    slack = load_fixture("slack")
    dm = slack.snapshots["s-slack-1"]
    typ = parse_action_pattern({"op": "type", "text": "late", "target": 20}, "c")
    assert action_matches(typ, step(Action.type_text("late"), dm))  # focus is the composer
    assert not action_matches(typ, step(Action.type_text("late", target=4), dm))  # Search field
    loose = parse_action_pattern({"op": "type", "text": "late"}, "c")
    nothing_focused = load_fixture("safari").snapshots["s-safari-0"]
    assert not action_matches(loose, step(Action.type_text("late"), nothing_focused))


def test_disabled_menu_shortcut_does_not_match() -> None:
    finder0 = load_fixture("finder").snapshots["s-finder-0"]  # Move to Trash (120) disabled
    menu = parse_action_pattern({"op": "menu", "target": 120}, "c")
    assert not action_matches(menu, step(Action.key("cmd+delete"), finder0))


def test_open_app_dot_app_matches() -> None:
    app = parse_action_pattern({"op": "open_app", "app": "Notes"}, "c")
    assert action_matches(app, step(Action.open_app("Notes.app")))


@pytest.mark.parametrize(
    ("pattern", "needle"),
    [
        ({"any": [{"op": "menu", "target": 1, "optional": True}]}, "optional"),
        ({"op": "menu", "target": []}, "empty target"),
        ({"any": [{"any": [{"op": "menu", "target": 1}]}]}, "step level"),
    ],
)
def test_stricter_step_validation(pattern: JSON, needle: str) -> None:
    with pytest.raises(CaseError, match=needle):
        parse_step(pattern, "ctx")


def test_broken_case_setup_is_an_error_not_a_crash() -> None:
    ok = Route(kind="control", control="stop")
    bad_state = case({"state": "s-notes-9", "expect": {"route": "control"}})
    out = run_case(bad_state, comps_for(lambda t, s: ok))
    assert score(bad_state, out).status == "ERROR" and "setup" in (out.error or "")

    def broken_router(seed: Sequence[JSON]) -> RouterLike:
        raise ValueError("bad flow")

    out2 = run_case(case({"expect": {}}), Components(make_router=broken_router))
    assert out2.error and "bad flow" in out2.error


# ---------------------------------------------------------------------------
# W1b: case format extras, e2e mode
# ---------------------------------------------------------------------------


def test_case_meta_fields() -> None:
    c = case(
        {
            "split": "holdout",
            "modes": ["e2e"],
            "before": ["new note"],
            "speaker": {"verified": False},
            "needs_model": True,
            "expect": {"route": "rejected", "state": "s-notes-0", "app": "Notes", "e2e_actions": [[]]},
        }
    )
    meta = (c.split, c.modes, c.before, c.verified, c.needs_model)
    assert meta == ("holdout", ("e2e",), ("new note",), False, True)
    assert c.expect.state == "s-notes-0" and c.expect.app == "Notes"
    assert c.actions_for("e2e") == ((),) and c.actions_for("router") is None
    plain = case({"expect": {}})
    assert (plain.split, plain.modes, plain.verified) == ("dev", ("router", "e2e"), True)


@pytest.mark.parametrize(
    ("extra", "needle"),
    [({"split": "test"}, "split"), ({"modes": ["fast"]}, "modes"), ({"speaker": "nope"}, "speaker")],
)
def test_case_meta_errors(extra: JSON, needle: str) -> None:
    with pytest.raises(CaseError, match=needle):
        case({**extra, "expect": {}})


def test_select_by_split_mode_and_tag() -> None:
    cases = load_cases()
    hold = select_cases(cases, split="holdout")
    assert hold and all(c.split == "holdout" for c in hold)
    assert all(c.split == "dev" for c in select_cases(cases, split="dev"))
    assert "demo-hijack" not in {c.id for c in select_cases(cases, mode="router")}
    assert "demo-hijack" in {c.id for c in select_cases(cases, routes=["demo"])}


def test_reference_end_states_cover_optional_steps() -> None:
    from eval.e2e import reference_end_states

    c = next(c for c in load_cases() if c.id == "pl-finder-folder")
    ends = reference_end_states(c, c.actions_for("e2e") or ())
    assert ends == {("Finder", "s-finder-2"), ("Finder", "s-finder-0")}


def test_effective_drops_failed_and_no_effect_steps() -> None:
    from eval.e2e import effective

    steps = [
        Step(Action.menu(110), NOTES0, ok=True, changed=True),
        Step(Action.press(999), NOTES0, ok=False),
        Step(Action.menu(111), NOTES0, ok=True, changed=False),  # expected a change, got none
        Step(Action.menu(133), NOTES0, ok=True, changed=False),  # Edit > Copy: no visible effect is fine
        Step(Action.type_text("x"), NOTES0, ok=True, changed=True),
    ]
    assert [s.action for s in effective(steps)] == [Action.menu(110), Action.menu(133), Action.type_text("x")]


Script = dict[str, list[tuple[str, Action | None]]]


class ScriptedBrain:
    """Stub brain: emits scripted events per utterance and runs scripted actions on the executor."""

    def __init__(self, sink: EventSink, executor: Executor, script: Script) -> None:
        self.sink, self.executor, self.script = sink, executor, script

    def handle(self, utt: Utterance) -> Route:
        for etype, action in self.script.get(utt.text, []):
            if action is not None:
                self.executor.run("s-notes-0", action)
            data: JSON = {"kind": "planner"} if etype == "route" else {}
            self.sink.emit(Event(type=etype, utterance_id=utt.id, data=data))  # type: ignore[arg-type]
        return Route(kind="planner") if utt.speaker.verified else Route(kind="rejected")


def scripted(script: Script) -> BrainFactory:
    def factory(
        provider: FixtureUIProvider, executor: Executor, sink: EventSink, path: Path, seed: Sequence[JSON]
    ) -> ScriptedBrain:
        return ScriptedBrain(sink, executor, script)

    return factory


GOOD: list[tuple[str, Action | None]] = [
    ("heard", None),
    ("route", None),
    ("plan", None),
    ("action", Action.menu(110)),
    ("done", None),
]


def test_e2e_scoring_with_scripted_brain() -> None:
    c = case({"utterance": "x", "expect": {"route": "planner", "actions": [[{"op": "menu", "target": 110}]]}})
    out = run_case_e2e(c, scripted({"x": GOOD}), has_planner=True)
    assert score(c, out, "e2e").status == "PASS", score(c, out, "e2e").reasons
    assert out.first_action_ms is not None and out.events == ("heard", "route", "plan", "action", "done")
    no_done = run_case_e2e(c, scripted({"x": GOOD[:-1]}), has_planner=True)
    assert any("did not finish" in r for r in score(c, no_done, "e2e").reasons)
    no_plan = run_case_e2e(c, scripted({"x": [e for e in GOOD if e[0] != "plan"]}), has_planner=True)
    assert any("before any plan" in r for r in score(c, no_plan, "e2e").reasons)


def test_e2e_confirm_checks() -> None:
    c = case({"utterance": "x", "expect": {"route": "planner", "confirm": True}})
    acted_first = [*GOOD[:4], ("confirm", None)]
    out = run_case_e2e(c, scripted({"x": acted_first, "yes": [("done", None)]}), True)
    assert any("before asking" in r for r in score(c, out, "e2e").reasons)
    asks = [*GOOD[:3], ("confirm", None)]
    ok = run_case_e2e(c, scripted({"x": asks, "yes": [("action", Action.menu(137)), ("done", None)]}), True)
    assert score(c, ok, "e2e").status == "PASS", score(c, ok, "e2e").reasons
    assert [s.action for s in ok.steps] == [Action.menu(137)]


def test_e2e_unverified_and_skip() -> None:
    c = case(
        {
            "utterance": "x",
            "speaker": {"verified": False},
            "expect": {"route": "rejected", "e2e_actions": [[]]},
        }
    )
    clean = run_case_e2e(c, scripted({"x": [("rejected", None)]}), True)
    assert score(c, clean, "e2e").status == "PASS"
    leaky = run_case_e2e(c, scripted({"x": [("rejected", None), ("action", Action.menu(110))]}), True)
    reasons = score(c, leaky, "e2e").reasons
    assert any("only 'rejected'" in r for r in reasons) and any("changed the screen" in r for r in reasons)
    model = case({"utterance": "x", "needs_model": True, "expect": {}})
    assert run_case_e2e(model, scripted({}), has_planner=False).skipped


REAL_E2E = ["ct-undo", "ct-yes", "ct-no", "demo-hijack", "fl-teach-replay-fast", "sf-quit-slack", "fp-paste"]


@pytest.mark.parametrize("cid", REAL_E2E)
def test_e2e_real_pipeline_no_model(cid: str) -> None:
    """The real pipeline, offline: control words, confirm, voice lock and teach/replay work end to end."""
    pytest.importorskip("brain.pipeline")
    from eval.e2e import real_brain_factory

    c = next(c for c in load_cases() if c.id == cid)
    out = run_case_e2e(c, real_brain_factory(None), has_planner=False)
    verdict = score(c, out, "e2e")
    assert verdict.status == "PASS", (verdict.reasons, out.debug)


def test_cli_e2e_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pytest.importorskip("brain.pipeline")
    code = run_eval.main(["--e2e", "--no-model", "--only", "ct-undo,demo-hijack", "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert (
        code == 0 and "== router: 1 cases" in out and "== e2e: 2 cases" in out and "router-only vs e2e" in out
    )
    [written] = list(tmp_path.glob("*.json"))
    data = json.loads(written.read_text(encoding="utf-8"))
    assert set(data["modes"]) == {"router", "e2e"} and data["meta"]["modes"] == ["router", "e2e"]
    assert data["modes"]["e2e"]["cases"][1]["runs"][0]["events"] == ["rejected"]


# ---------------------------------------------------------------------------
# W1c: one-line JSON summary, demo board
# ---------------------------------------------------------------------------


def _results_data() -> JSON:
    router = [_result("PASS", "fastpath", 10.0), _result("FAIL", "planner", 900.0, split="holdout")]
    e2e = [_result("PASS", "fastpath", 10.0, mode="e2e"), _result("SKIP", "none", 1.0, mode="e2e")]
    return results_json(
        {"router": router, "e2e": e2e}, {"timestamp": "2026-11-14T13:00:00", "planner": "stub"}
    )


def test_headline() -> None:
    head = headline(_results_data(), "r.json")
    assert head["results"] == "r.json" and head["planner"] == "stub"
    r, e = head["modes"]["router"], head["modes"]["e2e"]
    assert (r["pass"], r["total"], r["rate"]) == (1, 2, 0.5)
    assert r["split"]["holdout"] == {"pass": 0, "ran": 1, "rate": 0.0}
    assert (e["rate"], e["rate_ran"], e["skip"]) == (0.5, 1.0, 1)  # skips count against `rate`
    assert e["latency"]["fastpath"]["median_ms"] == 5.0 and "none" not in e["latency"]
    assert e["latency_label"] == "end of speech -> first action"
    with pytest.raises(ValueError):
        headline({"nope": 1})


def test_big_digits_and_formatting() -> None:
    rows = big("85.0%", ascii_only=True)
    assert len(rows) == 5 and all(set(r) <= {"#", " "} for r in rows)
    assert "█" in "".join(big("1"))
    assert fmt_ms(0.42) == "0.4 ms" and fmt_ms(412.0) == "412 ms" and fmt_ms(4210.0) == "4.2 s"


def test_render_board() -> None:
    head = headline(_results_data())
    board = render_board(head, ascii_only=True)
    assert "end to end" in board and "1 / 2 commands pass" in board and "(1 skipped)" in board
    assert "router: 50.0%" in board and "e2e: 50.0%" in board and "fastpath" in board
    router = render_board(head, "router", color=True)
    assert "holdout 0/1 (0.0%)" in router and "\x1b[" in router
    with pytest.raises(ValueError, match="not in results"):
        render_board({"modes": {"router": head["modes"]["router"]}}, "e2e")


def test_demo_board_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert demo_board.main(["--dir", str(tmp_path)]) == 2  # no results yet
    (tmp_path / "20260101-000000.json").write_text(json.dumps(_results_data()), encoding="utf-8")
    newest = tmp_path / "20260102-000000.json"
    newest.write_text(json.dumps(_results_data()), encoding="utf-8")
    assert demo_board.latest_results(tmp_path) == newest
    capsys.readouterr()
    assert demo_board.main(["--dir", str(tmp_path), "--ascii", "--color", "never"]) == 0
    assert "commands pass" in capsys.readouterr().out
    assert demo_board.main(["--file", str(newest), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["modes"]["router"]["pass"] == 1
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    assert demo_board.main(["--file", str(bad)]) == 2


def test_cli_json_summary_is_one_stdout_line(capsys: pytest.CaptureFixture[str]) -> None:
    pytest.importorskip("brain.router")
    assert run_eval.main(["--no-model", "--no-write", "--only", "ct-stop,fp-new-note", "--json-summary"]) == 0
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert len(lines) == 1
    summary = json.loads(lines[0])
    assert summary["modes"]["router"]["pass"] == 2 and "results" not in summary
    assert "PASS  ct-stop" in captured.err  # human output moved to stderr


# ---------------------------------------------------------------------------
# W1d: demo rehearsal
# ---------------------------------------------------------------------------


class DeletePlanner:
    """Stub planner: only knows how to delete the current note (Edit > Delete); nothing else."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def plan(
        self, text: str, snapshot: UISnapshot, *, hint: str | None = None, error: str | None = None
    ) -> Plan:
        self.calls.append(text)
        item = next((i for i in snapshot.menus if i.path == ("Edit", "Delete")), None)
        if "delete" in text.lower() and item is not None:
            return Plan(snapshot_id=snapshot.snapshot_id, actions=(Action.menu(item.id),), say="Delete note")
        return Plan(snapshot_id=snapshot.snapshot_id, actions=())


def test_rehearsal_full_demo_with_stub_planner() -> None:
    from eval.rehearse import FixtureStage, exit_code, rehearse, results_json, timeline

    stage = FixtureStage(DeletePlanner())
    try:
        results = rehearse(stage)
    finally:
        stage.close()
    assert [r.status for r in results] == ["PASS"] * 7, [(r.step.key, r.problems) for r in results]
    assert exit_code(results) == 0
    text = timeline(results, "fixtures", "stub")
    assert "READY: 7 pass, 0 fail, 0 skip" in text and "[other voice]" in text
    data = results_json(results)
    assert data["steps"][1]["events"] == ["rejected"] and data["steps"][5]["events"][-1] == "confirm"


def test_rehearsal_without_model_skips_model_steps() -> None:
    from eval.rehearse import FixtureStage, exit_code, rehearse

    stage = FixtureStage(None)
    try:
        results = rehearse(stage, has_planner=False)
    finally:
        stage.close()
    by_key = {r.step.key: r.status for r in results}
    assert by_key["danger"] == "SKIP" and by_key["confirm"] == "SKIP" and by_key["replay"] == "PASS"
    assert exit_code(results) == 2


def test_rehearsal_check_failures() -> None:
    from eval.rehearse import DEMO, Heard, check

    hijack = next(s for s in DEMO if s.key == "hijack")
    leaked = Heard("rejected", "", (("rejected", {}), ("action", {"ok": True})), 1.0, 2.0)
    problems = check(hijack, leaked, None)
    assert any("only 'rejected'" in p for p in problems) and any("none should" in p for p in problems)
    teach = next(s for s in DEMO if s.key == "teach")
    wrong = Heard("flow", "", (("heard", {}), ("route", {}), ("done", {"saved": "other"})), None, 1.0)
    assert any("not saved" in p for p in check(teach, wrong, None))
    task = next(s for s in DEMO if s.key == "task")
    stuck = Heard("planner", "", (("heard", {}), ("route", {}), ("error", {"message": "boom"})), None, 1.0)
    problems = check(task, stuck, ("Finder", "s-finder-0"))
    assert any("ended with error" in p for p in problems) and any(
        "frontmost app Finder" in p for p in problems
    )


class _FakeServer:
    """Minimal stand-in for brain.server on loopback: /health, /utterance?wait=true, /events/history."""

    def __init__(self) -> None:
        import http.server

        self.tokens: list[str | None] = []
        self.events: list[JSON] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return None

            def _send(self, body: JSON) -> None:
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                outer.tokens.append(self.headers.get("X-Siriously-Token"))
                if self.path.startswith("/health"):
                    self._send({"ok": True, "bonsai": False})
                else:
                    self._send({"count": len(outer.events), "events": outer.events})

            def do_POST(self) -> None:
                outer.tokens.append(self.headers.get("X-Siriously-Token"))
                utt = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                uid = utt["id"]
                if not utt["speaker"]["verified"]:
                    outer.events.append(
                        {"type": "rejected", "utterance_id": uid, "data": {}, "ms_since_speech_end": 3}
                    )
                    self._send({"kind": "rejected", "reason": "unverified speaker"})
                    return
                for etype, ms in (("heard", 1), ("route", 2), ("plan", 3), ("action", 40), ("done", 45)):
                    data: JSON = {"ok": True} if etype == "action" else {}
                    outer.events.append(
                        {"type": etype, "utterance_id": uid, "data": data, "ms_since_speech_end": ms}
                    )
                self._send({"kind": "fastpath", "reason": ""})

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def test_rehearsal_live_against_fake_server(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from eval import rehearse as rh

    server = _FakeServer()
    monkeypatch.setenv("SIRIOUSLY_TOKEN", "s3cret")
    try:
        code = rh.main(["--live", "--url", server.url, "--only", "hijack", "--only", "away", "--json"])
    finally:
        server.close()
    data = json.loads(capsys.readouterr().out)
    assert code == 0, data
    hijack, away = data["steps"]
    assert hijack["events"] == ["rejected"] and away["first_ms"] == 40.0
    assert server.tokens and set(server.tokens) == {"s3cret"}


def test_rehearsal_live_unreachable(capsys: pytest.CaptureFixture[str]) -> None:
    from eval import rehearse as rh

    with socket.socket() as s:  # grab a free port, then close it so nothing listens there
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert rh.main(["--live", "--url", f"http://127.0.0.1:{port}", "--timeout", "2"]) == 1
    assert "cannot reach the brain server" in capsys.readouterr().err


def test_rehearsal_cli_no_model(capsys: pytest.CaptureFixture[str]) -> None:
    from eval import rehearse as rh

    assert rh.main(["--no-model"]) == 2
    assert "NOT READY: 5 pass, 0 fail, 2 skip" in capsys.readouterr().out
