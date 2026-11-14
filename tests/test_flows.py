from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain.contracts import Action, AppInfo, TargetDescriptor, UIItem, UISnapshot
from brain.flows import Flow, FlowStep, FlowStore, parse_teach, resolve_step


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("call that standup", "standup"),
        ("Call that Standup.", "standup"),
        ("name that morning routine", "morning routine"),
        ("remember that as  My Flow ", "my flow"),
        ("please call this daily setup", "daily setup"),
        ("okay, call that standup", "standup"),
        ("Hey Siriously, call that standup", "standup"),
        ("ok call that standup please", "standup"),
    ],
)
def test_parse_teach(text: str, name: str) -> None:
    assert parse_teach(text) == name


@pytest.mark.parametrize(
    "text",
    [
        "call mom",
        "open notes",
        "call that",
        "",
        "save the file",
        "save this as todo",
        "save that as report",
        "call it a day",
        "call that stop",
        "okay call that cancel",
        "call that undo",
        "call that yes",
    ],
)
def test_parse_teach_negative(text: str) -> None:
    assert parse_teach(text) is None


def _flow(name: str = "standup") -> Flow:
    desc = TargetDescriptor("element", "button", "New Note", (), "com.apple.Notes")
    mdesc = TargetDescriptor("menu", "menuitem", "New Note", ("File", "New Note"), "com.apple.Notes")
    return Flow(
        name,
        (
            FlowStep(Action.open_app("Notes")),
            FlowStep(Action.press(1), desc),
            FlowStep(Action.menu(101), mdesc),
            FlowStep(Action.type_text("hi")),
        ),
        "open notes and start a note",
    )


def test_flow_dict_roundtrip() -> None:
    f = _flow()
    assert Flow.from_dict(json.loads(json.dumps(f.to_dict()))) == f


def test_store_in_memory() -> None:
    s = FlowStore()
    assert s.names() == []
    s.save(_flow("Standup"))
    assert s.names() == ["standup"]
    assert s.get("STANDUP") is not None
    assert s.get("nope") is None


def test_store_persistence_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "dir" / "flows.json"
    FlowStore(path).save(_flow())
    assert path.exists()
    again = FlowStore(path)
    assert again.get("standup") == _flow()
    assert not list(path.parent.glob("*.tmp"))


def test_store_tolerates_missing_and_corrupt(tmp_path: Path) -> None:
    assert FlowStore(tmp_path / "missing.json").names() == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    s = FlowStore(bad)
    assert s.names() == []
    s.save(_flow())  # recovers by overwriting
    assert FlowStore(bad).names() == ["standup"]
    bad.write_text(json.dumps({"flows": [{"oops": 1}, _flow("ok").to_dict()]}), encoding="utf-8")
    assert FlowStore(bad).names() == ["ok"]
    bad.write_text("[1, 2]", encoding="utf-8")
    assert FlowStore(bad).names() == []


@pytest.fixture
def store() -> FlowStore:
    s = FlowStore()
    s.save(_flow("standup"))
    s.save(_flow("morning routine"))
    return s


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("standup", "standup"),
        ("Standup.", "standup"),
        ("run standup", "standup"),
        ("do the standup", "standup"),
        ("play morning routine", "morning routine"),
        ("stand up", "standup"),
        ("standups", "standup"),
        ("run morning routin", "morning routine"),
    ],
)
def test_match_positive(store: FlowStore, text: str, expected: str) -> None:
    assert store.match(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "open the standup notes for me please",
        "please write a long message about the standup meeting",
        "open notes",
        "",
        "run something else",
    ],
)
def test_match_negative(store: FlowStore, text: str) -> None:
    assert store.match(text) is None


def test_match_empty_store() -> None:
    assert FlowStore().match("standup") is None


SNAP = UISnapshot(
    "s2",
    AppInfo("Notes", "com.apple.Notes"),
    items=(
        UIItem(50, "element", "button", "New Note"),
        UIItem(51, "element", "textarea", "Body"),
        UIItem(52, "element", "button", "Share"),
        UIItem(53, "element", "popup", "Share"),
        UIItem(60, "menu", "menuitem", "New Note", path=("File", "New Note")),
        UIItem(61, "menu", "menuitem", "New Note", path=("Edit", "New Note")),
        UIItem(62, "menu", "menuitem", "Rename", path=("File", "Rename")),
    ),
)


def test_resolve_no_target_returns_action() -> None:
    step = FlowStep(Action.type_text("hi"))
    assert resolve_step(step, SNAP) is step.action


def test_resolve_missing_descriptor_for_targeted_action() -> None:
    assert resolve_step(FlowStep(Action.press(1)), SNAP) is None


def test_resolve_element_role_and_label() -> None:
    d = TargetDescriptor("element", "button", "New Note", (), "com.apple.Notes")
    out = resolve_step(FlowStep(Action.press(1), d), SNAP)
    assert out == Action.press(50)


def test_resolve_element_label_fallback_unique_only() -> None:
    d = TargetDescriptor("element", "textfield", "Body")
    assert resolve_step(FlowStep(Action.press(1), d), SNAP) == Action.press(51)
    amb = TargetDescriptor("element", "link", "Share")
    assert resolve_step(FlowStep(Action.press(1), amb), SNAP) is None
    assert (
        resolve_step(FlowStep(Action.press(1), TargetDescriptor("element", "button", "Nope")), SNAP) is None
    )


def test_resolve_element_ignores_menus() -> None:
    d = TargetDescriptor("element", "menuitem", "Rename")
    assert resolve_step(FlowStep(Action.press(1), d), SNAP) is None


def test_resolve_menu_path_then_leaf() -> None:
    d = TargetDescriptor("menu", "menuitem", "New Note", ("Edit", "New Note"))
    assert resolve_step(FlowStep(Action.menu(9), d), SNAP) == Action.menu(61)
    moved = TargetDescriptor("menu", "menuitem", "Rename", ("Format", "Rename"))
    assert resolve_step(FlowStep(Action.menu(9), moved), SNAP) == Action.menu(62)
    gone = TargetDescriptor("menu", "menuitem", "Gone", ("File", "Gone"))
    assert resolve_step(FlowStep(Action.menu(9), gone), SNAP) is None


def test_resolve_wrong_app() -> None:
    d = TargetDescriptor("element", "button", "New Note", (), "com.other.app")
    assert resolve_step(FlowStep(Action.press(1), d), SNAP) is None


def test_resolve_keeps_other_fields() -> None:
    d = TargetDescriptor("element", "textarea", "Body")
    out = resolve_step(FlowStep(Action.type_text("hello", target=7), d), SNAP)
    assert out == Action.type_text("hello", target=51)


def test_unreadable_flows_file_is_backed_up_before_write(tmp_path: Path) -> None:
    path = tmp_path / "flows.json"
    path.write_text("{not json", encoding="utf-8")
    s = FlowStore(path)
    s.save(_flow())
    backups = list(tmp_path.glob("flows.json.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{not json"
    s.save(_flow("two"))  # only the first write backs up
    assert len(list(tmp_path.glob("flows.json.bak-*"))) == 1
    assert FlowStore(path).names() == ["standup", "two"]


def test_partially_readable_flows_file_is_backed_up(tmp_path: Path) -> None:
    path = tmp_path / "flows.json"
    path.write_text(json.dumps({"flows": [{"oops": 1}, _flow("ok").to_dict()]}), encoding="utf-8")
    s = FlowStore(path)
    s.save(_flow("new"))
    assert len(list(tmp_path.glob("flows.json.bak-*"))) == 1


def test_healthy_file_is_not_backed_up(tmp_path: Path) -> None:
    path = tmp_path / "flows.json"
    s = FlowStore(path)
    s.save(_flow())
    s.save(_flow("two"))
    assert not list(tmp_path.glob("*.bak-*"))


def test_settings_read_env_at_construction(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from brain.config import Settings

    monkeypatch.setenv("SIRIOUSLY_FLOWS", "~/my-flows.json")
    monkeypatch.setenv("SIRIOUSLY_CONFIRM_TTL_S", "7")
    monkeypatch.setenv("SIRIOUSLY_PORT", "9999")
    s = Settings()
    assert s.flows_path == Path("~/my-flows.json").expanduser()
    assert "~" not in str(s.flows_path)
    assert s.confirm_ttl_s == 7.0
    assert s.port == 9999
    monkeypatch.delenv("SIRIOUSLY_CONFIRM_TTL_S")
    assert Settings().confirm_ttl_s == 20.0
