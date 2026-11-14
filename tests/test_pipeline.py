from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Callable
from dataclasses import replace

import pytest

from brain.config import Settings
from brain.contracts import (
    Action,
    ActionResult,
    AppInfo,
    ControlWord,
    Event,
    ListSink,
    Plan,
    Route,
    Speaker,
    UIItem,
    UISnapshot,
    Utterance,
)
from brain.flows import Flow, FlowStep, FlowStore, parse_teach
from brain.pipeline import Brain, build_brain, describe, expects_change

BUNDLE = "com.apple.Notes"


def snap(sid: str, new: int, body: int, send: int = 0) -> UISnapshot:
    items = [
        UIItem(new, "element", "button", "New Note"),
        UIItem(body, "element", "textarea", "Body"),
        UIItem(900, "menu", "menuitem", "New Note", path=("File", "New Note")),
    ]
    if send:
        items.append(UIItem(send, "element", "button", "Send"))
    return UISnapshot(sid, AppInfo("Notes", BUNDLE), items=tuple(items))


SNAP_A = snap("a", 1, 2, send=3)
SNAP_B = snap("b", 11, 22, send=33)


class FakeProvider:
    def __init__(self, s: UISnapshot = SNAP_A) -> None:
        self.snap = s

    def snapshot(self) -> UISnapshot:
        return self.snap

    def list_apps(self) -> list[str]:
        return ["Notes", "Safari"]


class FakeExecutor:
    def __init__(self, provider: FakeProvider) -> None:
        self.provider = provider
        self.log: list[tuple[str, Action]] = []
        self.undo_ok = True
        self.undo_calls = 0
        self.hook: Callable[[str, Action], ActionResult | None] | None = None

    def run(self, snapshot_id: str, action: Action) -> ActionResult:
        self.log.append((snapshot_id, action))
        if self.hook is not None and (r := self.hook(snapshot_id, action)) is not None:
            return r
        return ActionResult(ok=True, changed=True)

    def undo(self) -> bool:
        self.undo_calls += 1
        return self.undo_ok

    @property
    def actions(self) -> list[Action]:
        return [a for _, a in self.log]


class FakePlanner:
    def __init__(self, plans: list[Plan] | None = None) -> None:
        self.plans = list(plans or [])
        self.calls: list[dict[str, object]] = []

    def plan(
        self, text: str, snapshot: UISnapshot, *, hint: str | None = None, error: str | None = None
    ) -> Plan:
        self.calls.append({"text": text, "snapshot": snapshot, "hint": hint, "error": error})
        return self.plans.pop(0) if self.plans else Plan(snapshot.snapshot_id, ())


CONTROL: dict[str, ControlWord] = {
    "yes": "yes",
    "no": "no",
    "cancel": "cancel",
    "undo": "undo",
    "stop": "stop",
}


class FakeRouter:
    def __init__(self, flows: FlowStore) -> None:
        self.flows = flows
        self.plans: dict[str, Route] = {}
        self.boom = False

    def route(self, text: str, snapshot: UISnapshot) -> Route:
        if self.boom:
            raise RuntimeError("router exploded")
        t = text.strip().lower()
        if t in CONTROL:
            return Route(kind="control", control=CONTROL[t])
        if (name := parse_teach(t)) is not None:
            return Route(kind="flow", teach=True, flow=name)
        if (flow := self.flows.match(t)) is not None:
            return Route(kind="flow", flow=flow)
        return self.plans.get(t, Route(kind="none", reason="no match"))


class Rig:
    def __init__(self, *, planner: FakePlanner | None = None, settings: Settings | None = None) -> None:
        self.provider = FakeProvider()
        self.executor = FakeExecutor(self.provider)
        self.flows = FlowStore()
        self.router = FakeRouter(self.flows)
        self.sink = ListSink()
        self.planner = planner
        self.brain = Brain(
            self.provider, self.executor, self.router, planner, self.flows, self.sink, settings
        )
        self.n = 0

    def say(self, text: str, **kw: object) -> Route:
        self.n += 1
        return self.brain.handle(Utterance(id=f"u{self.n}", text=text, **kw))  # type: ignore[arg-type]

    def plan_route(self, text: str, plan: Plan, kind: str = "fastpath") -> None:
        self.router.plans[text] = Route(kind=kind, plan=plan)  # type: ignore[arg-type]

    def types(self) -> list[str]:
        return self.sink.types()

    def events(self, type_: str) -> list[Event]:
        return [e for e in self.sink.events if e.type == type_]

    def clear(self) -> None:
        self.sink.events.clear()


@pytest.fixture
def rig() -> Rig:
    return Rig(planner=FakePlanner())


NOTE_PLAN = Plan("a", (Action.press(1), Action.type_text("hello", target=2)))


def test_rejected_speaker(rig: Rig) -> None:
    r = rig.say("open notes", speaker=Speaker(verified=False, score=0.2))
    assert r.kind == "rejected"
    assert rig.types() == ["rejected"]
    assert rig.events("rejected")[0].data == {"text": "open notes", "score": 0.2}
    assert rig.executor.log == []


def test_fastpath_event_order(rig: Rig) -> None:
    rig.plan_route("new note", NOTE_PLAN)
    r = rig.say("new note")
    assert r.kind == "fastpath"
    assert rig.types() == ["heard", "route", "plan", "action", "action", "done"]
    assert rig.executor.log == [("a", Action.press(1)), ("a", Action.type_text("hello", target=2))]
    done = rig.events("done")[0].data
    assert done["route"] == "fastpath"
    assert done["n_actions"] == 2
    assert isinstance(done["ms"], int)
    acts = rig.events("action")
    assert [e.data["i"] for e in acts] == [0, 1]
    assert acts[0].data["ok"] is True
    assert rig.events("heard")[0].data["text"] == "new note"
    assert rig.events("route")[0].data["kind"] == "fastpath"
    assert rig.events("plan")[0].data["source"] == "fastpath"


def test_dictation_route(rig: Rig) -> None:
    rig.plan_route(
        "hello world", Plan("a", (Action.type_text("hello world"),), mode="dictation"), "dictation"
    )
    rig.say("hello world")
    assert rig.types()[-1] == "done"
    assert rig.executor.actions == [Action.type_text("hello world")]


def test_ms_since_speech_end(rig: Rig) -> None:
    rig.plan_route("new note", NOTE_PLAN)
    rig.say("new note", t_speech_end=time.time() - 0.5)
    assert all(e.ms_since_speech_end >= 400 for e in rig.sink.events)
    rig.clear()
    rig.say("new note", t_speech_end=0.0)
    assert all(e.ms_since_speech_end == 0 for e in rig.sink.events)


def test_route_none_emits_error(rig: Rig) -> None:
    r = rig.say("gibberish")
    assert r.kind == "none"
    assert rig.types() == ["heard", "route", "error"]


def test_router_exception_never_escapes(rig: Rig) -> None:
    rig.router.boom = True
    r = rig.say("anything")
    assert r.kind == "none"
    assert "router exploded" in r.reason
    assert rig.types()[-1] == "error"


def test_confirm_then_yes_executes(rig: Rig) -> None:
    rig.plan_route("send it", Plan("a", (Action.press(3),)))
    rig.say("send it")
    assert rig.types() == ["heard", "route", "plan", "confirm"]
    assert rig.executor.log == []
    assert "Send" in str(rig.events("confirm")[0].data["reason"])
    rig.clear()
    r = rig.say("yes")
    assert r.kind == "control"
    assert rig.types() == ["heard", "route", "action", "done"]
    assert rig.executor.actions == [Action.press(3)]
    assert rig.events("done")[0].data["route"] == "fastpath"
    rig.clear()
    rig.say("yes")  # confirmation is consumed
    assert rig.types()[-1] == "error"
    assert len(rig.executor.log) == 1


def test_confirm_then_no_discards(rig: Rig) -> None:
    rig.plan_route("send it", Plan("a", (Action.press(3),)))
    rig.say("send it")
    rig.clear()
    rig.say("no")
    assert rig.types() == ["heard", "route", "done"]
    rig.say("yes")
    assert rig.executor.log == []


def test_confirm_on_keys_and_plan_flag(rig: Rig) -> None:
    rig.plan_route("quit", Plan("a", (Action.key("cmd+q"),)))
    rig.plan_route("careful", Plan("a", (Action.press(1),), confirm=True))
    rig.say("quit")
    rig.say("careful")
    assert rig.types().count("confirm") == 2
    assert rig.executor.log == []
    rig.say("yes")  # only the latest pending plan is confirmed
    assert rig.executor.actions == [Action.press(1)]


def test_new_command_supersedes_pending(rig: Rig) -> None:
    rig.plan_route("send it", Plan("a", (Action.press(3),)))
    rig.plan_route("new note", NOTE_PLAN)
    rig.say("send it")
    rig.say("new note")
    rig.say("yes")
    assert Action.press(3) not in rig.executor.actions


def test_undo(rig: Rig) -> None:
    r = rig.say("undo")
    assert r.control == "undo"
    assert rig.executor.undo_calls == 1
    assert rig.types() == ["heard", "route", "done"]
    rig.executor.undo_ok = False
    rig.clear()
    rig.say("undo")
    assert rig.types()[-1] == "error"


def test_stop_when_idle_does_not_poison_next_burst(rig: Rig) -> None:
    rig.plan_route("new note", NOTE_PLAN)
    r = rig.say("stop")
    assert r.kind == "control" and r.control == "stop"
    assert rig.types() == ["heard", "route", "done"]
    rig.say("new note")
    assert len(rig.executor.log) == 2


def test_brain_stop_method_aborts_between_actions(rig: Rig) -> None:
    rig.plan_route("new note", NOTE_PLAN)

    def hook(_sid: str, _a: Action) -> ActionResult | None:
        rig.brain.stop()
        return None

    rig.executor.hook = hook
    rig.say("new note")
    assert len(rig.executor.log) == 1
    assert rig.events("error")[0].data["message"] == "stopped"
    assert "done" not in rig.types()


def test_stop_mid_burst_from_another_thread(rig: Rig) -> None:
    rig.plan_route("big", Plan("a", (Action.press(1), Action.press(1), Action.press(1))))
    entered, release = threading.Event(), threading.Event()

    def hook(_sid: str, _a: Action) -> ActionResult | None:
        entered.set()
        release.wait(5)
        return None

    rig.executor.hook = hook
    result: list[Route] = []
    t = threading.Thread(target=lambda: result.append(rig.say("big")))
    t.start()
    assert entered.wait(5)
    # handle() holds the utterance lock; "stop" must not wait for it.
    start = time.monotonic()
    stop_route = rig.say("stop")
    assert time.monotonic() - start < 1.0
    assert stop_route.control == "stop"
    release.set()
    t.join(5)
    assert not t.is_alive()
    assert len(rig.executor.log) == 1
    assert result[0].kind == "fastpath"
    assert "stopped" in [e.data.get("message") for e in rig.events("error")]


def test_replan_on_failure_succeeds(rig: Rig) -> None:
    fixed = Plan("b", (Action.press(11),))
    rig.planner = FakePlanner([fixed])
    rig.brain = Brain(rig.provider, rig.executor, rig.router, rig.planner, rig.flows, rig.sink)
    rig.plan_route("new note", Plan("a", (Action.press(1), Action.press(2))))

    def hook(_sid: str, a: Action) -> ActionResult | None:
        if a.target == 1:
            return ActionResult(ok=False, error="stale id")
        return None

    rig.executor.hook = hook
    rig.provider.snap = SNAP_B
    rig.say("new note")
    assert rig.executor.actions == [Action.press(1), Action.press(11)]
    assert rig.types() == ["heard", "route", "plan", "action", "plan", "action", "done"]
    assert rig.events("plan")[1].data["source"] == "replan"
    assert rig.planner.calls[0]["error"] is not None and "stale id" in str(rig.planner.calls[0]["error"])
    assert rig.planner.calls[0]["snapshot"] is SNAP_B
    assert rig.events("done")[0].data["n_actions"] == 2


def test_replan_on_unchanged_press(rig: Rig) -> None:
    rig.planner = FakePlanner([Plan("a", (Action.type_text("x"),))])
    rig.brain = Brain(rig.provider, rig.executor, rig.router, rig.planner, rig.flows, rig.sink)
    rig.plan_route("go", Plan("a", (Action.press(1),)))
    rig.executor.hook = lambda _s, a: ActionResult(ok=True, changed=False) if a.op == "press" else None
    rig.say("go")
    assert len(rig.planner.calls) == 1
    assert "no effect" in str(rig.planner.calls[0]["error"])
    assert rig.types()[-1] == "done"


def test_unchanged_type_wait_scroll_do_not_replan(rig: Rig) -> None:
    rig.plan_route("go", Plan("a", (Action.type_text("x"), Action.wait(1), Action.scroll("down"))))
    rig.executor.hook = lambda _s, _a: ActionResult(ok=True, changed=False)
    rig.say("go")
    assert rig.types()[-1] == "done"
    assert rig.planner is not None and rig.planner.calls == []  # type: ignore[attr-defined]


def test_replan_gives_up_after_max(rig: Rig) -> None:
    bad = Plan("a", (Action.press(1),))
    rig.planner = FakePlanner([bad, bad, bad])
    rig.brain = Brain(rig.provider, rig.executor, rig.router, rig.planner, rig.flows, rig.sink)
    rig.plan_route("go", bad)
    rig.executor.hook = lambda _s, _a: ActionResult(ok=False, error="nope")
    r = rig.say("go")
    assert r.kind == "fastpath"
    assert len(rig.planner.calls) == 2  # settings.max_replans
    assert len(rig.executor.log) == 3
    assert rig.types()[-1] == "error"
    assert "done" not in rig.types()
    assert "nope" in str(rig.events("error")[0].data["message"])


def test_failure_without_planner() -> None:
    rig = Rig(planner=None)
    rig.plan_route("go", Plan("a", (Action.press(1),)))
    rig.executor.hook = lambda _s, _a: ActionResult(ok=False, error="boom")
    rig.say("go")
    assert rig.types()[-1] == "error"


def test_executor_exception_becomes_failure() -> None:
    rig = Rig(planner=None)
    rig.plan_route("go", Plan("a", (Action.press(1),)))

    def hook(_s: str, _a: Action) -> ActionResult | None:
        raise OSError("disk on fire")

    rig.executor.hook = hook
    rig.say("go")
    assert rig.types()[-1] == "error"
    assert "disk on fire" in str(rig.events("error")[0].data["message"])


def test_timeout() -> None:
    rig = Rig(planner=FakePlanner(), settings=replace(Settings(), utterance_timeout_s=0.05))
    rig.plan_route("slow", Plan("a", (Action.press(1), Action.press(1), Action.press(1))))

    def hook(_s: str, _a: Action) -> ActionResult | None:
        time.sleep(0.1)
        return None

    rig.executor.hook = hook
    rig.say("slow")
    assert len(rig.executor.log) == 1
    assert "timeout" in str(rig.events("error")[0].data["message"])
    assert "done" not in rig.types()


def test_empty_plan_and_ask(rig: Rig) -> None:
    rig.plan_route("what", Plan("a", ()), "planner")
    rig.say("what")
    assert rig.types()[-1] == "error"
    rig.clear()
    rig.plan_route("which", Plan("a", (), ask=(1, 2)), "planner")
    rig.say("which")
    assert rig.types() == ["heard", "route", "plan", "done"]
    assert rig.executor.log == []


def test_teach_then_replay_with_changed_ids(rig: Rig) -> None:
    rig.plan_route("new note", NOTE_PLAN)
    rig.say("new note")
    rig.clear()
    rig.say("call that standup")
    assert rig.types() == ["heard", "route", "done"]
    flow = rig.flows.get("standup")
    assert flow is not None
    assert [s.action.op for s in flow.steps] == ["press", "type"]
    assert flow.steps[0].target is not None and flow.steps[0].target.label == "New Note"
    assert flow.steps[0].target.bundle_id == BUNDLE
    assert flow.steps[1].target is not None and flow.steps[1].target.label == "Body"
    assert flow.utterance == "new note"

    # Ids change: now on SNAP_B, and pressing "New Note" shifts ids again.
    rig.provider.snap = SNAP_B
    snap_c = snap("c", 101, 202, send=303)

    def hook(_s: str, a: Action) -> ActionResult | None:
        if a.op == "press":
            rig.provider.snap = snap_c
        return None

    rig.executor.hook = hook
    rig.executor.log.clear()
    rig.clear()
    r = rig.say("run standup")
    assert r.kind == "flow"
    assert rig.executor.log == [("b", Action.press(11)), ("c", Action.type_text("hello", target=202))]
    assert rig.types() == ["heard", "route", "action", "action", "done"]
    assert rig.events("done")[0].data["route"] == "flow"


def test_teach_nothing_to_save(rig: Rig) -> None:
    rig.say("call that standup")
    assert rig.types() == ["heard", "route", "error"]
    assert rig.flows.names() == []


def test_failed_run_not_taught(rig: Rig) -> None:
    rig.plan_route("go", Plan("a", (Action.press(1),)))
    rig.executor.hook = lambda _s, _a: ActionResult(ok=False, error="x")
    rig.say("go")
    rig.say("call that nope")
    assert rig.flows.names() == []


def test_replay_falls_back_to_planner_when_descriptor_missing() -> None:
    fallback = Plan("a", (Action.press(1),))
    rig = Rig(planner=FakePlanner([fallback]))
    rig.flows.save(
        Flow(
            "standup",
            (FlowStep(Action.press(77)), FlowStep(Action.key("cmd+n"))),  # targeted step lacks descriptor
            "open the standup note",
        )
    )
    rig.say("standup")
    assert len(rig.planner.calls) == 1  # type: ignore[union-attr]
    call = rig.planner.calls[0]  # type: ignore[union-attr]
    assert call["text"] == "open the standup note"
    assert str(call["hint"]).startswith("taught flow:")
    assert rig.executor.actions == [Action.press(1)]
    assert rig.events("plan")[0].data["source"] == "flow-fallback"
    assert rig.types()[-1] == "done"


def test_replay_fallback_when_label_gone_midway() -> None:
    from brain.contracts import TargetDescriptor

    rig = Rig(planner=FakePlanner([Plan("a", (Action.type_text("z"),))]))
    gone = TargetDescriptor("element", "button", "Vanished", (), BUNDLE)
    rig.flows.save(Flow("f", (FlowStep(Action.key("cmd+n")), FlowStep(Action.press(5), gone))))
    rig.say("f")
    assert rig.executor.actions == [Action.key("cmd+n"), Action.type_text("z")]
    assert rig.planner.calls[0]["text"] == "f"  # type: ignore[union-attr]


def test_replay_without_planner_errors() -> None:
    rig = Rig(planner=None)
    rig.flows.save(Flow("f", (FlowStep(Action.press(5)),)))
    rig.say("f")
    assert rig.types()[-1] == "error"


def test_replay_dangerous_step_requires_confirm(rig: Rig) -> None:
    from brain.contracts import TargetDescriptor

    desc = TargetDescriptor("element", "button", "Send", (), BUNDLE)
    rig.flows.save(Flow("mail", (FlowStep(Action.press(3), desc), FlowStep(Action.type_text("bye")))))
    rig.say("mail")
    assert rig.types() == ["heard", "route", "confirm"]
    assert rig.executor.log == []
    rig.say("yes")
    assert rig.executor.actions == [Action.press(3), Action.type_text("bye")]
    assert rig.types()[-1] == "done"


def test_unknown_flow_errors(rig: Rig) -> None:
    rig.router.plans["x"] = Route(kind="flow", flow="ghost")
    rig.say("x")
    assert rig.types()[-1] == "error"


def test_taught_flow_survives_restart(tmp_path_factory: pytest.TempPathFactory) -> None:
    path = tmp_path_factory.mktemp("flows") / "flows.json"
    rig = Rig(planner=FakePlanner())
    rig.flows = FlowStore(path)
    rig.router = FakeRouter(rig.flows)
    rig.brain = Brain(rig.provider, rig.executor, rig.router, None, rig.flows, rig.sink)
    rig.plan_route("new note", NOTE_PLAN)
    rig.say("new note")
    rig.say("remember that as standup")
    assert FlowStore(path).get("standup") is not None


def test_helpers() -> None:
    assert describe(Action.press(1), SNAP_A) is not None
    assert describe(Action.press(999), SNAP_A) is None
    assert describe(Action.key("cmd+n"), SNAP_A) is None
    assert expects_change(Action.press(1))
    assert expects_change(Action.key("cmd+n"))
    assert not expects_change(Action.key("enter"))
    assert not expects_change(Action.type_text("x"))
    assert not expects_change(Action.wait(5))
    assert not expects_change(Action.scroll("up"))


def test_build_brain_wires_components() -> None:
    pytest.importorskip("brain.router")
    pytest.importorskip("brain.fastpath")
    provider = FakeProvider()
    sink = ListSink()
    settings = replace(Settings(), flows_path=Settings().flows_path.parent / "_never_written.json")
    brain = build_brain(provider, FakeExecutor(provider), sink, settings, planner=FakePlanner())
    r = brain.handle(Utterance(id="u", text="gibberish xyzzy"))
    assert r.kind in ("planner", "none", "fastpath", "dictation")
    assert sink.types()[:2] == ["heard", "route"]


# --------------------------------------------------------------- review fixes


def _fast(rig: Rig) -> Rig:
    rig.brain.resolve_interval_s = 0.0
    return rig


def _confirm_send(rig: Rig) -> None:
    rig.plan_route("send it", Plan("a", (Action.press(3),)))
    rig.say("send it")
    assert rig.types()[-1] == "confirm"
    rig.clear()


@pytest.mark.parametrize("other", ["call that foo", "undo", "new note", "gibberish"])
def test_pending_confirm_cleared_by_any_other_utterance(rig: Rig, other: str) -> None:
    rig.plan_route("new note", NOTE_PLAN)
    rig.say("new note")
    _confirm_send(rig)
    rig.say(other)
    rig.executor.log.clear()
    rig.clear()
    rig.say("yes")
    assert rig.executor.log == []
    assert rig.events("error")[0].data["message"] == "nothing to confirm"


def test_pending_confirm_expires() -> None:
    rig = Rig(planner=None, settings=replace(Settings(), confirm_ttl_s=0.01))
    _confirm_send(rig)
    time.sleep(0.05)
    rig.say("yes")
    assert rig.executor.log == []
    assert rig.events("error")[0].data["message"] == "nothing to confirm"


def test_confirm_resumes_on_fresh_snapshot_ids(rig: Rig) -> None:
    _confirm_send(rig)
    rig.provider.snap = SNAP_B  # Send is now id 33 in snapshot "b"
    rig.say("yes")
    assert rig.executor.log == [("b", Action.press(33))]
    assert rig.types()[-1] == "done"


def test_confirm_refused_when_target_gone(rig: Rig) -> None:
    _confirm_send(rig)
    rig.provider.snap = snap("c", 1, 2)  # no Send button anymore
    rig.say("yes")
    assert rig.executor.log == []
    assert rig.types() == ["heard", "route", "error"]
    assert "nothing was executed" in str(rig.events("error")[0].data["message"])


def test_confirm_refused_when_app_changed(rig: Rig) -> None:
    _confirm_send(rig)
    rig.provider.snap = UISnapshot("z", AppInfo("Mail", "com.apple.mail"), items=SNAP_A.items)
    rig.say("yes")
    assert rig.executor.log == []
    assert rig.types()[-1] == "error"


def test_flow_confirm_refused_when_target_gone(rig: Rig) -> None:
    from brain.contracts import TargetDescriptor

    desc = TargetDescriptor("element", "button", "Send", (), BUNDLE)
    rig.flows.save(Flow("mail", (FlowStep(Action.press(3), desc),)))
    rig.say("mail")
    rig.provider.snap = snap("c", 1, 2)
    rig.say("yes")
    assert rig.executor.log == []
    assert rig.types()[-1] == "error"


def test_confirm_in_flow_keeps_done_steps_for_teach(rig: Rig) -> None:
    from brain.contracts import TargetDescriptor

    new = TargetDescriptor("element", "button", "New Note", (), BUNDLE)
    send = TargetDescriptor("element", "button", "Send", (), BUNDLE)
    rig.flows.save(Flow("mail", (FlowStep(Action.press(1), new), FlowStep(Action.press(3), send)), "mail it"))
    rig.say("mail")
    assert rig.types()[-1] == "confirm"
    assert rig.executor.actions == [Action.press(1)]
    rig.say("yes")
    rig.say("call that mail2")
    saved = rig.flows.get("mail2")
    assert saved is not None
    assert [s.action.target for s in saved.steps] == [1, 3]


def test_control_words_never_snapshot(rig: Rig) -> None:
    calls = 0

    def boom() -> UISnapshot:
        nonlocal calls
        calls += 1
        raise RuntimeError("AX walk failed")

    rig.provider.snapshot = boom  # type: ignore[method-assign]
    for word in ("undo", "no", "cancel"):
        rig.say(word)
    assert rig.events("error") == []
    rig.clear()
    rig.say("yes")  # nothing pending: no snapshot either
    assert rig.events("error")[0].data["message"] == "nothing to confirm"
    assert calls == 0
    assert rig.types()[:2] == ["heard", "route"]


@pytest.mark.parametrize("keys", ["cmd+c", "cmd+a", "cmd+s", "cmd+f", "cmd+g"])
def test_no_visible_effect_keys_do_not_fail(keys: str) -> None:
    rig = Rig(planner=FakePlanner())
    rig.plan_route("go", Plan("a", (Action.key(keys),)))
    rig.executor.hook = lambda _s, _a: ActionResult(ok=True, changed=False)
    rig.say("go")
    assert rig.types()[-1] == "done"
    assert rig.planner.calls == []  # type: ignore[union-attr]


def test_no_visible_effect_menu_items_do_not_fail() -> None:
    items = (
        UIItem(50, "menu", "menuitem", "Copy", path=("Edit", "Copy"), shortcut="cmd+c"),
        UIItem(51, "menu", "menuitem", "Select All", path=("Edit", "Select All"), shortcut="cmd+a"),
        UIItem(52, "menu", "menuitem", "Save", path=("File", "Save"), shortcut="cmd+s"),
        UIItem(53, "menu", "menuitem", "New Window", path=("File", "New Window"), shortcut="cmd+n"),
    )
    s = UISnapshot("m", AppInfo("Notes", BUNDLE), items=items)
    for mid in (50, 51, 52):
        assert not expects_change(Action.menu(mid), s)
    assert expects_change(Action.menu(53), s)
    assert expects_change(Action.menu(99), s)
    assert expects_change(Action.press(1), s) and expects_change(Action.open_app("x"), s)
    assert expects_change(Action.key("cmd+n"), s)
    assert not expects_change(Action.key("cmd+shift+g"))  # shortcut alone is enough
    rig = Rig(planner=FakePlanner())
    rig.provider.snap = s
    rig.plan_route("go", Plan("m", (Action.menu(50), Action.menu(51), Action.menu(52))))
    rig.executor.hook = lambda _s, _a: ActionResult(ok=True, changed=False)
    rig.say("go")
    assert rig.types()[-1] == "done"
    assert len(rig.executor.log) == 3
    assert rig.planner.calls == []  # type: ignore[union-attr]


def test_unchanged_flag_is_checked_against_a_fresh_snapshot() -> None:
    rig = Rig(planner=FakePlanner())
    rig.plan_route("go", Plan("a", (Action.press(1),)))

    def hook(_s: str, _a: Action) -> ActionResult | None:
        rig.provider.snap = replace(SNAP_A, window="Untitled")  # UI changed, executor did not say so
        return ActionResult(ok=True, changed=False)

    rig.executor.hook = hook
    rig.say("go")
    assert rig.types()[-1] == "done"
    assert rig.planner.calls == []  # type: ignore[union-attr]
    assert rig.events("action")[0].data["changed"] is True


def test_replan_tells_planner_what_already_succeeded() -> None:
    rig = Rig(planner=FakePlanner([Plan("a", (Action.press(3),))]))
    rig.plan_route("go", Plan("a", (Action.press(1), Action.type_text("hi", target=2), Action.press(2))))
    rig.executor.hook = lambda _s, a: (
        ActionResult(ok=False, error="stale") if a.op == "press" and a.target == 2 else None
    )
    rig.say("go")
    error = str(rig.planner.calls[0]["error"])  # type: ignore[union-attr]
    assert "stale" in error
    assert "Already done" in error
    assert 'press button "New Note"' in error
    assert 'type "hi"' in error


def test_replan_skipped_when_not_enough_time() -> None:
    settings = replace(Settings(), utterance_timeout_s=2.0, bonsai_timeout_s=6.0)
    rig = Rig(planner=FakePlanner([Plan("a", (Action.press(1),))]), settings=settings)
    rig.plan_route("go", Plan("a", (Action.press(1),)))
    rig.executor.hook = lambda _s, _a: ActionResult(ok=False, error="nope")
    rig.say("go")
    assert rig.planner.calls == []  # type: ignore[union-attr]
    assert "re-plan" in str(rig.events("error")[0].data["message"])


def test_stale_ids_rebound_across_burst(rig: Rig) -> None:
    snap_c = snap("c", 101, 202, send=303)
    rig.plan_route("go", Plan("a", (Action.press(1), Action.type_text("hi", target=2))))

    def hook(_s: str, a: Action) -> ActionResult | None:
        return ActionResult(ok=True, changed=True, snapshot=snap_c) if a.op == "press" else None

    rig.executor.hook = hook
    rig.say("go")
    assert rig.executor.log == [("a", Action.press(1)), ("c", Action.type_text("hi", target=202))]


def test_stale_ids_unresolvable_goes_to_replan() -> None:
    rig = Rig(planner=FakePlanner([Plan("d", (Action.type_text("z"),))]))
    gone = UISnapshot("d", AppInfo("Notes", BUNDLE), items=())
    rig.plan_route("go", Plan("a", (Action.press(1), Action.type_text("hi", target=2))))
    rig.executor.hook = lambda _s, a: (
        ActionResult(ok=True, changed=True, snapshot=gone) if a.op == "press" else None
    )
    rig.say("go")
    assert rig.executor.actions == [Action.press(1), Action.type_text("z")]
    assert "no longer resolve" in str(rig.planner.calls[0]["error"])  # type: ignore[union-attr]


def test_stop_cancels_utterance_spoken_before_it(rig: Rig) -> None:
    rig.plan_route("new note", NOTE_PLAN)
    rig.brain.stop()
    r = rig.say("new note", t_speech_end=time.time() - 5)
    assert r.kind == "none" and r.reason == "cancelled by stop"
    assert rig.executor.log == []
    assert rig.events("error")[0].data["message"] == "cancelled by stop"


def test_stop_cancels_utterance_queued_before_it(rig: Rig) -> None:
    rig.plan_route("new note", NOTE_PLAN)
    t_queued = time.monotonic()
    time.sleep(0.01)
    rig.brain.stop()
    r = rig.brain.handle(Utterance(id="q", text="new note"), received_at=t_queued)
    assert r.reason == "cancelled by stop"
    assert rig.executor.log == []


def test_stop_during_lock_wait_cancels_queued_utterance(rig: Rig) -> None:
    rig.plan_route("new note", NOTE_PLAN)
    rig.brain._lock.acquire()
    result: list[Route] = []
    t = threading.Thread(target=lambda: result.append(rig.say("new note")))
    t.start()
    time.sleep(0.05)
    rig.say("stop")
    rig.brain._lock.release()
    t.join(5)
    assert result[0].reason == "cancelled by stop"
    assert rig.executor.log == []


def test_utterance_after_stop_runs(rig: Rig) -> None:
    rig.plan_route("new note", NOTE_PLAN)
    rig.say("stop", t_speech_end=time.time() - 1)
    rig.say("new note", t_speech_end=time.time())
    assert len(rig.executor.log) == 2


def test_flow_fallback_polls_until_target_appears() -> None:
    from brain.contracts import TargetDescriptor

    rig = _fast(Rig(planner=FakePlanner()))
    later = TargetDescriptor("element", "button", "Later", (), BUNDLE)
    rig.flows.save(Flow("f", (FlowStep(Action.key("cmd+n")), FlowStep(Action.press(9), later))))
    polls = 0
    appeared = UISnapshot("p", AppInfo("Notes", BUNDLE), items=(UIItem(77, "element", "button", "Later"),))
    real = rig.provider.snapshot

    def snapshot() -> UISnapshot:
        nonlocal polls
        polls += 1
        return appeared if polls >= 3 else real()

    rig.provider.snapshot = snapshot  # type: ignore[method-assign]
    rig.say("f")
    assert rig.executor.actions == [Action.key("cmd+n"), Action.press(77)]
    assert rig.planner.calls == []  # type: ignore[union-attr]
    assert rig.types()[-1] == "done"


def test_flow_fallback_hint_lists_remaining_and_done() -> None:
    from brain.contracts import TargetDescriptor

    rig = _fast(Rig(planner=FakePlanner([Plan("a", (Action.type_text("z"),))])))
    gone = TargetDescriptor("element", "button", "Vanished", (), BUNDLE)
    rig.flows.save(Flow("f", (FlowStep(Action.key("cmd+n")), FlowStep(Action.press(5), gone)), "make it so"))
    rig.say("f")
    call = rig.planner.calls[0]  # type: ignore[union-attr]
    assert call["text"] == "make it so"
    hint = str(call["hint"])
    assert "remaining steps only" in hint and "Vanished" in hint
    assert "Already done" in hint and "keys cmd+n" in hint
    assert rig.executor.actions == [Action.key("cmd+n"), Action.type_text("z")]


# ---- continuation across screens (planner bursts that navigate)

SETTINGS_SNAP = UISnapshot(
    "s",
    AppInfo("System Settings", "com.apple.systempreferences"),
    items=(UIItem(5, "element", "button", "Dark"),),
)


def _navigate_to(rig: Rig, target: UISnapshot) -> None:
    def hook(_s: str, a: Action) -> ActionResult | None:
        if a.op == "open_app":
            rig.provider.snap = target
        return None

    rig.executor.hook = hook


def test_planner_continues_after_open_app() -> None:
    rig = _fast(Rig(planner=FakePlanner([Plan("s", (Action.press(5),)), Plan("s", ())])))
    rig.plan_route("dark mode", Plan("a", (Action.open_app("System Settings"),)), "planner")
    _navigate_to(rig, SETTINGS_SNAP)
    rig.say("dark mode")
    assert rig.executor.log == [("a", Action.open_app("System Settings")), ("s", Action.press(5))]
    call = rig.planner.calls[0]  # type: ignore[union-attr]
    assert call["text"] == "dark mode" and call["snapshot"] is SETTINGS_SNAP
    hint = str(call["hint"])
    assert hint.startswith("Continuing the user's request. Already done: open_app System Settings")
    assert hint.endswith("return no actions if nothing is left.")
    assert rig.types()[-1] == "done"
    assert [e.data["source"] for e in rig.events("plan")] == ["planner", "continue"]


def test_continuation_empty_plan_is_done() -> None:
    rig = _fast(Rig(planner=FakePlanner([Plan("s", ())])))
    rig.plan_route("go", Plan("a", (Action.open_app("System Settings"),)), "planner")
    _navigate_to(rig, SETTINGS_SNAP)
    rig.say("go")
    assert len(rig.planner.calls) == 1  # type: ignore[union-attr]
    assert rig.types()[-1] == "done"


def test_continuation_bounded_by_max_replans() -> None:
    plans = [Plan("s", (Action.open_app(name),)) for name in "BCDE"]  # distinct: a repeat is dropped
    rig = _fast(Rig(planner=FakePlanner(plans)))
    rig.plan_route("go", Plan("a", (Action.open_app("A"),)), "planner")
    n = 0

    def hook(_s: str, _a: Action) -> ActionResult | None:
        nonlocal n
        n += 1
        rig.provider.snap = UISnapshot(f"s{n}", AppInfo("x", f"bundle{n}"))
        return None

    rig.executor.hook = hook
    rig.say("go")
    assert len(rig.planner.calls) == Settings().max_replans  # type: ignore[union-attr]


def test_no_continuation_for_fastpath_or_when_screen_unchanged() -> None:
    rig = _fast(Rig(planner=FakePlanner()))
    rig.plan_route("open it", Plan("a", (Action.open_app("Notes"),)), "fastpath")
    rig.say("open it")
    assert rig.planner.calls == []  # type: ignore[union-attr]
    rig.plan_route("open it 2", Plan("a", (Action.open_app("Notes"),)), "planner")
    rig.say("open it 2")  # app never changes: polled, then treated as done
    assert rig.planner.calls == []  # type: ignore[union-attr]
    assert rig.types()[-1] == "done"


def test_continuation_plan_is_confirm_checked() -> None:
    send_snap = UISnapshot(
        "s", AppInfo("Mail", "com.apple.mail"), items=(UIItem(8, "element", "button", "Send"),)
    )
    rig = _fast(Rig(planner=FakePlanner([Plan("s", (Action.press(8),))])))
    rig.plan_route("mail", Plan("a", (Action.open_app("Mail"),)), "planner")
    _navigate_to(rig, send_snap)
    rig.say("mail")
    assert rig.types()[-1] == "confirm"
    assert rig.executor.actions == [Action.open_app("Mail")]


# ---- ambiguity answers

ASK_SNAP = UISnapshot(
    "k",
    AppInfo("Notes", BUNDLE),
    items=(
        UIItem(1, "element", "button", "Share"),
        UIItem(2, "element", "button", "Share Note"),
        UIItem(3, "menu", "menuitem", "Share", path=("File", "Share")),
    ),
)


def _ask(rig: Rig) -> None:
    rig.provider.snap = ASK_SNAP
    rig.plan_route("share", Plan("k", (), ask=(1, 3)), "planner")
    rig.say("share")


def test_ask_emits_labels_and_answer_picks_candidate() -> None:
    rig = Rig(planner=None)
    _ask(rig)
    data = rig.events("plan")[0].data
    assert data["ask"] == [1, 3]
    assert data["labels"] == ["Share", "File › Share"]
    rig.clear()
    rig.provider.snap = replace(ASK_SNAP, snapshot_id="k2")
    r = rig.say("the second one")
    assert r.kind == "planner"
    assert rig.executor.log == [("k2", Action.menu(3))]  # no planner: menu -> menu{id}
    assert rig.types()[-1] == "done"


def test_ask_answer_with_planner_gets_hint_and_fresh_ids() -> None:
    rig = Rig(planner=FakePlanner([Plan("k2", (Action.press(11),))]))
    _ask(rig)
    fresh = UISnapshot(
        "k2",
        AppInfo("Notes", BUNDLE),
        items=(UIItem(11, "element", "button", "Share"), *ASK_SNAP.items[2:]),
    )
    rig.provider.snap = fresh
    rig.say("one")
    call = rig.planner.calls[0]  # type: ignore[union-attr]
    assert call["text"] == "share"
    assert call["hint"] == 'User chose item 11 "Share". Use it as the target.'
    assert rig.executor.actions == [Action.press(11)]


def test_ask_without_planner_presses_element() -> None:
    rig = Rig(planner=None)
    _ask(rig)
    rig.say("number 1")
    assert rig.executor.actions == [Action.press(1)]


def test_ask_cleared_by_other_utterance_and_expires() -> None:
    rig = Rig(planner=None)
    _ask(rig)
    rig.say("gibberish")
    rig.say("one")
    assert rig.executor.log == []
    short = Rig(planner=None, settings=replace(Settings(), confirm_ttl_s=0.01))
    _ask(short)
    time.sleep(0.05)
    short.say("one")
    assert short.executor.log == []


def test_ask_answer_target_gone_errors() -> None:
    rig = Rig(planner=None)
    _ask(rig)
    rig.provider.snap = UISnapshot("k3", AppInfo("Notes", BUNDLE), items=())
    rig.say("one")
    assert rig.executor.log == []
    assert rig.types()[-1] == "error"


def test_filler_does_not_cancel_pending_confirm() -> None:
    """A Whisper silence hallucination between "delete" and "yes" must not cancel the confirm."""
    from brain.contracts import Action, ListSink, Plan, Route, Speaker, UISnapshot, Utterance
    from brain.flows import FlowStore
    from brain.pipeline import Brain

    snap = UISnapshot.from_dict(
        {
            "snapshot_id": "s-1",
            "app": {"name": "Notes", "bundle_id": "com.apple.Notes"},
            "items": [{"id": 137, "kind": "menu", "path": ["Edit", "Delete"], "shortcut": "cmd+delete"}],
        }
    )

    class _P:
        def snapshot(self) -> UISnapshot:
            return snap

        def list_apps(self) -> list[str]:
            return ["Notes"]

    class _E:
        def __init__(self) -> None:
            self.ran: list[Action] = []

        def run(self, snapshot_id: str, action: Action):  # type: ignore[no-untyped-def]
            from brain.contracts import ActionResult

            self.ran.append(action)
            return ActionResult(ok=True, changed=True, snapshot=snap)

        def undo(self) -> bool:
            return True

    class _R:
        def route(self, text: str, snapshot: UISnapshot) -> Route:
            plan = Plan(snapshot_id=snapshot.snapshot_id, actions=(Action.menu(137),))
            return Route(kind="fastpath", plan=plan)

    ex = _E()
    sink = ListSink()
    brain = Brain(_P(), ex, _R(), None, FlowStore(None), sink)

    def say(text: str) -> Route:
        return brain.handle(Utterance(id=text, text=text, speaker=Speaker(verified=True, score=1.0)))

    say("delete")
    assert "confirm" in sink.types() and not ex.ran
    n = len(sink.events)
    assert say("Thank you.").reason == "empty or filler utterance"
    assert sink.events[n:] == []  # no heard/error noise either
    say("yes")
    assert [a.target for a in ex.ran] == [137]


def _slack_like(
    sid: str, composer_label: str, row_role: str = "row", row_label: str = "Henrik"
) -> UISnapshot:
    return UISnapshot(
        sid,
        AppInfo("Slack", "com.tinyspeck.slackmacgap"),
        items=(
            UIItem(10, "element", row_role, row_label),
            UIItem(20, "element", "textarea", composer_label),
            UIItem(30, "element", "button", "Send"),
        ),
    )


def test_rebind_keeps_same_id_when_text_input_relabels() -> None:
    a = _slack_like("a", "Message #general")
    b = _slack_like("b", "Message Henrik")
    rig = Rig(planner=FakePlanner())
    rig.provider.snap = a
    rig.plan_route("dm", Plan("a", (Action.press(10), Action.type_text("hi", target=20))))
    rig.executor.hook = lambda _s, act: (
        ActionResult(ok=True, changed=True, snapshot=b) if act.op == "press" else None
    )
    rig.say("dm")
    assert rig.executor.log[-1] == ("b", Action.type_text("hi", target=20))
    assert rig.planner.calls == []  # type: ignore[union-attr]


def test_rebind_does_not_keep_relabeled_press_target() -> None:
    base = _slack_like("a", "x")
    a = replace(base, items=(*base.items[:2], UIItem(30, "element", "button", "Archive")))
    b = UISnapshot(
        "b",
        a.app,
        items=(
            UIItem(10, "element", "row", "Henrik"),
            UIItem(20, "element", "textarea", "x"),
            UIItem(30, "element", "button", "Delete"),
        ),  # id 30 now means something else
    )
    rig = Rig(planner=FakePlanner([Plan("b", ())]))
    rig.provider.snap = a
    rig.plan_route("go", Plan("a", (Action.press(10), Action.press(30))))
    rig.executor.hook = lambda _s, act: (
        ActionResult(ok=True, changed=True, snapshot=b) if act.target == 10 else None
    )
    rig.say("go")
    assert Action.press(30) not in rig.executor.actions
    assert "no longer resolve" in str(rig.planner.calls[0]["error"])  # type: ignore[union-attr]


def test_rebind_requires_same_role_for_kept_id() -> None:
    a = _slack_like("a", "Message #general")
    b = UISnapshot(
        "b", a.app, items=(UIItem(10, "element", "row", "Henrik"), UIItem(20, "element", "button", "Other"))
    )
    rig = Rig(planner=FakePlanner([Plan("b", ())]))
    rig.provider.snap = a
    rig.plan_route("dm", Plan("a", (Action.press(10), Action.type_text("hi", target=20))))
    rig.executor.hook = lambda _s, act: (
        ActionResult(ok=True, changed=True, snapshot=b) if act.op == "press" else None
    )
    rig.say("dm")
    assert Action.type_text("hi", target=20) not in rig.executor.actions


@pytest.mark.parametrize(
    "role", ["row", "cell", "outlinerow", "tab", "radiobutton", "sidebaritem", "listitem"]
)
def test_press_on_selected_item_is_success(role: str) -> None:
    s = _slack_like("a", "x", row_role=role)
    rig = Rig(planner=FakePlanner())
    rig.provider.snap = s
    rig.plan_route("go", Plan("a", (Action.press(10), Action.type_text("hi", target=20))))
    rig.executor.hook = lambda _s, act: ActionResult(ok=True, changed=False) if act.op == "press" else None
    rig.say("go")
    assert rig.planner.calls == []  # type: ignore[union-attr]
    assert rig.executor.actions == [Action.press(10), Action.type_text("hi", target=20)]
    assert rig.types()[-1] == "done"


def test_press_on_button_without_change_still_a_miss() -> None:
    rig = Rig(planner=FakePlanner([Plan("a", ())]))
    rig.plan_route("go", Plan("a", (Action.press(1),)))
    rig.executor.hook = lambda _s, _a: ActionResult(ok=True, changed=False)
    rig.say("go")
    assert "no effect" in str(rig.planner.calls[0]["error"])  # type: ignore[union-attr]


class SlowTimedPlanner(FakePlanner):
    """A planner that takes ``delay`` seconds and accepts a per-call ``timeout_s`` (like BonsaiPlanner)."""

    def __init__(self, plans: list[Plan], delay: float) -> None:
        super().__init__(plans)
        self.delay = delay
        self.timeouts: list[float | None] = []

    def plan(  # type: ignore[override]
        self,
        text: str,
        snapshot: UISnapshot,
        *,
        hint: str | None = None,
        error: str | None = None,
        timeout_s: float | None = None,
    ) -> Plan:
        self.timeouts.append(timeout_s)
        time.sleep(self.delay)
        return super().plan(text, snapshot, hint=hint, error=error)


def test_continuation_runs_when_less_than_bonsai_timeout_left() -> None:
    """Mac regression: 8 s budget, 6 s Bonsai timeout, a slow first plan must not block continuation."""
    settings = dataclasses.replace(
        Settings(), utterance_timeout_s=3.0, bonsai_timeout_s=6.0, min_plan_budget_s=0.5
    )
    planner = SlowTimedPlanner([Plan("s", (Action.press(5),))], delay=0.3)
    rig = _fast(Rig(planner=planner, settings=settings))
    rig.plan_route("dark mode", Plan("a", (Action.open_app("System Settings"),)), "planner")
    _navigate_to(rig, SETTINGS_SNAP)
    rig.say("dark mode")
    assert rig.executor.log[-1] == ("s", Action.press(5))
    assert rig.types()[-1] == "done"
    # the continuation call is capped to what's left of the 3 s budget, not the 6 s Bonsai timeout
    (timeout,) = planner.timeouts
    assert timeout is not None and timeout <= 3.0


def test_continuation_skipped_when_measured_plan_time_exceeds_budget() -> None:
    """With planner calls measured at ~0.6 s, a 0.7 s budget leaves no room for a continuation."""
    settings = dataclasses.replace(Settings(), utterance_timeout_s=0.7, min_plan_budget_s=0.1)
    planner = SlowTimedPlanner([Plan("s", ())], delay=0.6)
    rig = _fast(Rig(planner=planner, settings=settings))
    rig.plan_route("dark mode", Plan("a", (Action.open_app("System Settings"),)), "planner")
    rig.brain._plan_s_ema = 0.6  # 1.25 x 0.6 = 0.75 s expected > 0.7 s budget
    _navigate_to(rig, SETTINGS_SNAP)
    rig.say("dark mode")
    assert planner.calls == []
    assert rig.types()[-1] == "done"


def test_plan_and_action_events_carry_target_names() -> None:
    """Overlays show "File › New Note", not "menu #110"."""
    from brain.fixtures import FakeExecutor as FxExecutor
    from brain.fixtures import FixtureUIProvider
    from brain.pipeline import build_brain

    provider = FixtureUIProvider("notes")
    sink = ListSink()
    settings = dataclasses.replace(Settings(), flows_path=None)  # type: ignore[arg-type]
    brain = build_brain(provider, FxExecutor(provider), sink, settings=settings, use_model=False)
    brain.handle(Utterance(id="u1", text="new note"))
    plan = next(e for e in sink.events if e.type == "plan")
    assert "New Note" in " ".join(plan.data["targets"].values())
    action = next(e for e in sink.events if e.type == "action")
    assert action.data["label"].endswith("New Note")


# ---- loop guard: a continuation/replan must not repeat the action that just succeeded


def _view_snap(sid: str, window: str, appearance: int) -> UISnapshot:
    return UISnapshot(
        sid,
        AppInfo("Settings", "com.apple.systempreferences"),
        window=window,
        items=(UIItem(appearance, "menu", "menuitem", "Appearance", path=("View", "Appearance")),),
    )


def _view_rig(plans: list[Plan]) -> Rig:
    rig = _fast(Rig(planner=FakePlanner(plans)))
    rig.provider.snap = _view_snap("v1", "General", 20)
    after = _view_snap("v2", "Appearance", 30)

    def hook(_s: str, a: Action) -> ActionResult | None:
        rig.provider.snap = after
        return ActionResult(ok=True, changed=True, snapshot=None)

    rig.executor.hook = hook
    rig.plan_route("make it dark", Plan("v1", (Action.menu(20),)), "planner")
    return rig


def test_continuation_skips_repeat_of_last_action() -> None:
    rig = _view_rig([Plan("v2", (Action.menu(30),))])  # planner picks View > Appearance again
    rig.say("make it dark")
    assert rig.executor.actions == [Action.menu(20)]
    assert rig.types()[-1] == "done"
    assert not rig.events("error")


def test_continuation_keeps_remaining_actions_after_dropped_repeat() -> None:
    rig = _view_rig([Plan("v2", (Action.menu(30), Action.key("cmd+d")))])
    rig.say("make it dark")
    assert rig.executor.actions == [Action.menu(20), Action.key("cmd+d")]


def test_continuation_different_action_still_runs() -> None:
    other = UISnapshot(
        "v2",
        AppInfo("Settings", "com.apple.systempreferences"),
        window="Appearance",
        items=(UIItem(31, "menu", "menuitem", "Zoom", path=("View", "Zoom")),),
    )
    rig = _view_rig([Plan("v2", (Action.menu(31),))])
    rig.executor.hook = lambda _s, _a: (setattr(rig.provider, "snap", other), None)[1]
    rig.say("make it dark")
    assert rig.executor.actions == [Action.menu(20), Action.menu(31)]


# ---- destructive template through the real router: confirm first, then execute


def test_remove_this_note_confirms_then_executes() -> None:
    from brain.fastpath import FastPath
    from brain.router import Router

    notes = UISnapshot(
        "n1",
        AppInfo("Notes", BUNDLE),
        items=(
            UIItem(2, "element", "button", "Delete"),
            UIItem(137, "menu", "menuitem", "Delete", path=("Edit", "Delete"), shortcut="cmd+delete"),
        ),
    )
    provider, flows, sink = FakeProvider(notes), FlowStore(), ListSink()
    ex = FakeExecutor(provider)
    brain = Brain(provider, ex, Router(FastPath(["Notes"]), flows, None), None, flows, sink)
    brain.handle(Utterance(id="u1", text="remove this note"))
    assert [e.type for e in sink.events][-2:] == ["plan", "confirm"]
    assert ex.log == []
    brain.handle(Utterance(id="u2", text="yes"))
    assert ex.actions == [Action.menu(137)]
    brain.handle(Utterance(id="u3", text="delete everything"))
    assert ex.actions == [Action.menu(137)]  # no router match, no planner: nothing runs
    assert sink.events[-1].type == "error"


def test_continuation_never_rechooses_already_open_pane() -> None:
    """open settings -> Appearance row pressed -> planner re-picks the row, then Dark: only Dark runs."""
    app = AppInfo("System Settings", "com.apple.systempreferences")

    def pane(sid: str, window: str) -> UISnapshot:
        return UISnapshot(
            sid,
            app,
            window=window,
            items=(
                UIItem(15, "element", "row", "Appearance"),
                UIItem(42, "element", "button", "Dark"),
            ),
        )

    plans = [Plan("s1", (Action.press(15),)), Plan("s2", (Action.press(15), Action.press(42)))]
    rig = _fast(Rig(planner=FakePlanner(plans)))
    rig.provider.snap = UISnapshot("s0", AppInfo("Finder", "com.apple.finder"))
    rig.plan_route("dark", Plan("s0", (Action.open_app("System Settings"),)), "planner")

    def hook(_s: str, a: Action) -> ActionResult | None:
        if a.op == "open_app":
            rig.provider.snap = pane("s1", "General")
        elif a.target == 15:
            rig.provider.snap = pane("s2", "Appearance")
        return None

    rig.executor.hook = hook
    rig.say("dark")
    assert rig.executor.actions == [Action.open_app("System Settings"), Action.press(15), Action.press(42)]
    assert 'Do NOT select "Appearance" again' in str(rig.planner.calls[1]["hint"])  # type: ignore[union-attr]
    assert rig.types()[-1] == "done" and not rig.events("error")


def test_failure_messages_name_the_target_id() -> None:
    """The planner parses "(press|menu) <id>" from ERROR to exclude the item that didn't work."""
    from brain.pipeline import _op_ref

    assert _op_ref(Action.menu(110)) == "menu 110"
    assert _op_ref(Action.press(7)) == "press 7"
    assert _op_ref(Action.key("cmd+n")) == "keys"
