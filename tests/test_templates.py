"""Destructive-object and web-search fast-path templates (no model)."""

from __future__ import annotations

from typing import Any

import pytest

from brain.contracts import Action, Plan, UISnapshot
from brain.fastpath import FastPath

APPS = ["Finder", "Notes", "Safari", "Google Chrome", "Calendar"]


def snap(items: list[dict[str, Any]], app: str = "Notes", bundle: str = "") -> UISnapshot:
    return UISnapshot.from_dict(
        {"snapshot_id": "s1", "app": {"name": app, "bundle_id": bundle}, "items": items}
    )


def menu(i: int, *path: str, enabled: bool = True) -> dict[str, Any]:
    return {"id": i, "kind": "menu", "path": list(path), "shortcut": None, "enabled": enabled}


NOTES = snap([menu(137, "Edit", "Delete"), menu(114, "File", "Close")])
FINDER = snap([menu(120, "File", "Move to Trash"), menu(137, "Edit", "Delete")], app="Finder")
FINDER_OFF = snap([menu(120, "File", "Move to Trash", enabled=False)], app="Finder")
NOTES_EXACT = snap([menu(5, "Note", "Delete Note"), menu(137, "Edit", "Delete")])
SAFARI = snap(
    [menu(114, "File", "Open Location…"), menu(112, "File", "New Tab")],
    app="Safari",
    bundle="com.apple.Safari",
)
SAFARI_NO_LOC = snap([menu(112, "File", "New Tab")], app="Safari", bundle="com.apple.Safari")


@pytest.fixture(scope="module")
def fp() -> FastPath:
    return FastPath(APPS)


def acts(plan: Plan | None) -> tuple[Action, ...]:
    assert plan is not None
    return plan.actions


class TestDestructive:
    @pytest.mark.parametrize(
        "text",
        ["remove this note", "get rid of this note", "delete the current note", "Trash that note.",
         "throw away the selected note", "discard this note"],
    )  # fmt: skip
    def test_edit_delete_fallback(self, fp: FastPath, text: str) -> None:
        assert acts(fp.match(text, NOTES)) == (Action.menu(137),)

    def test_exact_noun_leaf_preferred(self, fp: FastPath) -> None:
        assert acts(fp.match("delete this note", NOTES_EXACT)) == (Action.menu(5),)

    def test_trash_before_edit_delete(self, fp: FastPath) -> None:
        assert acts(fp.match("delete this file", FINDER)) == (Action.menu(120),)

    def test_disabled_trash_falls_to_edit_delete_or_none(self, fp: FastPath) -> None:
        assert fp.match("delete this file", FINDER_OFF) is None

    def test_ambiguous_returns_none(self, fp: FastPath) -> None:
        both = snap([menu(1, "Message", "Delete Message"), menu(2, "Edit", "Delete Message")])
        assert fp.match("delete this message", both) is None

    @pytest.mark.parametrize(
        "text",
        ["delete everything", "delete all notes", "remove everything", "delete this", "delete the last word",
         "delete notes", "delete the first note", "delete this note and that note"],
    )  # fmt: skip
    def test_never_without_demonstrative(self, fp: FastPath, text: str) -> None:
        assert fp.match(text, NOTES) is None


class TestWebSearch:
    @pytest.mark.parametrize(
        ("text", "typed"),
        [("search for cornell dining hours", "cornell dining hours"),
         ("search the web for cornell dining hours", "cornell dining hours"),
         ("look up cornell dining hours", "cornell dining hours"),
         ("Google best pizza near me.", "best pizza near me"),
         ("go to github.com", "github.com"), ("open www.apple.com", "www.apple.com"),
         ("visit github.com.", "github.com")],
    )  # fmt: skip
    def test_match(self, fp: FastPath, text: str, typed: str) -> None:
        assert acts(fp.match(text, SAFARI)) == (
            Action.menu(114),
            Action.type_text(typed),
            Action.key("return"),
        )

    def test_keys_fallback_without_open_location(self, fp: FastPath) -> None:
        assert acts(fp.match("search for cats", SAFARI_NO_LOC))[0] == Action.key("cmd+l")

    def test_chrome_by_name_and_bundle(self, fp: FastPath) -> None:
        by_name = snap([], app="Google Chrome")
        by_bundle = snap([], app="Brave", bundle="com.google.Chrome")
        assert acts(fp.match("look up cats", by_name))[0] == Action.key("cmd+l")
        assert acts(fp.match("look up cats", by_bundle))[0] == Action.key("cmd+l")

    def test_not_in_non_browser(self, fp: FastPath) -> None:
        assert fp.match("search for budget", NOTES) is None
        assert fp.match("go to github.com", NOTES) is None

    @pytest.mark.parametrize("text", ["go to calendar", "open github", "search for", "go to my site.com now"])
    def test_non_domain_or_empty(self, fp: FastPath, text: str) -> None:
        plan = fp.match(text, SAFARI)
        assert plan is None or all(a.op != "type" for a in plan.actions)


@pytest.mark.parametrize(
    "text", ["remove this formatting", "delete this line", "delete the selection", "trash that idea"]
)
def test_generic_delete_tiers_need_an_object_noun(text: str) -> None:
    """Edit › Delete deletes the current selection; only fire it for nouns that name a whole object."""
    from brain.fastpath import FastPath
    from brain.fixtures import FixtureUIProvider, load_apps

    snap = FixtureUIProvider("notes").snapshot()
    assert FastPath(load_apps()).match(text, snap) is None


# ---- create template safety ("New <X> called <name>")


def _create(text: str, *items: dict[str, Any], app: str = "Notes") -> Plan | None:
    return FastPath(APPS).match(text, snap(list(items), app=app))


def test_create_terminal_new_command_is_never_matched() -> None:
    items = (menu(5, "Shell", "New Command…"), menu(6, "Shell", "New Window"))
    assert _create("create a command called ls", *items, app="Terminal") is None
    assert _create("make a new command called rm", menu(5, "Shell", "New Command"), app="Terminal") is None


@pytest.mark.parametrize("leaf", ["New Shell Script", "New Terminal Tab", "New Run Configuration"])
def test_create_skips_command_like_leaves(leaf: str) -> None:
    noun = leaf.split(" ", 1)[1].lower()
    assert _create(f"create a {noun} called x", menu(5, "File", leaf)) is None


def test_create_skips_items_under_a_shell_menu() -> None:
    assert _create("create a session called x", menu(5, "Shell", "New Session")) is None


def test_create_skips_submenu_and_dialog_leaves() -> None:
    assert _create("create a calendar called work", menu(5, "File", "New Calendar"), app="Calendar") is None
    assert _create("create a subscription called x", menu(5, "File", "New Subscription…")) is None


@pytest.mark.parametrize("name", ["lunch at noon", "standup on friday", "dentist tomorrow"])
def test_create_with_time_phrase_falls_through(name: str) -> None:
    assert _create(f"add an event called {name}", menu(5, "File", "New Event"), app="Calendar") is None


def test_create_plain_leaf_still_works() -> None:
    plan = _create("create a folder called receipts", menu(5, "File", "New Folder"), app="Finder")
    assert plan is not None and [a.op for a in plan.actions] == ["menu", "type", "keys"]
    plan = _create("new note called Meeting called off", menu(5, "File", "New Note"))
    assert plan is not None and plan.actions[1].text == "Meeting called off"
