from __future__ import annotations

import time
from typing import Any

import pytest

from brain.contracts import Action, Plan, UISnapshot
from brain.fastpath import FastPath, parse_keys

APPS = ["Finder", "Notes", "System Settings", "Calendar", "Slack", "Visual Studio Code", "Safari", "Mail"]


def snap(items: list[dict[str, Any]], app: str = "Notes", sid: str = "s1") -> UISnapshot:
    return UISnapshot.from_dict({"snapshot_id": sid, "app": {"name": app}, "items": items})


def menu(i: int, *path: str, shortcut: str | None = None, enabled: bool = True) -> dict[str, Any]:
    return {"id": i, "kind": "menu", "path": list(path), "shortcut": shortcut, "enabled": enabled}


def el(i: int, role: str, label: str, enabled: bool = True) -> dict[str, Any]:
    return {"id": i, "kind": "element", "role": role, "label": label, "enabled": enabled}


NOTES = snap(
    [
        el(1, "button", "New Note"),
        el(2, "button", "Share"),
        el(3, "button", "Delete"),
        el(7, "textarea", "Note body"),
        menu(101, "File", "New Note", shortcut="cmd+n"),
        menu(102, "File", "New Folder", shortcut="cmd+shift+n"),
        menu(103, "File", "Close", shortcut="cmd+w"),
        menu(104, "File", "Print…", shortcut="cmd+p"),
        menu(110, "Edit", "Undo", shortcut="cmd+z"),
        menu(111, "Edit", "Copy", shortcut="cmd+c"),
        menu(112, "Edit", "Paste", shortcut="cmd+v", enabled=False),
        menu(113, "Edit", "Select All", shortcut="cmd+a"),
    ],
)
FINDER = snap(
    [
        menu(201, "File", "New Finder Window", shortcut="cmd+n"),
        menu(202, "File", "New Folder", shortcut="cmd+shift+n"),
        menu(203, "File", "New Folder with Selection"),
        menu(204, "Go", "Downloads", shortcut="cmd+alt+l"),
        menu(205, "Go", "Documents"),
        menu(206, "Window", "Show Next Tab"),
        menu(207, "File", "New Tab", shortcut="cmd+t"),
        menu(208, "File", "Close Tab", shortcut="cmd+w"),
        el(5, "button", "Back"),
    ],
    app="Finder",
)
EMPTY = snap([], app="Safari")


@pytest.fixture(scope="module")
def fp() -> FastPath:
    return FastPath(APPS)


def only(plan: Plan | None) -> Action:
    assert plan is not None
    assert len(plan.actions) == 1
    return plan.actions[0]


class TestOpenApp:
    @pytest.mark.parametrize(
        ("text", "app"),
        [
            ("open notes", "Notes"), ("Open Notes.", "Notes"), ("launch finder", "Finder"),
            ("switch to slack", "Slack"), ("go to calendar", "Calendar"), ("bring up safari", "Safari"),
            ("start mail", "Mail"), ("open the notes app", "Notes"), ("please open notes", "Notes"),
            ("open settings", "System Settings"), ("open system settings", "System Settings"),
            ("open vs code", "Visual Studio Code"), ("open vscode", "Visual Studio Code"),
            ("open calender", "Calendar"), ("open note", "Notes"),
        ],
    )  # fmt: skip
    def test_match(self, fp: FastPath, text: str, app: str) -> None:
        plan = fp.match(text, EMPTY)
        assert only(plan).op == "open_app"
        assert only(plan).app == app
        assert plan is not None
        assert plan.snapshot_id == "s1"
        assert plan.say

    @pytest.mark.parametrize(
        "text",
        [
            "open notes and start a note called standup", "open notes then scroll down",
            "open notes, then new note", "open slack and message bob", "open the pod bay doors",
            "open zoom", "open", "launch", "open notes called standup",
        ],
    )  # fmt: skip
    def test_no_match(self, fp: FastPath, text: str) -> None:
        assert fp.match(text, EMPTY) is None

    def test_alias_requires_installed_app(self) -> None:
        assert FastPath(["Notes"]).match("open vs code", EMPTY) is None


class TestStandardCommands:
    def test_menu_preferred(self, fp: FastPath) -> None:
        a = only(fp.match("copy", NOTES))
        assert (a.op, a.target) == ("menu", 111)
        a = only(fp.match("select all", NOTES))
        assert (a.op, a.target) == ("menu", 113)
        a = only(fp.match("print", NOTES))
        assert (a.op, a.target) == ("menu", 104)

    def test_duplicate_menu_names_fall_back_to_keys(self, fp: FastPath) -> None:
        s = snap([menu(1, "Edit", "Find", "Find"), menu(2, "Edit", "Find", "Find…")])
        assert only(fp.match("find", s)).keys == "cmd+f"

    def test_disabled_menu_defers_to_planner(self, fp: FastPath) -> None:
        assert fp.match("paste", NOTES) is None  # Edit › Paste is greyed out: cmd+v would do nothing
        s = snap([menu(1, "Edit", "Copy", shortcut="cmd+c", enabled=False)])
        assert fp.match("copy", s) is None

    @pytest.mark.parametrize(
        ("text", "items"),
        [
            ("new window", [menu(1, "File", "New Note", shortcut="cmd+n")]),  # Notes
            ("new window", [menu(1, "File", "New Event", shortcut="Command+N")]),  # Calendar
            ("new tab", [menu(1, "View", "Go to Today", shortcut="cmd+t")]),  # Calendar
            ("find", [menu(1, "Edit", "Find Next", shortcut="cmd+f", enabled=False)]),
        ],
    )
    def test_shortcut_owned_by_other_item_defers_to_planner(
        self, fp: FastPath, text: str, items: list[dict[str, Any]]
    ) -> None:
        assert fp.match(text, snap(items, app="Calendar")) is None

    def test_keys_when_no_item_owns_the_shortcut(self, fp: FastPath) -> None:
        s = snap([menu(1, "File", "New Note", shortcut="cmd+n"), menu(2, "Edit", "Undo", shortcut="cmd+z")])
        a = only(fp.match("save", s))
        assert (a.op, a.keys) == ("keys", "cmd+s")

    def test_finder_new_window_uses_menu(self, fp: FastPath) -> None:
        assert only(fp.match("new window", FINDER)).target == 201

    @pytest.mark.parametrize(
        ("text", "keys"),
        [
            ("new tab", "cmd+t"), ("close tab", "cmd+w"), ("next tab", "ctrl+tab"),
            ("previous tab", "ctrl+shift+tab"), ("new window", "cmd+n"), ("close window", "cmd+w"),
            ("cut", "cmd+x"), ("save", "cmd+s"), ("redo", "cmd+shift+z"), ("find", "cmd+f"),
            ("open a new tab", "cmd+t"), ("close this tab", "cmd+w"), ("go to next tab", "ctrl+tab"),
            ("Copy.", "cmd+c"), ("please paste", "cmd+v"),
        ],
    )  # fmt: skip
    def test_keys_when_no_menu(self, fp: FastPath, text: str, keys: str) -> None:
        a = only(fp.match(text, EMPTY))
        assert (a.op, a.keys) == ("keys", keys)

    def test_tab_menu_items(self, fp: FastPath) -> None:
        assert only(fp.match("new tab", FINDER)).target == 207
        assert only(fp.match("close tab", FINDER)).target == 208
        assert only(fp.match("next tab", FINDER)).target == 206

    def test_close_window_uses_close(self, fp: FastPath) -> None:
        assert only(fp.match("close window", NOTES)).target == 103

    @pytest.mark.parametrize(
        "text", ["copy this to notes", "save as", "paste and send", "print the page twice"]
    )
    def test_payload_not_std(self, fp: FastPath, text: str) -> None:
        assert fp.match(text, EMPTY) is None


class TestScroll:
    @pytest.mark.parametrize(
        ("text", "d", "amt"),
        [
            ("scroll down", "down", 5), ("scroll up", "up", 5), ("Scroll down a lot", "down", 15),
            ("scroll up a little", "up", 2), ("scroll down a bit", "down", 2),
        ],
    )  # fmt: skip
    def test_scroll(self, fp: FastPath, text: str, d: str, amt: int) -> None:
        a = only(fp.match(text, EMPTY))
        assert (a.op, a.direction, a.amount) == ("scroll", d, amt)

    @pytest.mark.parametrize("text", ["scroll down and click share", "scroll to the bottom", "scroll"])
    def test_no(self, fp: FastPath, text: str) -> None:
        assert fp.match(text, EMPTY) is None


class TestKeys:
    @pytest.mark.parametrize(
        ("text", "keys"),
        [
            ("press return", "return"), ("press enter", "return"), ("hit escape", "escape"),
            ("press tab", "tab"), ("press space", "space"), ("press delete", "delete"),
            ("press up arrow", "up"), ("press left", "left"), ("press command n", "cmd+n"),
            ("press cmd+n", "cmd+n"), ("press command shift n", "cmd+shift+n"),
            ("press shift command n", "cmd+shift+n"), ("press control tab", "ctrl+tab"),
            ("press option space", "alt+space"),
        ],
    )  # fmt: skip
    def test_press(self, fp: FastPath, text: str, keys: str) -> None:
        a = only(fp.match(text, EMPTY))
        assert (a.op, a.keys) == ("keys", keys)

    def test_parse_keys(self) -> None:
        assert parse_keys("command command n") is None
        assert parse_keys("banana") is None
        assert parse_keys("command") is None

    def test_press_unknown_falls_through(self, fp: FastPath) -> None:
        assert fp.match("press banana", EMPTY) is None


class TestButtons:
    def test_click_label(self, fp: FastPath) -> None:
        a = only(fp.match("click share", NOTES))
        assert (a.op, a.target) == ("press", 2)
        assert only(fp.match("press the share button", NOTES)).target == 2
        assert only(fp.match("hit new note", NOTES)).target == 1

    def test_unknown_label(self, fp: FastPath) -> None:
        assert fp.match("click banana", NOTES) is None

    def test_duplicate_labels_not_matched(self, fp: FastPath) -> None:
        s = snap([el(1, "button", "OK"), el(2, "button", "OK")])
        assert fp.match("click ok", s) is None

    def test_disabled_not_matched(self, fp: FastPath) -> None:
        s = snap([el(1, "button", "Share", enabled=False)])
        assert fp.match("click share", s) is None


class TestMenuByName:
    @pytest.mark.parametrize(
        ("text", "target"),
        [
            ("new note", 101), ("New Note.", 101), ("File, New Note", 101), ("file new note", 101),
            ("new folder", 102), ("new notes", 101), ("please make a new folder", None),
        ],
    )  # fmt: skip
    def test_notes(self, fp: FastPath, text: str, target: int | None) -> None:
        plan = fp.match(text, NOTES)
        if target is None:
            assert plan is None
        else:
            assert only(plan).op == "menu"
            assert only(plan).target == target

    def test_finder_downloads(self, fp: FastPath) -> None:
        assert only(fp.match("show downloads", FINDER)).target == 204
        assert only(fp.match("go to downloads", FINDER)).target == 204
        assert only(fp.match("Go, Documents", FINDER)).target == 205

    def test_ambiguous_menus_rejected(self, fp: FastPath) -> None:
        s = snap([menu(1, "File", "New Folder"), menu(2, "Edit", "New Folder")], app="Finder")
        assert fp.match("new folder", s) is None
        s2 = snap([menu(1, "File", "New Folder"), menu(2, "File", "New Folders")], app="Finder")
        assert fp.match("new folder", s2) is None

    def test_prefix_menu_items_do_not_confuse(self, fp: FastPath) -> None:
        assert only(fp.match("new folder", FINDER)).target == 202

    def test_disabled_menu_ignored(self, fp: FastPath) -> None:
        s = snap([menu(1, "File", "New Note", enabled=False)])
        assert fp.match("new note", s) is None

    @pytest.mark.parametrize(
        "text",
        [
            "new note called standup", "new note and type hello", "new note then scroll down",
            "new note with the title standup", "", "   ", "what is the weather like in San Francisco today",
            "new folder named taxes",
        ],
    )  # fmt: skip
    def test_payload_and_multiclause(self, fp: FastPath, text: str) -> None:
        assert fp.match(text, NOTES) is None


def test_ids_always_from_snapshot(fp: FastPath) -> None:
    for text in ["new note", "copy", "click share", "show downloads", "new folder", "close window"]:
        for s in (NOTES, FINDER):
            plan = fp.match(text, s)
            if plan:
                assert plan.snapshot_id == s.snapshot_id
                for a in plan.actions:
                    if a.target is not None:
                        assert s.item(a.target) is not None


def test_empty_apps_and_snapshot() -> None:
    assert FastPath([]).match("open notes", EMPTY) is None
    assert FastPath([]).match("new note", EMPTY) is None


def test_latency_300_items() -> None:
    items = [el(i, "button", f"Button number {i}") for i in range(150)]
    items += [menu(1000 + i, f"Menu{i % 9}", f"Command {i} item") for i in range(149)]
    items.append(menu(2000, "File", "New Note", shortcut="cmd+n"))
    big = snap(items)
    assert len(big.items) == 299 + 0 or len(big.items) == 300
    fp = FastPath(APPS + [f"Application {i}" for i in range(300)])
    texts = [
        "new note",
        "open notes",
        "show downloads",
        "click button number 7",
        "something unrelated here",
        "copy",
    ]
    for t in texts:
        fp.match(t, big)
    worst = 0.0
    for t in texts:
        t0 = time.perf_counter()
        fp.match(t, big)
        worst = max(worst, (time.perf_counter() - t0) * 1000)
    print(f"fastpath worst-case ms: {worst:.2f}")
    assert worst < 30, worst
