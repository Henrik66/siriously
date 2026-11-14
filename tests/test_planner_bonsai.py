from __future__ import annotations

import json
import os
import threading
import urllib.error
from email.message import Message
from io import BytesIO

import pytest

from brain.config import Settings
from brain.contracts import Action, Planner
from brain.planner import BonsaiPlanner, PlannerError
from brain.planner.prompt import static_messages
from brain.planner.testing import FakeTransport, completion, notes_snapshot

SETTINGS = Settings(bonsai_url="http://127.0.0.1:9/v1", bonsai_model="bonsai", bonsai_timeout_s=3.0)
APPS = ["Notes", "Slack", "Safari"]


def _planner(t: FakeTransport) -> BonsaiPlanner:
    return BonsaiPlanner(APPS, SETTINGS, transport=t)


def test_satisfies_planner_protocol() -> None:
    assert isinstance(_planner(FakeTransport()), Planner)


def test_plan_end_to_end() -> None:
    reply = '{"a":[{"menu":101},{"type":"standup"},{"keys":"return"}]}'
    t = FakeTransport(completion(reply, cache_n=900, prompt_n=110, predicted_n=24))
    p = _planner(t)
    plan = p.plan("start a new note called standup", notes_snapshot())
    assert plan.snapshot_id == "s-1"
    assert plan.actions == (Action.menu(101), Action.type_text("standup"), Action.key("return"))
    assert p.last_stats["cached_tokens"] == 900
    assert p.last_stats["prompt_tokens"] == 1010
    assert p.last_stats["completion_tokens"] == 24
    assert p.last_stats["items_shown"] == 9
    assert {"ms", "prompt_ms", "decode_ms", "suffix_tokens_est"} <= set(p.last_stats)

    body = t.last_body
    assert body["messages"][:-1] == static_messages()
    assert body["messages"][-1]["content"] == p.last_suffix
    assert p.last_suffix.endswith("USER: start a new note called standup")
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert 2.9 < t.calls[0][2] <= 3.0  # remaining part of Settings.bonsai_timeout_s


def test_schema_sent_per_call_with_shown_ids() -> None:
    t = FakeTransport(completion('{"a":[{"menu":104}]}'))
    _planner(t).plan("make it bold", notes_snapshot())
    schema = t.last_body["response_format"]["json_schema"]["schema"]
    menu = next(
        v for v in schema["anyOf"][0]["properties"]["a"]["items"]["anyOf"] if v["required"][0] == "menu"
    )
    assert 104 in menu["properties"]["menu"]["enum"]
    assert 4 not in schema["anyOf"][1]["properties"]["ask"]["items"]["enum"]  # disabled: never offered


def test_open_app_enum_comes_from_matching_installed_apps() -> None:
    t = FakeTransport(completion('{"a":[{"open":"Slack"}]}'))
    plan = _planner(t).plan("open slack", notes_snapshot())
    assert plan.actions == (Action.open_app("Slack"),)
    assert "APPS: Slack" in t.last_body["messages"][-1]["content"]


def test_hint_and_error_reach_the_prompt() -> None:
    t = FakeTransport(completion('{"a":[{"press":1}]}'))
    _planner(t).plan("new note", notes_snapshot(), hint="menu File › New Note", error="menu 101 did nothing")
    content = t.last_body["messages"][-1]["content"]
    assert "HINT: menu File › New Note" in content and "ERROR: menu 101 did nothing" in content


def test_ask_and_dictation() -> None:
    p = _planner(FakeTransport(completion('{"ask":[1,2]}'), completion('{"mode":"dictation","text":"Hi."}')))
    assert p.plan("that one", notes_snapshot()).ask == (1, 2)
    assert p.plan("hi period", notes_snapshot()).mode == "dictation"


@pytest.mark.parametrize("content", ["not json", '{"a":[{"menu":999}]}', '{"a":[]}'])
def test_bad_output_raises(content: str) -> None:
    with pytest.raises(PlannerError) as exc:
        _planner(FakeTransport(completion(content))).plan("x", notes_snapshot())
    assert exc.value.kind == "bad_output"


def test_server_down_raises() -> None:
    err = urllib.error.URLError(ConnectionRefusedError())
    p = BonsaiPlanner(APPS, SETTINGS, transport=FakeTransport(err, err))
    p.client.backoff_s = 0.0
    with pytest.raises(PlannerError) as exc:
        p.plan("x", notes_snapshot())
    assert exc.value.kind == "down"


def test_warmup_sends_static_prefix_with_one_token() -> None:
    t = FakeTransport(completion("ok"))
    _planner(t).warmup()
    body = t.last_body
    assert body["max_tokens"] == 1
    assert body["messages"][:-1] == static_messages()
    assert 119.0 < t.calls[0][2] <= 120.0


# ---------------------------------------------------------------------------
# Opt-in live test against a running llama-server (SIRIOUSLY_LIVE_BONSAI=1)
# ---------------------------------------------------------------------------

live = pytest.mark.skipif(
    os.environ.get("SIRIOUSLY_LIVE_BONSAI") != "1", reason="set SIRIOUSLY_LIVE_BONSAI=1"
)


@live
def test_live_bonsai_plans_valid_actions() -> None:
    p = BonsaiPlanner(APPS, Settings(bonsai_timeout_s=60.0))
    p.warmup()
    snap = notes_snapshot()
    plan = p.plan("make a new note called standup", snap)
    shown_ids = {it.id for it in snap.items if it.enabled}
    assert plan.actions or plan.ask
    for a in plan.actions:
        assert a.target is None or a.target in shown_ids
    assert p.last_stats["cached_tokens"] > 0  # static prefix reused
    assert p.last_stats["completion_tokens"] < 60


def test_open_app_rejected_when_no_installed_app_matched() -> None:
    t = FakeTransport(completion('{"a":[{"open":"Terminal"}]}'))
    with pytest.raises(PlannerError) as exc:
        _planner(t).plan("hmm", notes_snapshot())
    assert exc.value.kind == "bad_output"
    schema = t.last_body["response_format"]["json_schema"]["schema"]
    ops = [v["required"][0] for v in schema["anyOf"][0]["properties"]["a"]["items"]["anyOf"]]
    assert "open" not in ops


def test_open_app_free_when_no_app_list() -> None:
    t = FakeTransport(completion('{"a":[{"open":"Slack"}]}'))
    plan = BonsaiPlanner((), SETTINGS, transport=t).plan("open slack", notes_snapshot())
    assert plan.actions == (Action.open_app("Slack"),)


def test_truncated_reply_is_bad_output() -> None:
    body = json.loads(completion('{"a":[{"type":"abc'))
    body["choices"][0]["finish_reason"] = "length"
    with pytest.raises(PlannerError, match="truncated"):
        _planner(FakeTransport(json.dumps(body).encode())).plan("x", notes_snapshot())


def test_last_stats_on_failure_and_success() -> None:
    err = urllib.error.URLError(ConnectionRefusedError())
    p = _planner(FakeTransport(err, err, completion('{"a":[{"press":1}]}')))
    p.client.backoff_s = 0.0
    with pytest.raises(PlannerError):
        p.plan("x", notes_snapshot())
    assert p.last_stats["failed"] == 1.0 and "prompt_tokens" not in p.last_stats
    p.plan("x", notes_snapshot())
    assert "failed" not in p.last_stats and p.last_stats["prompt_tokens"] > 0


def test_busy_planner_times_out_instead_of_waiting() -> None:
    settings = Settings(bonsai_url="http://x/v1", bonsai_timeout_s=0.05)
    p = BonsaiPlanner(APPS, settings, transport=FakeTransport())
    acquired = p._lock.acquire()
    assert acquired
    try:
        with pytest.raises(PlannerError) as exc:
            p.plan("x", notes_snapshot())
        assert exc.value.kind == "timeout"
    finally:
        p._lock.release()
    assert isinstance(p._lock, type(threading.Lock()))


def test_warmup_polls_while_server_loads() -> None:
    loading = urllib.error.HTTPError("http://x", 503, "loading", Message(), BytesIO(b""))
    t = FakeTransport(loading, loading, loading, completion("ok"))
    p = _planner(t)
    p.client.backoff_s = 0.0
    p.warmup(timeout_s=10.0)
    assert len(t.calls) == 4


def test_continuation_hint_allows_nothing_left() -> None:
    t = FakeTransport(completion('{"a":[]}'))
    plan = _planner(t).plan("new note", notes_snapshot(), hint="Already done: menu(101).")
    assert plan.actions == () and plan.ask is None
    assert (
        t.last_body["response_format"]["json_schema"]["schema"]["anyOf"][0]["properties"]["a"]["minItems"]
        == 0
    )
    with pytest.raises(PlannerError):
        _planner(FakeTransport(completion('{"a":[]}'))).plan("new note", notes_snapshot())


def test_frontmost_app_is_never_an_open_target() -> None:
    # Notes is in front: "open notes and ..." must not re-open it (no-op that stalls continuations).
    t = FakeTransport(completion('{"a":[{"menu":101}]}'))
    _planner(t).plan("open notes and start a note called standup", notes_snapshot(), hint="Already done: x.")
    assert "APPS:" not in t.last_body["messages"][-1]["content"]
    free = BonsaiPlanner(
        (), SETTINGS, transport=FakeTransport(completion('{"a":[{"open":"notes"},{"menu":101}]}'))
    )
    assert free.plan("open notes", notes_snapshot()).actions == (Action.menu(101),)
