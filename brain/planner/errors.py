"""Planner failure type (PRD §5.3). The pipeline treats any PlannerError as "planner unavailable"."""

from __future__ import annotations

from typing import Literal

PlannerErrorKind = Literal["down", "timeout", "http", "bad_output"]


class PlannerError(RuntimeError):
    """Bonsai could not produce a usable plan.

    kind:
      down        server not reachable (connection refused/reset, DNS)
      timeout     no reply within Settings.bonsai_timeout_s
      http        server answered with an HTTP error (e.g. 503 while loading)
      bad_output  reply was not valid JSON, or failed plan validation (unknown id, bad op, ...)
    """

    def __init__(self, kind: PlannerErrorKind, message: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind: PlannerErrorKind = kind
