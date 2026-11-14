# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Planner (PRD §5.3): Bonsai 2 4B turns an utterance + UI snapshot into a short action plan.

    python3 mac/planner.py "make a new folder called receipts" eval/fixtures/finder.json   # dry run on a fixture
    from planner import plan                                                               # used by mini_brain

Design (all from the PRD):
- Static prefix (rules + few-shot turns) is byte-identical on every call, so llama-server's prompt cache skips it.
- Dynamic suffix lists only the ~15 elements and ~10 menu items that best match the utterance.
- Output is schema-constrained per call: press/menu targets are enums of the ids actually shown, op is an enum,
  at most 6 actions. Thinking off, temperature 0. Stdlib only (talks to llama-server on :8080).
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
import time
import urllib.request

BONSAI_URL = os.environ.get("SIRIOUSLY_LLM_URL", "http://127.0.0.1:8080/v1/chat/completions")
MAX_ELEMENTS = 15
MAX_MENUS = 10
LABEL_MAX = 40

SYSTEM = """You operate a Mac for the user. Given what is on screen and the user's request, reply with a JSON plan.
Ops:
- open_app {app}: launch or switch to an app by name
- press {target}: click the on-screen element with that id
- menu {target}: choose the menu item with that id
- type {text, target?}: type text into the field with that id, or into the focused field
- keys {keys}: a keyboard shortcut such as "cmd+n", "cmd+shift+t", "return", "esc", "tab"
- scroll {direction}: "up" or "down"
Rules:
- Use only ids that are listed; the best matches are listed first. If a menu item matches the request, use it.
- Prefer a listed menu item or a well-known shortcut over clicking.
- At most 6 actions. Do only what was asked.
- If the request is about a different app than the one shown, start with open_app. You will then see that app's
  screen and can continue. After open_app the list still shows the OLD app, so use keys/type, not ids.
- Lines starting with DONE are steps already completed: do not repeat them. If nothing is left, return no actions.
- If the request cannot be done with these ops, return no actions and say why in a few words.
- If something essential is missing (who, what text, which file, how long), return no actions and put one short
  question in "ask".
- "say" is a 2 to 6 word imperative summary of what you are about to do, like "Send “hello” to Hari" or
  "New folder “receipts”". Never past tense ("Sent", "Moved"). Quote user text with “ ”."""

# Few-shot turns: (screen + request, plan). Part of the cached static prefix.
EXAMPLES = [
    ("""APP: Finder | WINDOW: Downloads
12 button "Back"
31 row "invoice.pdf"
40 menu "File › New Folder" (cmd+shift+n)
41 menu "File › Rename…"
USER: make a new folder called receipts""",
     {"say": "New folder “receipts”", "actions": [{"op": "menu", "target": 40}, {"op": "type", "text": "receipts"},
                                                  {"op": "keys", "keys": "return"}]}),
    ("""APP: Safari | WINDOW: Start Page
5 searchfield "Search or enter website name"
62 menu "File › New Tab" (cmd+t)
USER: search for weather in ithaca""",
     {"say": "Search “weather in ithaca”", "actions": [{"op": "type", "text": "weather in ithaca", "target": 5},
                                                      {"op": "keys", "keys": "return"}]}),
    ("""APP: Finder | WINDOW: Desktop
3 button "Back"
USER: open safari and go to github.com""",
     {"say": "Open github.com in Safari", "actions": [{"op": "open_app", "app": "Safari"}, {"op": "keys", "keys": "cmd+l"},
                                                     {"op": "type", "text": "github.com"}, {"op": "keys", "keys": "return"}]}),
    ("""APP: Terminal | WINDOW: zsh
2 button "Close window"
USER: open the bluetooth settings""",
     {"say": "Open Bluetooth settings", "actions": [{"op": "open_app", "app": "System Settings"}]}),
    ("""APP: System Settings | WINDOW: General
4 searchfield "Search"
68 menu "View › Bluetooth"
9 row "General"
DONE: open_app System Settings
USER: open the bluetooth settings""",
     {"say": "Open Bluetooth settings", "actions": [{"op": "menu", "target": 68}]}),
    ("""APP: Messages | WINDOW: Messages
3 searchfield "Search"
10 textfield "iMessage" = ""
44 menu "File › New Message" (cmd+n)
USER: text hari hello""",
     {"say": "Send “hello” to Hari", "actions": [{"op": "menu", "target": 44}, {"op": "type", "text": "Hari"},
                                                 {"op": "keys", "keys": "return"}, {"op": "type", "text": "hello"},
                                                 {"op": "keys", "keys": "return"}]}),
    ("""APP: Slack | WINDOW: general (Channel) - Hackathon
8 button "Huddle"
14 textarea "Message #general" = ""
22 row "random"
USER: tell the channel I'm running five minutes late""",
     {"say": "Post “I'm running five minutes late”", "actions": [{"op": "type", "text": "I'm running five minutes late", "target": 14},
                                             {"op": "keys", "keys": "return"}]}),
    ("""APP: System Settings | WINDOW: Wi‑Fi
4 searchfield "Search"
9 row "Bluetooth"
10 row "Network"
11 switch "Wi‑Fi"
USER: go to the bluetooth settings""",
     {"say": "Bluetooth settings", "actions": [{"op": "press", "target": 9}]}),
    ("""APP: System Settings | WINDOW: Privacy & Security
88 menu "View › Displays"
9 row "Location Services"
12 row "Full Disk Access"
USER: open display settings""",
     {"say": "Display settings", "actions": [{"op": "menu", "target": 88}]}),
    ("""APP: Notes | WINDOW: Notes
7 textarea "Note body" = "Groceries" (focused)
101 menu "Format › Checklist" (cmd+shift+l)
USER: turn this into a checklist and add milk""",
     {"say": "Checklist with milk", "actions": [{"op": "keys", "keys": "cmd+down"}, {"op": "keys", "keys": "return"},
                                               {"op": "menu", "target": 101}, {"op": "type", "text": "milk"}]}),
    ("""APP: Mail | WINDOW: Inbox
12 button "Compose"
31 menu "Message › Forward" (cmd+shift+f)
USER: forward this to him""",
     {"say": "", "actions": [], "ask": "Who should I forward it to?"}),
    ("""APP: Calendar | WINDOW: Calendar
3 button "Today"
USER: what's the capital of peru""",
     {"say": "I only control the Mac", "actions": []}),
]

STOP = {"the", "a", "an", "to", "and", "in", "on", "of", "for", "my", "me", "please", "this", "that", "it", "called",
        "named", "with", "into", "from", "is", "can", "you", "i", "go", "do", "some", "up", "down"}
WINDOW_CHROME = {"Close window", "Minimize window", "Zoom window", "Full screen"}


def _tokens(s: str) -> set[str]:
    s = (s or "").lower()
    joined = re.sub(r"[-\u2010-\u2015]", "", s)            # "Wi‑Fi" (U+2011) also yields "wifi"
    return {t for t in re.findall(r"[a-z0-9]+", s) + re.findall(r"[a-z0-9]+", joined) if t not in STOP}


def _score(label: str, utt_tokens: set[str], utt: str) -> float:
    lt = _tokens(label)
    exact = len(lt & utt_tokens)
    prefix = sum(1 for a in lt for b in utt_tokens if a != b and len(a) >= 4 and len(b) >= 4 and (a.startswith(b) or b.startswith(a)))
    return exact * 2 + prefix + difflib.SequenceMatcher(None, (label or "").lower(), utt.lower()).ratio()


def _short(s: str, n: int = LABEL_MAX) -> str:
    s = re.sub(r"[\u2010-\u2012]", "-", " ".join((s or "").split()))   # "Wi‑Fi" (U+2011) -> "Wi-Fi"
    return s if len(s) <= n else s[: n - 1] + "…"


def context(utterance: str, snap: dict, done: list[str] | None = None, error: str | None = None):
    """Dynamic suffix + the element/menu ids it shows."""
    ut = _tokens(utterance) - _tokens(snap["app"]["name"])  # "settings" in System Settings matches everything
    els = [i for i in snap["items"] if i["kind"] == "element" and i.get("enabled", True)
           and (i["label"] or i.get("value") is not None)]
    menus = [i for i in snap["items"] if i["kind"] == "menu" and i.get("enabled", True)]

    def el_score(i):
        s = _score(i["label"], ut, utterance)
        if i["label"] in WINDOW_CHROME:
            s -= 1.5
        if i["id"] == snap.get("focused_id"):
            s += 1.5
        if i["role"] in ("textfield", "textarea", "searchfield", "combobox"):
            s += 0.8
        return s

    chosen = sorted(els, key=el_score, reverse=True)[:MAX_ELEMENTS]
    menu_pick = sorted(menus, key=lambda i: _score(i["label"], ut, utterance), reverse=True)[:MAX_MENUS]
    scored = [(el_score(i), i) for i in chosen] + [(_score(i["label"], ut, utterance), i) for i in menu_pick]
    scored.sort(key=lambda x: -x[0])                         # best matches first: small models read top-down

    lines = [f"APP: {snap['app']['name']} | WINDOW: {_short(snap.get('window') or '-', 50)}"]
    for _, i in scored:
        if i["kind"] == "menu":
            lines.append(f'{i["id"]} menu "{_short(i["label"], 60)}"' + (f' ({i["shortcut"]})' if i.get("shortcut") else ""))
            continue
        line = f'{i["id"]} {i["role"]} "{_short(i["label"])}"'
        if i.get("value") is not None:
            line += f' = "{_short(i["value"], 30)}"'
        if i["id"] == snap.get("focused_id"):
            line += " (focused)"
        lines.append(line)
    for d in done or []:
        lines.append(f"DONE: {d}")
    if error:
        lines.append(f"FAILED: {_short(error, 120)}")
    lines.append(f"USER: {utterance}")
    inputs = [i["id"] for i in chosen if i["role"] in ("textfield", "textarea", "searchfield", "combobox")]
    return "\n".join(lines), [i["id"] for i in chosen], [i["id"] for i in menu_pick], inputs


def schema(element_ids: list[int], menu_ids: list[int], input_ids: list[int]) -> dict:
    def obj(props, required):
        return {"type": "object", "properties": props, "required": required, "additionalProperties": False}
    variants = [
        obj({"op": {"const": "open_app"}, "app": {"type": "string", "maxLength": 40}}, ["op", "app"]),
        obj({"op": {"const": "keys"}, "keys": {"type": "string", "maxLength": 24}}, ["op", "keys"]),
        obj({"op": {"const": "scroll"}, "direction": {"enum": ["up", "down"]}}, ["op", "direction"]),
    ]
    type_props = {"op": {"const": "type"}, "text": {"type": "string", "maxLength": 200}}
    if input_ids:
        type_props["target"] = {"enum": input_ids}
    variants.append(obj(type_props, ["op", "text"]))
    if element_ids:
        variants.append(obj({"op": {"const": "press"}, "target": {"enum": element_ids}}, ["op", "target"]))
    if menu_ids:
        variants.append(obj({"op": {"const": "menu"}, "target": {"enum": menu_ids}}, ["op", "target"]))
    return obj({"say": {"type": "string", "maxLength": 60},
                "actions": {"type": "array", "maxItems": 6, "items": {"anyOf": variants}},
                "ask": {"type": "string", "maxLength": 80}}, ["say", "actions"])


def messages(dynamic: str) -> list[dict]:
    msgs = [{"role": "system", "content": SYSTEM}]
    for screen, out in EXAMPLES:
        msgs.append({"role": "user", "content": screen})
        msgs.append({"role": "assistant", "content": json.dumps(out, ensure_ascii=False, separators=(",", ":"))})
    msgs.append({"role": "user", "content": dynamic})
    return msgs


def plan(utterance: str, snap: dict, done: list[str] | None = None, error: str | None = None,
         timeout: float = 15.0) -> dict:
    """-> {"say", "actions", "ms", "prompt_tokens", "cached_tokens", "dynamic"}  (raises on server errors)"""
    dynamic, els, menus, inputs = context(utterance, snap, done, error)
    body = {
        "messages": messages(dynamic),
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "plan", "strict": True, "schema": schema(els, menus, inputs)}},
        "temperature": 0, "max_tokens": 256, "cache_prompt": True,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(BONSAI_URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.load(r)
    ms = round((time.perf_counter() - t0) * 1000)
    out = json.loads(resp["choices"][0]["message"]["content"])
    usage = resp.get("usage", {})
    timings = resp.get("timings", {})
    return {"say": out.get("say", ""), "actions": out.get("actions", []), "ask": out.get("ask"), "ms": ms,
            "prompt_tokens": usage.get("prompt_tokens"), "cached_tokens": timings.get("cache_n"),
            "new_prompt_tokens": timings.get("prompt_n"), "dynamic": dynamic}


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(2)
    snap = json.load(open(sys.argv[2]))
    p = plan(sys.argv[1], snap)
    print(p["dynamic"], "\n")
    print(json.dumps({"say": p["say"], "actions": p["actions"]}, ensure_ascii=False))
    print(f"{p['ms']} ms  prompt {p['prompt_tokens']} tokens ({p['cached_tokens']} cached, {p['new_prompt_tokens']} new)")
