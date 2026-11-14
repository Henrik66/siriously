"""Mac UIProvider / Executor for the brain (docs/MAC_INTEGRATION.md §2).

Thin adapter over Henrik's Accessibility code in ``mac/`` (``ui_provider.py``, ``executor.py``), which works
in plain dicts in the PRD §4.2/§4.4 shapes. This module converts them to the brain's contract types:

    snapshot()            -> UISnapshot       (dict -> UISnapshot.from_dict)
    run(snapshot_id, a)   -> ActionResult     (Action.to_dict() in, dict -> ActionResult.from_dict out)

Ids are resolved against the snapshot the plan was made on (the provider keeps the last 16 snapshots' element
maps), so a burst can run on its base snapshot id. The executor refuses to type into terminal apps unless
SIRIOUSLY_ALLOW_TERMINAL=1.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from brain.contracts import Action, ActionResult, UIProvider, UISnapshot

_MAC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mac")
if _MAC_DIR not in sys.path:
    sys.path.insert(0, _MAC_DIR)

import executor as _executor  # noqa: E402  (mac/executor.py)
import ui_provider as _ui  # noqa: E402  (mac/ui_provider.py)


class MacUIProvider:
    """Frontmost app's actionable AX elements + menu bar, as a UISnapshot."""

    def __init__(self) -> None:
        self.raw = _ui.MacUIProvider()

    def snapshot(self) -> UISnapshot:
        return UISnapshot.from_dict(self.raw.snapshot())

    def list_apps(self) -> list[str]:
        return list(self.raw.list_apps())

    def screen_text(self, max_chars: int = 3000) -> dict[str, Any]:
        """Readable text of the front window (questions about the screen). {app, window, text, truncated}"""
        out: dict[str, Any] = self.raw.screen_text(max_chars=max_chars)
        return out


class MacExecutor:
    """Runs one Action against the snapshot it was planned on; returns the post-action snapshot."""

    def __init__(self, provider: UIProvider | None = None) -> None:
        # Must share the provider's element maps: ids are resolved against its snapshots.
        self.provider = provider if isinstance(provider, MacUIProvider) else MacUIProvider()
        self.raw = _executor.MacExecutor(self.provider.raw)

    def run(self, snapshot_id: str, action: Action) -> ActionResult:
        try:
            r: dict[str, Any] = self.raw.run(snapshot_id, action.to_dict())
        except Exception as exc:  # the brain prefers ok=False with a reason over an exception
            return ActionResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        snap = r.get("snapshot")
        return ActionResult(
            ok=bool(r.get("ok")),
            changed=bool(r.get("changed")),
            error=r.get("error"),
            snapshot=UISnapshot.from_dict(snap) if snap else None,
        )

    def undo(self) -> bool:
        return bool(self.raw.undo())
