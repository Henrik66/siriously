"""Brain CLI: run utterances in-process, talk to a running server, or watch its event stream.

python -m brain.cli "open notes and start a note called standup" --fixture notes --no-model
python -m brain.cli --repl --fixture notes            # one utterance per stdin line
python -m brain.cli --post "stop"                      # POST /utterance?wait=true to a server
python -m brain.cli --post --repl                      # same, line by line
python -m brain.cli --watch --history 20               # print WS /events as they arrive

--post / --watch send ``SIRIOUSLY_TOKEN`` (if set) as ``X-Siriously-Token``. In-process mode checks
Bonsai's /health first; if it is down it warns once and skips the planner warmup instead of blocking.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from typing import Literal, TextIO, cast

from brain.config import Settings
from brain.contracts import EVENT_TYPES, JSON, Event, EventType, Speaker, Utterance
from brain.events import format_event

Runner = Callable[[Utterance], JSON]
_ids = itertools.count(1)


class PrintSink:
    """EventSink that prints each event as one line (in-process mode)."""

    def __init__(self, out: TextIO | None = None) -> None:
        self._out = out

    def emit(self, event: Event) -> None:
        print("  " + format_event(event), file=self._out or sys.stdout, flush=True)


def make_utterance(text: str, *, verified: bool = True) -> Utterance:
    speaker = Speaker(verified=verified, score=1.0 if verified else 0.0, name="cli")
    return Utterance(id=f"cli-{next(_ids)}", text=text, t_speech_end=time.time(), speaker=speaker)


def event_from_json(raw: str | bytes) -> Event | None:
    """Parse one WS message into an Event (None if it isn't one)."""
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(d, dict) or d.get("type") not in EVENT_TYPES:
        return None
    data = d.get("data")
    return Event(
        type=cast(EventType, d["type"]),
        utterance_id=str(d.get("utterance_id", "")),
        data=data if isinstance(data, dict) else {},
        ms_since_speech_end=int(d.get("ms_since_speech_end") or 0),
    )


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------


def auth_headers() -> dict[str, str]:
    """``{"X-Siriously-Token": ...}`` when ``SIRIOUSLY_TOKEN`` is set, else empty."""
    from brain.server import TOKEN_ENV, TOKEN_HEADER

    token = os.environ.get(TOKEN_ENV)
    return {TOKEN_HEADER: token} if token else {}


def local_runner(fixture: str, no_model: bool, settings: Settings | None = None) -> Runner:
    """Build a Brain over fixtures once; each call handles one utterance in-process."""
    from brain.server import bonsai_up, build_runtime

    settings = settings or Settings()
    warmup: Literal["sync", "off"] = "sync"
    if not no_model and not bonsai_up(settings.bonsai_url):
        print(
            f"brain.cli: warning: Bonsai is not answering at {settings.bonsai_url}; "
            "skipping warmup (planner commands will fail until it is up, or use --no-model)",
            file=sys.stderr,
        )
        warmup = "off"
    brain = build_runtime(PrintSink(), settings, fixture=fixture, no_model=no_model, warmup=warmup)

    def run(utt: Utterance) -> JSON:
        return brain.handle(utt).to_dict()

    return run


def post_runner(base_url: str, timeout_s: float = 30.0) -> Runner:
    """POST each utterance to a running server with ?wait=true and return its JSON reply."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def run(utt: Utterance) -> JSON:
        req = urllib.request.Request(
            f"{base_url}/utterance?wait=true",
            data=json.dumps(utt.to_dict()).encode(),
            headers={"Content-Type": "application/json", **auth_headers()},
            method="POST",
        )
        try:
            with opener.open(req, timeout=timeout_s) as resp:
                body: JSON = json.loads(resp.read())
                return body
        except urllib.error.HTTPError as exc:
            reply: JSON = json.loads(exc.read() or b"{}")
            return {"http_status": exc.code, **reply}

    return run


def run_lines(lines: Iterable[str], runner: Runner, *, verified: bool, out: TextIO) -> int:
    """Run each non-empty line; prints events (local mode) and the final route. Returns exit code."""
    status = 0
    for line in lines:
        text = line.strip()
        if not text:
            continue
        if text.lower() in {"quit", "exit"}:
            break
        utt = make_utterance(text, verified=verified)
        print(f"> {text}  [{utt.id}]", file=out, flush=True)
        try:
            route = runner(utt)
        except (OSError, RuntimeError) as exc:
            print(f"error: {exc}", file=out, flush=True)
            status = 1
            continue
        print(json.dumps(route, indent=2, default=str), file=out, flush=True)
    return status


def watch(ws_url: str, out: TextIO, *, max_events: int | None = None) -> int:
    """Print every event from WS /events until interrupted (or after ``max_events`` messages)."""
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect

    try:
        with connect(ws_url, open_timeout=3, additional_headers=auth_headers()) as ws:
            print(f"watching {ws_url} (ctrl+c to quit)", file=out, flush=True)
            for n, message in enumerate(ws, start=1):
                event = event_from_json(message)
                print(format_event(event) if event else f"? {message!r}", file=out, flush=True)
                if max_events is not None and n >= max_events:
                    break
    except KeyboardInterrupt:
        return 0
    except ConnectionClosed:
        print("server closed the connection", file=out)
        return 0
    except (OSError, TimeoutError) as exc:
        print(f"cannot connect to {ws_url}: {exc}", file=out)
        return 1
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    s = Settings()
    p = argparse.ArgumentParser(prog="python -m brain.cli", description="Siriously brain CLI")
    p.add_argument("text", nargs="*", help="utterance text (omit with --repl / --watch)")
    p.add_argument("--fixture", default="notes", help="fixture for in-process mode (default notes)")
    p.add_argument("--no-model", action="store_true", help="no Bonsai planner")
    p.add_argument("--unverified", action="store_true", help="send as an unverified speaker")
    p.add_argument("--repl", action="store_true", help="read one utterance per stdin line")
    remote = p.add_mutually_exclusive_group()
    remote.add_argument("--post", action="store_true", help="POST to a running server instead")
    remote.add_argument("--watch", action="store_true", help="print WS /events from a running server")
    p.add_argument("--history", type=int, default=0, help="--watch: replay the last N events first")
    p.add_argument("--host", default=s.host)
    p.add_argument("--port", type=int, default=s.port)
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    out = sys.stdout
    if args.watch:
        return watch(f"ws://{args.host}:{args.port}/events?history={max(args.history, 0)}", out)
    if not args.repl and not args.text:
        print("nothing to do: give an utterance, --repl or --watch (see --help)", file=sys.stderr)
        return 2
    if args.post:
        runner = post_runner(f"http://{args.host}:{args.port}")
    else:
        from brain.server import SetupError

        try:
            runner = local_runner(args.fixture, args.no_model)
        except SetupError as exc:
            print(f"brain.cli: {exc}", file=sys.stderr)
            return 2
    lines: Iterable[str] = sys.stdin if args.repl else [" ".join(args.text)]
    if args.repl and sys.stdin.isatty():
        print("one utterance per line; 'quit' to exit", file=out)
    return run_lines(lines, runner, verified=not args.unverified, out=out)


if __name__ == "__main__":
    sys.exit(main())
