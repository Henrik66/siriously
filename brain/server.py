"""HTTP/WS front door of the brain (PRD §4.1, §4.6).

- ``POST /utterance``  speech daemon -> brain (202 immediately; ``?wait=true`` returns the Route)
- ``POST /text``       dev convenience: plain text wrapped into an Utterance
- ``WS   /events``     brain -> overlay event stream (``?history=N`` replays the last N)
- ``GET  /events/history``, ``GET /health``
- ``GET  /`` -> ``/console``: offline debug console (static page in ``brain/console/``, same event stream)
- ``GET  /eval/latest``  eval scoreboard headline from the newest ``eval/results/*.json`` (read-only)

Utterances run one at a time on a single worker thread, except a verified stop phrase (anything
``brain.control.parse_control`` calls "stop": "stop", "stop stop", "wait", "hold on", ...), which cancels
every utterance still queued (each gets an ``error`` event, ``"cancelled by stop"``), calls ``brain.stop()``
straight from the request so it cuts into a running burst, and then runs ``brain.handle(stop)`` inline on
the request (thread pool), never queued behind the running work, so its events go out at once.
With ``?wait=true`` a request whose utterance is cancelled gets 409 ``CANCELLED``; one that times out gets
504 ``TIMEOUT`` and an ``error`` event, and its utterance is cancelled if it had not started yet.

Browser hardening (the API drives the user's Mac, and any web page can reach loopback):

- ``POST /utterance`` and ``POST /text`` require ``Content-Type: application/json`` (else 415
  ``UNSUPPORTED_MEDIA_TYPE``), so a page cannot send a CORS-preflight-free ``text/plain`` POST.
- Any HTTP request or WS upgrade carrying an ``Origin`` header is refused with 403 ``FORBIDDEN_ORIGIN``
  unless the origin is listed in ``SIRIOUSLY_ALLOWED_ORIGINS`` (comma-separated, default empty). The speech
  daemon, the CLI and other native clients send no ``Origin``. ``"null"`` is never accepted.
- Optional shared secret: when ``SIRIOUSLY_TOKEN`` is set, every request except ``GET /health`` and the
  console's static files must send it in ``X-Siriously-Token``; else 401 ``UNAUTHORIZED``. Browsers cannot
  set WS headers, so a WS upgrade may instead offer the subprotocols ``siriously`` and
  ``token.<base64url(token), unpadded>`` (``Sec-WebSocket-Protocol``); the server answers ``siriously``,
  never the token. The token is never accepted in a URL (``?token=`` is refused: URLs end up in logs and
  history). Compared in constant time, never logged. ``python -m brain.cli`` sends the header itself.
- Every response carries ``X-Frame-Options: DENY`` and a CSP with ``frame-ancestors 'none'`` (the console
  must not be framed: a framed console has our origin, so its clicks would pass the Origin check), plus
  ``nosniff`` and ``Referrer-Policy: no-referrer``. The console has no inline script or style.
- A WS client that negotiates the ``siriously`` subprotocol (the console) first gets
  ``{"hello": {"replay": N}}``: the next N messages are history replay, not live events.

Bind to loopback (``SIRIOUSLY_HOST`` defaults to 127.0.0.1); set a token before binding anywhere else.
No CDN assets anywhere. Run with ``uv run siriously-brain --fixture notes`` or
``python -m brain.server --mac``.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import contextlib
import dataclasses
import functools
import hmac
import inspect
import itertools
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.request
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Literal, Protocol, cast, runtime_checkable

import anyio
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders, QueryParams
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from brain.config import Settings
from brain.contracts import (
    JSON,
    UTTERANCE_SCHEMA,
    ContractError,
    Event,
    EventSink,
    Executor,
    Route,
    Speaker,
    UIProvider,
    Utterance,
)
from brain.events import EventBus

logger = logging.getLogger("brain.server")

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
BONSAI_PROBE_TIMEOUT_S = 0.5
BONSAI_PROBE_TTL_S = 5.0
WAIT_MARGIN_S = 2.0  # ?wait=true gives up this long after the brain's own utterance timeout
WARMUP_CAP_S = 20.0  # build_runtime(warmup="sync") stops waiting for Bonsai after this long
ORIGINS_ENV = "SIRIOUSLY_ALLOWED_ORIGINS"
TOKEN_ENV = "SIRIOUSLY_TOKEN"
HOSTS_ENV = "SIRIOUSLY_ALLOWED_HOSTS"
# Host-header allowlist defeats DNS rebinding (a page on evil.example resolving to 127.0.0.1).
# Tests add Starlette's "testserver" through ``create_app(extra_hosts=...)``; the runtime never does.
DEFAULT_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
TOKEN_HEADER = "X-Siriously-Token"
WS_SUBPROTOCOL = "siriously"  # the console's WS protocol: auth via subprotocol, a replay "hello" first
TOKEN_PROTOCOL_PREFIX = "token."  # + base64url(token) without padding, offered next to WS_SUBPROTOCOL
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; connect-src 'self' ws://127.0.0.1:* ws://localhost:*; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy", CONTENT_SECURITY_POLICY),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
)
CONSOLE_DIR = Path(__file__).resolve().parent / "console"
CONSOLE_FILES = {  # URL path -> (file name, content type); an explicit whitelist, nothing else is served
    "/console": ("index.html", "text/html; charset=utf-8"),
    "/console/console.css": ("console.css", "text/css; charset=utf-8"),
    "/console/console.js": ("console.js", "text/javascript; charset=utf-8"),
}
JSON_ONLY_PATHS = frozenset({"/utterance", "/text"})
# The console's static files hold no secrets; the page then sends the token (from ``#token=``) itself.
TOKEN_EXEMPT_PATHS = frozenset({"/health", "/", *CONSOLE_FILES})
STOP_CANCEL_MESSAGE = "cancelled by stop"
_STOP_RE = re.compile(r"^\s*stop\b", re.IGNORECASE)
_WS_GONE = (WebSocketDisconnect, RuntimeError, OSError)  # client went away mid-send/receive


@runtime_checkable
class BrainLike(Protocol):
    """What the server needs from ``brain.pipeline.Brain``.

    ``handle`` may also accept ``received_at`` (``time.monotonic()`` when the request was accepted); the
    dispatcher passes it when the signature allows, so the brain can drop utterances queued before a stop.
    """

    def handle(self, utt: Utterance) -> Route: ...

    def stop(self) -> None: ...


BrainFactory = Callable[[EventSink], BrainLike]


class _HasFlowNames(Protocol):
    def names(self) -> list[str]: ...


# ---------------------------------------------------------------------------
# Request / response models (OpenAPI docs; the Utterance itself is validated by contracts)
# ---------------------------------------------------------------------------


class TextIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000, description="What the user said")
    verified: bool = Field(default=True, description="Speaker verified (False -> brain only emits rejected)")


class Accepted(BaseModel):
    accepted: bool = True
    id: str
    stopped: bool = Field(default=False, description="True when this utterance cut in with brain.stop()")


class ErrorOut(BaseModel):
    error: str
    code: str
    details: object | None = None


def _error(status: int, message: str, code: str, details: object | None = None) -> JSONResponse:
    body: JSON = {"error": message, "code": code}
    if details is not None:
        body["details"] = jsonable_encoder(details)
    return JSONResponse(status_code=status, content=body)


# ---------------------------------------------------------------------------
# Access guard: JSON-only POSTs, Origin allowlist, optional shared token
# ---------------------------------------------------------------------------


def _norm_origin(origin: str) -> str:
    return origin.strip().rstrip("/").lower()


def allowed_origins(settings: Settings) -> frozenset[str]:
    """``settings.allowed_origins`` if present, else ``SIRIOUSLY_ALLOWED_ORIGINS``. Never "null"."""
    raw: object = getattr(settings, "allowed_origins", None)
    if raw is None:
        raw = os.environ.get(ORIGINS_ENV, "")
    items: Iterable[object] = raw.split(",") if isinstance(raw, str) else cast(Iterable[object], raw)
    origins = {_norm_origin(str(o)) for o in items}
    return frozenset(o for o in origins if o and o != "null")


def console_origins(settings: Settings) -> frozenset[str]:
    """The server's own ``http://<host>:<port>`` origins (both loopback spellings + the bind host).

    The console page is same-origin, but browsers still send ``Origin`` on its fetch POSTs and WS upgrade.
    """
    hosts = {"127.0.0.1", "localhost", str(settings.host).lower().strip("[]")}
    hosts.discard("0.0.0.0")  # a wildcard bind is not a name a browser can have as its origin
    hosts.discard("::")
    return frozenset(
        f"http://[{h}]:{settings.port}" if ":" in h else f"http://{h}:{settings.port}" for h in hosts
    )


def allowed_hosts(settings: Settings, extra_hosts: Iterable[str] = ()) -> frozenset[str]:
    """Loopback names + the bind host + ``SIRIOUSLY_ALLOWED_HOSTS`` (comma-separated) + ``extra_hosts``."""
    env = {h.strip().lower() for h in os.environ.get(HOSTS_ENV, "").split(",") if h.strip()}
    extra = {h.strip().lower() for h in extra_hosts if h.strip()}
    return DEFAULT_HOSTS | {str(settings.host).lower().strip("[]")} | env | extra


def _host_of(header: str | None) -> str:
    raw = (header or "").strip().lower()
    if raw.startswith("["):  # [::1]:8765
        return raw[1:].split("]", 1)[0]
    return raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw


def api_token(settings: Settings) -> str | None:
    """``settings.token`` if present, else ``SIRIOUSLY_TOKEN``; None when unset or empty."""
    raw: object = getattr(settings, "token", None) or os.environ.get(TOKEN_ENV)
    return str(raw) if raw else None


def ws_subprotocols(headers: Headers) -> list[str]:
    """The subprotocols a WS upgrade offers (``Sec-WebSocket-Protocol``, comma-separated)."""
    return [p.strip() for p in headers.get("sec-websocket-protocol", "").split(",") if p.strip()]


def subprotocol_token(protocols: Sequence[str]) -> bytes | None:
    """The token from a ``token.<base64url>`` subprotocol; None when absent or not valid base64url."""
    for proto in protocols:
        if not proto.startswith(TOKEN_PROTOCOL_PREFIX):
            continue
        encoded = proto[len(TOKEN_PROTOCOL_PREFIX) :]
        if not _B64URL_RE.match(encoded):
            return None
        try:
            return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except (binascii.Error, ValueError):
            return None
    return None


def _is_json(content_type: str | None) -> bool:
    media = (content_type or "").split(";", 1)[0].strip().lower()
    return media == "application/json" or (media.startswith("application/") and media.endswith("+json"))


class AccessGuard:
    """Pure ASGI middleware (HTTP and WS upgrades) enforcing the rules in the module docstring."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        origins: frozenset[str],
        token: str | None,
        hosts: frozenset[str] | None = None,
    ) -> None:
        self.app = app
        self.origins = origins
        self.hosts = hosts
        self.token = token.encode() if token else None

    def denial(self, scope: Scope) -> tuple[int, str, str] | None:
        headers = Headers(scope=scope)
        if self.hosts is not None and _host_of(headers.get("host")) not in self.hosts:
            return 403, f"host {headers.get('host')!r} is not allowed (see {HOSTS_ENV})", "FORBIDDEN_HOST"
        origin = headers.get("origin")
        if origin is not None and _norm_origin(origin) not in self.origins:
            return 403, f"origin {origin!r} is not allowed (see {ORIGINS_ENV})", "FORBIDDEN_ORIGIN"
        path = str(scope.get("path", ""))
        if self.token is not None and path not in TOKEN_EXEMPT_PATHS:
            header = headers.get(TOKEN_HEADER)
            given = header.encode() if header is not None else None
            if given is None and scope["type"] == "websocket":
                given = subprotocol_token(ws_subprotocols(headers))
            if given is None or not hmac.compare_digest(given, self.token):
                if "token" in QueryParams(scope.get("query_string", b"")):
                    hint = f"send {TOKEN_HEADER} or the 'token.<base64url>' WS subprotocol"
                    return 401, f"?token= is not accepted; {hint}", "UNAUTHORIZED"
                return 401, f"missing or wrong {TOKEN_HEADER}", "UNAUTHORIZED"
        is_post = scope["type"] == "http" and scope.get("method") == "POST"
        if is_post and path in JSON_ONLY_PATHS and not _is_json(headers.get("content-type")):
            return 415, "Content-Type must be application/json", "UNSUPPORTED_MEDIA_TYPE"
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        denial = self.denial(scope) if scope["type"] in ("http", "websocket") else None
        if denial is None:
            await self.app(scope, receive, send)
            return
        status, message, code = denial
        logger.warning("refused %s %s: %s", scope["type"], scope.get("path"), code)
        response = _error(status, message, code)
        if scope["type"] == "http":
            await response(scope, receive, send)
            return
        websocket = WebSocket(scope, receive, send)
        if "websocket.http.response" in scope.get("extensions", {}):
            await websocket.send_denial_response(response)
        else:
            await websocket.close(code=1008)  # policy violation; the server answers the handshake 403


class SecurityHeaders:
    """Pure ASGI middleware adding ``SECURITY_HEADERS`` to every HTTP response (errors and denials too)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS:
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


# ---------------------------------------------------------------------------
# Control detection
# ---------------------------------------------------------------------------


@functools.cache
def _control_parser() -> Callable[[str], str | None]:
    try:
        from brain.control import parse_control
    except ImportError:  # W3 not available: minimal fallback
        logger.warning("brain.control unavailable; using fallback stop regex")

        def _fallback(text: str) -> str | None:
            return "stop" if _STOP_RE.match(text) else None

        return _fallback
    return parse_control


def is_stop(text: str) -> bool:
    return _control_parser()(text) == "stop"


# ---------------------------------------------------------------------------
# Bonsai health probe (stdlib, cached, proxy-free)
# ---------------------------------------------------------------------------


def bonsai_health_url(bonsai_url: str) -> str:
    base = bonsai_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base + "/health"


def bonsai_up(bonsai_url: str, timeout_s: float = BONSAI_PROBE_TIMEOUT_S) -> bool:
    """One quick, proxy-free GET of Bonsai's /health (never raises)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(bonsai_health_url(bonsai_url), timeout=timeout_s) as resp:
            return 200 <= int(resp.status) < 300
    except (OSError, ValueError):  # URLError/HTTPError/timeouts are OSErrors
        return False


class BonsaiProbe:
    def __init__(self, bonsai_url: str, ttl_s: float = BONSAI_PROBE_TTL_S) -> None:
        self.bonsai_url = bonsai_url
        self.url = bonsai_health_url(bonsai_url)
        self._ttl = ttl_s
        self._value = False
        self._at = float("-inf")
        self._lock = asyncio.Lock()

    def _probe(self) -> bool:
        return bonsai_up(self.bonsai_url)

    async def check(self) -> bool:
        async with self._lock:
            if time.monotonic() - self._at >= self._ttl:
                self._value = await asyncio.to_thread(self._probe)
                self._at = time.monotonic()
            return self._value


# ---------------------------------------------------------------------------
# Dispatcher: single worker thread + stop cut-in
# ---------------------------------------------------------------------------


def _accepts_kwarg(fn: Callable[..., object], name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == name or p.kind is inspect.Parameter.VAR_KEYWORD for p in params)


class Dispatcher:
    def __init__(self, brain: BrainLike, bus: EventBus) -> None:
        self.brain = brain
        self.bus = bus
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="brain-worker")
        self._lock = threading.Lock()
        self._queued: list[tuple[Utterance, Future[Route]]] = []  # submitted, not started yet
        self._pass_received_at = _accepts_kwarg(brain.handle, "received_at")

    def cut_in(self, utt: Utterance) -> bool:
        """If ``utt`` is a verified stop: drop everything queued, then call ``brain.stop()`` right now."""
        if utt.speaker.verified and is_stop(utt.text):
            logger.info("stop cut-in from %s", utt.id)
            self.cancel_queued(STOP_CANCEL_MESSAGE)  # first, so the worker can't pick the next one up
            self.brain.stop()
            return True
        return False

    def submit(self, utt: Utterance, received_at: float | None = None) -> Future[Route]:
        received_at = time.monotonic() if received_at is None else received_at
        with self._lock:  # _run takes the lock too, so it can't start before it is listed
            future = self._pool.submit(self._run, utt, received_at)
            self._queued.append((utt, future))
        return future

    def queued_ids(self) -> list[str]:
        with self._lock:
            return [u.id for u, _ in self._queued]

    def cancel_queued(self, message: str, only: Utterance | None = None) -> list[str]:
        """Cancel queued-but-not-started utterances (all, or just ``only``); each gets an error event."""
        cancelled: list[Utterance] = []
        with self._lock:
            keep: list[tuple[Utterance, Future[Route]]] = []
            for u, f in self._queued:
                if (only is None or u is only) and f.cancel():
                    cancelled.append(u)
                else:
                    keep.append((u, f))
            self._queued = keep
        for u in cancelled:
            self.emit_error(u, message)
        return [u.id for u in cancelled]

    def emit_error(self, utt: Utterance, message: str) -> None:
        ms = int((time.time() - utt.t_speech_end) * 1000) if utt.t_speech_end else 0
        self.bus.emit(Event("error", utt.id, {"message": message}, max(ms, 0)))

    def _run(self, utt: Utterance, received_at: float) -> Route:
        with self._lock:
            self._queued = [(u, f) for u, f in self._queued if u is not utt]
        return self.handle_now(utt, received_at)

    def handle_now(self, utt: Utterance, received_at: float) -> Route:
        """``brain.handle`` on the calling thread (the worker, or a request thread for a stop)."""
        try:
            if self._pass_received_at:
                handle: Callable[..., Route] = self.brain.handle
                return handle(utt, received_at=received_at)
            return self.brain.handle(utt)
        except Exception as exc:
            logger.exception("brain.handle failed for %s", utt.id)
            self.emit_error(utt, f"internal error: {exc}")
            raise

    def shutdown(self) -> None:
        with contextlib.suppress(Exception):
            self.brain.stop()
        self._pool.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _resolve_brain(brain: BrainLike | BrainFactory, sink: EventSink) -> BrainLike:
    # A class is a factory too, but its attributes would satisfy the Protocol check.
    if isinstance(brain, type) or not isinstance(brain, BrainLike):
        factory = cast(BrainFactory, brain)
        return factory(sink)
    return brain


def _flow_count(brain: BrainLike) -> int:
    flows = getattr(brain, "flows", None)
    if flows is None or not callable(getattr(flows, "names", None)):
        return 0
    store: _HasFlowNames = flows
    return len(store.names())


def _utterance_openapi() -> JSON:
    schema = {k: v for k, v in UTTERANCE_SCHEMA.items() if k != "$schema"}
    return {"requestBody": {"required": True, "content": {"application/json": {"schema": schema}}}}


def create_app(
    brain: BrainLike | BrainFactory,
    bus: EventBus | None = None,
    settings: Settings | None = None,
    *,
    extra_hosts: Iterable[str] = (),
) -> FastAPI:
    """Build the app. ``brain`` may be a Brain or a factory taking the EventSink (the bus).

    ``extra_hosts`` widens the Host allowlist (tests pass Starlette's ``"testserver"``).
    """
    bus = bus if bus is not None else EventBus()
    settings = settings if settings is not None else Settings()
    the_brain = _resolve_brain(brain, bus)
    dispatcher = Dispatcher(the_brain, bus)
    probe = BonsaiProbe(settings.bonsai_url)
    text_ids = itertools.count(1)
    wait_timeout = settings.utterance_timeout_s + WAIT_MARGIN_S

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        dispatcher.shutdown()

    app = FastAPI(title="Siriously brain", version="0.1.0", lifespan=lifespan)
    app.state.bus = bus
    app.state.brain = the_brain
    app.state.dispatcher = dispatcher
    app.add_middleware(
        AccessGuard,
        origins=allowed_origins(settings) | console_origins(settings),
        token=api_token(settings),
        hosts=allowed_hosts(settings, extra_hosts),
    )
    app.add_middleware(SecurityHeaders)  # added last = outermost: also covers AccessGuard's denials
    _install_error_handlers(app)

    async def dispatch(utt: Utterance, wait: bool) -> JSONResponse:
        received_at = time.monotonic()
        if dispatcher.cut_in(utt):
            # The stop path takes no brain lock: handle it right here, not behind the running work.
            inline = asyncio.ensure_future(run_in_threadpool(dispatcher.handle_now, utt, received_at))
            result = await wait_for_route(utt, inline, queued=False)
            if wait:
                return result
            return JSONResponse(status_code=202, content=Accepted(id=utt.id, stopped=True).model_dump())
        future = dispatcher.submit(utt, received_at)
        if not wait:
            return JSONResponse(status_code=202, content=Accepted(id=utt.id).model_dump())
        return await wait_for_route(utt, asyncio.wrap_future(future), queued=True)

    async def wait_for_route(utt: Utterance, waiter: asyncio.Future[Route], *, queued: bool) -> JSONResponse:
        done, _ = await asyncio.wait({waiter}, timeout=wait_timeout)  # unlike wait_for, never cancels
        if not done:
            waiter.add_done_callback(_consume_result)
            limit = f"no result after {wait_timeout:.1f}s"
            cancelled = queued and dispatcher.cancel_queued(f"{limit}; cancelled before it started", only=utt)
            if not cancelled:
                dispatcher.emit_error(utt, f"{limit} (still running)")
            return _error(504, f"utterance {utt.id} did not finish in {wait_timeout:.1f}s", "TIMEOUT")
        if waiter.cancelled():
            return _error(409, f"utterance {utt.id} was cancelled before it ran", "CANCELLED")
        exc = waiter.exception()
        if exc is not None:
            return _error(500, f"brain failed: {exc}", "BRAIN_ERROR")
        return JSONResponse(status_code=200, content=waiter.result().to_dict())

    @app.post(
        "/utterance",
        status_code=202,
        response_model=Accepted,
        responses={200: {"description": "Route (with ?wait=true)"}, 422: {"model": ErrorOut}},
        openapi_extra=_utterance_openapi(),
    )
    async def post_utterance(
        request: Request,
        wait: bool = Query(False, description="Block until handled and return the Route"),
    ) -> JSONResponse:
        try:
            payload = await request.json()
        except ValueError:
            return _error(422, "body is not valid JSON", "BAD_UTTERANCE")
        if not isinstance(payload, dict):
            return _error(422, "utterance must be a JSON object", "BAD_UTTERANCE")
        try:
            utt = Utterance.from_dict(payload)
        except (ContractError, ValueError, TypeError) as exc:
            return _error(422, str(exc), "BAD_UTTERANCE")
        return await dispatch(utt, wait)

    @app.post("/text", status_code=202, response_model=Accepted, responses={422: {"model": ErrorOut}})
    async def post_text(body: TextIn, wait: bool = Query(False)) -> JSONResponse:
        speaker = Speaker(verified=body.verified, score=1.0 if body.verified else 0.0, name="text")
        utt = Utterance(id=f"t-{next(text_ids)}", text=body.text, t_speech_end=time.time(), speaker=speaker)
        return await dispatch(utt, wait)

    @app.get("/health")
    async def health() -> JSON:
        return {
            "ok": True,
            "bonsai": await probe.check(),
            "flows": _flow_count(the_brain),
            "confirm_ttl_s": settings.confirm_ttl_s,  # how long a confirm / question stays answerable
        }

    def console_file(path: str) -> Callable[[], FileResponse]:
        name, media_type = CONSOLE_FILES[path]

        def serve() -> FileResponse:
            return FileResponse(CONSOLE_DIR / name, media_type=media_type)

        return serve

    for console_path in CONSOLE_FILES:
        app.add_api_route(console_path, console_file(console_path), methods=["GET"], include_in_schema=False)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/console", status_code=307)

    @app.get("/eval/latest", responses={404: {"model": ErrorOut}})
    async def eval_latest() -> JSONResponse:
        return await asyncio.to_thread(_latest_eval)

    @app.get("/events/history")
    async def events_history(n: int = Query(50, ge=0, le=200)) -> JSON:
        events = bus.recent(n)
        return {"count": len(events), "events": [e.to_dict() for e in events]}

    @app.websocket("/events")
    async def events_ws(websocket: WebSocket, history: int = Query(0, ge=0, le=200)) -> None:
        offered = WS_SUBPROTOCOL in ws_subprotocols(websocket.headers)
        await stream_events(websocket, bus, history, subprotocol=WS_SUBPROTOCOL if offered else None)

    return app


def _consume_result(fut: asyncio.Future[Route]) -> None:
    """Retrieve a late result/exception so asyncio does not log "exception was never retrieved"."""
    if not fut.cancelled():
        fut.exception()


def _latest_eval() -> JSONResponse:
    """Headline of the newest eval results file. Dev tooling: imported lazily, read-only."""
    try:
        from eval import demo_board
        from eval.report import headline
    except ImportError:
        return _error(404, "eval harness is not installed", "EVAL_UNAVAILABLE")
    path = (
        demo_board.latest_results(demo_board.DEFAULT_RESULTS) if demo_board.DEFAULT_RESULTS.is_dir() else None
    )
    if path is None:
        return _error(404, "no eval results yet (run: uv run python -m eval.run_eval)", "NO_EVAL_RESULTS")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return JSONResponse(content=headline(data, path.name))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return _error(404, f"unreadable eval results {path.name}: {exc}", "NO_EVAL_RESULTS")


def _install_error_handlers(app: FastAPI) -> None:
    async def on_validation(_: Request, exc: Exception) -> JSONResponse:
        errors = exc.errors() if isinstance(exc, RequestValidationError) else str(exc)
        return _error(422, "invalid request", "VALIDATION_ERROR", errors)

    async def on_http(_: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, StarletteHTTPException):
            return _error(500, "internal error", "INTERNAL")
        return _error(exc.status_code, str(exc.detail), f"HTTP_{exc.status_code}")

    app.add_exception_handler(RequestValidationError, on_validation)
    app.add_exception_handler(StarletteHTTPException, on_http)


async def stream_events(
    websocket: WebSocket, bus: EventBus, history: int = 0, *, subprotocol: str | None = None
) -> None:
    """Pump bus events to one WS client until it disconnects.

    With ``subprotocol`` (the console's ``siriously``) the client first gets ``{"hello": {"replay": N}}``,
    N being how many of the following messages are history replay rather than live events.
    """
    sub = bus.subscribe(replay=history)  # before accept: nothing emitted after connect is missed
    replayed = sub.qsize()  # exact: live events reach the queue only via this loop, which hasn't yielded
    try:
        await websocket.accept(subprotocol=subprotocol)
        if subprotocol is not None:
            await websocket.send_text(json.dumps({"hello": {"replay": replayed}}))
        # anyio (Starlette's own concurrency layer) so both halves die with the request's cancel scope.
        async with anyio.create_task_group() as tg:

            async def pump() -> None:
                try:
                    async for event in sub:
                        await websocket.send_text(json.dumps(event.to_dict(), default=str))
                except _WS_GONE:
                    pass
                tg.cancel_scope.cancel()

            async def drain() -> None:  # client messages are ignored; this only notices disconnects
                try:
                    while (await websocket.receive())["type"] != "websocket.disconnect":
                        pass
                except _WS_GONE:
                    pass
                tg.cancel_scope.cancel()

            tg.start_soon(pump)
            tg.start_soon(drain)
    finally:
        sub.close()


# ---------------------------------------------------------------------------
# Runtime assembly (shared with brain.cli)
# ---------------------------------------------------------------------------


class SetupError(RuntimeError):
    """A runtime component could not be built (missing module, bad fixture, ...)."""


def make_io(fixture: str | None, mac: bool) -> tuple[UIProvider, Executor]:
    """Fixture fakes (dev, off-Mac) or Henrik's Mac Accessibility provider/executor."""
    if mac:
        try:
            from brain.mac import MacExecutor, MacUIProvider
        except ImportError as exc:
            raise SetupError(
                f"--mac needs brain.mac (MacUIProvider/MacExecutor, Henrik's module, macOS + pyobjc): {exc}. "
                "Off-Mac, use --fixture <name> instead."
            ) from exc
        mac_provider: UIProvider = MacUIProvider()
        try:  # the executor resolves (snapshot_id, id) through the provider's element map
            mac_executor: Executor = MacExecutor(mac_provider)
        except TypeError:  # an executor that keeps no provider reference
            mac_executor = MacExecutor()
        return mac_provider, mac_executor
    try:
        from brain.fixtures import FakeExecutor, FixtureUIProvider
    except ImportError as exc:
        raise SetupError(f"brain.fixtures is unavailable: {exc}") from exc
    try:
        fx_provider = FixtureUIProvider(fixture or "notes")
    except (OSError, KeyError, ValueError) as exc:
        raise SetupError(f"cannot load fixture {fixture!r}: {exc}") from exc
    fx_executor: Executor = FakeExecutor(fx_provider)
    provider: UIProvider = fx_provider
    return provider, fx_executor


def build_runtime(
    sink: EventSink,
    settings: Settings,
    *,
    fixture: str | None = None,
    mac: bool = False,
    no_model: bool = False,
    warmup: Literal["background", "sync", "off"] = "background",
) -> BrainLike:
    """Provider + executor (+ Bonsai unless ``no_model``) -> Brain. Raises SetupError.

    ``warmup`` primes Bonsai's prompt cache so the first real command isn't slow; "sync" waits for it at
    most ``WARMUP_CAP_S`` (the warmup keeps going in the background after that).
    """
    provider, executor = make_io(fixture, mac)
    try:
        from brain.addressing import Prefs
        from brain.pipeline import build_brain

        # On the Mac: wake word, follow-ups and menu-bar permissions (~/.siriously/settings.json).
        prefs = Prefs() if mac else None
        brain = build_brain(provider, executor, sink, settings=settings, use_model=not no_model, prefs=prefs)
    except ImportError as exc:
        raise SetupError(f"brain pipeline is unavailable: {exc}") from exc
    if not no_model and warmup != "off":
        warm = threading.Thread(target=brain.warmup, name="planner-warmup", daemon=True)
        warm.start()
        if warmup == "sync":
            warm.join(WARMUP_CAP_S)
            if warm.is_alive():
                logger.warning("planner warmup still running after %.0fs; continuing", WARMUP_CAP_S)
    result: BrainLike = brain
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    defaults = Settings()
    p = argparse.ArgumentParser(prog="siriously-brain", description="Siriously brain HTTP/WS server")
    io = p.add_mutually_exclusive_group()
    io.add_argument("--fixture", metavar="NAME", help="dev mode: FixtureUIProvider + FakeExecutor")
    io.add_argument("--mac", action="store_true", help="real macOS provider/executor (brain.mac)")
    p.add_argument("--no-model", action="store_true", help="no Bonsai planner (control/flows/fast path)")
    p.add_argument("--host", default=defaults.host, help=f"bind address (default {defaults.host})")
    p.add_argument("--port", type=int, default=defaults.port, help=f"port (default {defaults.port})")
    p.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    settings = dataclasses.replace(Settings(), host=args.host, port=args.port)
    mac = bool(args.mac) or (args.fixture is None and sys.platform == "darwin")
    fixture = args.fixture or (None if mac else "notes")
    bus = EventBus()
    try:
        brain = build_runtime(bus, settings, fixture=fixture, mac=mac, no_model=args.no_model)
    except SetupError as exc:
        print(f"siriously-brain: {exc}", file=sys.stderr)
        return 2
    if settings.host not in LOOPBACK_HOSTS and api_token(settings) is None:
        logger.warning(
            "binding to %s without %s: anyone on the network can drive this Mac", settings.host, TOKEN_ENV
        )
    logger.info(
        "brain ready (%s, model %s) on http://%s:%d",
        "mac" if mac else f"fixture {fixture}",
        "off" if args.no_model else "on",
        settings.host,
        settings.port,
    )
    import uvicorn

    uvicorn.run(
        create_app(brain, bus, settings), host=settings.host, port=settings.port, log_level=args.log_level
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
