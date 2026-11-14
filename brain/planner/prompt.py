"""Prompt = byte-identical static prefix + small dynamic suffix (PRD §5.3).

The static part (system rules + few-shot turns) never changes between calls, so llama-server's
prompt cache (cache_prompt + id_slot 0) only has to process the dynamic suffix:

    APP: Notes | WINDOW: Notes
    APPS: Notes, Slack                      (only when an installed app matches the utterance)
    1 button "New Note"
    7 textarea "Note body" (focused)
    101 menu "File › New Note" (cmd+n)
    HINT: ...                               (optional, flow replay fallback)
    ERROR: ...                              (optional, re-plan after a failed action)
    USER: open notes and start a note called standup
"""

from __future__ import annotations

from collections.abc import Sequence

from brain.contracts import UIItem, UISnapshot
from brain.planner.rank import truncate

SUFFIX_TOKEN_BUDGET = 150
HINT_MAX = 200  # eval/pipeline hints end with the instruction ("Plan only the remaining steps…")
_MIN_ITEMS = 5
_CHARS_PER_TOKEN = 3.2  # conservative for short English labels + ids (Qwen-style BPE)

SYSTEM = """You turn a voice command into actions in the frontmost Mac app.
Input: APP and WINDOW, maybe APPS (installed apps you may open), numbered on-screen items, maybe HINT \
(how it was done before) or ERROR (what failed last time), then USER (the command).
Item lines: <id> <role> "<label>" for elements, <id> menu "Menu › Item" (shortcut) for menu-bar items.
Reply with ONE JSON object and nothing else:
{"a":[action,...]}  1 to 6 actions, run in order
{"ask":[id,id]}  two shown items fit the command equally well; the user will pick
{"mode":"dictation","text":"..."}  the user is dictating text to type, not giving a command
Actions:
{"menu":id}  choose a menu item. Best choice when one fits.
{"press":id}  press an on-screen element
{"type":"text"}  type into the focused field; {"type":"text","in":id} types into that text field
{"keys":"cmd+n"}  key combo, e.g. return, tab, esc, cmd+f
{"open":"Notes"}  open or switch to an app from APPS
{"scroll":"down","n":5}
{"wait":500}
Rules: use only ids that are listed. Prefer menu items and shortcuts over pressing. Copy text to type \
from the user's words. If USER names an app from APPS that is not the current APP, start with \
open_app. If USER names a place that is listed (a channel, folder, contact, tab), go there first. \
To turn a setting on or off, press its switch or checkbox. If HINT says steps are already done, plan \
only what is left. If ERROR is given, do it a different way. Never explain."""

# (dynamic suffix, model reply) pairs. Keep them short: they are cached, but they still cost
# prompt-processing time on the first call after a server restart.
FEW_SHOT: tuple[tuple[str, str], ...] = (
    (
        'APP: Notes | WINDOW: Notes\n1 button "New Note"\n7 textarea "Note body"\n'
        '101 menu "File › New Note" (cmd+n)\n102 menu "File › Close" (cmd+w)\n'
        "USER: start a note called groceries",
        '{"a":[{"menu":101},{"type":"groceries"},{"keys":"return"}]}',
    ),
    (
        'APP: Finder | WINDOW: Downloads\nAPPS: Notes\n3 button "Back"\n201 menu "File › New Folder" '
        "(shift+cmd+n)\nUSER: open notes and make a new note called standup",
        '{"a":[{"open":"Notes"},{"wait":500},{"keys":"cmd+n"},{"type":"standup"}]}',
    ),
    (
        'APP: Safari | WINDOW: Pricing - Acme\n2 textfield "Search or enter website name"\n'
        '301 menu "File › New Tab" (cmd+t)\n320 menu "Edit › Find › Find…" (cmd+f)\n'
        "USER: find pricing on this page",
        '{"a":[{"menu":320},{"type":"pricing"},{"keys":"return"}]}',
    ),
    (
        'APP: TextEdit | WINDOW: Untitled\n1 textarea "Document" (focused)\n'
        '401 menu "Format › Font › Underline" (cmd+u)\n402 menu "Edit › Select All" (cmd+a)\n'
        "USER: underline everything",
        '{"a":[{"menu":402},{"menu":401}]}',
    ),
    (
        'APP: Messages | WINDOW: Alex\n3 row "Alex"\n4 row "Mom"\n9 textfield "iMessage" (focused)\n'
        '120 menu "Edit › Undo" (cmd+z)\nUSER: text mom see you at six',
        '{"a":[{"press":4},{"type":"see you at six","in":9},{"keys":"return"}]}',
    ),
    (
        'APP: System Settings | WINDOW: General\n6 row "Bluetooth"\n7 row "Displays"\n8 row "Sound"\n'
        '120 menu "Edit › Undo" (cmd+z)\nUSER: turn on night shift',
        '{"a":[{"press":7}]}',
    ),
    (
        'APP: TextEdit | WINDOW: Untitled\n1 textarea "Document" (focused)\n'
        "USER: dear team comma the build is green period",
        '{"mode":"dictation","text":"Dear team, the build is green."}',
    ),
    (
        'APP: System Settings | WINDOW: Bluetooth\n6 row "Bluetooth"\n40 switch "Bluetooth"\n'
        '41 popupbutton "Show in menu bar"\nHINT: Already done: press(6). Plan only the remaining steps.\n'
        "USER: turn off bluetooth",
        '{"a":[{"press":40}]}',
    ),
)


def static_messages() -> list[dict[str, str]]:
    """System + few-shot turns. Byte-identical on every call (cache-friendly)."""
    msgs = [{"role": "system", "content": SYSTEM}]
    for user, reply in FEW_SHOT:
        msgs.append({"role": "user", "content": user})
        msgs.append({"role": "assistant", "content": reply})
    return msgs


def _clean(s: str) -> str:
    return " ".join(s.replace('"', "'").split())


def item_line(item: UIItem, focused_id: int | None) -> str:
    if item.kind == "menu":
        line = f'{item.id} menu "{truncate(_clean(item.display))}"'
        return f"{line} ({item.shortcut})" if item.shortcut else line
    role = _clean(item.role) or "element"
    line = f'{item.id} {role} "{truncate(_clean(item.label))}"'
    return f"{line} (focused)" if item.id == focused_id else line


def estimate_tokens(text: str) -> int:
    return int(len(text) / _CHARS_PER_TOKEN) + 1


def _render(
    text: str,
    snapshot: UISnapshot,
    items: Sequence[UIItem],
    apps: Sequence[str],
    hint: str | None,
    error: str | None,
) -> str:
    lines = [f"APP: {_clean(snapshot.app.name)} | WINDOW: {truncate(_clean(snapshot.window))}"]
    if apps:
        lines.append("APPS: " + ", ".join(_clean(a) for a in apps))
    lines.extend(item_line(it, snapshot.focused_id) for it in items)
    if hint:
        lines.append(f"HINT: {truncate(_clean(hint), HINT_MAX)}")
    if error:
        lines.append(f"ERROR: {truncate(_clean(error), HINT_MAX)}")
    lines.append(f"USER: {_clean(text)}")
    return "\n".join(lines)


def build_suffix(
    text: str,
    snapshot: UISnapshot,
    ranked: Sequence[UIItem],
    apps: Sequence[str] = (),
    *,
    hint: str | None = None,
    error: str | None = None,
    budget: int = SUFFIX_TOKEN_BUDGET,
) -> tuple[str, list[UIItem]]:
    """Render the dynamic suffix; drop lowest-ranked items until it fits ``budget`` tokens.

    ``ranked`` is best-first (from rank_items). Items are displayed elements first, then menus,
    each group best-first: a 4B model leans toward the first plausible line, so the most relevant
    candidates go on top. Returns (suffix, items actually shown).
    """
    keep = list(ranked)
    while True:
        shown = sorted(keep, key=lambda it: it.kind == "menu")  # stable: keeps rank order per group
        suffix = _render(text, snapshot, shown, apps, hint, error)
        if estimate_tokens(suffix) <= budget or len(keep) <= _MIN_ITEMS:
            return suffix, shown
        keep.pop()


def build_messages(suffix: str) -> list[dict[str, str]]:
    return [*static_messages(), {"role": "user", "content": suffix}]
