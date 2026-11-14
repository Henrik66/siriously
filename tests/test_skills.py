"""Mac skills (brain.skills): texting with a recipient check, new folder with an on-disk name check."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import brain.skills as skills
from brain.addressing import Prefs
from brain.contracts import Action, ActionResult, AppInfo, ListSink, Speaker, UIItem, UISnapshot, Utterance
from brain.flows import FlowStore
from brain.pipeline import Brain
from tests.test_pipeline import FakeRouter


def messages(sid: str, window: str = "", typed: str = "") -> UISnapshot:
    return UISnapshot(
        sid,
        AppInfo("Messages", "com.apple.MobileSMS"),
        window=window,
        items=(
            UIItem(10, "element", "row", "Hariprasad Gridharan, I cant bro, 2:31 PM"),
            UIItem(11, "element", "row", "Hari Patel, see you, Yesterday"),
            UIItem(12, "element", "row", "Omar Haddad, lol, 1:02 PM"),
            UIItem(20, "element", "textfield", "Message", value=typed),
        ),
    )


class MessagesProvider:
    """Pressing a row opens that conversation (window title = the person)."""

    def __init__(self) -> None:
        self.snap = messages("m0")
        self.n = 0

    def snapshot(self) -> UISnapshot:
        return self.snap

    def list_apps(self) -> list[str]:
        return ["Messages", "Finder"]


class MessagesExecutor:
    def __init__(self, p: MessagesProvider) -> None:
        self.p = p
        self.actions: list[Action] = []

    def run(self, snapshot_id: str, action: Action) -> ActionResult:
        self.actions.append(action)
        self.p.n += 1
        s = self.p.snap
        if action.op == "press" and action.target in (10, 11, 12):
            label = next(i.label for i in s.items if i.id == action.target)
            s = messages(f"m{self.p.n}", window=label.split(",")[0])
        elif action.op == "type":
            s = messages(f"m{self.p.n}", window=s.window, typed=action.text or "")
        else:
            s = messages(f"m{self.p.n}", window=s.window)
        self.p.snap = s
        return ActionResult(ok=True, changed=True, snapshot=s)

    def undo(self) -> bool:
        return False


def _rig(tmp_path: Path, **settings: object) -> tuple[Brain, MessagesExecutor, ListSink]:
    (tmp_path / "s.json").write_text(json.dumps({"wake_word_required": False, **settings}), encoding="utf-8")
    p = MessagesProvider()
    x = MessagesExecutor(p)
    sink = ListSink()
    brain = Brain(p, x, FakeRouter(FlowStore()), None, FlowStore(), sink, prefs=Prefs(tmp_path / "s.json"))
    return brain, x, sink


def _say(brain: Brain, text: str, n: list[int] = [0]) -> None:  # noqa: B006
    n[0] += 1
    brain.handle(Utterance(id=f"t{n[0]}", text=text, speaker=Speaker(verified=True, score=1.0)))


def test_parse_text_command() -> None:
    assert skills.parse_text_command("Text Omar hello.", "Finder") == "omar hello"
    assert skills.parse_text_command("send a message to Hari see you", "Finder") == "hari see you"
    assert skills.parse_text_command("message the channel hi", "Slack") is None


def test_match_people_dedupes_and_tolerates_spelling() -> None:
    convos = skills.conversations(messages("m"))
    assert [n for n, _ in skills.match_people("omar", convos)] == ["Omar Haddad"]
    assert [n for n, _ in skills.match_people("harry", convos)] == ["Hariprasad Gridharan", "Hari Patel"]


def test_text_asks_which_then_confirms_then_sends(tmp_path: Path) -> None:
    brain, x, sink = _rig(tmp_path)
    _say(brain, "text Hari see you soon")
    assert sink.events[-1].type == "ask"
    assert sink.events[-1].data["options"] == ["Hariprasad Gridharan", "Hari Patel"]
    _say(brain, "the second one")
    assert sink.events[-1].type == "confirm"
    assert sink.events[-1].data["plan"]["say"] == "Send “see you soon” to Hari Patel"
    assert Action.key("return") not in x.actions
    _say(brain, "yes")
    assert x.actions[-1] == Action.key("return")
    assert Action.press(11) in x.actions


def test_text_declined_clears_the_draft(tmp_path: Path) -> None:
    brain, x, sink = _rig(tmp_path)
    _say(brain, "text Omar hello")
    assert sink.events[-1].type == "confirm"
    _say(brain, "no")
    assert x.actions[-3:] == [Action.press(20), Action.key("cmd+a"), Action.key("delete")]
    assert Action.key("return") not in x.actions


def test_text_allowed_sends_without_asking(tmp_path: Path) -> None:
    brain, x, sink = _rig(tmp_path, permissions={"send_messages": "allow"})
    _say(brain, "text Omar hello")
    assert x.actions[-1] == Action.key("return")
    assert sink.events[-1].type == "done"


def test_text_without_message_asks_for_it(tmp_path: Path) -> None:
    brain, x, sink = _rig(tmp_path, permissions={"send_messages": "allow"})
    _say(brain, "text Omar")
    assert sink.events[-1].type == "ask"
    _say(brain, "running late")
    assert Action.type_text("running late", 20) in x.actions
    assert x.actions[-1] == Action.key("return")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('make a new folder and downloads called "Verity".', ("Verity", "downloads")),
        ("make a folder in downloads called test", ("test", "downloads")),
        ("Create a new folder called Project Notes on the desktop.", ("Project Notes", "desktop")),
        ("new folder", ("", None)),
        ("open the downloads folder", None),
    ],
)
def test_folder_command(text: str, expected: tuple[str, str | None] | None) -> None:
    assert skills.folder_command(text) == expected


def test_taken_folder_name_offers_the_next_free_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "dl" / "test").mkdir(parents=True)
    monkeypatch.setitem(skills.PLACE_DIRS, "downloads", str(tmp_path / "dl"))
    brain, x, sink = _rig(tmp_path)
    _say(brain, "make a folder in downloads called test")
    assert x.actions == []  # nothing touched Finder
    assert sink.events[-1].type == "confirm"
    assert "Make “test 2” instead" in sink.events[-1].data["plan"]["say"]
    _say(brain, "yes")
    assert Action.type_text("test 2") in x.actions
