from __future__ import annotations

import pytest

from brain.contracts import Action, AppInfo, Plan, UIItem, UISnapshot
from brain.safety import DANGER_RE, confirm_reason, needs_confirm

SNAP = UISnapshot(
    snapshot_id="s1",
    app=AppInfo("Mail", "com.apple.mail"),
    items=(
        UIItem(1, "element", "button", "Send"),
        UIItem(2, "element", "button", "New Message"),
        UIItem(3, "menu", path=("File", "Move to Trash"), label="Move to Trash"),
        UIItem(4, "menu", path=("Mail", "Quit Mail"), label="Quit Mail"),
        UIItem(5, "element", "button", "Sender info"),
        UIItem(6, "menu", path=("File", "New Window"), label="New Window"),
        UIItem(7, "element", "button", "Delete"),
    ),
)


@pytest.mark.parametrize(
    "text",
    [
        "Send",
        "send message",
        "Delete",
        "Remove account",
        "Quit Mail",
        "Erase disk",
        "Empty Trash",
        "Sign Out",
        "Log out",
        "Move to Trash",
        "Discard changes",
        "SEND",
    ],
)
def test_danger_positive(text: str) -> None:
    assert DANGER_RE.search(text)


@pytest.mark.parametrize(
    "text", ["Sender", "Resend info", "Formatting bar", "Format", "New Note", "Undelete", "", "Quite"]
)
def test_danger_negative(text: str) -> None:
    assert not DANGER_RE.search(text)


def test_plan_confirm_flag() -> None:
    assert needs_confirm(Plan("s1", (Action.press(2),), confirm=True), SNAP)


def test_press_dangerous_label() -> None:
    assert needs_confirm(Plan("s1", (Action.press(1),)), SNAP)
    assert needs_confirm(Plan("s1", (Action.press(7),)), SNAP)


def test_press_safe_label() -> None:
    assert not needs_confirm(Plan("s1", (Action.press(2),)), SNAP)
    assert not needs_confirm(Plan("s1", (Action.press(5),)), SNAP)


def test_menu_path_matches() -> None:
    assert needs_confirm(Plan("s1", (Action.menu(3),)), SNAP)
    assert needs_confirm(Plan("s1", (Action.menu(4),)), SNAP)
    assert not needs_confirm(Plan("s1", (Action.menu(6),)), SNAP)


def test_unknown_target_ignored() -> None:
    assert not needs_confirm(Plan("s1", (Action.press(99),)), SNAP)


@pytest.mark.parametrize(
    "keys",
    [
        "cmd+q",
        "CMD+Q",
        "command+q",
        "cmd+delete",
        "cmd+backspace",
        "shift+cmd+delete",
        "cmd+option+esc",
        "option+cmd+escape",
    ],
)
def test_dangerous_keys(keys: str) -> None:
    assert needs_confirm(Plan("s1", (Action.key(keys),)), SNAP)


@pytest.mark.parametrize("keys", ["cmd+n", "cmd+z", "enter", "cmd+w"])
def test_safe_keys(keys: str) -> None:
    assert not needs_confirm(Plan("s1", (Action.key(keys),)), SNAP)


def test_type_text_not_scanned() -> None:
    assert not needs_confirm(Plan("s1", (Action.type_text("please delete this"),)), SNAP)


def test_confirm_reason() -> None:
    assert confirm_reason(Plan("s1", (Action.press(2),)), SNAP) is None
    assert "Send" in (confirm_reason(Plan("s1", (Action.press(1),)), SNAP) or "")
    assert "cmd+q" in (confirm_reason(Plan("s1", (Action.key("cmd+q"),)), SNAP) or "")
    assert confirm_reason(Plan("s1", (), confirm=True, say="Really?"), SNAP) == "Really?"
    assert confirm_reason(Plan("s1", (), confirm=True), SNAP)


MENU_SNAP = UISnapshot(
    "m",
    AppInfo("Notes", "com.apple.Notes"),
    items=(
        UIItem(1, "menu", "menuitem", "Bold", path=("Format", "Bold"), shortcut="cmd+b"),
        UIItem(2, "menu", "menuitem", "Delete", path=("Edit", "Delete"), shortcut="delete"),
        UIItem(3, "menu", "menuitem", "Send", path=("Message", "Send"), shortcut="cmd+return"),
        UIItem(4, "menu", "menuitem", "New", path=("File", "New"), shortcut="cmd+n"),
        UIItem(5, "menu", "menuitem", "Format", path=("Format", "Format")),
    ),
)


@pytest.mark.parametrize(
    "keys",
    [
        "cmd+shift+q",
        "cmd+alt+shift+q",
        "cmd+option+shift+delete",
        "shift+cmd+backspace",
        "ctrl+cmd+option+esc",
    ],
)
def test_dangerous_key_supersets(keys: str) -> None:
    assert needs_confirm(Plan("s1", (Action.key(keys),)), SNAP)


@pytest.mark.parametrize("keys", ["delete", "cmd+return"])
def test_key_owned_by_dangerous_menu_item(keys: str) -> None:
    assert needs_confirm(Plan("m", (Action.key(keys),)), MENU_SNAP)
    assert not needs_confirm(Plan("s1", (Action.key(keys),)), SNAP)  # no menu item owns it there


def test_key_owned_by_safe_menu_item() -> None:
    assert not needs_confirm(Plan("m", (Action.key("cmd+b"),)), MENU_SNAP)
    assert not needs_confirm(Plan("m", (Action.key("cmd+n"),)), MENU_SNAP)


def test_format_menu_is_not_destructive() -> None:
    assert not needs_confirm(Plan("m", (Action.menu(1),)), MENU_SNAP)
    assert not needs_confirm(Plan("m", (Action.menu(5),)), MENU_SNAP)
    assert needs_confirm(Plan("m", (Action.menu(2),)), MENU_SNAP)
