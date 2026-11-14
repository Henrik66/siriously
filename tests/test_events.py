"""EventBus: thread-safe emit, async fan-out, bounded queues and history."""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from brain.contracts import Event, EventSink
from brain.events import EventBus, format_event


def ev(i: int, utt: str = "u-1", type_: str = "action") -> Event:
    return Event(type=type_, utterance_id=utt, data={"i": i}, ms_since_speech_end=i)  # type: ignore[arg-type]


def emit_range(bus: EventBus, n: int) -> None:
    for i in range(n):
        bus.emit(ev(i))


def test_bus_is_an_event_sink() -> None:
    assert isinstance(EventBus(log=False), EventSink)


def test_emit_from_threads_delivers_all_in_history_order() -> None:
    n_threads, per_thread = 8, 50
    bus = EventBus(history_size=n_threads * per_thread, queue_size=10_000, log=False)

    async def scenario() -> list[Event]:
        sub = bus.subscribe()
        start = threading.Barrier(n_threads)

        def worker(t: int) -> None:
            start.wait()
            for i in range(per_thread):
                bus.emit(ev(i, utt=f"u-{t}"))

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for th in threads:
            th.start()
        got = [await asyncio.wait_for(sub.get(), timeout=5) for _ in range(n_threads * per_thread)]
        for th in threads:
            th.join()
        sub.close()
        return got

    got = asyncio.run(scenario())
    assert len(got) == n_threads * per_thread
    assert got == bus.history  # every subscriber sees exactly the history order
    for t in range(n_threads):
        mine = [e.data["i"] for e in got if e.utterance_id == f"u-{t}"]
        assert mine == list(range(per_thread))  # per-producer order preserved


def test_multiple_subscribers_each_get_everything() -> None:
    bus = EventBus(log=False)

    async def scenario() -> tuple[list[Event], list[Event]]:
        a, b = bus.subscribe(), bus.subscribe()
        th = threading.Thread(target=emit_range, args=(bus, 20))
        th.start()
        got_a = [await asyncio.wait_for(a.get(), 5) for _ in range(20)]
        got_b = [await asyncio.wait_for(b.get(), 5) for _ in range(20)]
        th.join()
        return got_a, got_b

    got_a, got_b = asyncio.run(scenario())
    assert got_a == got_b == bus.history


def test_subscribe_replays_last_n_then_streams_live() -> None:
    bus = EventBus(log=False)
    for i in range(5):
        bus.emit(ev(i))

    async def scenario() -> list[int]:
        sub = bus.subscribe(replay=2)
        bus.emit(ev(99))
        return [(await sub.get()).data["i"] for _ in range(3)]

    assert asyncio.run(scenario()) == [3, 4, 99]


def test_no_replay_by_default() -> None:
    bus = EventBus(log=False)
    bus.emit(ev(1))

    async def scenario() -> int:
        sub = bus.subscribe()
        await asyncio.sleep(0)
        return sub.qsize()

    assert asyncio.run(scenario()) == 0


def test_overflow_drops_oldest_and_never_blocks() -> None:
    bus = EventBus(queue_size=3, log=False)

    async def scenario() -> tuple[list[int], int]:
        sub = bus.subscribe()
        th = threading.Thread(target=emit_range, args=(bus, 10))
        th.start()
        th.join(timeout=2)  # emit must not block even though nobody consumes
        assert not th.is_alive()
        await asyncio.sleep(0.05)  # let the call_soon_threadsafe callbacks run
        got = []
        while (e := sub.get_nowait()) is not None:
            got.append(e.data["i"])
        return got, sub.dropped

    got, dropped = asyncio.run(scenario())
    assert got == [7, 8, 9]
    assert dropped == 7


def test_history_is_bounded() -> None:
    bus = EventBus(log=False)
    for i in range(250):
        bus.emit(ev(i))
    hist = bus.history
    assert len(hist) == 200
    assert hist[0].data["i"] == 50 and hist[-1].data["i"] == 249
    assert [e.data["i"] for e in bus.recent(3)] == [247, 248, 249]
    assert bus.recent(0) == []


def test_close_unsubscribes() -> None:
    bus = EventBus(log=False)

    async def scenario() -> int:
        async with bus.subscribe() as sub:
            assert bus.subscriber_count == 1
        bus.emit(ev(1))
        await asyncio.sleep(0)
        return sub.qsize()

    assert asyncio.run(scenario()) == 0
    assert bus.subscriber_count == 0


def test_subscriber_with_dead_loop_is_dropped_silently() -> None:
    bus = EventBus(log=False)

    async def scenario() -> None:
        bus.subscribe()  # never closed; its loop dies with asyncio.run

    asyncio.run(scenario())
    assert bus.subscriber_count == 1
    bus.emit(ev(1))  # must not raise
    assert bus.subscriber_count == 0
    assert len(bus.history) == 1


def test_subscribe_requires_running_loop() -> None:
    with pytest.raises(RuntimeError):
        EventBus(log=False).subscribe()


def test_emit_logs_one_compact_line(caplog: pytest.LogCaptureFixture) -> None:
    bus = EventBus()
    with caplog.at_level(logging.INFO, logger="brain.events"):
        bus.emit(Event("heard", "u-7", {"text": "open notes"}, 12))
    lines = [r.getMessage() for r in caplog.records if r.name == "brain.events"]
    assert len(lines) == 1
    assert "\n" not in lines[0]
    assert "heard" in lines[0] and "u-7" in lines[0] and "+   12ms" in lines[0]


def test_format_event_truncates_large_data() -> None:
    line = format_event(Event("plan", "u-1", {"blob": "x" * 1000}))
    assert len(line) < 220
    assert line.endswith("…")
