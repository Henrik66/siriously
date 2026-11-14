from __future__ import annotations

import http.client
import io
import json
import urllib.error
from email.message import Message

import pytest

from brain.planner.client import BonsaiClient, parse_response
from brain.planner.errors import PlannerError
from brain.planner.testing import FakeTransport, completion

MSGS = [{"role": "user", "content": "hi"}]


def _client(t: FakeTransport, **kw: object) -> BonsaiClient:
    return BonsaiClient("http://127.0.0.1:8080/v1/", "bonsai", 6.0, transport=t, backoff_s=0.0, **kw)  # type: ignore[arg-type]


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", Message(), io.BytesIO(b""))


def test_request_body_has_required_llama_server_fields() -> None:
    t = FakeTransport(completion("{}"))
    _client(t).chat(MSGS, {"type": "json_schema"})
    url, body, timeout = t.calls[0]
    assert url == "http://127.0.0.1:8080/v1/chat/completions"
    assert 5.9 < timeout <= 6.0  # remaining time to the request deadline
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["cache_prompt"] is True
    assert body["id_slot"] == 0
    assert body["temperature"] == 0.0
    assert "top_p" not in body
    assert body["response_format"] == {"type": "json_schema"}
    assert body["model"] == "bonsai" and body["messages"] == MSGS and body["stream"] is False


def test_card_sampling_profile() -> None:
    t = FakeTransport(completion("{}"))
    _client(t, sampling="card").chat(MSGS)
    b = t.last_body
    assert (b["temperature"], b["top_p"], b["top_k"], b["min_p"], b["presence_penalty"]) == (
        0.7,
        0.8,
        20,
        0.0,
        1.5,
    )
    assert "response_format" not in b


def test_unknown_profile_rejected() -> None:
    with pytest.raises(ValueError):
        _client(FakeTransport(), sampling="hot")


def test_stats_from_timings() -> None:
    r = _client(FakeTransport(completion('{"a":1}', cache_n=900, prompt_n=120, predicted_n=20))).chat(MSGS)
    assert r.content == '{"a":1}'
    assert (r.prompt_tokens, r.cached_tokens, r.completion_tokens) == (1020, 900, 20)
    assert (r.prompt_ms, r.decode_ms) == (400.0, 800.0)
    assert r.ms >= 0


def test_stats_fallback_to_openai_usage_details() -> None:
    raw = {
        "choices": [{"message": {"content": "x"}}],
        "usage": {
            "prompt_tokens": 50,
            "completion_tokens": 3,
            "prompt_tokens_details": {"cached_tokens": 40},
        },
    }
    r = parse_response(raw, 12.0)
    assert (r.prompt_tokens, r.cached_tokens, r.completion_tokens, r.ms) == (50, 40, 3, 12.0)


def test_retries_once_on_connection_refused() -> None:
    t = FakeTransport(urllib.error.URLError(ConnectionRefusedError()), completion("{}"))
    assert _client(t).chat(MSGS).content == "{}"
    assert len(t.calls) == 2


def test_down_after_retry() -> None:
    t = FakeTransport(urllib.error.URLError(ConnectionRefusedError()), ConnectionResetError())
    with pytest.raises(PlannerError) as exc:
        _client(t).chat(MSGS)
    assert exc.value.kind == "down" and len(t.calls) == 2


def test_timeout_not_retried() -> None:
    for err in (TimeoutError(), TimeoutError(), urllib.error.URLError(TimeoutError())):
        t = FakeTransport(err, completion("{}"))
        with pytest.raises(PlannerError) as exc:
            _client(t).chat(MSGS)
        assert exc.value.kind == "timeout" and len(t.calls) == 1


def test_503_retried_then_ok() -> None:
    t = FakeTransport(_http_error(503), completion("{}"))
    assert _client(t).chat(MSGS).content == "{}"


def test_400_not_retried() -> None:
    t = FakeTransport(_http_error(400), completion("{}"))
    with pytest.raises(PlannerError) as exc:
        _client(t).chat(MSGS)
    assert exc.value.kind == "http" and len(t.calls) == 1


@pytest.mark.parametrize(
    "body", [b"not json", b"[1,2]", b'{"choices": []}', b'{"choices":[{"message":{"content":5}}]}']
)
def test_bad_server_reply(body: bytes) -> None:
    with pytest.raises(PlannerError) as exc:
        _client(FakeTransport(body)).chat(MSGS)
    assert exc.value.kind == "bad_output"


@pytest.mark.parametrize("err", [http.client.IncompleteRead(b"x", 10), http.client.BadStatusLine("x")])
def test_http_protocol_errors_are_down(err: Exception) -> None:
    with pytest.raises(PlannerError) as exc:
        _client(FakeTransport(err, err)).chat(MSGS)
    assert exc.value.kind == "down"


@pytest.mark.parametrize("body", [b'{"x": ' + b"1" * 5000 + b"}", b"[" * 100000])
def test_pathological_json_is_bad_output(body: bytes) -> None:
    with pytest.raises(PlannerError) as exc:
        _client(FakeTransport(body)).chat(MSGS)
    assert exc.value.kind == "bad_output"


def test_no_retry_when_deadline_nearly_spent() -> None:
    t = FakeTransport(urllib.error.URLError(ConnectionRefusedError()), completion("{}"))
    c = BonsaiClient("http://x/v1", "bonsai", 0.3, transport=t, backoff_s=0.0)
    with pytest.raises(PlannerError) as exc:
        c.chat(MSGS)
    assert exc.value.kind == "down" and len(t.calls) == 1


def test_finish_reason_is_parsed() -> None:
    raw = json.loads(completion("{}"))
    raw["choices"][0]["finish_reason"] = "length"
    assert parse_response(raw, 1.0).finish_reason == "length"
