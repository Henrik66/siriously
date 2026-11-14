"""Runtime settings, read from environment variables (never hardcode machine paths).

Defaults are resolved when ``Settings()`` is constructed, not at import time, so tests and
launchers can set ``SIRIOUSLY_*`` variables after importing this module.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _str(name: str, default: str) -> Callable[[], str]:
    return lambda: os.environ.get(name, default)


def _float(name: str, default: float) -> Callable[[], float]:
    return lambda: _env_float(name, default)


def _int(name: str, default: int) -> Callable[[], int]:
    return lambda: int(_env_float(name, default))


def _flows_path() -> Path:
    raw = os.environ.get("SIRIOUSLY_FLOWS")
    return Path(raw).expanduser() if raw else Path.home() / ".siriously" / "flows.json"


@dataclass(frozen=True)
class Settings:
    bonsai_url: str = field(default_factory=_str("SIRIOUSLY_BONSAI_URL", "http://127.0.0.1:8080/v1"))
    bonsai_model: str = field(default_factory=_str("SIRIOUSLY_BONSAI_MODEL", "bonsai"))
    bonsai_timeout_s: float = field(default_factory=_float("SIRIOUSLY_BONSAI_TIMEOUT_S", 6.0))
    # "greedy" (temperature 0) or "card" (model-card thinking-off profile); A/B with the eval harness
    bonsai_sampling: str = field(default_factory=_str("SIRIOUSLY_BONSAI_SAMPLING", "greedy"))
    host: str = field(default_factory=_str("SIRIOUSLY_HOST", "127.0.0.1"))
    port: int = field(default_factory=_int("SIRIOUSLY_PORT", 8765))
    flows_path: Path = field(default_factory=_flows_path)
    utterance_timeout_s: float = field(default_factory=_float("SIRIOUSLY_UTTERANCE_TIMEOUT_S", 8.0))
    max_replans: int = field(default_factory=_int("SIRIOUSLY_MAX_REPLANS", 2))
    # minimum budget that must remain to start another planner call (re-plan / continuation);
    # the real guard is max(this, 1.25 x measured planner time)
    min_plan_budget_s: float = field(default_factory=_float("SIRIOUSLY_MIN_PLAN_BUDGET_S", 2.5))
    # a pending "are you sure?" expires after this long; a late "yes" is ignored
    confirm_ttl_s: float = field(default_factory=_float("SIRIOUSLY_CONFIRM_TTL_S", 20.0))


def load_settings() -> Settings:
    return Settings()
