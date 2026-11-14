"""BonsaiPlanner: utterance + snapshot -> Plan via the local Bonsai 2 4B server (PRD §5.3).

rank items -> render suffix (<~150 tokens) -> per-call schema -> llama-server -> validate -> Plan.
Never invents ids: the grammar only allows shown ids and the reply is validated again here.
Raises PlannerError on server down / timeout / HTTP error / unusable output.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from brain.config import Settings
from brain.contracts import Plan, UIItem, UISnapshot
from brain.planner.client import BonsaiClient, ChatResult, SamplingProfile, Transport
from brain.planner.errors import PlannerError
from brain.planner.prompt import build_messages, build_suffix, estimate_tokens
from brain.planner.rank import match_apps, rank_items
from brain.planner.schema import build_schema, response_format, to_plan

DEFAULT_K = 15
_WARMUP_POLL_S = 0.5


class BonsaiPlanner:
    """Satisfies ``contracts.Planner``. ``last_stats`` describes the most recent call."""

    def __init__(
        self,
        apps: Sequence[str] = (),
        settings: Settings | None = None,
        *,
        sampling: SamplingProfile = "greedy",
        k: int = DEFAULT_K,
        transport: Transport | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.apps: tuple[str, ...] = tuple(apps)
        self.k = k
        self.client = BonsaiClient(
            self.settings.bonsai_url,
            self.settings.bonsai_model,
            self.settings.bonsai_timeout_s,
            sampling=sampling,
            transport=transport,
        )
        self.last_stats: dict[str, float] = {}
        self.last_suffix: str = ""
        self._lock = threading.Lock()  # one slot on the server

    def warmup(self, timeout_s: float = 120.0) -> float:
        """Prime the server's prompt cache with the static prefix; returns ms taken.

        Call once at brain startup (and after a server restart): the first uncached call
        processes ~1k prefix tokens, which takes seconds on CPU/iGPU and would blow the
        per-utterance budget. Polls while the server is still starting (connection refused /
        HTTP 503) until ``timeout_s``; then raises PlannerError.
        """
        deadline = time.perf_counter() + timeout_s
        messages = build_messages("APP: Finder | WINDOW: Finder\nUSER: hi")
        while True:
            client = BonsaiClient(
                self.settings.bonsai_url,
                self.settings.bonsai_model,
                max(deadline - time.perf_counter(), 1.0),
                sampling=self.client.sampling,
                transport=self.client.transport,
                max_tokens=1,
            )
            try:
                with self._locked(timeout_s):
                    return client.chat(messages).ms
            except PlannerError as e:
                if e.kind not in ("down", "http") or time.perf_counter() + _WARMUP_POLL_S >= deadline:
                    raise
            time.sleep(_WARMUP_POLL_S)

    @contextmanager
    def _locked(self, timeout_s: float) -> Iterator[None]:
        """One request at a time (single server slot); never wait past the caller's budget."""
        if not self._lock.acquire(timeout=max(timeout_s, 0.0)):
            raise PlannerError("timeout", "planner busy with another request")
        try:
            yield
        finally:
            self._lock.release()

    def plan(
        self,
        text: str,
        snapshot: UISnapshot,
        *,
        hint: str | None = None,
        error: str | None = None,
    ) -> Plan:
        ranked = rank_items(text, snapshot, self.k)
        # The frontmost app is never an open_app target: re-opening it is a no-op that stalls
        # continuations ("open notes and …" planned again once Notes is already in front).
        front = snapshot.app.name.casefold()
        apps = [a for a in match_apps(text, self.apps) if a.casefold() != front]
        free_app = not self.apps  # no installed-app list at all: allow any app name
        suffix, shown = build_suffix(text, snapshot, ranked, apps, hint=hint, error=error)
        allow_empty = hint is not None  # continuation: {"a":[]} means "nothing left to do"
        schema = build_schema(shown, apps, free_app=free_app, allow_empty=allow_empty)
        stats: dict[str, float] = {
            "items_shown": float(len(shown)),
            "suffix_tokens_est": float(estimate_tokens(suffix)),
        }
        t0 = time.perf_counter()
        try:
            with self._locked(self.settings.bonsai_timeout_s):
                result = self.client.chat(build_messages(suffix), response_format(schema))
        except PlannerError:
            stats["ms"] = (time.perf_counter() - t0) * 1000.0
            stats["failed"] = 1.0
            self.last_stats, self.last_suffix = stats, suffix
            raise
        stats.update(
            prompt_tokens=float(result.prompt_tokens),
            completion_tokens=float(result.completion_tokens),
            cached_tokens=float(result.cached_tokens),
            prompt_ms=result.prompt_ms,
            decode_ms=result.decode_ms,
            ms=result.ms,
        )
        # Assign whole dicts only: concurrent readers never see a half-filled last_stats.
        self.last_stats, self.last_suffix = stats, suffix
        return self._to_plan(result, snapshot, shown, apps, free_app, allow_empty)

    @staticmethod
    def _to_plan(
        result: ChatResult,
        snapshot: UISnapshot,
        shown: Sequence[UIItem],
        apps: Sequence[str],
        free_app: bool,
        allow_empty: bool,
    ) -> Plan:
        if result.finish_reason == "length":
            raise PlannerError("bad_output", f"reply truncated at max_tokens: {result.content[:80]!r}")
        try:
            reply = json.loads(result.content)
        except (ValueError, RecursionError) as e:
            raise PlannerError("bad_output", f"reply is not JSON: {result.content[:120]!r}") from e
        # Validate against what the model was shown (not the whole snapshot): shown ⊆ snapshot.
        focused = snapshot.item(snapshot.focused_id) if snapshot.focused_id is not None else None
        return to_plan(
            reply,
            snapshot.snapshot_id,
            shown,
            apps,
            free_app=free_app,
            allow_empty=allow_empty,
            focused=focused,
            front_app=snapshot.app.name,
        )
