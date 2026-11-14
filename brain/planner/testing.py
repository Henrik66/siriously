"""Test helpers (no server needed): a scripted HTTP transport and a Notes-like snapshot.

Used by tests/test_planner_*.py; the eval harness may use FakeTransport too.
"""

from __future__ import annotations

import json
from typing import Any

from brain.contracts import AppInfo, UIItem, UISnapshot


def notes_snapshot() -> UISnapshot:
    return UISnapshot(
        snapshot_id="s-1",
        app=AppInfo("Notes", "com.apple.Notes"),
        window="Notes",
        focused_id=7,
        items=(
            UIItem(1, "element", "button", "New Note"),
            UIItem(2, "element", "button", "Delete"),
            UIItem(3, "element", "searchfield", "Search"),
            UIItem(4, "element", "button", "Share", enabled=False),
            UIItem(7, "element", "textarea", "Note body"),
            UIItem(101, "menu", path=("File", "New Note"), shortcut="cmd+n"),
            UIItem(102, "menu", path=("File", "Close"), shortcut="cmd+w"),
            UIItem(103, "menu", path=("Edit", "Undo"), shortcut="cmd+z"),
            UIItem(104, "menu", path=("Format", "Bold"), shortcut="cmd+b"),
            UIItem(105, "menu", path=("View", "Show Folders")),
        ),
    )


def completion(content: str, *, cache_n: int = 900, prompt_n: int = 120, predicted_n: int = 20) -> bytes:
    """A llama-server style /v1/chat/completions body."""
    body = {
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": cache_n + prompt_n, "completion_tokens": predicted_n},
        "timings": {
            "cache_n": cache_n,
            "prompt_n": prompt_n,
            "prompt_ms": 400.0,
            "predicted_n": predicted_n,
            "predicted_ms": 800.0,
        },
    }
    return json.dumps(body).encode()


class FakeTransport:
    """Pops scripted responses; an Exception instance is raised instead of returned."""

    def __init__(self, *responses: bytes | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any], float]] = []

    def __call__(self, url: str, body: bytes, timeout: float) -> bytes:
        self.calls.append((url, json.loads(body), timeout))
        if not self.responses:
            raise AssertionError("FakeTransport: no scripted response left")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    @property
    def last_body(self) -> dict[str, Any]:
        return self.calls[-1][1]
