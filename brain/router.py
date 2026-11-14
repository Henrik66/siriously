"""Router (PRD 5.1): control -> filler drop -> teach -> taught flow -> fast path -> planner.

Empty text and well-known Whisper hallucinations on silence ("Thank you.", "you", "[BLANK_AUDIO]",
"Thanks for watching!") are dropped as ``Route(kind="none")`` before the fast path or planner can act.
"""

from __future__ import annotations

import re
import time
from dataclasses import replace
from typing import Protocol

from brain.contracts import Planner, Route, UISnapshot
from brain.control import fold, normalize, parse_control
from brain.fastpath import FastPath
from brain.flows import parse_teach

FILLER_REASON = "empty or filler utterance"
# Whole-utterance only, on fold()ed text (lowercase, no punctuation).
_HALLUCINATION = re.compile(
    r"(?:thank you|thanks)(?: (?:very much|so much|a lot|for watching|for listening))?"
    r"|you(?: you)*|bye(?: bye)*|blank audio|(?:subtitles|captions|transcription) by .*"
)
_BRACKETED = re.compile(r"\s*(?:[\[(][^\])]*[\])]\s*)+")  # "[BLANK_AUDIO]", "(silence)", "[Music]"


def is_filler(text: str) -> bool:
    """True for empty text or a known speech-to-text hallucination (whole utterance)."""
    if not normalize(text) or _BRACKETED.fullmatch(text):
        return True
    return _HALLUCINATION.fullmatch(fold(text)) is not None


class FlowMatcher(Protocol):
    """The slice of ``brain.flows.FlowStore`` the router needs."""

    def names(self) -> list[str]: ...

    def match(self, text: str) -> str | None: ...


def _parse_teach(text: str) -> str | None:
    return parse_teach(text)


class Router:
    def __init__(self, fastpath: FastPath, flows: FlowMatcher, planner: Planner | None) -> None:
        self.fastpath = fastpath
        self.flows = flows
        self.planner = planner

    def route(self, text: str, snapshot: UISnapshot) -> Route:
        """Classify one utterance. Safety (confirm) is applied later by the pipeline."""
        t0 = time.perf_counter()
        route = self._decide(text, snapshot)
        return _timed(route, t0)

    def _decide(self, text: str, snapshot: UISnapshot) -> Route:
        word = parse_control(text)
        if word is not None:
            return Route(kind="control", control=word)
        if is_filler(text):
            return Route(kind="none", reason=FILLER_REASON)
        taught = _parse_teach(text)
        if taught:
            return Route(kind="flow", teach=True, flow=taught)
        flow = self.flows.match(text)
        if flow:
            return Route(kind="flow", flow=flow)
        plan = self.fastpath.match(text, snapshot)
        if plan is not None:
            return Route(kind="fastpath", plan=plan)
        return self._plan(text, snapshot)

    def _plan(self, text: str, snapshot: UISnapshot) -> Route:
        if self.planner is None:
            return Route(kind="none", reason="no planner")
        try:
            plan = self.planner.plan(text, snapshot)
        except Exception as exc:  # planner failures must never crash the pipeline
            return Route(kind="none", reason=f"planner error: {exc}")
        if plan.mode == "dictation":
            return Route(kind="dictation", plan=plan)
        return Route(kind="planner", plan=plan)


def _timed(route: Route, t0: float) -> Route:
    return replace(route, ms=(time.perf_counter() - t0) * 1000.0)
