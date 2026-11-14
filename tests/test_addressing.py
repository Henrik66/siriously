"""Wake word, follow-up window and menu-bar permissions (brain.addressing), as wired into the Brain."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from brain.addressing import Prefs, risk_categories, sounds_like_command, strip_wake, verdict, wake_rx
from brain.contracts import Action, AppInfo, ListSink, Plan, Route, Speaker, UIItem, UISnapshot, Utterance
from brain.flows import FlowStore
from brain.pipeline import Brain
from tests.test_pipeline import FakeExecutor, FakeProvider, FakeRouter

RX = wake_rx("siriously")


@pytest.mark.parametrize(
    ("text", "woke", "body"),
    [
        ("Seriously, open Safari.", True, "open Safari."),
        ("Hey Siriously open notes", True, "open notes"),
        ("Right. Seriously, text Omar hello.", True, "text Omar hello."),
        ("That's seriously cool.", False, "That's seriously cool."),
        ("I told him that was a really dumb idea. Seriously, he never listens.", False, None),
        ("Seriously.", True, ""),
    ],
)
def test_strip_wake(text: str, woke: bool, body: str | None) -> None:
    w, b = strip_wake(text, RX)
    assert w is woke
    assert b == (text if body is None else body)


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("Oh, it worked that time. I just got shy.", False),
        ("It's not what I meant, not fair.", False),
        ("Notify anyway?", False),
        ("I undo everything.", False),
        ("undo that", True),
        ("and now open Safari", True),
        ("Can you open notes?", True),
        ("Okay, close it.", True),
        ("go to safari", True),
    ],
)
def test_follow_up_must_sound_like_a_command(text: str, ok: bool) -> None:
    assert sounds_like_command(text) is ok


NOTES = UISnapshot(
    "s1",
    AppInfo("Messages", "com.apple.MobileSMS"),
    items=(
        UIItem(1, "element", "textfield", "Message"),
        UIItem(2, "menu", "menuitem", "Delete Conversation…", path=("Conversation", "Delete Conversation…")),
        UIItem(3, "menu", "menuitem", "Move to Trash", path=("File", "Move to Trash")),
    ),
)


def test_risk_categories() -> None:
    send = [Action.type_text("hello", 1), Action.key("return")]
    assert risk_categories("hello", send, NOTES) == {"send_messages"}
    assert risk_categories("delete it", [Action.menu(2)], NOTES) == {"delete"}
    assert risk_categories("trash it", [Action.menu(3)], NOTES) == {"trash"}
    assert risk_categories("quit", [Action.key("cmd+q")], NOTES) == {"quit_apps"}
    assert risk_categories("type send me the file", [Action.type_text("send me the file")], NOTES) == set()


def _prefs(tmp_path: Path, **over: object) -> Prefs:
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(over), encoding="utf-8")
    return Prefs(p)


def test_verdict_follows_settings(tmp_path: Path) -> None:
    send = Plan("s1", (Action.type_text("hello", 1), Action.key("return")))
    assert verdict(send, NOTES, "hello", _prefs(tmp_path)).mode == "ask"
    allow = _prefs(tmp_path, permissions={"send_messages": "allow"})
    assert verdict(send, NOTES, "hello", allow).mode == "allow"
    block = _prefs(tmp_path, permissions={"send_messages": "block"})
    v = verdict(send, NOTES, "hello", block)
    assert v.mode == "block" and v.reason and "blocked" in v.reason
    trash = Plan("s1", (Action.menu(3),))
    assert verdict(trash, NOTES, "move it to the trash", _prefs(tmp_path)).mode == "allow"  # default: allow


class WakeRig:
    def __init__(self, prefs: Prefs) -> None:
        self.provider = FakeProvider()
        self.executor = FakeExecutor(self.provider)
        self.router = FakeRouter(FlowStore())
        self.sink = ListSink()
        self.brain = Brain(
            self.provider, self.executor, self.router, None, FlowStore(), self.sink, prefs=prefs
        )
        self.n = 0
        self.router.plans["open safari"] = Route(
            kind="fastpath", plan=Plan("a", (Action.open_app("Safari"),))
        )
        self.router.plans["oh it worked"] = Route(kind="fastpath", plan=Plan("a", (Action.key("cmd+z"),)))

    def say(self, text: str, *, verified: bool = True, t_end: float | None = None, **sp: float) -> Route:
        self.n += 1
        spk = Speaker(verified=verified, score=sp.get("score", 1.0), threshold=sp.get("threshold"))
        t = time.time() if t_end is None else t_end
        return self.brain.handle(Utterance(id=f"u{self.n}", text=text, t_speech_end=t, speaker=spk))

    def types(self) -> list[str]:
        return [e.type for e in self.sink.events]


def test_wake_word_required_and_follow_up_window(tmp_path: Path) -> None:
    rig = WakeRig(_prefs(tmp_path, follow_up_seconds=10))
    assert rig.say("open safari").reason == "not addressed"
    assert rig.sink.events == []  # unaddressed speech is silent: no overlay
    assert rig.say("Seriously, open safari").kind == "fastpath"
    done = [e for e in rig.sink.events if e.type == "done"][-1]
    assert done.data["follow_up_s"] == 10
    assert rig.say("open safari").kind == "fastpath"  # follow-up, no wake word
    assert rig.say("Oh it worked").reason == "not addressed"  # inside the window but not a command
    rig.brain._follow.last_active = time.time() - 30
    assert rig.say("open safari").reason == "not addressed"  # window over


def test_wake_word_only_listens(tmp_path: Path) -> None:
    rig = WakeRig(_prefs(tmp_path))
    assert rig.say("Siriously.").reason == "wake word only"
    assert rig.sink.events[-1].data["text"] == "Listening…"
    assert rig.say("open safari").kind == "fastpath"


def test_rejected_only_when_addressed(tmp_path: Path) -> None:
    rig = WakeRig(_prefs(tmp_path))
    rig.say("we should get dinner", verified=False, score=-1.5)
    assert rig.sink.events == []
    rig.say("Seriously, delete everything", verified=False, score=-1.5)
    assert rig.types() == ["rejected"]


def test_short_yes_slightly_below_threshold_confirms(tmp_path: Path) -> None:
    rig = WakeRig(_prefs(tmp_path))
    send = Plan("a", (Action.press(3),))  # SNAP_A item 3 is the "Send" button
    rig.router.plans["send it"] = Route(kind="fastpath", plan=send)
    rig.say("Seriously, send it")
    assert rig.types()[-1] == "confirm"
    rig.say("Yeah.", verified=False, score=0.1, threshold=0.4)
    assert rig.executor.actions == [Action.press(3)]


def test_blocked_category_never_runs(tmp_path: Path) -> None:
    rig = WakeRig(_prefs(tmp_path, permissions={"send_messages": "block"}))
    rig.router.plans["send it"] = Route(kind="fastpath", plan=Plan("a", (Action.press(3),)))
    rig.say("Seriously, send it")
    assert rig.executor.actions == []
    assert "blocked" in rig.sink.events[-1].data["message"]


def test_no_prefs_keeps_old_behaviour() -> None:
    rig = WakeRig(Prefs(Path("/nonexistent/settings.json")))
    rig.brain._prefs = None
    assert rig.say("open safari").kind == "fastpath"
