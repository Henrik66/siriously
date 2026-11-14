"""Safety gate (PRD 5.6): decide whether a plan must wait for an explicit "yes"."""

from __future__ import annotations

import re

from brain.contracts import Plan, UIItem, UISnapshot

DANGER_RE: re.Pattern[str] = re.compile(
    r"\b(?:send|delete|remove|quit|erase|empty\s+trash|sign\s*out|log\s*out|move\s+to\s+trash|discard)\b",
    re.IGNORECASE,
)

_KEY_ALIASES = {
    "command": "cmd",
    "meta": "cmd",
    "opt": "option",
    "alt": "option",
    "escape": "esc",
    "ctrl": "control",
}

# A combo is dangerous if it *contains* any of these (so cmd+shift+q, cmd+opt+shift+delete match).
_DANGEROUS_COMBOS: tuple[frozenset[str], ...] = (
    frozenset({"cmd", "q"}),  # quit (and cmd+shift+q: log out)
    frozenset({"cmd", "delete"}),
    frozenset({"cmd", "backspace"}),
    frozenset({"cmd", "option", "esc"}),  # force quit
)


def normalize_keys(keys: str) -> frozenset[str]:
    """Order/alias-insensitive key set: "Command+Q" -> {"cmd", "q"}."""
    parts = (p.strip().lower() for p in keys.replace(" ", "").split("+"))
    return frozenset(_KEY_ALIASES.get(p, p) for p in parts if p)


def item_is_dangerous(item: UIItem) -> bool:
    """DANGER_RE on the item's own (leaf) name, never on its top-level menu ("Format")."""
    return any(t and DANGER_RE.search(t) for t in (item.name, item.label))


def _keys_reason(keys: str, snapshot: UISnapshot) -> str | None:
    combo = normalize_keys(keys)
    if any(d <= combo for d in _DANGEROUS_COMBOS):
        return f"shortcut {keys} looks destructive"
    for item in snapshot.menus:  # whatever menu item owns this shortcut in the current app
        if item.shortcut and normalize_keys(item.shortcut) == combo and item_is_dangerous(item):
            return f"shortcut {keys} runs '{item.display}' which looks destructive"
    return None


def confirm_reason(plan: Plan, snapshot: UISnapshot) -> str | None:
    """Return why ``plan`` needs confirmation, or None if it can run immediately."""
    if plan.confirm:
        return plan.say or "plan requires confirmation"
    for action in plan.actions:
        if action.op in ("press", "menu") and action.target is not None:
            item = snapshot.item(action.target)
            if item is not None and item_is_dangerous(item):
                return f"{action.op} '{item.display}' looks destructive"
        elif action.op == "keys" and action.keys and (reason := _keys_reason(action.keys, snapshot)):
            return reason
    return None


def needs_confirm(plan: Plan, snapshot: UISnapshot) -> bool:
    """True if the plan is flagged, targets a dangerous item, or uses a dangerous shortcut."""
    return confirm_reason(plan, snapshot) is not None
