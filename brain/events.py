"""Event bus: brain -> overlay transport (PRD §4.6).

``EventBus.emit`` is called from brain worker threads; it never blocks. Each WebSocket client
gets a :class:`Subscription` bound to its own asyncio loop: events are handed over with
``loop.call_soon_threadsafe`` into a bounded queue, and on overflow the oldest queued event is
dropped (a slow overlay must never stall the brain).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import deque
from collections.abc import Iterable

from brain.contracts import Event

logger = logging.getLogger("brain.events")

HISTORY_SIZE = 200
QUEUE_SIZE = 256
_DATA_PREVIEW = 160


def format_event(event: Event) -> str:
    """One compact human-readable line: ``+412ms action  u-1 {"i": 0, ...}``."""
    data = json.dumps(event.data, separators=(",", ":"), default=str, ensure_ascii=False)
    if len(data) > _DATA_PREVIEW:
        data = data[: _DATA_PREVIEW - 1] + "…"
    return f"+{event.ms_since_speech_end:>5}ms {event.type:<8} {event.utterance_id} {data}"


class Subscription:
    """One subscriber's bounded queue, owned by the asyncio loop it was created on."""

    def __init__(self, bus: EventBus, loop: asyncio.AbstractEventLoop, maxsize: int) -> None:
        self._bus = bus
        self._loop = loop
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self.closed = False

    # -- producer side (any thread) ---------------------------------------------------------
    def offer(self, event: Event) -> bool:
        """Hand ``event`` to the subscriber's loop. Returns False if that loop is gone."""
        if self.closed:
            return False
        try:
            self._loop.call_soon_threadsafe(self._put, event)
        except RuntimeError:  # loop closed
            self.closed = True
            return False
        return True

    def _put(self, event: Event) -> None:
        """Runs on the subscriber's loop: enqueue, dropping the oldest event when full."""
        if self._queue.full():
            self._queue.get_nowait()
            self.dropped += 1
        self._queue.put_nowait(event)

    # -- consumer side (subscriber's loop) --------------------------------------------------
    async def get(self) -> Event:
        return await self._queue.get()

    def get_nowait(self) -> Event | None:
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def qsize(self) -> int:
        return self._queue.qsize()

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Event:
        return await self.get()

    def close(self) -> None:
        self.closed = True
        self._bus.unsubscribe(self)

    async def __aenter__(self) -> Subscription:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.close()


class EventBus:
    """Thread-safe :class:`~brain.contracts.EventSink` fanning events out to async subscribers."""

    def __init__(
        self,
        history_size: int = HISTORY_SIZE,
        queue_size: int = QUEUE_SIZE,
        *,
        log: bool = True,
    ) -> None:
        self._lock = threading.Lock()
        self._history: deque[Event] = deque(maxlen=history_size)
        self._subs: list[Subscription] = []
        self._queue_size = queue_size
        self._log = log

    @property
    def history(self) -> list[Event]:
        """Snapshot of the last ``history_size`` events, oldest first."""
        with self._lock:
            return list(self._history)

    def recent(self, n: int) -> list[Event]:
        with self._lock:
            return _tail(self._history, n)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def emit(self, event: Event) -> None:
        """Record and fan out ``event``. Safe from any thread; never blocks on subscribers."""
        with self._lock:
            # Delivery happens under the lock so every subscriber sees history order.
            self._history.append(event)
            dead = [s for s in self._subs if not s.offer(event)]
            for s in dead:
                self._subs.remove(s)
        if self._log:
            logger.info("%s", format_event(event))

    def subscribe(self, replay: int = 0) -> Subscription:
        """Register a subscriber on the *running* loop, pre-filled with the last ``replay`` events.

        Registration and replay happen atomically, so no event is missed or duplicated.
        """
        loop = asyncio.get_running_loop()
        sub = Subscription(self, loop, self._queue_size)
        with self._lock:
            for event in _tail(self._history, replay):
                sub._put(event)
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)


def _tail(events: Iterable[Event], n: int) -> list[Event]:
    if n <= 0:
        return []
    items = list(events)
    return items[-n:]
