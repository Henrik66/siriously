from __future__ import annotations

from typing import Any

import pytest

from brain.contracts import MAX_ACTIONS, Action, UIItem
from brain.planner.errors import PlannerError
from brain.planner.schema import build_schema, is_text_input, response_format, to_action, to_plan
from brain.planner.testing import notes_snapshot

SHOWN = [it for it in notes_snapshot().items if it.enabled]
APPS = ["Notes", "Slack"]


def _variants(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Action variants keyed by their op key ("menu", "press", "type", "open", ...)."""
    actions = schema["anyOf"][0]["properties"]["a"]
    return {v["required"][0]: v for v in actions["items"]["anyOf"]}


def test_schema_target_enums_are_exactly_the_shown_ids() -> None:
    schema = build_schema(SHOWN, APPS)
    v = _variants(schema)
    assert v["menu"]["properties"]["menu"]["enum"] == [101, 102, 103, 104, 105]
    assert v["press"]["properties"]["press"]["enum"] == [1, 2, 3, 7]
    assert v["type"]["properties"]["in"]["enum"] == [3, 7]  # searchfield + textarea, never buttons
    assert v["open"]["properties"]["open"]["enum"] == APPS
    actions = schema["anyOf"][0]["properties"]["a"]
    assert actions["maxItems"] == MAX_ACTIONS and actions["minItems"] == 1
    ask = schema["anyOf"][1]["properties"]["ask"]
    assert ask["items"]["enum"] == [1, 2, 3, 7, 101, 102, 103, 104, 105]
    assert schema["anyOf"][2]["properties"]["mode"] == {"const": "dictation"}


def test_every_variant_requires_exactly_its_op_key() -> None:
    for key, v in _variants(build_schema(SHOWN, APPS)).items():
        assert v["required"] == [key]
        assert next(iter(v["properties"])) == key  # op key first; optional extra (in / n) after


def test_schema_omits_ops_without_candidates() -> None:
    v = _variants(build_schema([], []))
    assert set(v) == {"type", "keys", "scroll", "wait"}
    assert "in" not in v["type"]["properties"]
    assert len(build_schema([], [])["anyOf"]) == 2  # no ask with < 2 ids


def test_all_objects_are_closed() -> None:
    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
            for val in node.values():
                walk(val)
        elif isinstance(node, list):
            for val in node:
                walk(val)

    walk(build_schema(SHOWN, APPS))


def test_response_format_wraps_schema() -> None:
    rf = response_format({"x": 1})
    assert rf == {"type": "json_schema", "json_schema": {"name": "plan", "strict": True, "schema": {"x": 1}}}


@pytest.mark.parametrize(
    ("raw", "action"),
    [
        ({"menu": 101}, Action.menu(101)),
        ({"type": "standup"}, Action.type_text("standup")),
        ({"type": "budget", "in": 3}, Action.type_text("budget", 3)),
        ({"keys": "Cmd+N"}, Action.key("cmd+n")),
        ({"press": 1}, Action.press(1)),
        ({"open": "Notes"}, Action.open_app("Notes")),
    ],
)
def test_to_action_maps_compact_keys(raw: dict[str, object], action: Action) -> None:
    assert to_action(raw, SHOWN, APPS) == action


def test_to_plan_keeps_snapshot_id_and_order() -> None:
    plan = to_plan({"a": [{"press": 2}, {"type": "x", "in": 3}, {"keys": "return"}]}, "s-1", SHOWN)
    assert plan.snapshot_id == "s-1" and plan.mode == "actions"
    assert plan.actions == (Action.press(2), Action.type_text("x", 3), Action.key("return"))


def test_to_plan_scroll_and_wait() -> None:
    plan = to_plan({"a": [{"scroll": "down"}, {"wait": 300}]}, "s", SHOWN)
    assert plan.actions == (Action.scroll("down", 5), Action.wait(300))
    plan = to_plan({"a": [{"scroll": "up", "n": 10}]}, "s", SHOWN)
    assert plan.actions == (Action.scroll("up", 10),)


def test_to_plan_ask_and_dictation() -> None:
    plan = to_plan({"ask": [1, 2, 1]}, "s", SHOWN)
    assert plan.ask == (1, 2) and plan.actions == ()
    plan = to_plan({"mode": "dictation", "text": "Hi there."}, "s", SHOWN)
    assert plan.mode == "dictation" and plan.actions == (Action.type_text("Hi there."),)


@pytest.mark.parametrize(
    "reply",
    [
        {"a": [{"menu": 999}]},  # hallucinated id
        {"a": [{"menu": 4}]},  # disabled / not shown
        {"a": [{"press": 101}]},  # press on a menu id
        {"a": [{"menu": 1}]},  # menu on an element id
        {"a": [{"menu": True}]},
        {"a": [{"type": ""}]},
        {"a": [{"type": "x", "in": 1}]},  # 1 is a button, not a text field
        {"a": [{"keys": "cmd+n; rm -rf"}]},
        {"a": [{"open": "Terminal"}]},
        {"a": [{"scroll": "left"}]},
        {"a": [{"scroll": "up", "n": 0}]},
        {"a": [{"wait": 99999}]},
        {"a": [{"click": 1}]},  # unknown op
        {"a": [{"menu": 101, "press": 1}]},  # two ops in one action
        {"a": [{"menu": 101, "in": 3}]},  # extra key not allowed for menu
        {"a": [{"op": "menu", "t": 101}]},  # old long format
        {"a": ["menu"]},
        {"a": []},
        {"a": [{"wait": 1}] * (MAX_ACTIONS + 1)},
        {"ask": [1]},
        {"ask": [1, 2, 3, 7, 101]},  # more than 4
        {"ask": [1, 999]},
        {"ask": "1,2"},
        {"mode": "dictation", "text": " "},
        {},
        [],
    ],
)
def test_to_plan_rejects_bad_replies(reply: object) -> None:
    with pytest.raises(PlannerError) as exc:
        to_plan(reply, "s", SHOWN, APPS)
    assert exc.value.kind == "bad_output"


def test_to_plan_only_trusts_shown_items() -> None:
    shown = [UIItem(1, "element", "button", "New Note")]
    with pytest.raises(PlannerError):
        to_plan({"a": [{"press": 2}]}, "s", shown)  # 2 exists in snapshot but was not shown


def test_open_app_free_text_only_when_planner_has_no_app_list() -> None:
    reply = {"a": [{"open": "Notes"}]}
    assert to_plan(reply, "s", SHOWN, [], free_app=True).actions == (Action.open_app("Notes"),)
    with pytest.raises(PlannerError):  # apps known but none matched: no open_app at all
        to_plan(reply, "s", SHOWN, [])
    v = _variants(build_schema(SHOWN, [], free_app=True))
    assert v["open"]["properties"]["open"]["type"] == "string"
    assert "open" not in _variants(build_schema(SHOWN, []))


def test_key_aliases_are_normalized() -> None:
    plan = to_plan({"a": [{"keys": "Enter"}, {"keys": "cmd+esc"}]}, "s", SHOWN)
    assert plan.actions == (Action.key("return"), Action.key("cmd+escape"))


def test_is_text_input_roles() -> None:
    for role in ("textfield", "AXTextField", "AXTextArea", "searchfield", "combobox", "AXSecureTextField"):
        assert is_text_input(UIItem(1, "element", role, "x"))
    for role in ("button", "row", "AXButton", "switch", ""):
        assert not is_text_input(UIItem(1, "element", role, "x"))
    assert not is_text_input(UIItem(1, "menu", path=("Edit", "Find")))
    no_text = [UIItem(1, "element", "button", "Go")]
    assert "in" not in _variants(build_schema(no_text))["type"]["properties"]


# --- sanitize_burst: never act on a screen the model could not see ---------------------------

_SAVE_SCREEN = [
    UIItem(10, "element", "textarea", "Text"),
    UIItem(2, "element", "row", "Appearance"),
    UIItem(113, "menu", path=("File", "Save…"), shortcut="cmd+s"),
    UIItem(110, "menu", path=("File", "New Note"), shortcut="cmd+n"),
    UIItem(128, "menu", path=("View", "Appearance")),
]
_FOCUSED_TEXT = _SAVE_SCREEN[0]


def _sanitized(actions: list[dict[str, object]], focused: UIItem | None = None) -> tuple[Action, ...]:
    return to_plan({"a": actions}, "s", _SAVE_SCREEN, ["Notes"], focused=focused).actions


def test_open_app_ends_the_burst() -> None:
    got = _sanitized([{"open": "Notes"}, {"wait": 500}, {"keys": "cmd+n"}, {"type": "standup"}])
    assert got == (Action.open_app("Notes"),)


def test_targeted_type_after_dialog_opener_loses_stale_target() -> None:
    got = _sanitized([{"menu": 113}, {"type": "todo", "in": 10}, {"keys": "return"}], _FOCUSED_TEXT)
    assert got == (Action.menu(113), Action.type_text("todo"), Action.key("return"))


def test_targeted_press_after_opener_ends_burst() -> None:
    assert _sanitized([{"menu": 110}, {"press": 2}]) == (Action.menu(110),)


def test_untargeted_type_into_nothing_is_cut() -> None:
    got = _sanitized([{"menu": 128}, {"type": "dark mode"}, {"keys": "return"}])
    assert got == (Action.menu(128),)


def test_untargeted_type_allowed_with_focus_or_after_focus_creating_step() -> None:
    assert _sanitized([{"type": "hi"}], _FOCUSED_TEXT) == (Action.type_text("hi"),)
    got = _sanitized([{"menu": 110}, {"type": "standup"}])
    assert got == (Action.menu(110), Action.type_text("standup"))
    assert _sanitized([{"keys": "cmd+f"}, {"type": "x"}]) == (Action.key("cmd+f"), Action.type_text("x"))
    got = _sanitized([{"type": "x", "in": 10}, {"type": "y"}])
    assert got == (Action.type_text("x", 10), Action.type_text("y"))


def test_empty_actions_only_on_continuation() -> None:
    assert to_plan({"a": []}, "s", SHOWN, allow_empty=True).actions == ()
    with pytest.raises(PlannerError):
        to_plan({"a": []}, "s", SHOWN)
    schema = build_schema(SHOWN, allow_empty=True)
    assert schema["anyOf"][0]["properties"]["a"]["minItems"] == 0
