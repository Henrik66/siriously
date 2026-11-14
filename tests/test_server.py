"""HTTP/WS server (brain.server) and CLI helpers (brain.cli), with small local fake brains."""

from __future__ import annotations

import base64
import io
import json
import socket
import sys
import threading
import time
import types
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from brain import cli
from brain import server as server_mod
from brain.config import Settings
from brain.contracts import JSON, Event, EventSink, Route, Utterance
from brain.events import EventBus
from brain.server import BrainFactory, BrainLike, bonsai_health_url, create_app, is_stop, main, make_io

# Starlette's TestClient sends ``Host: testserver``; only tests allow it (never the runtime defaults).
TEST_HOSTS = frozenset({"testserver"})

# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------


class FakeFlows:
    def names(self) -> list[str]:
        return ["standup", "inbox"]


class FakeBrain:
    """Emits heard -> route -> done for every utterance and returns Route(kind='none')."""

    def __init__(self, sink: EventSink, fail: bool = False) -> None:
        self.sink = sink
        self.fail = fail
        self.handled: list[Utterance] = []
        self.stops = 0
        self.flows = FakeFlows()

    def handle(self, utt: Utterance) -> Route:
        self.handled.append(utt)
        self.sink.emit(Event("heard", utt.id, {"text": utt.text, "speaker": utt.speaker.to_dict()}))
        if self.fail:
            raise RuntimeError("boom")
        self.sink.emit(Event("route", utt.id, {"kind": "none", "reason": "fake", "ms": 0.1}))
        self.sink.emit(Event("done", utt.id, {"route": "none", "n_actions": 0, "ms": 1}))
        return Route(kind="none", reason="fake")

    def stop(self) -> None:
        self.stops += 1


class BlockingBrain:
    """A burst that runs until stop() is called; 'stop' itself is handled instantly."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self._stop = threading.Event()
        self.stop_calls = 0
        self.finished: list[str] = []

    def handle(self, utt: Utterance) -> Route:
        if is_stop(utt.text):
            self.finished.append(utt.id)
            return Route(kind="control", control="stop")
        self.started.set()
        aborted = self._stop.wait(timeout=5)
        self.finished.append(utt.id)
        return Route(kind="planner", reason="aborted" if aborted else "completed")

    def stop(self) -> None:
        self.stop_calls += 1
        self._stop.set()


def utt_json(uid: str, text: str, verified: bool = True) -> JSON:
    return {
        "id": uid,
        "text": text,
        "t_speech_end": time.time(),
        "stt_ms": 120,
        "speaker": {"verified": verified, "score": 0.6 if verified else 0.01, "name": "henrik"},
    }


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def wait_until(pred: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


def down_settings() -> Settings:
    return Settings(bonsai_url=f"http://127.0.0.1:{free_port()}/v1")


def make_app(
    brain: BrainLike | BrainFactory, bus: EventBus | None = None, settings: Settings | None = None
) -> FastAPI:
    return create_app(brain, bus, settings, extra_hosts=TEST_HOSTS)


@pytest.fixture
def bus() -> EventBus:
    return EventBus(log=False)


@pytest.fixture
def fake(bus: EventBus) -> FakeBrain:
    return FakeBrain(bus)


@pytest.fixture
def client(fake: FakeBrain, bus: EventBus) -> Iterator[TestClient]:
    with TestClient(make_app(fake, bus, down_settings())) as c:
        yield c


# ---------------------------------------------------------------------------
# POST /utterance
# ---------------------------------------------------------------------------


def test_utterance_accepted_202_and_handled_in_background(client: TestClient, fake: FakeBrain) -> None:
    r = client.post("/utterance", json=utt_json("u-1", "open notes"))
    assert r.status_code == 202
    assert r.json() == {"accepted": True, "id": "u-1", "stopped": False}
    wait_until(lambda: [u.id for u in fake.handled] == ["u-1"])
    assert fake.handled[0].speaker.name == "henrik"


@pytest.mark.parametrize(
    "body",
    [
        {"id": "u-1", "text": "hi"},  # missing speaker
        {"id": 5, "text": "hi", "speaker": {"verified": True}},  # wrong id type
        {"id": "u-1", "text": "hi", "speaker": {"score": 0.3}},  # speaker.verified missing
        {"id": "u-1", "text": "hi", "t_speech_end": "soon", "speaker": {"verified": True}},
        ["not", "an", "object"],
    ],
)
def test_utterance_validation_422(client: TestClient, fake: FakeBrain, body: object) -> None:
    r = client.post("/utterance", json=body)
    assert r.status_code == 422
    assert r.json()["code"] == "BAD_UTTERANCE"
    assert isinstance(r.json()["error"], str) and r.json()["error"]
    assert fake.handled == []


def test_utterance_invalid_json_422(client: TestClient) -> None:
    r = client.post("/utterance", content=b"{nope", headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert r.json()["code"] == "BAD_UTTERANCE"


def test_utterance_wait_returns_route(client: TestClient, fake: FakeBrain) -> None:
    r = client.post("/utterance?wait=true", json=utt_json("u-2", "open notes"))
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "none" and body["reason"] == "fake"
    assert [u.id for u in fake.handled] == ["u-2"]


def test_brain_exception_gives_500_and_error_event(bus: EventBus) -> None:
    brain = FakeBrain(bus, fail=True)
    with TestClient(make_app(brain, bus, down_settings())) as c:
        r = c.post("/utterance?wait=true", json=utt_json("u-3", "explode"))
    assert r.status_code == 500
    assert r.json()["code"] == "BRAIN_ERROR"
    assert [e.type for e in bus.history] == ["heard", "error"]
    assert "boom" in bus.history[-1].data["message"]


def test_utterances_are_serialized(client: TestClient, fake: FakeBrain) -> None:
    for i in range(10):
        assert client.post("/utterance", json=utt_json(f"u-{i}", "x")).status_code == 202
    wait_until(lambda: len(fake.handled) == 10)
    assert [u.id for u in fake.handled] == [f"u-{i}" for i in range(10)]


# ---------------------------------------------------------------------------
# stop cut-in
# ---------------------------------------------------------------------------


def test_stop_cuts_into_running_burst() -> None:
    brain = BlockingBrain()
    with TestClient(make_app(brain, EventBus(log=False), down_settings())) as c:
        assert c.post("/utterance", json=utt_json("u-1", "write a long essay")).status_code == 202
        assert brain.started.wait(3)
        t0 = time.monotonic()
        r = c.post("/utterance?wait=true", json=utt_json("u-2", "Stop!"))
        elapsed = time.monotonic() - t0
    assert r.status_code == 200
    assert r.json()["kind"] == "control" and r.json()["control"] == "stop"
    assert elapsed < 2.0  # did not wait for the 5 s burst timeout
    assert brain.stop_calls >= 1
    wait_until(lambda: sorted(brain.finished) == ["u-1", "u-2"])  # burst aborted; the stop handled too


class StubbornBrain:
    """A burst that ignores stop() until released; records which thread handled each utterance."""

    def __init__(self, sink: EventSink) -> None:
        self.sink = sink
        self.started = threading.Event()
        self.release = threading.Event()
        self.stop_calls = 0
        self.calls: list[tuple[str, str, float | None]] = []  # (id, thread name, received_at)

    def handle(self, utt: Utterance, received_at: float | None = None) -> Route:
        self.calls.append((utt.id, threading.current_thread().name, received_at))
        self.sink.emit(Event("heard", utt.id, {"text": utt.text}))
        if is_stop(utt.text):
            self.sink.emit(Event("done", utt.id, {"route": "control", "n_actions": 0, "ms": 0}))
            return Route(kind="control", control="stop")
        self.started.set()
        self.release.wait(timeout=5)
        self.sink.emit(Event("done", utt.id, {"route": "planner", "n_actions": 1, "ms": 1}))
        return Route(kind="planner", reason="completed")

    def stop(self) -> None:
        self.stop_calls += 1


def test_stop_is_handled_inline_not_behind_running_work(bus: EventBus) -> None:
    """The stop utterance runs on the request (thread pool), never queued behind the busy worker."""
    brain = StubbornBrain(bus)
    with TestClient(make_app(brain, bus, down_settings())) as c:
        assert c.post("/utterance", json=utt_json("u-1", "write a long essay")).status_code == 202
        assert brain.started.wait(3)
        t0 = time.monotonic()
        r = c.post("/utterance?wait=true", json=utt_json("u-s", "stop"))
        t1 = time.monotonic()
        assert r.status_code == 200 and r.json()["control"] == "stop"
        assert t1 - t0 < 2.0
        assert brain.stop_calls == 1
        # the stop's events are already out while u-1 is still running
        done = [e.utterance_id for e in bus.history if e.type == "done"]
        assert done == ["u-s"]
        assert c.app.state.dispatcher.queued_ids() == []  # type: ignore[attr-defined]
        brain.release.set()
        wait_until(lambda: len([e for e in bus.history if e.type == "done"]) == 2)
    by_id = {uid: (thread, got) for uid, thread, got in brain.calls}
    assert not by_id["u-s"][0].startswith("brain-worker")
    assert by_id["u-1"][0].startswith("brain-worker")
    stop_received = by_id["u-s"][1]
    assert stop_received is not None and t0 <= stop_received <= t1


def test_stop_without_wait_reports_stopped(fake: FakeBrain, client: TestClient) -> None:
    r = client.post("/utterance", json=utt_json("u-9", "stop"))
    assert r.status_code == 202 and r.json()["stopped"] is True
    assert fake.stops == 1
    assert [u.id for u in fake.handled] == ["u-9"]  # handled before the 202 went out


def test_stop_handle_failure_is_reported(bus: EventBus) -> None:
    brain = FakeBrain(bus, fail=True)
    with TestClient(make_app(brain, bus, down_settings())) as c:
        r = c.post("/utterance?wait=true", json=utt_json("u-s", "stop"))
        assert r.status_code == 500 and r.json()["code"] == "BRAIN_ERROR"
        r = c.post("/utterance", json=utt_json("u-t", "stop"))
        assert r.status_code == 202 and r.json()["stopped"] is True
        assert brain.stops == 2  # (shutdown calls stop() once more)
    assert set(_errors(bus)) == {"u-s", "u-t"}


def test_unverified_stop_does_not_cut_in(fake: FakeBrain, client: TestClient) -> None:
    r = client.post("/utterance", json=utt_json("u-5", "stop", verified=False))
    assert r.json()["stopped"] is False
    assert fake.stops == 0
    wait_until(lambda: len(fake.handled) == 1)  # brain still sees it (and emits `rejected`)


def test_non_stop_text_does_not_cut_in(fake: FakeBrain, client: TestClient) -> None:
    client.post("/utterance?wait=true", json=utt_json("u-6", "stop the music in spotify"))
    assert fake.stops == 0
    assert not is_stop("undo")


# ---------------------------------------------------------------------------
# WS /events and history
# ---------------------------------------------------------------------------


def test_ws_receives_events_emitted_during_request(client: TestClient) -> None:
    with client.websocket_connect("/events") as ws:
        assert client.post("/text?wait=true", json={"text": "open notes"}).status_code == 200
        msgs = [ws.receive_json() for _ in range(3)]
    assert [m["type"] for m in msgs] == ["heard", "route", "done"]
    assert all(m["utterance_id"] == "t-1" for m in msgs)
    assert set(msgs[0]) == {"type", "utterance_id", "data", "ms_since_speech_end"}
    assert msgs[0]["data"]["text"] == "open notes"


def test_ws_history_replay(client: TestClient, bus: EventBus) -> None:
    for i in range(5):
        bus.emit(Event("action", "u-1", {"i": i}))
    with client.websocket_connect("/events?history=2") as ws:
        first = [ws.receive_json()["data"]["i"] for _ in range(2)]
        bus.emit(Event("done", "u-1", {"i": 99}))
        live = ws.receive_json()
    assert first == [3, 4]
    assert live["type"] == "done"


def test_ws_disconnect_unsubscribes(client: TestClient, bus: EventBus) -> None:
    with client.websocket_connect("/events"):
        assert bus.subscriber_count == 1
    wait_until(lambda: bus.subscriber_count == 0)


def test_events_history_endpoint(client: TestClient, bus: EventBus) -> None:
    for i in range(7):
        bus.emit(Event("action", "u-1", {"i": i}))
    body = client.get("/events/history?n=3").json()
    assert body["count"] == 3
    assert [e["data"]["i"] for e in body["events"]] == [4, 5, 6]
    assert client.get("/events/history?n=999").status_code == 422


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


def test_health_bonsai_down(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body == {"ok": True, "bonsai": False, "flows": 2, "confirm_ttl_s": 20.0}


def test_health_reports_confirm_ttl(fake: FakeBrain, bus: EventBus) -> None:
    settings = Settings(bonsai_url=down_settings().bonsai_url, confirm_ttl_s=7.5)
    with TestClient(make_app(fake, bus, settings)) as c:
        assert c.get("/health").json()["confirm_ttl_s"] == 7.5


def test_runtime_hosts_do_not_include_testserver(fake: FakeBrain, bus: EventBus) -> None:
    assert "testserver" not in server_mod.DEFAULT_HOSTS
    with TestClient(create_app(fake, bus, down_settings())) as c:
        r = c.get("/health")  # TestClient's default Host is "testserver"
        assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN_HOST"
        assert c.get("/health", headers={"Host": "127.0.0.1:8765"}).status_code == 200


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, format: str, *args: object) -> None:
        return None


def test_health_bonsai_up_and_cached(bus: EventBus) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings = Settings(bonsai_url=f"http://127.0.0.1:{server.server_address[1]}/v1")
    try:
        with TestClient(make_app(FakeBrain(bus), bus, settings)) as c:
            assert c.get("/health").json()["bonsai"] is True
            server.shutdown()
            server.server_close()
            assert c.get("/health").json()["bonsai"] is True  # cached for 5 s
    finally:
        server.server_close()


def test_bonsai_health_url() -> None:
    assert bonsai_health_url("http://127.0.0.1:8080/v1") == "http://127.0.0.1:8080/health"
    assert bonsai_health_url("http://127.0.0.1:8080/v1/") == "http://127.0.0.1:8080/health"
    assert bonsai_health_url("http://h:1") == "http://h:1/health"


# ---------------------------------------------------------------------------
# POST /text, factory form, main()
# ---------------------------------------------------------------------------


def test_text_wraps_into_utterance(client: TestClient, fake: FakeBrain) -> None:
    before = time.time()
    r = client.post("/text?wait=true", json={"text": "open notes", "verified": False})
    assert r.status_code == 200
    r2 = client.post("/text", json={"text": "again"})
    assert r2.status_code == 202 and r2.json()["id"] == "t-2"
    wait_until(lambda: len(fake.handled) == 2)
    u = fake.handled[0]
    assert u.id == "t-1" and u.text == "open notes"
    assert u.speaker.verified is False
    assert u.t_speech_end >= before
    assert fake.handled[1].speaker.verified is True


def test_text_validation_uses_standard_error_shape(client: TestClient) -> None:
    r = client.post("/text", json={"text": ""})
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "VALIDATION_ERROR" and body["error"] and body["details"]


def test_app_accepts_brain_factory(bus: EventBus) -> None:
    made: list[FakeBrain] = []

    def factory(sink: EventSink) -> FakeBrain:
        made.append(FakeBrain(sink))
        return made[-1]

    with TestClient(make_app(factory, bus, down_settings())) as c:
        c.post("/text?wait=true", json={"text": "hi"})
    assert made[0].sink is bus
    assert [e.type for e in bus.history] == ["heard", "route", "done"]


def test_openapi_documents_utterance_schema(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    schema = spec["paths"]["/utterance"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert schema["required"] == ["id", "text", "speaker"]


def test_main_mac_without_module_fails_clearly(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "brain.mac", None)  # brain/mac.py exists: simulate a failed import
    assert main(["--mac", "--no-model"]) == 2
    assert "brain.mac" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Browser hardening: content type, Origin allowlist, optional shared token
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIRIOUSLY_TOKEN", raising=False)
    monkeypatch.delenv("SIRIOUSLY_ALLOWED_ORIGINS", raising=False)


def _ws_refused(c: TestClient, url: str, headers: dict[str, str] | None = None) -> int:
    """Open a WS the server must refuse; returns the HTTP status of the denial (or the close code)."""
    try:
        with c.websocket_connect(url, headers=headers or {}):
            pass
    except WebSocketDenialResponse as exc:
        return int(exc.status_code)
    except WebSocketDisconnect as exc:
        return int(exc.code)
    raise AssertionError("websocket was accepted")


@pytest.mark.parametrize("path", ["/utterance", "/text"])
@pytest.mark.parametrize("ctype", ["text/plain", "application/x-www-form-urlencoded", None])
def test_post_requires_json_content_type(
    client: TestClient, fake: FakeBrain, path: str, ctype: str | None
) -> None:
    body = utt_json("u-1", "empty trash") if path == "/utterance" else {"text": "empty trash"}
    headers = {"Content-Type": ctype} if ctype else {}
    r = client.post(path, content=json.dumps(body).encode(), headers=headers)
    assert r.status_code == 415
    assert r.json()["code"] == "UNSUPPORTED_MEDIA_TYPE" and r.json()["error"]
    time.sleep(0.05)
    assert fake.handled == []


def test_json_content_type_with_charset_ok(client: TestClient) -> None:
    r = client.post(
        "/utterance",
        content=json.dumps(utt_json("u-1", "x")).encode(),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    assert r.status_code == 202


@pytest.mark.parametrize("origin", ["http://evil.example", "null", "http://127.0.0.1:8766"])
def test_cross_origin_http_rejected(client: TestClient, fake: FakeBrain, origin: str) -> None:
    hdr = {"Origin": origin}
    r = client.post("/utterance", json=utt_json("u-1", "empty trash"), headers=hdr)
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN_ORIGIN"
    assert client.get("/events/history", headers=hdr).status_code == 403
    assert client.get("/health", headers=hdr).status_code == 403
    time.sleep(0.05)
    assert fake.handled == []


def test_cross_origin_ws_rejected(client: TestClient, bus: EventBus) -> None:
    assert _ws_refused(client, "/events", {"Origin": "http://evil.example"}) in (403, 1008)
    assert bus.subscriber_count == 0


def test_allowed_origin_passes(monkeypatch: pytest.MonkeyPatch, fake: FakeBrain, bus: EventBus) -> None:
    monkeypatch.setenv("SIRIOUSLY_ALLOWED_ORIGINS", "http://localhost:5173, app://overlay")
    with TestClient(make_app(fake, bus, down_settings())) as c:
        ok = {"Origin": "http://localhost:5173"}
        assert c.post("/utterance", json=utt_json("u-1", "x"), headers=ok).status_code == 202
        with c.websocket_connect("/events", headers={"Origin": "app://overlay"}):
            pass
        bad = {"Origin": "http://evil.example"}
        assert c.post("/utterance", json=utt_json("u-2", "x"), headers=bad).status_code == 403


@pytest.fixture
def token_client(monkeypatch: pytest.MonkeyPatch, fake: FakeBrain, bus: EventBus) -> Iterator[TestClient]:
    monkeypatch.setenv("SIRIOUSLY_TOKEN", "s3cret")
    with TestClient(make_app(fake, bus, down_settings())) as c:
        yield c


def test_token_required_when_configured(token_client: TestClient, fake: FakeBrain) -> None:
    c = token_client
    r = c.post("/utterance", json=utt_json("u-1", "x"))
    assert r.status_code == 401 and r.json()["code"] == "UNAUTHORIZED"
    assert c.post("/text", json={"text": "x"}, headers={"X-Siriously-Token": "wrong"}).status_code == 401
    assert c.get("/events/history").status_code == 401
    good = {"X-Siriously-Token": "s3cret"}
    assert c.post("/utterance", json=utt_json("u-2", "x"), headers=good).status_code == 202
    assert c.get("/events/history", headers=good).status_code == 200
    assert c.get("/health").status_code == 200  # liveness stays open (the Origin check still applies)
    wait_until(lambda: [u.id for u in fake.handled] == ["u-2"])


def _token_protocol(token: str) -> str:
    return "token." + base64.urlsafe_b64encode(token.encode()).decode().rstrip("=")


def test_ws_token_header_or_subprotocol(token_client: TestClient) -> None:
    assert _ws_refused(token_client, "/events") in (401, 1008)
    with token_client.websocket_connect("/events", headers={"X-Siriously-Token": "s3cret"}):
        pass
    protocols = ["siriously", _token_protocol("s3cret")]
    with token_client.websocket_connect("/events", subprotocols=protocols) as ws:
        assert ws.accepted_subprotocol == "siriously"  # the token itself is never echoed back
    bad = {"Sec-WebSocket-Protocol": f"siriously, {_token_protocol('nope')}"}
    assert _ws_refused(token_client, "/events", bad) in (401, 1008)
    assert _ws_refused(token_client, "/events", {"Sec-WebSocket-Protocol": "siriously, token.%%%"}) in (
        401,
        1008,
    )


def test_ws_query_token_no_longer_accepted(
    token_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The token must not travel in the URL (access logs, history); ``?token=`` is refused."""
    with caplog.at_level("WARNING", logger="brain.server"):
        assert _ws_refused(token_client, "/events?token=s3cret") in (401, 1008)
    assert "s3cret" not in caplog.text


def test_ws_siriously_subprotocol_gets_replay_hello(client: TestClient, bus: EventBus) -> None:
    for i in range(3):
        bus.emit(Event("action", "u-1", {"i": i}))
    with client.websocket_connect("/events?history=2", subprotocols=["siriously"]) as ws:
        assert ws.accepted_subprotocol == "siriously"
        assert ws.receive_json() == {"hello": {"replay": 2}}
        assert [ws.receive_json()["data"]["i"] for _ in range(2)] == [1, 2]
    with client.websocket_connect("/events?history=2") as ws:  # plain clients see events only
        assert ws.accepted_subprotocol is None
        assert ws.receive_json()["data"]["i"] == 1


# ---------------------------------------------------------------------------
# stop cancels queued work; ?wait=true timeouts are reported
# ---------------------------------------------------------------------------


def _errors(bus: EventBus) -> dict[str, str]:
    return {e.utterance_id: str(e.data.get("message")) for e in bus.history if e.type == "error"}


def _queued(c: TestClient) -> list[str]:
    ids: list[str] = c.app.state.dispatcher.queued_ids()  # type: ignore[attr-defined]
    return ids


def test_stop_cancels_queued_utterances() -> None:
    brain, bus = BlockingBrain(), EventBus(log=False)
    with TestClient(make_app(brain, bus, down_settings())) as c:
        assert c.post("/utterance", json=utt_json("u-1", "write a long essay")).status_code == 202
        assert brain.started.wait(3)
        for uid in ("u-2", "u-3"):
            assert c.post("/utterance", json=utt_json(uid, "delete this note")).status_code == 202
        assert _queued(c) == ["u-2", "u-3"]
        r = c.post("/utterance?wait=true", json=utt_json("u-s", "stop stop"))
        assert r.status_code == 200 and r.json()["control"] == "stop"
        assert _queued(c) == []
        wait_until(lambda: len(brain.finished) == 2)
    assert sorted(brain.finished) == ["u-1", "u-s"]  # u-2 / u-3 never ran
    assert _errors(bus) == {"u-2": "cancelled by stop", "u-3": "cancelled by stop"}


class ReceivedAtBrain(FakeBrain):
    """Accepts the optional ``received_at`` keyword like ``brain.pipeline.Brain.handle``."""

    def __init__(self, sink: EventSink) -> None:
        super().__init__(sink)
        self.received: list[float | None] = []

    def handle(self, utt: Utterance, received_at: float | None = None) -> Route:
        self.received.append(received_at)
        return super().handle(utt)


def test_received_at_is_passed_when_supported(bus: EventBus) -> None:
    brain = ReceivedAtBrain(bus)
    with TestClient(make_app(brain, bus, down_settings())) as c:
        t0 = time.monotonic()
        assert c.post("/utterance?wait=true", json=utt_json("u-1", "open notes")).status_code == 200
        t1 = time.monotonic()
    assert len(brain.received) == 1
    got = brain.received[0]
    assert got is not None and t0 <= got <= t1


@pytest.mark.parametrize("text", ["wait", "hold on", "stop everything"])
def test_other_stop_phrasings_cut_in(fake: FakeBrain, client: TestClient, text: str) -> None:
    r = client.post("/utterance", json=utt_json("u-9", text))
    assert r.json()["stopped"] is True and fake.stops == 1


def test_waiting_request_for_cancelled_utterance_gets_409() -> None:
    brain, bus = BlockingBrain(), EventBus(log=False)
    codes: list[tuple[int, str]] = []
    with TestClient(make_app(brain, bus, down_settings())) as c:
        c.post("/utterance", json=utt_json("u-1", "write a long essay"))
        assert brain.started.wait(3)

        def waiter() -> None:
            r = c.post("/utterance?wait=true", json=utt_json("u-2", "delete this note"))
            codes.append((r.status_code, r.json()["code"]))

        th = threading.Thread(target=waiter)
        th.start()
        wait_until(lambda: _queued(c) == ["u-2"])
        c.post("/utterance", json=utt_json("u-s", "stop"))
        th.join(5)
    assert codes == [(409, "CANCELLED")]


def test_wait_timeout_cancels_queued_and_emits_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_mod, "WAIT_MARGIN_S", 0.0)
    brain, bus = BlockingBrain(), EventBus(log=False)
    settings = Settings(bonsai_url=down_settings().bonsai_url, utterance_timeout_s=0.3)
    with TestClient(make_app(brain, bus, settings)) as c:
        c.post("/utterance", json=utt_json("u-1", "write a long essay"))
        assert brain.started.wait(3)
        r = c.post("/utterance?wait=true", json=utt_json("u-2", "delete this note"))
        assert r.status_code == 504 and r.json()["code"] == "TIMEOUT"
        assert "cancelled" in _errors(bus)["u-2"]
        assert _queued(c) == []
        c.post("/utterance", json=utt_json("u-s", "stop"))
        wait_until(lambda: "u-s" in brain.finished)
    assert "u-2" not in brain.finished


def test_wait_timeout_while_running_emits_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_mod, "WAIT_MARGIN_S", 0.0)
    brain, bus = BlockingBrain(), EventBus(log=False)
    settings = Settings(bonsai_url=down_settings().bonsai_url, utterance_timeout_s=0.3)
    with TestClient(make_app(brain, bus, settings)) as c:
        r = c.post("/utterance?wait=true", json=utt_json("u-1", "write a long essay"))
        assert r.status_code == 504
        assert "still running" in _errors(bus)["u-1"]
        c.post("/utterance", json=utt_json("u-s", "stop"))


# ---------------------------------------------------------------------------
# make_io: Mac executor shares the provider's element map
# ---------------------------------------------------------------------------


class _FakeMacProvider:
    pass


class _ExecutorTakesProvider:
    def __init__(self, provider: _FakeMacProvider) -> None:
        self.provider = provider


class _ExecutorNoArgs:
    def __init__(self) -> None:
        self.provider = None


@pytest.mark.parametrize("executor_cls", [_ExecutorTakesProvider, _ExecutorNoArgs])
def test_make_io_mac_passes_provider_when_supported(
    monkeypatch: pytest.MonkeyPatch, executor_cls: type
) -> None:
    mod = types.ModuleType("brain.mac")
    mod.MacUIProvider = _FakeMacProvider  # type: ignore[attr-defined]
    mod.MacExecutor = executor_cls  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "brain.mac", mod)
    provider, executor = make_io(None, mac=True)
    assert isinstance(provider, _FakeMacProvider) and isinstance(executor, executor_cls)
    expected = provider if executor_cls is _ExecutorTakesProvider else None
    assert executor.provider is expected  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def test_cli_event_from_json_roundtrip() -> None:
    e = Event("action", "u-1", {"i": 0, "ok": True}, 42)
    import json

    assert cli.event_from_json(json.dumps(e.to_dict())) == e
    assert cli.event_from_json("not json") is None
    assert cli.event_from_json('{"type": "bogus"}') is None


def test_cli_run_lines_with_runner() -> None:
    seen: list[Utterance] = []

    def runner(u: Utterance) -> JSON:
        seen.append(u)
        return Route(kind="none", reason="fake").to_dict()

    out = io.StringIO()
    code = cli.run_lines(["open notes", "", "  stop  ", "quit", "never"], runner, verified=False, out=out)
    assert code == 0
    assert [u.text for u in seen] == ["open notes", "stop"]
    assert all(not u.speaker.verified for u in seen)
    assert '"kind": "none"' in out.getvalue()


@pytest.fixture
def live_port(fake: FakeBrain, bus: EventBus) -> Iterator[int]:
    """A real uvicorn server on a free loopback port (exercises the real WS stack)."""
    import uvicorn

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(make_app(fake, bus, down_settings()), host="127.0.0.1", port=port, log_level="error")
    )
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    try:
        wait_until(lambda: server.started, timeout=5)
        yield port
    finally:
        server.should_exit = True
        th.join(timeout=5)


def test_cli_post_runner_against_live_server(live_port: int, fake: FakeBrain) -> None:
    route = cli.post_runner(f"http://127.0.0.1:{live_port}")(cli.make_utterance("open notes"))
    assert route["kind"] == "none"
    assert fake.handled[0].text == "open notes"


def test_live_ws_stream_and_disconnect(live_port: int, bus: EventBus) -> None:
    from websockets.sync.client import connect

    with connect(f"ws://127.0.0.1:{live_port}/events", open_timeout=3) as ws:
        wait_until(lambda: bus.subscriber_count == 1)
        bus.emit(Event("heard", "u-1", {"text": "hi"}))  # emitted from this (non-loop) thread
        got = cli.event_from_json(ws.recv(timeout=3))
    assert got is not None and got.type == "heard"
    wait_until(lambda: bus.subscriber_count == 0)


def test_cli_requires_something_to_do(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 2
    assert "nothing to do" in capsys.readouterr().err


@pytest.fixture
def secured_live_port(monkeypatch: pytest.MonkeyPatch, fake: FakeBrain, bus: EventBus) -> Iterator[int]:
    """A live server with SIRIOUSLY_TOKEN set (the CLI reads the same env var)."""
    import uvicorn

    monkeypatch.setenv("SIRIOUSLY_TOKEN", "s3cret")
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(make_app(fake, bus, down_settings()), host="127.0.0.1", port=port, log_level="error")
    )
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    try:
        wait_until(lambda: server.started, timeout=5)
        yield port
    finally:
        server.should_exit = True
        th.join(timeout=5)


def test_cli_post_sends_token(secured_live_port: int, fake: FakeBrain) -> None:
    route = cli.post_runner(f"http://127.0.0.1:{secured_live_port}")(cli.make_utterance("open notes"))
    assert route["kind"] == "none"
    assert fake.handled[0].text == "open notes"


def test_cli_watch_sends_token(secured_live_port: int, bus: EventBus) -> None:
    out = io.StringIO()
    url = f"ws://127.0.0.1:{secured_live_port}/events"
    th = threading.Thread(target=cli.watch, args=(url, out), kwargs={"max_events": 1}, daemon=True)
    th.start()
    wait_until(lambda: bus.subscriber_count == 1)
    bus.emit(Event("heard", "u-1", {"text": "hi"}))
    th.join(5)
    assert not th.is_alive()
    assert "u-1" in out.getvalue() and "cannot connect" not in out.getvalue()


def test_cli_local_runner_does_not_block_when_bonsai_down(capsys: pytest.CaptureFixture[str]) -> None:
    t0 = time.monotonic()
    runner = cli.local_runner("notes", no_model=False, settings=down_settings())
    assert time.monotonic() - t0 < 3.0
    assert "Bonsai" in capsys.readouterr().err
    assert callable(runner)


def test_foreign_host_header_rejected() -> None:
    """DNS rebinding: a request whose Host is an attacker domain is refused."""
    from fastapi.testclient import TestClient

    from brain.config import Settings
    from brain.events import EventBus
    from brain.server import create_app

    class _Brain:
        def handle(self, utt: object) -> object:
            raise AssertionError("must not be called")

        def stop(self) -> None:
            return None

    client = TestClient(create_app(_Brain(), EventBus(log=False), Settings()))  # type: ignore[arg-type]
    r = client.get("/events/history", headers={"host": "evil.example:8765"})
    assert r.status_code == 403
    assert r.json()["code"] == "FORBIDDEN_HOST"
    assert client.get("/health", headers={"host": "127.0.0.1:8765"}).status_code == 200
    assert client.get("/health", headers={"host": "localhost"}).status_code == 200
