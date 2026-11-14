"""Minimal stdlib OpenAI-compatible chat client for the local llama-server (PRD §5.3).

Every request sends ``chat_template_kwargs.enable_thinking=false`` (thinking adds seconds),
``cache_prompt`` + ``id_slot`` (reuse the static prefix) and a sampling profile:

  greedy  temperature 0 (default; deterministic, best for choosing ids)
  card    the Bonsai 2 model-card thinking-off profile: temp 0.7, top_p 0.8, top_k 20, min_p 0,
          presence_penalty 1.5 over a 64-token window

Retries once (with backoff) on connection errors and 5xx (llama-server answers 503 while the model
loads), but only within the overall ``timeout_s`` deadline. Timeouts are NOT retried: the utterance
budget (8 s) is already spent by then. Proxies are bypassed: the server is always local, and a
configured HTTP(S)_PROXY must not break the offline demo.
"""

from __future__ import annotations

import http.client
import json
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from brain.planner.errors import PlannerError

JSON = dict[str, Any]
SamplingProfile = Literal["greedy", "card"]
# (url, body bytes, timeout seconds) -> response body bytes. Raise urllib/socket errors like urlopen.
Transport = Callable[[str, bytes, float], bytes]

SAMPLING: dict[str, JSON] = {
    "greedy": {"temperature": 0.0},
    "card": {
        "temperature": 0.7,
        "top_p": 0.8,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 1.5,
        "repeat_penalty": 1.0,
        "repeat_last_n": 64,
    },
}


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never route loopback via a proxy
_MIN_RETRY_S = 0.5  # don't start a retry with less than this left before the deadline


def urllib_transport(url: str, body: bytes, timeout: float) -> bytes:
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with _OPENER.open(req, timeout=timeout) as resp:
        data: bytes = resp.read()
        return data


@dataclass
class ChatResult:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    prompt_ms: float = 0.0
    decode_ms: float = 0.0
    ms: float = 0.0
    finish_reason: str = ""
    raw: JSON = field(default_factory=dict)


def _num(d: object, key: str) -> float:
    if isinstance(d, dict):
        v = d.get(key)
        if isinstance(v, int | float) and not isinstance(v, bool):
            return float(v)
    return 0.0


def parse_response(raw: JSON, ms: float) -> ChatResult:
    """Pull content + token/timing stats out of a llama-server chat completion."""
    try:
        choice = raw["choices"][0]
        content = choice["message"].get("content") or ""
        finish = choice.get("finish_reason") or ""
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        raise PlannerError("bad_output", f"no choices[0].message in reply: {e}") from e
    if not isinstance(content, str):
        raise PlannerError("bad_output", "message.content is not a string")
    usage = raw.get("usage")
    timings = raw.get("timings")
    details = usage.get("prompt_tokens_details") if isinstance(usage, dict) else None
    # llama-server: timings.cache_n = prompt tokens reused from the slot cache; prompt_n = processed.
    cached = _num(timings, "cache_n") or _num(details, "cached_tokens")
    prompt = _num(usage, "prompt_tokens") or (_num(timings, "prompt_n") + cached)
    return ChatResult(
        content=content,
        prompt_tokens=int(prompt),
        completion_tokens=int(_num(usage, "completion_tokens") or _num(timings, "predicted_n")),
        cached_tokens=int(cached),
        prompt_ms=_num(timings, "prompt_ms"),
        decode_ms=_num(timings, "predicted_ms"),
        ms=ms,
        finish_reason=str(finish),
        raw=raw,
    )


class BonsaiClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_s: float,
        *,
        sampling: SamplingProfile = "greedy",
        transport: Transport | None = None,
        retries: int = 1,
        backoff_s: float = 0.25,
        id_slot: int | None = 0,
        max_tokens: int = 200,
    ) -> None:
        if sampling not in SAMPLING:
            raise ValueError(f"unknown sampling profile {sampling!r}")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.timeout_s = timeout_s
        self.sampling: SamplingProfile = sampling
        self.transport: Transport = transport or urllib_transport
        self.retries = retries
        self.backoff_s = backoff_s
        self.id_slot = id_slot
        self.max_tokens = max_tokens

    def body(self, messages: Sequence[JSON], response_format: JSON | None) -> JSON:
        b: JSON = {
            "model": self.model,
            "messages": list(messages),
            "max_tokens": self.max_tokens,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
            "cache_prompt": True,
            **SAMPLING[self.sampling],
        }
        if self.id_slot is not None:
            b["id_slot"] = self.id_slot
        if response_format is not None:
            b["response_format"] = response_format
        return b

    def chat(self, messages: Sequence[JSON], response_format: JSON | None = None) -> ChatResult:
        payload = json.dumps(self.body(messages, response_format)).encode("utf-8")
        t0 = time.perf_counter()
        data = self._post(payload, deadline=t0 + self.timeout_s)
        ms = (time.perf_counter() - t0) * 1000.0
        try:
            raw = json.loads(data)
        except (ValueError, RecursionError) as e:  # JSONDecodeError/UnicodeDecodeError are ValueErrors
            raise PlannerError("bad_output", f"server reply is not JSON: {type(e).__name__}") from e
        if not isinstance(raw, dict):
            raise PlannerError("bad_output", "server reply is not a JSON object")
        return parse_response(raw, ms)

    def _post(self, payload: bytes, deadline: float) -> bytes:
        attempt = 0
        while True:
            timeout = max(deadline - time.perf_counter(), 0.05)
            try:
                return self.transport(self.url, payload, timeout)
            except urllib.error.HTTPError as e:
                e.close()  # release the socket before retrying
                err = PlannerError("http", f"HTTP {e.code} from {self.url}")
                retryable = e.code >= 500
            except TimeoutError as e:
                raise PlannerError("timeout", f"no reply within {self.timeout_s:.1f}s") from e
            except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
                reason = getattr(e, "reason", e)
                if isinstance(reason, TimeoutError | socket.timeout):
                    raise PlannerError("timeout", f"no reply within {self.timeout_s:.1f}s") from e
                err = PlannerError("down", f"cannot reach {self.url}: {reason!r}")
                retryable = True
            pause = self.backoff_s * (2**attempt)
            if (
                not retryable
                or attempt >= self.retries
                or deadline - time.perf_counter() - pause < _MIN_RETRY_S
            ):
                raise err
            time.sleep(pause)
            attempt += 1
