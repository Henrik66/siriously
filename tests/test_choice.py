from __future__ import annotations

import pytest

from brain.choice import parse_choice

LABELS = ["Save", "Save As", "Save All"]


@pytest.mark.parametrize(
    ("text", "idx"),
    [
        ("one", 0),
        ("1", 0),
        ("the first", 0),
        ("first one", 0),
        ("number two", 1),
        ("two", 1),
        ("the second one", 1),
        ("Number 3.", 2),
        ("the last one", 2),
        ("last", 2),
        ("save as", 1),
        ("the save all", 2),
    ],
)
def test_parse_choice_positive(text: str, idx: int) -> None:
    assert parse_choice(text, LABELS) == idx


@pytest.mark.parametrize(
    "text", ["", "open notes", "four", "the fifth one", "new note please", "yes", "two things"]
)
def test_parse_choice_negative(text: str) -> None:
    assert parse_choice(text, LABELS) is None


def test_parse_choice_no_labels() -> None:
    assert parse_choice("one", []) is None
