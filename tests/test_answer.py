"""Questions (brain.answer): which utterances are questions, and the answer events the Brain emits."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import brain.answer as answer
from brain.addressing import Prefs
from brain.contracts import ListSink, Speaker, Utterance
from brain.flows import FlowStore
from brain.pipeline import Brain
from tests.test_pipeline import FakeExecutor, FakeProvider, FakeRouter


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Summarize this.", (True, True)),
        ("What does this email say?", (True, True)),
        ("What's the capital of Peru?", (True, False)),
        ("can you summarize this page", (True, True)),
        ("Can you open notes?", (False, False)),
        ("open notes", (False, False)),
        ("Go to Terminal.", (False, False)),
    ],
)
def test_classify(text: str, expected: tuple[bool, bool]) -> None:
    assert answer.classify(text) == expected


class ScreenProvider(FakeProvider):
    def screen_text(self) -> dict[str, Any]:
        return {"app": "Safari", "window": "Start Page", "text": "Safari is fast and private."}


def test_question_streams_an_answer_and_runs_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_stream(question: str, screen: Any, settings: Any, on_text: Any, max_tokens: int = 90) -> str:
        seen["screen"] = screen
        on_text("Safari is")
        return "Safari is fast and private."

    monkeypatch.setattr(answer, "stream_answer", fake_stream)
    (tmp_path / "s.json").write_text(json.dumps({"wake_word_required": False}), encoding="utf-8")
    p = ScreenProvider()
    x = FakeExecutor(p)
    sink = ListSink()
    brain = Brain(p, x, FakeRouter(FlowStore()), None, FlowStore(), sink, prefs=Prefs(tmp_path / "s.json"))
    brain.handle(Utterance(id="q1", text="Summarize this", speaker=Speaker(verified=True, score=1.0)))
    answers = [e.data for e in sink.events if e.type == "answer"]
    assert answers[-1] == {"question": "Summarize this", "text": "Safari is fast and private.", "done": True}
    assert any(a["text"] == "Safari is" for a in answers)
    assert seen["screen"]["window"] == "Start Page"
    assert x.actions == []
    assert sink.events[-1].type == "done"
