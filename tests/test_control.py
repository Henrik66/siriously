import time

import pytest

from brain.control import parse_control


@pytest.mark.parametrize(
    ("text", "word"),
    [
        ("stop", "stop"), ("Stop it!", "stop"), ("halt", "stop"), ("abort", "stop"),
        ("cancel", "cancel"), ("never mind", "cancel"), ("Nevermind.", "cancel"), ("forget it", "cancel"),
        ("undo", "undo"), ("Undo that", "undo"), ("take that back", "undo"),
        ("yes", "yes"), ("Yeah", "yes"), ("yep", "yes"), ("confirm", "yes"), ("do it", "yes"),
        ("go ahead", "yes"), ("sure", "yes"),
        ("no", "no"), ("Nope.", "no"), ("don't", "no"), ("don’t", "no"),
        ("please stop", "stop"), ("okay, undo", "undo"), ("hey siriously, cancel", "cancel"),
        ("Hey Siri stop", "stop"), ("yes please", "yes"), ("  STOP  ", "stop"),
    ],
)  # fmt: skip
def test_control_words(text: str, word: str) -> None:
    assert parse_control(text) == word


@pytest.mark.parametrize(
    "text",
    [
        "ok", "OK.", "okay", "Okay.", "okay do it", "OK, go ahead", "yes, delete it", "yes go ahead",
        "yeah do it", "Yep, that's fine.", "sure, go ahead", "okay yes", "yes yes", "yes please do it",
        "hey siriously, yes delete it",
    ],
)  # fmt: skip
def test_confirm_phrasings(text: str) -> None:
    assert parse_control(text) == "yes"


@pytest.mark.parametrize(
    "text",
    [
        "stop stop", "Stop, stop, stop!", "stop now", "stop it now", "wait", "Wait!", "hold on",
        "hold it", "stop everything", "please wait", "wait a second", "stop right now",
    ],
)  # fmt: skip
def test_stop_phrasings(text: str) -> None:
    assert parse_control(text) == "stop"


@pytest.mark.parametrize(
    "text",
    [
        "", "   ", "Thank you.", "ok google", "okay open notes", "yes delete the file in finder",
        "yes delete it and open notes", "stop the music in spotify", "wait for the download",
        "hold on to this note", "stop the timer", "stop everything in spotify now",
        "undo the last two edits in notes", "stop the music", "yes open notes", "no thanks I want notes",
        "open notes", "cancel my meeting", "stopping", "do it later",
    ],
)  # fmt: skip
def test_not_control(text: str) -> None:
    assert parse_control(text) is None


@pytest.mark.parametrize(
    "text", ["hey siriously please undo that", "okay yes please do it", "stop everything in spotify now"]
)
def test_latency_under_1ms(text: str) -> None:
    parse_control(text)
    t0 = time.perf_counter()
    for _ in range(100):
        parse_control(text)
    assert (time.perf_counter() - t0) / 100 < 0.001
