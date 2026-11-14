"""Round-trip and validation tests for brain.contracts (PRD §4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain.contracts import (
    SCHEMAS,
    Action,
    ActionResult,
    ContractError,
    Event,
    Plan,
    TargetDescriptor,
    UISnapshot,
    Utterance,
)

PRD_SNAPSHOT = {
    "snapshot_id": "s-0192",
    "app": {"name": "Notes", "bundle_id": "com.apple.Notes"},
    "window": "Notes",
    "focused_id": 7,
    "items": [
        {"id": 1, "kind": "element", "role": "button", "label": "New Note", "enabled": True},
        {"id": 7, "kind": "element", "role": "textarea", "label": "Note body", "value": "", "enabled": True},
        {"id": 101, "kind": "menu", "path": ["File", "New Note"], "shortcut": "cmd+n", "enabled": True},
    ],
    "truncated": False,
}
PRD_PLAN = {
    "snapshot_id": "s-0192",
    "confirm": False,
    "say": "New note: standup",
    "actions": [
        {"op": "menu", "target": 101},
        {"op": "type", "text": "standup"},
        {"op": "keys", "keys": "return"},
    ],
}
PRD_UTTERANCE = {
    "id": "u-0042",
    "text": "open notes and start a note called standup",
    "t_speech_end": 1731600000.12,
    "stt_ms": 150,
    "speaker": {"verified": True, "score": 0.61, "name": "henrik"},
}


def test_snapshot_roundtrip_and_helpers() -> None:
    s = UISnapshot.from_dict(PRD_SNAPSHOT)
    assert UISnapshot.from_dict(s.to_dict()) == s
    menu = s.item(101)
    assert menu is not None
    assert menu.display == "File › New Note"
    assert menu.name == "New Note"
    assert menu.role == "menuitem"
    assert len(s.menus) == 1
    assert len(s.elements) == 2
    assert s.item(999) is None


def test_plan_roundtrip() -> None:
    p = Plan.from_dict(PRD_PLAN)
    assert p.to_dict() == PRD_PLAN
    assert Plan.from_dict({**PRD_PLAN, "ask": [1, 7]}).ask == (1, 7)


def test_utterance_and_result_roundtrip() -> None:
    u = Utterance.from_dict(PRD_UTTERANCE)
    assert u.to_dict() == PRD_UTTERANCE
    r = ActionResult(ok=True, changed=True, snapshot=UISnapshot.from_dict(PRD_SNAPSHOT))
    assert ActionResult.from_dict(json.loads(json.dumps(r.to_dict()))) == r


@pytest.mark.parametrize(
    "bad",
    [
        {"op": "press"},
        {"op": "menu"},
        {"op": "open_app"},
        {"op": "type"},
        {"op": "keys"},
        {"op": "scroll", "direction": "left"},
        {"op": "wait"},
        {"op": "click", "target": 1},
    ],
)
def test_invalid_actions_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ContractError):
        Action.from_dict(bad)


def test_missing_fields_rejected() -> None:
    with pytest.raises(ContractError):
        Utterance.from_dict({"id": "u", "text": "x"})
    with pytest.raises(ContractError):
        UISnapshot.from_dict({"app": {"name": "x"}})


def test_descriptor_roundtrip() -> None:
    s = UISnapshot.from_dict(PRD_SNAPSHOT)
    item = s.item(101)
    assert item is not None
    d = TargetDescriptor.of(item, s.app.bundle_id)
    assert TargetDescriptor.from_dict(d.to_dict()) == d


def test_event_dict() -> None:
    e = Event(type="heard", utterance_id="u-1", data={"text": "hi"}, ms_since_speech_end=12)
    assert e.to_dict()["type"] == "heard"


def test_exported_schemas_up_to_date() -> None:
    out = Path(__file__).resolve().parent.parent / "schemas"
    for name, schema in SCHEMAS.items():
        path = out / f"{name}.schema.json"
        assert path.exists(), f"run: uv run python -m brain.export_schemas ({path} missing)"
        assert json.loads(path.read_text(encoding="utf-8")) == schema, f"{path} stale; re-export"
