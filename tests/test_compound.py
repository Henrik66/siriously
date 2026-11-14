"""Create-template fast path, compound split and rest execution (no model needed)."""

from __future__ import annotations

from brain.contracts import Action, AppInfo, ListSink, Plan, UIItem, UISnapshot, Utterance
from brain.fastpath import FastPath
from brain.fixtures import FakeExecutor, FixtureUIProvider
from brain.flows import FlowStore
from brain.pipeline import Brain, build_brain
from brain.router import Router


def _menu(i: int, *path: str, enabled: bool = True) -> UIItem:
    return UIItem(i, "menu", "menuitem", path=tuple(path), enabled=enabled)


def _snap(app: str = "Finder", *items: UIItem) -> UISnapshot:
    return UISnapshot("s1", AppInfo(app), items=items)


FINDER = _snap(
    "Finder",
    _menu(1, "File", "New Folder"),
    _menu(2, "File", "New Folder with Selection", enabled=False),
    _menu(3, "File", "New Smart Folder"),
    _menu(4, "File", "New Finder Window"),
    _menu(5, "File", "New Tab"),
)
FP = FastPath(["Notes", "Finder", "Slack", "Calendar"])


def _actions(text: str, snap: UISnapshot = FINDER) -> list[tuple[str, object]] | None:
    plan = FP.match(text, snap)
    if plan is None:
        return None
    return [(a.op, a.target or a.text or a.keys) for a in plan.actions]


def test_create_folder_called() -> None:
    assert _actions("create a folder called demo") == [("menu", 1), ("type", "demo"), ("keys", "return")]


def test_variants_and_case_preserved() -> None:
    expect = [("menu", 1), ("type", "My Stuff"), ("keys", "return")]
    assert _actions("Make a new folder named My Stuff.") == expect
    assert _actions("new folder titled My Stuff") == expect
    assert _actions("add folders called My Stuff") == expect


def test_smart_folder_only_when_said() -> None:
    assert _actions("create a smart folder called x") == [("menu", 3), ("type", "x"), ("keys", "return")]


def test_requires_called() -> None:
    assert _actions("create a recipes folder") is None
    assert _actions("create a folder demo") is None


def test_skips_tabs_windows_and_unknown() -> None:
    assert _actions("create a tab called x") is None
    assert _actions("create a window called x") is None
    assert _actions("create a note called x") is None


def test_multi_clause_not_fastpathed() -> None:
    assert _actions("create a folder called demo and open notes") is None


def test_disabled_menu_not_used() -> None:
    snap = _snap("Finder", _menu(1, "File", "New Folder", enabled=False))
    assert _actions("create a folder called demo", snap) is None


def test_router_compound_split() -> None:
    r = Router(FP, _NoFlows(), None).route("open notes and start a note called standup", FINDER)
    assert r.kind == "fastpath" and r.reason == "compound"
    assert r.plan is not None and r.plan.actions == (Action.open_app("Notes"),)
    assert r.rest == "start a note called standup"


def test_router_compound_variants() -> None:
    r = Router(FP, _NoFlows(), None).route("Launch Slack, then message henrik.", FINDER)
    assert r.rest == "message henrik." and r.plan is not None
    assert r.plan.actions[0].app == "Slack"


def test_router_unknown_app_falls_through() -> None:
    r = Router(FP, _NoFlows(), None).route("open zzzzqq and start a note called x", FINDER)
    assert r.kind == "none" and r.rest is None


def test_router_frontmost_routes_rest_directly() -> None:
    r = Router(FP, _NoFlows(), None).route("open finder and create a folder called demo", FINDER)
    assert r.kind == "fastpath" and r.rest is None and r.plan is not None
    assert [a.op for a in r.plan.actions] == ["menu", "type", "keys"]


class _NoFlows:
    def names(self) -> list[str]:
        return []

    def match(self, text: str) -> str | None:
        return None


class _SlackPlanner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.last_stats = {"prompt_tokens": 100, "cached_tokens": 80, "completion_tokens": 5, "ms": 40.0}

    def plan(
        self, text: str, snapshot: UISnapshot, *, hint: str | None = None, error: str | None = None
    ) -> Plan:
        self.calls.append((text, hint))
        return Plan(snapshot.snapshot_id, (Action.type_text("hi"),))


def _brain(tmp_path, planner=None, fixture="finder"):  # type: ignore[no-untyped-def]
    provider = FixtureUIProvider(fixture)
    execu = FakeExecutor(provider)
    sink = ListSink()
    brain = build_brain(provider, execu, sink, planner=planner, use_model=planner is not None)
    brain._flows = FlowStore(tmp_path / "flows.json")  # isolate from the user's flows
    brain._router.flows = brain._flows  # type: ignore[attr-defined]
    return brain, provider, execu, sink


def _say(brain: Brain, text: str, uid: str = "u1") -> None:
    brain.handle(Utterance(uid, text))


def test_demo_line_end_to_end_no_planner(tmp_path) -> None:  # type: ignore[no-untyped-def]
    brain, provider, execu, sink = _brain(tmp_path)
    brain.resolve_interval_s = 0.0
    _say(brain, "open notes and start a note called standup")
    ops = [(a.op, a.text or a.keys or a.app) for a in execu.log]
    assert ops == [("open_app", "Notes"), ("menu", None), ("type", "standup"), ("keys", "return")]
    assert sink.types().count("done") == 1 and "error" not in sink.types()
    routes = [e.data for e in sink.events if e.type == "route"]
    assert routes[-1]["source"] == "rest" and routes[-1]["kind"] == "fastpath"
    done = next(e for e in sink.events if e.type == "done")
    assert done.data["n_actions"] == 4 and "usage" not in done.data

    _say(brain, "call that standup", "u2")
    flow = brain.flows.get("standup")
    assert flow is not None and len(flow.steps) == 4

    provider.reset()
    execu.log.clear()
    _say(brain, "standup", "u3")
    assert [a.op for a in execu.log] == ["open_app", "menu", "type", "keys"]


def test_create_folder_in_finder(tmp_path) -> None:  # type: ignore[no-untyped-def]
    brain, _, execu, sink = _brain(tmp_path)
    _say(brain, "create a folder called demo")
    assert [a.op for a in execu.log] == ["menu", "type", "keys"]
    assert execu.log[1].text == "demo" and "error" not in sink.types()


def test_rest_goes_to_planner_with_usage(tmp_path) -> None:  # type: ignore[no-untyped-def]
    planner = _SlackPlanner()
    brain, _, execu, sink = _brain(tmp_path, planner)
    brain.resolve_interval_s = 0.0
    _say(brain, "open slack and message henrik")
    assert execu.log[0].op == "open_app" and execu.log[1].op == "type"
    assert planner.calls and planner.calls[0][0] == "message henrik"
    assert "just opened" in (planner.calls[0][1] or "")
    done = next(e for e in sink.events if e.type == "done")
    assert done.data["usage"]["calls"] == 1 and done.data["usage"]["prompt_tokens"] == 100
    assert done.data["n_actions"] == 2


def test_rest_stop_is_not_control(tmp_path) -> None:  # type: ignore[no-untyped-def]
    planner = _SlackPlanner()
    brain, _, _, _ = _brain(tmp_path, planner)
    brain.resolve_interval_s = 0.0
    _say(brain, "open slack and stop the music")
    assert planner.calls and planner.calls[0][0] == "stop the music"


def test_already_frontmost_no_open_app(tmp_path) -> None:  # type: ignore[no-untyped-def]
    brain, _, execu, _ = _brain(tmp_path)
    _say(brain, "open finder and create a folder called demo")
    assert [a.op for a in execu.log] == ["menu", "type", "keys"]
