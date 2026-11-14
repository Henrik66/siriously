"""Per-call JSON schema for the planner reply (PRD §5.3), plus validation into ``contracts.Plan``.

The schema is rebuilt for every call so that every target is an enum of the ids actually shown:
llama-server compiles it to a grammar, which makes hallucinated ids impossible to emit. Each action
is a one-key object named after its op, so a reply costs as few output tokens as possible (decode
is the slowest part of a plan on the Mac):

    {"a":[{"menu":101},{"type":"standup"},{"keys":"return"}]}   -> actions
    {"a":[{"open":"Notes"},{"wait":500},{"keys":"cmd+n"}]}        -> open_app = "open"
    {"a":[{"type":"budget","in":3},{"scroll":"down","n":5}]}       -> optional extras: in, n
    {"ask":[4,9]}                                                 -> ambiguity
    {"mode":"dictation","text":"Dear team, ..."}                  -> dictation

Validation runs again on the parsed reply (grammars can be bypassed by a misconfigured server).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from brain.contracts import MAX_ACTIONS, Action, ContractError, Plan, UIItem
from brain.planner.errors import PlannerError

JSON = dict[str, Any]
TEXT_MAX = 300
KEYS_MAX = 24
WAIT_MAX_MS = 5000
SCROLL_MAX = 30
ASK_MAX = 4
APP_MAX = 80
_KEY_ALIASES = {"enter": "return", "esc": "escape"}  # one spelling for the executor and fixtures
_KEY_PART = r"(?:[a-z0-9]+|[=\-\[\];',./`\\])"  # a named key, or one punctuation key
_KEYS_RE = re.compile(rf"^{_KEY_PART}(?:\+{_KEY_PART})*$")  # cmd+n, shift+cmd+], return, cmd+,
# reply key -> (contracts op, optional extra key)
OP_KEYS: dict[str, tuple[str, str | None]] = {
    "menu": ("menu", None),
    "press": ("press", None),
    "type": ("type", "in"),
    "keys": ("keys", None),
    "open": ("open_app", None),
    "scroll": ("scroll", "n"),
    "wait": ("wait", None),
}
_TEXT_ROLE_HINTS = ("text", "search", "combo", "field", "edit", "input", "secure")


def _obj(props: JSON, required: Sequence[str]) -> JSON:
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


def _one(key: str, value: JSON, extra: tuple[str, JSON] | None = None) -> JSON:
    """``{key: value}`` with an optional second key (always after the first in llama.cpp grammars)."""
    props: JSON = {key: value}
    if extra is not None:
        props[extra[0]] = extra[1]
    return _obj(props, [key])


def is_text_input(item: UIItem) -> bool:
    """Elements ``type`` may target: text fields/areas, search and combo boxes (AX or plain roles)."""
    role = item.role.lower().removeprefix("ax")
    return item.kind == "element" and any(h in role for h in _TEXT_ROLE_HINTS)


def action_variants(
    element_ids: Sequence[int],
    menu_ids: Sequence[int],
    apps: Sequence[str],
    free_app: bool = False,
    text_ids: Sequence[int] = (),
) -> list[JSON]:
    variants: list[JSON] = []
    if menu_ids:
        variants.append(_one("menu", {"enum": list(menu_ids)}))
    if element_ids:
        variants.append(_one("press", {"enum": list(element_ids)}))
    text: JSON = {"type": "string", "minLength": 1, "maxLength": TEXT_MAX}
    variants.append(_one("type", text, ("in", {"enum": list(text_ids)}) if text_ids else None))
    variants.append(_one("keys", {"type": "string", "minLength": 1, "maxLength": KEYS_MAX}))
    if apps:
        variants.append(_one("open", {"enum": list(apps)}))
    elif free_app:
        variants.append(_one("open", {"type": "string", "minLength": 1, "maxLength": APP_MAX}))
    amount = {"type": "integer", "minimum": 1, "maximum": SCROLL_MAX}
    variants.append(_one("scroll", {"enum": ["up", "down"]}, ("n", amount)))
    variants.append(_one("wait", {"type": "integer", "minimum": 0, "maximum": WAIT_MAX_MS}))
    return variants


def build_schema(shown: Sequence[UIItem], apps: Sequence[str] = (), *, free_app: bool = False) -> JSON:
    """Reply schema for one call.

    ``apps`` = installed apps allowed for open_app. Empty means no open_app, unless ``free_app``
    (the planner has no installed-app list at all), which allows any app name.
    """
    element_ids = [it.id for it in shown if it.kind == "element"]
    menu_ids = [it.id for it in shown if it.kind == "menu"]
    text_ids = [it.id for it in shown if is_text_input(it)]
    actions: JSON = {
        "type": "array",
        "minItems": 1,
        "maxItems": MAX_ACTIONS,
        "items": {"anyOf": action_variants(element_ids, menu_ids, apps, free_app, text_ids)},
    }
    shapes: list[JSON] = [_obj({"a": actions}, ["a"])]
    all_ids = element_ids + menu_ids
    if len(all_ids) >= 2:
        ask = {"type": "array", "minItems": 2, "maxItems": ASK_MAX, "items": {"enum": all_ids}}
        shapes.append(_obj({"ask": ask}, ["ask"]))
    dictation = {
        "mode": {"const": "dictation"},
        "text": {"type": "string", "minLength": 1, "maxLength": TEXT_MAX},
    }
    shapes.append(_obj(dictation, ["mode", "text"]))
    return {"anyOf": shapes}


def response_format(schema: JSON) -> JSON:
    return {"type": "json_schema", "json_schema": {"name": "plan", "strict": True, "schema": schema}}


# ---------------------------------------------------------------------------
# Validation: reply dict -> contracts.Plan
# ---------------------------------------------------------------------------


def _bad(msg: str) -> PlannerError:
    return PlannerError("bad_output", msg)


def _int(v: object, what: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool):
        raise _bad(f"{what} must be an integer, got {v!r}")
    return v


def _str(v: object, what: str, max_len: int) -> str:
    if not isinstance(v, str) or not v.strip():
        raise _bad(f"{what} must be a non-empty string, got {v!r}")
    if len(v) > max_len:
        raise _bad(f"{what} longer than {max_len} chars")
    return v


def _target(v: object, allowed: set[int], what: str) -> int:
    tid = _int(v, what)
    if tid not in allowed:
        raise _bad(f"{what} {tid} is not a shown id")
    return tid


def _op_key(d: JSON) -> str:
    """The single op key of a reply action; rejects zero/several ops and unknown extra keys."""
    ops = [k for k in d if k in OP_KEYS]
    if len(ops) != 1:
        raise _bad(f"action needs exactly one op key, got {sorted(d)}")
    extra = OP_KEYS[ops[0]][1]
    unknown = set(d) - {ops[0], extra}
    if unknown:
        raise _bad(f"unexpected keys {sorted(k for k in unknown if k)} in {ops[0]} action")
    return ops[0]


def to_action(d: object, shown: Sequence[UIItem], apps: Sequence[str], free_app: bool = False) -> Action:
    if not isinstance(d, dict):
        raise _bad(f"action must be an object, got {d!r}")
    key = _op_key(d)
    v = d[key]
    try:
        if key == "menu":
            return Action.menu(_target(v, {it.id for it in shown if it.kind == "menu"}, "menu target"))
        if key == "press":
            return Action.press(_target(v, {it.id for it in shown if it.kind == "element"}, "press target"))
        if key == "type":
            into = d.get("in")
            text_ids = {it.id for it in shown if is_text_input(it)}
            target = None if into is None else _target(into, text_ids, "type target")
            return Action.type_text(_str(v, "type text", TEXT_MAX), target)
        if key == "keys":
            return Action.key(_keys(v))
        if key == "open":
            return Action.open_app(_app(v, apps, free_app))
        if key == "scroll":
            return _scroll(v, d.get("n", 5))
        ms = _int(v, "wait ms")
        if not 0 <= ms <= WAIT_MAX_MS:
            raise _bad(f"wait ms {ms} out of range")
        return Action.wait(ms)
    except ContractError as e:
        raise _bad(str(e)) from e


def _keys(v: object) -> str:
    keys = _str(v, "keys", KEYS_MAX).strip().lower().replace(" ", "")
    if not _KEYS_RE.match(keys):
        raise _bad(f"bad key combo {keys!r}")
    *mods, key = keys.split("+")
    return "+".join([*mods, _KEY_ALIASES.get(key, key)])


def _app(v: object, apps: Sequence[str], free_app: bool) -> str:
    app = _str(v, "app", APP_MAX)
    if not free_app and app not in apps:
        raise _bad(f"app {app!r} is not an allowed app")
    return app


def _scroll(direction: object, n: object) -> Action:
    if direction not in ("up", "down"):
        raise _bad(f"scroll direction {direction!r}")
    amount = _int(n, "scroll amount")
    if not 1 <= amount <= SCROLL_MAX:
        raise _bad(f"scroll amount {amount} out of range")
    return Action.scroll(direction, amount)


def to_plan(
    reply: object,
    snapshot_id: str,
    shown: Sequence[UIItem],
    apps: Sequence[str] = (),
    *,
    free_app: bool = False,
) -> Plan:
    """Validate a parsed reply against the ids/apps that were offered and build a Plan.

    open_app must name one of ``apps``; with ``free_app`` any non-empty name is accepted.
    """
    if not isinstance(reply, dict):
        raise _bad(f"reply must be a JSON object, got {type(reply).__name__}")
    if "ask" in reply:
        raw = reply["ask"]
        if not isinstance(raw, list):
            raise _bad("ask must be a list")
        allowed = {it.id for it in shown}
        ids = tuple(dict.fromkeys(_target(v, allowed, "ask id") for v in raw))
        if not 2 <= len(ids) <= ASK_MAX:
            raise _bad(f"ask needs 2-{ASK_MAX} distinct ids, got {len(ids)}")
        return Plan(snapshot_id=snapshot_id, ask=ids, say="Which one?")
    if reply.get("mode") == "dictation":
        text = _str(reply.get("text"), "dictation text", TEXT_MAX)
        return Plan(snapshot_id=snapshot_id, actions=(Action.type_text(text),), mode="dictation")
    raw_actions = reply.get("a")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise _bad("reply needs a non-empty 'a' list, 'ask' or dictation mode")
    if len(raw_actions) > MAX_ACTIONS:
        raise _bad(f"more than {MAX_ACTIONS} actions")
    actions = drop_stale_after_open(tuple(to_action(a, shown, apps, free_app) for a in raw_actions))
    return Plan(snapshot_id=snapshot_id, actions=actions)


def drop_stale_after_open(actions: tuple[Action, ...]) -> tuple[Action, ...]:
    """Cut the burst at the first targeted action after an open_app.

    Ids belong to the snapshot of the app that was frontmost when planning; once open_app switches
    apps they are stale by construction. Untargeted keys/type/wait after open_app are kept
    ("open Notes, cmd+n, type standup" is a valid blind burst).
    """
    opened = False
    for i, a in enumerate(actions):
        if opened and a.target is not None:
            return actions[:i]
        opened = opened or a.op == "open_app"
    return actions
