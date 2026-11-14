from __future__ import annotations

import json
import re

from brain.contracts import AppInfo, UIItem, UISnapshot
from brain.planner.prompt import (
    FEW_SHOT,
    SUFFIX_TOKEN_BUDGET,
    build_messages,
    build_suffix,
    estimate_tokens,
    item_line,
    static_messages,
)
from brain.planner.rank import rank_items
from brain.planner.schema import to_plan
from brain.planner.testing import notes_snapshot


def test_static_prefix_is_byte_identical_across_calls() -> None:
    a = build_messages("APP: A | WINDOW: x\nUSER: one")
    b = build_messages("APP: B | WINDOW: y\nUSER: two")
    assert json.dumps(a[:-1]) == json.dumps(b[:-1]) == json.dumps(static_messages())
    assert a[-1] == {"role": "user", "content": "APP: A | WINDOW: x\nUSER: one"}


def test_few_shot_count_and_shape() -> None:
    assert 6 <= len(FEW_SHOT) <= 8
    msgs = static_messages()
    assert msgs[0]["role"] == "system"
    assert [m["role"] for m in msgs[1:]] == ["user", "assistant"] * len(FEW_SHOT)


_LINE = re.compile(r'^(\d+) (\S+) "(.*)"')


def test_few_shot_replies_only_use_ids_shown_in_their_example() -> None:
    for suffix, reply in FEW_SHOT:
        shown: list[UIItem] = []
        for line in suffix.splitlines():
            m = _LINE.match(line)
            if m:
                if m.group(2) == "menu":
                    shown.append(UIItem(int(m.group(1)), "menu", "menuitem", m.group(3)))
                else:
                    shown.append(UIItem(int(m.group(1)), "element", m.group(2), m.group(3)))
        apps_line = next((ln for ln in suffix.splitlines() if ln.startswith("APPS: ")), "")
        apps = apps_line[6:].split(", ") if apps_line else []
        to_plan(json.loads(reply), "s", shown, apps)  # raises if an example is inconsistent


def test_suffix_matches_prd_format() -> None:
    snap = notes_snapshot()
    suffix, shown = build_suffix(
        "open notes and start a note called standup", snap, rank_items("x", snap, 50)
    )
    lines = suffix.splitlines()
    assert lines[0] == "APP: Notes | WINDOW: Notes"
    assert '1 button "New Note"' in lines
    assert '7 textarea "Note body" (focused)' in lines
    assert '101 menu "File › New Note" (cmd+n)' in lines
    assert '105 menu "View › Show Folders"' in lines
    assert lines[-1] == "USER: open notes and start a note called standup"
    # elements first, then menus, in snapshot order
    kinds = [it.kind for it in shown]
    assert kinds == sorted(kinds, key=lambda k: k == "menu")


def test_suffix_hint_error_and_apps_lines() -> None:
    snap = notes_snapshot()
    suffix, _ = build_suffix(
        "new note",
        snap,
        rank_items("new note", snap),
        ["Notes"],
        hint="menu File › New Note",
        error="press 1 failed",
    )
    lines = suffix.splitlines()
    assert lines[1] == "APPS: Notes"
    assert lines[-3] == "HINT: menu File › New Note"
    assert lines[-2] == "ERROR: press 1 failed"
    assert lines[-1] == "USER: new note"


def test_item_line_escapes_quotes_and_truncates() -> None:
    it = UIItem(9, "element", "button", 'Say "hi" ' + "x" * 80)
    line = item_line(it, None)
    assert line.startswith("9 button \"Say 'hi' ")
    assert len(line) <= len('9 button ""') + 40
    assert item_line(UIItem(5, "element", "", "Thing"), None) == '5 element "Thing"'


def test_suffix_stays_under_budget_with_many_items() -> None:
    items = tuple(
        UIItem(i, "element", "button", f"Some fairly long button label number {i}") for i in range(40)
    )
    snap = UISnapshot("s", AppInfo("Big App"), "Main window", items=items)
    suffix, shown = build_suffix("press the button", snap, rank_items("press the button", snap, 15))
    assert estimate_tokens(suffix) <= SUFFIX_TOKEN_BUDGET
    assert 5 <= len(shown) < 15


def test_typical_suffix_fits_budget() -> None:
    snap = notes_snapshot()
    suffix, shown = build_suffix("open notes and start a note called standup", snap, rank_items("x", snap))
    assert estimate_tokens(suffix) <= SUFFIX_TOKEN_BUDGET
    assert len(shown) == 9  # all enabled items fit
