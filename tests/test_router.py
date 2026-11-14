from __future__ import annotations

import time

import pytest

from brain.contracts import Action, Plan, Route, UISnapshot
from brain.fastpath import FastPath
from brain.router import Router

SNAP = UISnapshot.from_dict(
    {
        "snapshot_id": "s1",
        "app": {"name": "Notes"},
        "items": [{"id": 101, "kind": "menu", "path": ["File", "New Note"], "shortcut": "cmd+n"}],
    }
)


class FakeFlows:
    def __init__(self, names: list[str] | None = None) -> None:
        self._names = names or ["standup"]

    def names(self) -> list[str]:
        return list(self._names)

    def match(self, text: str) -> str | None:
        t = text.lower().strip()
        for n in self._names:
            if t in (n, f"run {n}"):
                return n
        return None


class FakePlanner:
    def __init__(self, plan: Plan | None = None, exc: Exception | None = None) -> None:
        self.plan_ = plan or Plan(snapshot_id="s1", actions=(Action.key("return"),), say="ok")
        self.exc = exc
        self.calls: list[str] = []

    def plan(
        self, text: str, snapshot: UISnapshot, *, hint: str | None = None, error: str | None = None
    ) -> Plan:
        self.calls.append(text)
        if self.exc:
            raise self.exc
        return self.plan_


def make(planner: FakePlanner | None) -> Router:
    return Router(FastPath(["Notes", "Finder"]), FakeFlows(), planner)


@pytest.mark.parametrize(
    ("text", "word"),
    [("stop", "stop"), ("undo that", "undo"), ("yes", "yes"), ("never mind", "cancel"), ("no", "no")],
)
def test_control_first(text: str, word: str) -> None:
    p = FakePlanner()
    r = make(p).route(text, SNAP)
    assert (r.kind, r.control) == ("control", word)
    assert not p.calls


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("call that standup", "standup"),
        ("remember that as morning routine", "morning routine"),
        ("name that x", "x"),
    ],
)
def test_teach(text: str, name: str) -> None:
    r = make(FakePlanner()).route(text, SNAP)
    assert (r.kind, r.teach, r.flow) == ("flow", True, name)


def test_taught_flow_before_fastpath() -> None:
    p = FakePlanner()
    r = make(p).route("run standup", SNAP)
    assert (r.kind, r.flow, r.teach, r.plan) == ("flow", "standup", False, None)
    assert not p.calls


def test_flow_name_beats_fastpath() -> None:
    router = Router(FastPath(["Notes"]), FakeFlows(["open notes"]), None)
    assert router.route("open notes", SNAP).kind == "flow"


def test_fastpath() -> None:
    p = FakePlanner()
    r = make(p).route("open notes", SNAP)
    assert r.kind == "fastpath" and r.plan is not None and r.plan.actions[0].app == "Notes"
    r = make(p).route("new note", SNAP)
    assert r.kind == "fastpath" and r.plan is not None and r.plan.actions[0].target == 101
    assert not p.calls


def test_planner_fallback() -> None:
    p = FakePlanner()
    text = "open zzzqq and start a note called standup"  # unknown app: no compound split
    r = make(p).route(text, SNAP)
    assert r.kind == "planner" and r.plan is p.plan_
    assert p.calls == [text]


def test_dictation() -> None:
    plan = Plan(snapshot_id="s1", actions=(Action.type_text("hello"),), mode="dictation")
    r = make(FakePlanner(plan)).route("the quick brown fox jumps", SNAP)
    assert r.kind == "dictation" and r.plan is plan


def test_planner_error() -> None:
    r = make(FakePlanner(exc=RuntimeError("server down"))).route("do something odd", SNAP)
    assert r.kind == "none" and r.reason == "planner error: server down"


def test_no_planner() -> None:
    r = make(None).route("do something odd", SNAP)
    assert (r.kind, r.reason) == ("none", "no planner")


def test_ms_recorded() -> None:
    r = make(FakePlanner()).route("open notes", SNAP)
    assert isinstance(r, Route) and 0 < r.ms < 30


@pytest.mark.parametrize(
    "text",
    [
        "", "   ", ".", "...", "Thank you.", "thank you very much", "Thanks.", "you", "You", "Bye.",
        "Thanks for watching!", "Thank you for watching.", "[BLANK_AUDIO]", "(silence)", "[Music]",
        "(upbeat music)", "♪♪", "Subtitles by the Amara.org community", "you you",
    ],
)  # fmt: skip
def test_empty_or_filler_is_dropped(text: str) -> None:
    p = FakePlanner()
    r = make(p).route(text, SNAP)
    assert (r.kind, r.reason) == ("none", "empty or filler utterance")
    assert not p.calls


@pytest.mark.parametrize("text", ["okay", "thank you, open notes", "you know what, open notes", "music"])
def test_filler_blocklist_is_whole_utterance_only(text: str) -> None:
    r = make(FakePlanner()).route(text, SNAP)
    assert r.reason != "empty or filler utterance"


# ---- compound regex: linear, whitespace-normalized


@pytest.mark.parametrize(
    "text",
    [
        "open " + "a " * 1000,
        "open notes" + " " * 1990,
        "open " + "  and " * 400,
        "open notes " + "and " * 500,
        "open the " + "app " * 490,
        "open " + "x, " * 660,
    ],
)
def test_pathological_input_routes_fast(text: str) -> None:
    router = Router(FastPath(["Notes", "Slack"]), FakeFlows([]), None)
    t0 = time.perf_counter()
    router.route(text[:2000], SNAP)
    assert (time.perf_counter() - t0) * 1000 < 50


def test_compound_tolerates_irregular_whitespace() -> None:
    router = Router(FastPath(["Notes", "Slack"]), FakeFlows([]), None)
    r = router.route("  open \t slack   and\nmessage henrik ", SNAP)
    assert r.reason == "compound" and r.rest == "message henrik"


def test_frontmost_check_uses_app_aliases() -> None:
    from brain.contracts import AppInfo

    code = UISnapshot("s9", AppInfo("Code", "com.microsoft.VSCode"), items=())
    router = Router(FastPath(["Visual Studio Code", "Notes"]), FakeFlows([]), None)
    r = router.route("open vs code and new file", code)
    assert r.reason != "compound"  # already frontmost: no open_app in the plan
    assert r.plan is None or all(a.op != "open_app" for a in r.plan.actions)


def test_compound_of_two_apps_opens_both() -> None:
    router = Router(FastPath(["Pages", "Numbers", "Notes"]), FakeFlows([]), None)
    r = router.route("open pages and numbers", SNAP)
    assert r.plan is not None and r.rest is None
    assert [(a.op, a.app) for a in r.plan.actions] == [("open_app", "Pages"), ("open_app", "Numbers")]
