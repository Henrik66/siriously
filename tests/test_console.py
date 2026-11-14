"""Offline debug console (brain/console/*) and the server routes/origin rules behind it."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from brain import server as server_mod
from brain.config import Settings
from brain.contracts import Event, EventSink, Route, Utterance
from brain.events import EventBus
from brain.server import console_origins, create_app

CONSOLE_DIR = Path(server_mod.__file__).resolve().parent / "console"
PORT = 8799
OWN_ORIGIN = f"http://127.0.0.1:{PORT}"


class EchoBrain:
    def __init__(self, sink: EventSink) -> None:
        self.sink = sink

    def handle(self, utt: Utterance) -> Route:
        self.sink.emit(Event("heard", utt.id, {"text": utt.text, "speaker": utt.speaker.to_dict()}))
        self.sink.emit(Event("done", utt.id, {"route": "none", "n_actions": 0, "ms": 1}))
        return Route(kind="none", reason="echo")

    def stop(self) -> None:
        return None


@pytest.fixture
def client() -> TestClient:
    settings = Settings(host="127.0.0.1", port=PORT)
    return TestClient(create_app(EchoBrain, EventBus(), settings))


# -- static page ----------------------------------------------------------------------------------


def test_console_page_and_assets_served(client: TestClient) -> None:
    page = client.get("/console")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "Siriously" in page.text
    css = client.get("/console/console.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    js = client.get("/console/console.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    assert "/events" in js.text and "/text" in js.text


def test_root_redirects_to_console(client: TestClient) -> None:
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (301, 302, 307, 308)
    assert r.headers["location"] == "/console"
    assert client.get("/", follow_redirects=True).status_code == 200


def test_unlisted_console_paths_are_not_served(client: TestClient) -> None:
    assert client.get("/console/__init__.py").status_code == 404
    assert client.get("/console/../server.py").status_code == 404


def test_console_files_have_no_external_urls() -> None:
    files = [CONSOLE_DIR / n for n in ("index.html", "console.css", "console.js")]
    for f in files:
        assert f.is_file(), f
        text = f.read_text(encoding="utf-8")
        assert not re.findall(r"https?://", text), f"{f.name} references an external URL"
        assert "@import" not in text and "@font-face" not in text


def test_console_references_are_same_origin() -> None:
    html = (CONSOLE_DIR / "index.html").read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)="([^"]+)"', html)
    assert refs
    assert all(r.startswith(("/console/", "#")) for r in refs), refs


# -- /eval/latest ---------------------------------------------------------------------------------


def _results_file(path: Path) -> None:
    summary = {"total": 10, "pass": 9, "fail": 1, "error": 0, "skip": 0}
    split = {"dev": {"total": 6, "pass": 6, "skip": 0}, "holdout": {"total": 4, "pass": 3, "skip": 0}}
    body = {
        "meta": {"timestamp": "2026-01-01T00:00:00", "planner": "none"},
        "modes": {
            "router": {"summary": {**summary, "by_split": split}, "latency_by_route": {}},
            "e2e": {"summary": {**summary, "pass": 8, "by_split": split}, "latency_by_route": {}},
        },
    }
    path.write_text(json.dumps(body), encoding="utf-8")


def test_eval_latest_without_results(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from eval import demo_board

    monkeypatch.setattr(demo_board, "DEFAULT_RESULTS", tmp_path / "missing")
    r = client.get("/eval/latest")
    assert r.status_code == 404
    assert set(r.json()) == {"error", "code"}


def test_eval_latest_with_results(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from eval import demo_board

    _results_file(tmp_path / "20260101-000000.json")
    monkeypatch.setattr(demo_board, "DEFAULT_RESULTS", tmp_path)
    r = client.get("/eval/latest")
    assert r.status_code == 200
    body = r.json()
    assert body["modes"]["router"]["rate"] == 0.9
    assert body["modes"]["e2e"]["rate"] == 0.8
    assert body["modes"]["router"]["split"]["holdout"]["rate"] == 0.75


def test_eval_latest_unreadable_file_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from eval import demo_board

    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(demo_board, "DEFAULT_RESULTS", tmp_path)
    r = client.get("/eval/latest")
    assert r.status_code == 404 and r.json()["code"] == "NO_EVAL_RESULTS"


# -- same-origin allowance ------------------------------------------------------------------------


def test_console_origins_cover_both_loopback_forms() -> None:
    origins = console_origins(Settings(host="127.0.0.1", port=PORT))
    assert origins == {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
    assert not any("0.0.0.0" in o for o in console_origins(Settings(host="0.0.0.0", port=PORT)))


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_same_origin_post_and_ws_accepted(client: TestClient, host: str) -> None:
    origin = f"http://{host}:{PORT}"
    r = client.post("/text", json={"text": "hello"}, headers={"Origin": origin, "Host": f"{host}:{PORT}"})
    assert r.status_code == 202
    with client.websocket_connect("/events?history=5", headers={"Origin": origin}) as ws:
        assert ws.receive_json()["type"] in {"heard", "done"}


@pytest.mark.parametrize(
    "origin",
    ["http://evil.example", f"http://127.0.0.1:{PORT + 1}", f"https://127.0.0.1:{PORT}", "null"],
)
def test_foreign_origin_still_forbidden(client: TestClient, origin: str) -> None:
    r = client.post("/text", json={"text": "hello"}, headers={"Origin": origin})
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN_ORIGIN"
    denied = (WebSocketDenialResponse, WebSocketDisconnect)
    with pytest.raises(denied), client.websocket_connect("/events", headers={"Origin": origin}):
        pass


def test_host_check_still_enforced(client: TestClient) -> None:
    r = client.get("/console", headers={"Host": "evil.example"})
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN_HOST"
    r = client.post("/text", json={"text": "x"}, headers={"Host": "evil.example", "Origin": OWN_ORIGIN})
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN_HOST"


def test_token_still_required_for_api_but_not_console_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIRIOUSLY_TOKEN", "s3cret")
    c = TestClient(create_app(EchoBrain, EventBus(), Settings(host="127.0.0.1", port=PORT)))
    assert c.get("/console").status_code == 200
    assert c.get("/console/console.js").status_code == 200
    assert c.get("/eval/latest").status_code == 401
    assert c.post("/text", json={"text": "x"}).status_code == 401
    ok = c.post("/text", json={"text": "x"}, headers={"X-Siriously-Token": "s3cret"})
    assert ok.status_code == 202
