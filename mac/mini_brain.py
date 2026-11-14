# /// script
# requires-python = ">=3.11"
# dependencies = ["pyobjc-framework-ApplicationServices", "pyobjc-framework-Cocoa", "pyobjc-framework-Quartz"]
# ///
"""Mini brain: Mac-side stand-in for the real brain (PRD §5), to test voice -> action end to end.
The real brain is the teammate's; this one is deliberately small: router + fast path + Bonsai planner.

    STT=none ./start_all.sh                      # Bonsai 2 4B on :8080 (for the planner)
    uv run --script mac/mini_brain.py            # 127.0.0.1:8765, then: mac/ear/.build/release/siriously-ear run

Settings: ~/.siriously/settings.json (shared with the menu bar app and the speech daemon; reloaded on change)
  listening, wake_word ("siriously"), wake_word_required, follow_up_seconds, permissions{category: allow|ask|block}

Routing, in order (PRD §5.1):
  0. control words ("stop", "cancel", "never mind", "end action", "no") are handled the moment they arrive:
     they halt the running plan between steps, drop pending questions/confirmations and clear the queue
  1. addressed? Only "Siriously, …" (Whisper often writes "seriously"), or speech that starts within
     follow_up_seconds of the last command *finishing* (the overlay drains a line for that long), or answering a
     question. Unaddressed speech is ignored silently (no overlay).
  2. pending confirmation ("yes" runs it) or follow-up question (the answer resumes the task)
  3. settings by voice: "always allow sending messages", "ask before deleting", "never allow purchases",
     "turn off the wake word"
  4. texting skill: "text <name> <message>" (recipient verified from the Messages window title)
  5. fast path (instant): open/switch to <app> · scroll · undo · tabs · close window · copy/paste ·
     new note called X · delete that line/word · type X · click <label> · menu items by name
  5a. new folder [in downloads] called X: the name is checked on disk first; if taken, offers "X 2" instead
  6. planner (mac/planner.py, Bonsai 2 4B): anything else; up to 3 rounds; at most one follow-up question
Risky actions follow the permission for their category: allow (just do it), ask (say yes/no), block.
Events for the overlay go to UDP :8766 in the PRD §4.6 shape.
"""
import copy
import difflib
import json
import os
import queue
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import planner  # noqa: E402
from executor import MacExecutor, match_app  # noqa: E402
from ui_provider import MacUIProvider  # noqa: E402

PORT = 8765
OVERLAY = ("127.0.0.1", 8766)
MAX_ROUNDS = 3
CONFIRM_TIMEOUT = 15
CONFIRM_SLACK = 0.5      # a short "yes"/"stop" may score this much below the voice-lock threshold
_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

# ------------------------------------------------------------------------------------------------ settings
SETTINGS_PATH = os.path.expanduser("~/.siriously/settings.json")
DEFAULT_SETTINGS = {
    "listening": True,
    "wake_word": "siriously",
    "wake_word_required": True,
    "follow_up_seconds": 10,
    "permissions": {"send_messages": "ask", "delete": "ask", "purchases": "block", "system": "ask",
                    "quit_apps": "allow", "trash": "allow"},
}
CATEGORY_LABELS = {"send_messages": "Sending messages", "delete": "Deleting", "purchases": "Purchases",
                   "system": "Shut down, restart, log out", "quit_apps": "Quitting apps", "trash": "Moving to Trash"}
_settings_cache = {"mtime": None, "value": None}


def load_settings():
    try:
        mtime = os.path.getmtime(SETTINGS_PATH)
    except OSError:
        mtime = None
    if _settings_cache["value"] is not None and mtime == _settings_cache["mtime"]:
        return _settings_cache["value"]
    s = copy.deepcopy(DEFAULT_SETTINGS)
    if mtime is not None:
        try:
            user = json.load(open(SETTINGS_PATH))
            s.update({k: v for k, v in user.items() if k != "permissions"})
            s["permissions"].update(user.get("permissions", {}))
        except (OSError, ValueError):
            pass
    else:
        save_settings(s)
    _settings_cache.update(mtime=mtime, value=s)
    return s


def save_settings(s):
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, SETTINGS_PATH)
    _settings_cache.update(mtime=None, value=None)


# ------------------------------------------------------------------------------------------------ text utils
KEYS = {"new tab": "cmd+t", "close tab": "cmd+w", "close window": "cmd+w", "close this window": "cmd+w",
        "next tab": "ctrl+tab", "previous tab": "ctrl+shift+tab", "copy": "cmd+c", "copy that": "cmd+c",
        "paste": "cmd+v", "paste that": "cmd+v", "select all": "cmd+a", "save": "cmd+s", "zoom in": "cmd+=",
        "zoom out": "cmd+-", "go back": "cmd+[", "go forward": "cmd+]", "new window": "cmd+n",
        "press enter": "return", "press return": "return", "enter": "return", "escape": "esc",
        "find": "cmd+f", "search": "cmd+f"}
FILLER = re.compile(r"^(hey |ok |okay )?(please |can you |could you )?")
FILLER_ONLY = re.compile(r"^(um+|uh+|hmm+|mm+|ah+|oh+|huh|okay so|so)$")
YES_WORDS = {"yes", "yeah", "yep", "yup", "sure", "confirm", "confirmed", "ok", "okay", "absolutely", "correct",
             "affirmative"}
NO_WORDS = {"no", "nope", "cancel", "stop", "don't", "dont", "never", "nah", "wait", "neither", "nothing"}
STOP_RX = re.compile(r"^(?:(?:stop|cancel|never ?mind|forget it|forget about it|end action|abort|halt|neither|nothing|"
                     r"no+|nope|nah)(?: it| that| this| please| now| the action)*\s*)+$")
ORDINALS = {"one": 0, "first": 0, "1": 0, "two": 1, "second": 1, "2": 1, "to": 1, "too": 1, "three": 2, "third": 2,
            "3": 2, "four": 3, "fourth": 3, "4": 3}
WAKE_VARIANTS = {"siriously": r"(?:siriously|seriously|siri ?ously|sirius ?ly|sirious|cereusly|serious ?ly|seriosly)"}
PAST = {"sent": "Send", "moved": "Move", "deleted": "Delete", "created": "Create", "opened": "Open", "closed": "Close",
        "emptied": "Empty", "posted": "Post", "replied": "Reply", "forwarded": "Forward", "searched": "Search",
        "sending": "Send", "deleting": "Delete", "moving": "Move", "creating": "Create", "searching": "Search",
        "opening": "Open", "posting": "Post", "replying": "Reply"}
FINDER_PLACES = {"downloads": "cmd+option+l", "desktop": "cmd+shift+d", "documents": "cmd+shift+o",
                 "home": "cmd+shift+h", "applications": "cmd+shift+a"}
PLACE_DIRS = {"downloads": "~/Downloads", "desktop": "~/Desktop", "documents": "~/Documents", "home": "~",
              "applications": "/Applications"}
_PLACE = r"(?:\s+(?:in|on|inside|into|to|at|and)\s+(?:the |my )?(downloads?|desktop|documents?|home|applications)(?: folder)?)?"
FOLDER_RX = re.compile(r"^(?:(?:make|create|add)\s+)?(?:an? )?(new )?folder" + _PLACE +
                       r"(?:\s+(?:called|named|titled|call)\s+(.+?))?" + _PLACE + "$")
NAME_RX = re.compile(r"\b(?:called|named|titled|call)\s+(.+?)" + _PLACE.replace("(downloads?", "(?:downloads?")
                     + r"[\s.!?,]*$", re.I)


def norm(text):
    t = text.lower().strip()
    t = re.sub(r"[^\w\s']", " ", t)
    t = " ".join(t.split())
    t = FILLER.sub("", t)
    return re.sub(r"( please| for me| now)$", "", t).strip()


def wake_rx(settings):
    w = (settings.get("wake_word") or "siriously").lower().strip()
    core = WAKE_VARIANTS.get(w, r"\s+".join(map(re.escape, w.split())))
    return re.compile(r"^\W*(?:(?:hey|hi|ok|okay|yo)\W+)?" + core + r"\b\W*", re.I)


def is_yes(t):
    w = t.split()
    if not w or w[0] in NO_WORDS:
        return False
    return w[0] in YES_WORDS or t.startswith(("do it", "go ahead", "send it")) or \
        (len(w) <= 8 and any(x in YES_WORDS for x in w) and not any(x in NO_WORDS for x in w))


# Without the wake word (follow-up window), only things that sound like a command count. "Oh, it worked that
# time" or "notify anyway?" is you talking about the screen, not to it.
COMMAND_VERBS = set("""open close quit launch start switch go show hide type write dictate text message send reply
scroll undo redo delete remove clear click press tap select copy paste cut save new make create add rename move
find search play pause stop skip mute unmute turn set change zoom minimize maximize mark pin unpin archive forward
empty duplicate sort bookmark refresh reload print check uncheck toggle increase decrease raise lower put bring
navigate use enter accept decline approve compose attach upload download install sign log lock restart shut sleep
call facetime email reopen edit format bold italic underline highlight insert view expand collapse focus""".split())
LEAD_INS = {"and", "also", "then", "now", "next", "ok", "okay", "so", "please", "can", "could", "would", "will",
            "you", "just", "let's", "lets"}


def sounds_like_command(raw, t):
    r = raw.lower().strip()
    if not t or r.endswith("?") and not re.match(r"^\W*(?:(?:hey|ok|okay)\W+)?(?:can|could|would|will) you\b", r):
        return False
    words = t.split()
    i = 0
    while i < len(words) - 1 and words[i] in LEAD_INS:
        i += 1
    return words[i] in COMMAND_VERBS or bool(KEYS.get(t))


def tidy_say(say):
    """Imperative, curly quotes: "Sent 'hello'" -> "Send “hello”"."""
    say = re.sub(r"['\"]([^'\"]+)['\"]", r"“\1”", (say or "").strip())
    w = say.split(" ", 1)
    if w and w[0].lower() in PAST:
        say = PAST[w[0].lower()] + (" " + w[1] if len(w) > 1 else "")
    return say


def best(query, labels, cutoff):
    m = difflib.get_close_matches(query, labels, n=1, cutoff=cutoff)
    return m[0] if m else None


def pick_option(t, options):
    """Index of the option the user chose ("the first one", "two", "hariprasad"), else None."""
    for w in t.split():
        if w in ORDINALS and ORDINALS[w] < len(options) and len(t.split()) <= 4:
            return ORDINALS[w]
    scores = [difflib.SequenceMatcher(None, t, o.lower()).ratio() + (1 if any(o.lower().startswith(x) or
              any(p.startswith(x) for p in o.lower().split()) for x in t.split() if len(x) >= 3) else 0)
              for o in options]
    best_i = max(range(len(options)), key=lambda i: scores[i]) if options else None
    return best_i if best_i is not None and scores[best_i] >= 0.9 else None


# ------------------------------------------------------------------------------------------------ permissions
CMD_RISK = [("purchases", r"\b(buy|purchase|pay|order|checkout)\b"),
            ("system", r"\b(shut ?down|restart|reboot|log ?out|sign out)\b"),
            ("delete", r"\b(delete|erase|empty)\b"),
            ("send_messages", r"\b(send|post|reply|forward)\b"),
            ("quit_apps", r"\bquit\b"),
            ("trash", r"\btrash\b")]
KEY_RISK = {"cmd+shift+delete": "delete", "cmd+option+shift+delete": "delete", "cmd+option+delete": "delete",
            "cmd+shift+q": "system", "cmd+option+shift+q": "system", "cmd+q": "quit_apps",
            "cmd+delete": "trash", "cmd+backspace": "trash"}
SETTING_WORDS = [("purchases", r"\b(buy|buying|purchases?|pay|paying|payments?|orders?|checkout)\b"),
                 ("system", r"\b(shut ?down|shutting down|restart|restarting|reboot|log ?out|logging out)\b"),
                 ("delete", r"\b(delet\w*|eras\w*|empty\w*)\b"),
                 ("send_messages", r"\b(send\w*|messag\w*|text\w*|post\w*|repl\w*|forward\w*|emails?)\b"),
                 ("quit_apps", r"\b(quit\w*)\b"),
                 ("trash", r"\btrash\b")]
CHAT_APPS = {"Slack", "Messages", "Mail", "Discord", "WhatsApp", "Microsoft Teams", "Telegram", "Signal", "Outlook"}
STRICTNESS = {"allow": 0, "ask": 1, "block": 2}


def risk_categories(text, snap, actions):
    cats = set()
    t = text.lower()
    for cat, rx in CMD_RISK:
        if re.search(rx, t):
            cats.add(cat)
    labels = {i["id"]: i["label"] for i in snap["items"]}
    apps = {snap["app"]["name"]} | {a.get("app", "") for a in actions if a["op"] == "open_app"}
    for n, a in enumerate(actions):
        if a["op"] in ("press", "menu"):
            lab = labels.get(a.get("target"), "").lower()
            cats |= {cat for cat, rx in CMD_RISK if re.search(rx, lab)}
        if a["op"] == "keys":
            k = a["keys"].lower()
            if k in KEY_RISK:
                cats.add(KEY_RISK[k])
            if k == "return" and apps & CHAT_APPS and any(b["op"] == "type" for b in actions[:n]):
                cats.add("send_messages")
    if "delete" in cats and "trash" in cats and "empty" not in t:
        cats.discard("delete")                       # "delete the file" in Finder = Move to Trash
    return cats


def policy(cats, settings):
    if not cats:
        return "allow", None
    perms = settings["permissions"]
    cat = max(cats, key=lambda c: STRICTNESS.get(perms.get(c, "ask"), 1))
    return perms.get(cat, "ask"), cat


SET_ALLOW = re.compile(r"^(?:always allow|allow|stop asking(?: me)?(?: about| before| for| when)?|don'?t ask(?: me)?"
                       r"(?: about| before| for| when)?|no need to ask(?: about| before| for)?)\s+(.+)$")
SET_ASK = re.compile(r"^(?:always ask(?: me)?(?: about| before| for| when)?|ask(?: me)? (?:before|about|for|when)|"
                     r"confirm before)\s+(.+)$")
SET_BLOCK = re.compile(r"^(?:never allow|don'?t allow|block all|never let (?:you|me))\s+(.+)$")
WAKE_OFF = re.compile(r"^(?:turn off|disable|stop requiring|don'?t require|no more) (?:the )?wake ?word$")
WAKE_ON = re.compile(r"^(?:turn on|enable|require) (?:the )?wake ?word$")


def settings_command(t):
    """-> (kind, value) for voice settings commands, else None."""
    if WAKE_OFF.match(t):
        return "wake", False
    if WAKE_ON.match(t):
        return "wake", True
    for rx, mode in ((SET_BLOCK, "block"), (SET_ASK, "ask"), (SET_ALLOW, "allow")):
        if m := rx.match(t):
            for cat, words in SETTING_WORDS:
                if re.search(words, m.group(1)):
                    return "perm", (cat, mode)
    return None


# ------------------------------------------------------------------------------------------------ Messages
def conversations(snap):
    """Recent Messages conversations from the sidebar: [(name, item)]. Labels look like
    'Hariprasad Gridharan, I cant bro, 2:31 PM' (pinned: 'kat, CIS 142, …')."""
    out = []
    for i in snap["items"]:
        if i["kind"] == "element" and i["role"] in ("statictext", "row", "cell", "button", "group") and "," in i["label"]:
            name = i["label"].split(",")[0].strip()
            if name and not name.startswith("Your "):
                out.append((name, i))
    return out


def _nm(x):
    """Spelling-tolerant name form: 'Harry' ~ 'Hari' (collapse double letters, final y -> i)."""
    return re.sub(r"y$", "i", re.sub(r"(.)\1", r"\1", x.lower()))


def match_people(query, convos):
    q = [_nm(x) for x in query.split()]
    hits = []
    for name, item in convos:
        parts = [_nm(p) for p in name.split()]
        if all(any(p.startswith(x) or difflib.SequenceMatcher(None, x, p[:len(x) + 1]).ratio() >= 0.75 for p in parts)
               for x in q):
            hits.append((name, item))
    uniq, seen = [], set()
    for name, item in hits:
        if name not in seen:
            seen.add(name); uniq.append((name, item))
    # "Hariprasad" (pinned, first name) and "Hariprasad Gridharan" are the same person: keep the full name
    full = [n for n, _ in uniq]
    return [(n, i) for n, i in uniq if not any(o != n and o.lower().startswith(n.lower() + " ") for o in full)]


TEXT_CMD = [re.compile(r"^(?:text|imessage)\s+(.+)$"),
            re.compile(r"^send (?:a |an )?(?:text|message|imessage) to\s+(.+)$"),
            re.compile(r"^message\s+(.+)$")]


def parse_text_command(t, front_app):
    for n, rx in enumerate(TEXT_CMD):
        if n == 2 and front_app in CHAT_APPS - {"Messages"}:
            continue                                  # "message the channel …" in Slack is not an iMessage
        if m := rx.match(t):
            return m.group(1)
    return None


# ------------------------------------------------------------------------------------------------ folders
def folder_command(t, raw):
    """'make a folder in downloads called Test' -> (name, place) or None. The name keeps its spoken casing."""
    m = FOLDER_RX.match(t)
    if not m or not (m.group(1) or re.match(r"^(make|create|add)\b", t)):
        return None
    place = m.group(2) or m.group(4)
    place = {"download": "downloads", "document": "documents"}.get(place, place)
    name = (m.group(3) or "").strip()
    if name and (r := NAME_RX.search(raw or "")):
        name = r.group(1).strip(" \"'“”‘’.,!?") or name
    return name, place


def folder_actions(name, place, snap):
    acts = [] if snap["app"]["name"] == "Finder" else [{"op": "open_app", "app": "Finder"}]
    if place:
        acts.append({"op": "keys", "keys": FINDER_PLACES[place]})
    acts.append({"op": "keys", "keys": "cmd+shift+n"})
    if name:
        acts += [{"op": "type", "text": name}, {"op": "keys", "keys": "return"}]
    return acts


def folder_dir(place, snap):
    """The folder the new folder will land in, when we can tell (Finder windows don't expose their path)."""
    if place:
        return os.path.expanduser(PLACE_DIRS[place])
    if snap["app"]["name"] == "Finder":
        title = (snap.get("window") or "").strip().lower()
        for d in PLACE_DIRS.values():
            full = os.path.expanduser(d)
            if title and title == os.path.basename(full).lower():
                return full
    return None


# ------------------------------------------------------------------------------------------------ fast path
def fast_path(t, snap, apps):
    """(description, [actions]) for instant commands, else (None, None)."""
    t = re.sub(r"^(?:in|on) (?:the )?finder\s+", "", t)
    k = re.sub(r"^(?:go to |switch to |open |do )?(?:the |a )?", "", t)
    if k in KEYS:
        return k.capitalize(), [{"op": "keys", "keys": KEYS[k]}]
    if re.match(r"^(?:delete|remove|clear) (?:that|this|the) (?:whole |entire )?line$", t):
        return "Delete the line", [{"op": "keys", "keys": "cmd+right"}, {"op": "keys", "keys": "cmd+shift+left"},
                                   {"op": "keys", "keys": "delete"}, {"op": "keys", "keys": "delete"}]
    if re.match(r"^(?:delete|remove) (?:that|this|the|the last) word$", t):
        return "Delete the word", [{"op": "keys", "keys": "option+delete"}]
    if m := re.match(r"^(?:open|launch|start|switch to|go to|bring up|show)\s+(?:the\s+)?(?:app\s+)?(.+?)(?:\s+app)?$", t):
        name = match_app(m.group(1), apps)
        if name and not re.match(r"^(a |an |new |my )", m.group(1)):
            return f"Open {name}", [{"op": "open_app", "app": name}]
    if m := re.match(r"^scroll\s+(up|down)(?:\s+(a bit|a little|a lot|more))?$", t):
        amt = {"a bit": 2, "a little": 2, "a lot": 12, "more": 8}.get(m.group(2) or "", 5)
        return f"Scroll {m.group(1)}", [{"op": "scroll", "direction": m.group(1), "amount": amt}]
    if re.match(r"^undo( that| it)?$", t):
        return "Undo", [{"op": "keys", "keys": "cmd+z"}]
    if m := re.match(r"^(?:start |create |make )?(?:a )?new note(?: called| named| titled)?\s*(.*)$", t):
        title = m.group(1).strip()
        acts = [{"op": "open_app", "app": "Notes"}, {"op": "keys", "keys": "cmd+n"}]
        if title:
            acts.append({"op": "type", "text": title})
        return (f"New note “{title}”" if title else "New note"), acts
    if m := re.match(r"^(?:type|write|dictate)\s+(.+)$", t):
        return "Type", [{"op": "type", "text": m.group(1)}]
    els = {i["label"].lower(): i for i in snap["items"] if i["kind"] == "element" and i["label"]}
    if m := re.match(r"^(?:click|press|tap|hit|select)\s+(?:on\s+)?(?:the\s+)?(.+?)(?:\s+button)?$", t):
        lbl = best(m.group(1), list(els), 0.75)
        if lbl:
            return f"Click “{els[lbl]['label']}”", [{"op": "press", "target": els[lbl]["id"]}]
    menus = {i["path"][-1].lower().rstrip("…."): i for i in snap["items"] if i["kind"] == "menu" and i["enabled"]}
    lbl = best(t, list(menus), 0.9)
    if lbl:
        return menus[lbl]["label"], [{"op": "menu", "target": menus[lbl]["id"]}]
    return None, None


APP_ALIASES = [(r"\b(system )?settings\b|\bpreferences\b", "System Settings"), (r"^text\b|\bimessage\b", "Messages")]


def mentioned_app(t, apps):
    """App the request is about, if it says so: "... in Safari", "open the bluetooth settings"."""
    for pat, app in APP_ALIASES:
        if re.search(pat, t) and app in apps:
            return app
    for name in sorted(apps, key=len, reverse=True):
        n = re.escape(name.lower())
        if re.search(rf"\b(in|on|using|with|from|open|launch|to)\s+(the\s+)?{n}( app)?\b", t):
            return name
    return None


# ------------------------------------------------------------------------------------------------ events
def emit(kind, uid, data=None, t_end=None):
    ev = {"type": kind, "utterance_id": uid, "data": data or {}}
    if t_end:
        ev["ms_since_speech_end"] = round((time.time() - t_end) * 1000)
    try:
        _udp.sendto(json.dumps(ev, ensure_ascii=False).encode(), OVERLAY)
    except OSError:
        pass


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


# ------------------------------------------------------------------------------------------------ brain
class Stopped(Exception):
    pass


class Brain:
    def __init__(self, q):
        self.q = q
        self.ui = MacUIProvider()
        self.ex = MacExecutor(self.ui)
        self.apps = self.ui.list_apps()
        self.pending = None          # confirmation: {"uid", "say", "actions", "sid", "t", "cancel"}
        self.awaiting = None         # follow-up question: {"say", "options", "resume", "t"}
        self.cancel = threading.Event()
        self.busy = False
        self.last_active = 0.0

    # ---------------------------------------------------------------- called from the HTTP thread
    def speaker_ok(self, u, short_reply=False):
        sp = u.get("speaker", {})
        if sp.get("verified"):
            return True
        score, thr = sp.get("score"), sp.get("threshold")
        return short_reply and score is not None and thr is not None and score >= thr - CONFIRM_SLACK

    def strip_wake(self, text, settings):
        text, rx = text or "", wake_rx(settings)
        if m := rx.match(text):
            return True, text[m.end():].strip()
        # Whisper glues a few words said just before into the same clip: "Right. Seriously, text Omar hello."
        # Accept the wake word at the start of a later sentence (not mid-sentence: "that's seriously cool").
        for b in re.finditer(r"[.!?]\s+", text):
            if len(text[:b.start()].split()) > 6:
                break
            if m := rx.match(text[b.end():]):
                return True, text[b.end() + m.end():].strip()
        return False, text

    def try_stop(self, u):
        """Control words act immediately (PRD R1: < 50 ms), never queued behind a running plan."""
        _, body = self.strip_wake(u.get("text", ""), load_settings())
        if not STOP_RX.match(norm(body)) or not self.speaker_ok(u, short_reply=True):
            return False
        active = self.busy or self.pending or self.awaiting or not self.q.empty()
        if not active:
            return True                                   # nothing to stop: swallow quietly
        self.cancel.set()
        drained = 0
        while True:
            try:
                self.q.get_nowait(); drained += 1
            except queue.Empty:
                break
        p, self.pending, self.awaiting = self.pending, None, None
        if p and p.get("cancel"):
            self.q.put({"_cleanup": p["cancel"], "sid": p["sid"]})   # executor work stays on the worker thread
        log(f"■ {u.get('text')!r} -> stop (running={self.busy}, dropped {drained} queued)")
        emit("heard", u.get("id"), {"text": body or u.get("text")})
        emit("done", u.get("id"), {"say": "Stopped" if self.busy else "Cancelled", **self.follow_up()})
        return True

    # ---------------------------------------------------------------- worker thread
    def fail(self, uid, msg):
        if self.cancel.is_set():
            return
        log(f"     ✗ {msg}")
        emit("error", uid, {"message": msg, **self.follow_up()})

    def execute(self, uid, actions, sid, total=None, offset=0):
        """Run actions in order. -> (ok, error, last_snapshot). Stops between steps when cancelled."""
        labels = {i["id"]: i["label"] for i in (self.ui.get_snapshot(sid) or {"items": []})["items"]}
        snap = None
        for n, a in enumerate(actions, 1):
            if self.cancel.is_set():
                log("     ■ stopped before " + json.dumps(a, ensure_ascii=False))
                return False, "Stopped", snap
            emit("action", uid, {"index": offset + n, "of": total or len(actions), "action": a,
                                 "label": labels.get(a.get("target"))})
            r = self.ex.run(sid, a)
            snap, sid = r["snapshot"], r["snapshot"]["snapshot_id"]
            log(f"     {json.dumps(a, ensure_ascii=False):<60} {'ok' if r['ok'] else 'ERROR ' + r['error']}"
                f"  changed={r['changed']}  {r['ms']} ms")
            if not r["ok"]:
                return False, r["error"], snap
        return True, None, snap

    def follow_up(self):
        """Event data telling the overlay how long follow-ups work without the wake word (it shows a draining line)."""
        self.last_active = time.time()
        st = load_settings()
        if not st.get("wake_word_required", True) or not st.get("listening", True):
            return {}
        return {"follow_up_s": st.get("follow_up_seconds", 10)}

    def finish(self, uid, t_end, say=None):
        if not self.cancel.is_set():
            emit("done", uid, {**({"say": say} if say else {}), **self.follow_up()}, t_end)
            if t_end:
                log(f"     done {(time.time() - t_end) * 1000:.0f} ms after you stopped talking")

    def gate(self, uid, text, say, acts, snap, cancel=None):
        """Apply the permission for the actions' risk category. True = handled (asked or blocked)."""
        mode, cat = policy(risk_categories(text, snap, acts), load_settings())
        if mode == "block":
            self.fail(uid, f"{CATEGORY_LABELS.get(cat, cat)} is blocked in your settings")
            return True
        if mode == "ask":
            if snap["app"]["name"] in CHAT_APPS and snap.get("window") and cat == "send_messages":
                typed = next((a["text"] for a in acts if a["op"] == "type"), None)
                if typed and "“" not in say:
                    say = f"Send “{typed}” to {snap['window']}"
            self.confirm(uid, say, acts, snap, cancel)
            return True
        return False

    def handle(self, u):
        if "_cleanup" in u:                                      # undo a cancelled draft
            self.cancel.clear()
            self.execute(None, u["_cleanup"], u["sid"])
            return
        uid, raw, t_end = u.get("id"), u.get("text", ""), u.get("t_speech_end")
        st = load_settings()
        woke, body = self.strip_wake(raw, st)
        t = norm(body)
        t_start = (t_end - float(u.get("audio_s") or 0)) if t_end else time.time()
        following = t_start - self.last_active < st.get("follow_up_seconds", 10)
        follow_up_ok = following and sounds_like_command(body, t)
        if following and not follow_up_ok and not (self.pending or self.awaiting or woke):
            log(f"· not a command (follow-up window): {raw!r}")
        addressed = woke or not st.get("wake_word_required", True) or self.pending or self.awaiting \
            or follow_up_ok or u.get("internal")

        if not self.speaker_ok(u, short_reply=bool((self.pending or self.awaiting) and len(t.split()) <= 4)):
            if woke:
                emit("rejected", uid, {"text": body or raw})
            log(f"✗ ignored (voice lock, score {u.get('speaker', {}).get('score')}): {raw!r}")
            return
        if not addressed:
            log(f"· not addressed: {raw!r}")
            return
        if woke and not t:                                       # just "Siriously"
            self.last_active = time.time()
            emit("heard", uid, {"text": "Listening…", **self.follow_up()})
            return
        if not u.get("internal"):
            emit("heard", uid, {"text": body})
        self.last_active = time.time()

        # 2a. pending confirmation
        if self.pending:
            p, self.pending = self.pending, None
            if time.time() - p["t"] < CONFIRM_TIMEOUT and is_yes(t):
                log(f"✓ {raw!r} -> confirmed: {p['say']}")
                emit("plan", uid, {"say": p["say"], "steps": len(p["actions"])})
                ok, err, _ = self.execute(uid, p["actions"], p["sid"])
                return self.finish(uid, t_end) if ok else self.fail(uid, err)
            log(f"   pending '{p['say']}' cancelled by {raw!r}")
            if p.get("cancel"):
                self.execute(uid, p["cancel"], p["sid"])
            emit("done", uid, {"say": "Cancelled", **self.follow_up()})
            if not t or t.split()[0] in NO_WORDS or t.startswith("never mind"):
                return
        # 2b. answer to a follow-up question
        if self.awaiting:
            a, self.awaiting = self.awaiting, None
            if time.time() - a["t"] < CONFIRM_TIMEOUT + 5 and t and t.split()[0] not in NO_WORDS:
                log(f"✓ {raw!r} -> answer to {a['say']!r}")
                return a["resume"](body, uid, t_end)
            log(f"   question {a['say']!r} dropped")
            return emit("done", uid, {"say": "Cancelled", **self.follow_up()})
        if not t or FILLER_ONLY.match(t) or all(w in NO_WORDS for w in t.split()):
            return log(f"   ignored filler {raw!r}")

        # 3. settings by voice
        if cmd := settings_command(t):
            kind, val = cmd
            s = copy.deepcopy(load_settings())
            if kind == "wake":
                s["wake_word_required"] = val
                say = f"Wake word {'on' if val else 'off'}"
            else:
                cat, mode = val
                s["permissions"][cat] = mode
                say = f"{CATEGORY_LABELS[cat]}: {dict(allow='allowed', ask='will ask', block='blocked')[mode]}"
            save_settings(s)
            log(f"✓ {raw!r} -> settings: {say}")
            return self.finish(uid, None, say)

        snap = self.ui.snapshot()
        # 4. texting skill
        rest = parse_text_command(t, snap["app"]["name"])
        if rest:
            log(f"✓ {raw!r} -> text message flow  [in {snap['app']['name']}]")
            return self.text_flow(uid, rest, snap, t_end)
        # 5a. new folder: check the name on disk *before* touching Finder
        if fc := folder_command(t, body):
            return self.new_folder(uid, raw, *fc, snap, t_end)
        # 5b. fast path
        desc, acts = fast_path(t, snap, self.apps)
        if acts:
            log(f"✓ {raw!r} -> {desc}  [fast path, in {snap['app']['name']}]")
            if self.gate(uid, body, desc, acts, snap):
                return
            emit("plan", uid, {"say": desc, "steps": len(acts)})
            ok, err, last = self.execute(uid, acts, snap["snapshot_id"])
            return self.finish(uid, t_end) if ok else self.fail(uid, err)
        # 6. planner
        return self.run_planner(u, body, t, snap, t_end)

    def run_planner(self, u, body, t, snap, t_end):
        uid = u.get("id")
        log(f"✓ {body!r} -> planner  [in {snap['app']['name']}]")
        emit("route", uid, {"route": "planner"})
        done, error, steps = [], None, 0
        target_app = mentioned_app(t, self.apps)
        if target_app and target_app != snap["app"]["name"]:
            ok, err, snap2 = self.execute(uid, [{"op": "open_app", "app": target_app}], snap["snapshot_id"])
            if not ok:
                return self.fail(uid, err)
            snap, steps = snap2, 1
            done.append(f"open_app {target_app}")
        for rnd in range(MAX_ROUNDS):
            if self.cancel.is_set():
                return
            try:
                p = planner.plan(body, snap, done, error)
            except Exception as e:                      # server down, timeout, bad JSON
                log(f"     planner failed: {type(e).__name__}: {e}")
                return self.fail(uid, "Planner unavailable (is Bonsai running?)")
            acts = p["actions"]
            log(f"     plan r{rnd + 1} ({p['ms']} ms, {p['new_prompt_tokens']} new tokens): {p['say']!r} "
                f"{json.dumps(acts, ensure_ascii=False)}" + (f"  ask={p['ask']!r}" if p.get("ask") else ""))
            if p.get("ask") and not acts:
                if u.get("no_ask"):
                    return self.fail(uid, "Not sure what to do with that")
                original = body
                return self.ask_question(uid, p["ask"], [],
                                         lambda ans, uid2, t2: self.handle({"id": uid2, "text": f"{original} ({ans})",
                                                                            "t_speech_end": t2, "internal": True,
                                                                            "no_ask": True,
                                                                            "speaker": {"verified": True}}))
            if not acts:
                if rnd == 0:
                    say = (p["say"] or "").strip().rstrip(".")
                    if say and not re.match(r"^(i|i'm|i am|that|this|sorry)\b", say, re.I):
                        say = f"Not sure how to {say[0].lower() + say[1:]} here"
                    log(f"     nothing to do: {say!r}")
                    emit("route", uid, {"route": "none", "say": say or "Not sure how to do that here",
                                        **self.follow_up()})
                    return
                return self.finish(uid, t_end)
            if rnd == 0 and self.gate(uid, body, tidy_say(p["say"]), acts, snap):
                return
            emit("plan", uid, {"say": tidy_say(p["say"]), "steps": steps + len(acts)})
            cut = len(acts)                              # after open_app, the old app's ids are meaningless
            for k, a in enumerate(acts):
                if a["op"] == "open_app" and any("target" in b for b in acts[k + 1:]):
                    cut = k + 1
                    break
            ok, err, snap2 = self.execute(uid, acts[:cut], snap["snapshot_id"], total=steps + len(acts), offset=steps)
            if self.cancel.is_set():
                return
            steps += cut if ok else 0
            snap = snap2 or self.ui.snapshot()
            if not ok:
                done += [f"{a['op']} {a.get('app') or a.get('keys') or a.get('text') or a.get('target')}"
                         for a in acts[:cut]][:-1]
                error = err
                continue
            if cut < len(acts) or acts[-1]["op"] == "open_app":
                done.append(f"open_app {acts[cut - 1]['app']}")
                error = None
                continue
            return self.finish(uid, t_end)
        self.fail(uid, error or "Gave up after 3 tries")

    def new_folder(self, uid, raw, name, place, snap, t_end):
        where = place.title() if place else ""
        d = folder_dir(place, snap)
        if name and d and os.path.exists(os.path.join(d, name)):
            alt = next(f"{name} {k}" for k in range(2, 1000) if not os.path.exists(os.path.join(d, f"{name} {k}")))
            log(f"✓ {raw!r} -> “{name}” already exists in {d}: offering “{alt}”")
            return self.confirm(uid, f"“{name}” already exists in {where or os.path.basename(d)}. Make “{alt}” instead",
                                folder_actions(alt, place, snap), snap)
        desc = (f"New folder “{name}”" if name else "New folder") + (f" in {where}" if where else "")
        log(f"✓ {raw!r} -> {desc}  [fast path, in {snap['app']['name']}]")
        emit("plan", uid, {"say": desc, "steps": len(folder_actions(name, place, snap))})
        ok, err, last = self.execute(uid, folder_actions(name, place, snap), snap["snapshot_id"])
        if not ok:
            return self.fail(uid, err)
        return self.check_folder_name(uid, name, last, t_end) if name and last else self.finish(uid, t_end)

    def check_folder_name(self, uid, name, snap, t_end):
        """Fallback when we couldn't check on disk: Finder refuses a duplicate name with an alert, which can take
        a moment to appear. Dismiss it, cancel the rename and undo the new folder."""
        def ok_button(sn):
            return next((i for i in sn["items"] if i["kind"] == "element" and i["role"] == "button"
                         and i["label"] == "OK"), None)
        ok_btn = ok_button(snap)
        for _ in range(3):
            if ok_btn or self.cancel.is_set():
                break
            time.sleep(0.3)
            snap = self.ui.snapshot()
            ok_btn = ok_button(snap)
        if ok_btn is None:
            return self.finish(uid, t_end)
        log(f"     name “{name}” is taken: cleaning up")
        self.execute(uid, [{"op": "press", "target": ok_btn["id"]}, {"op": "keys", "keys": "esc"},
                           {"op": "keys", "keys": "cmd+z"}], snap["snapshot_id"])
        self.fail(uid, f"A folder named “{name}” already exists here")

    # ---------------------------------------------------------------- questions and confirmations
    def ask_question(self, uid, say, options, resume):
        self.awaiting = {"say": say, "options": options, "resume": resume, "t": time.time()}
        log(f"     asks: {say!r} {options}")
        emit("ask", uid, {"say": say, "options": options})

    def confirm(self, uid, say, acts, snap, cancel=None):
        say = tidy_say(say)
        self.pending = {"uid": uid, "say": say, "actions": acts, "sid": snap["snapshot_id"], "t": time.time(),
                        "cancel": cancel}
        log(f"     needs confirmation: {say!r} {json.dumps(acts, ensure_ascii=False)}")
        emit("confirm", uid, {"say": (say or "Do it").rstrip("?") + "?"})

    # ---------------------------------------------------------------- texting
    def text_flow(self, uid, rest, snap, t_end, chosen=None, message=None):
        """text <name> <message> in Messages, with the recipient verified from the window title."""
        if snap["app"]["name"] != "Messages":
            ok, err, snap = self.execute(uid, [{"op": "open_app", "app": "Messages"}], snap["snapshot_id"])
            if not ok:
                return self.fail(uid, err)
        convos = conversations(snap)
        if chosen is None:
            words = re.sub(r"\b(saying|that says|that)\b", "|", rest).split("|")[0].split()
            matches, name_len = [], 0
            for k in (3, 2, 1):                          # longest name prefix that matches a conversation
                if len(words) >= k and (m := match_people(" ".join(words[:k]), convos)):
                    matches, name_len = m, k
                    break
            if not matches:
                return self.fail(uid, f"No recent conversation with “{words[0].title() if words else '?'}”")
            msg = re.sub(r"^(saying|that says|that)\s+", "", " ".join(rest.split()[name_len:])).strip()
            if len(matches) > 1:
                names = [n for n, _ in matches[:4]]
                return self.ask_question(uid, f"Which {' '.join(words[:name_len]).title()}?", names,
                                         lambda ans, uid2, t2: self._text_pick(uid2, ans, names, rest, msg, t2))
            chosen, message = matches[0][0], msg
        item = next((i for n, i in conversations(snap) if n == chosen), None)
        if item is None:
            return self.fail(uid, f"Can't find the conversation with {chosen}")
        ok, err, snap = self.execute(uid, [{"op": "press", "target": item["id"]}], snap["snapshot_id"])
        if not ok:
            return self.fail(uid, err)
        title = (snap.get("window") or "").strip()
        if not (title == chosen or title.startswith(chosen + " ") or chosen.startswith(title + " ")):
            return self.fail(uid, f"Opened “{title}”, not {chosen}; nothing sent")
        if not message:
            return self.ask_question(uid, f"What should I send to {title}?", [],
                                     lambda ans, uid2, t2: self.text_flow(uid2, rest, self.ui.snapshot(), t2,
                                                                          chosen=chosen, message=ans.strip()))
        field = next((i for i in snap["items"] if i["kind"] == "element" and i["role"] == "textfield"
                      and i["label"] == "Message"), None)
        if field is None:
            return self.fail(uid, "Can't find the message field")
        ok, err, snap = self.execute(uid, [{"op": "type", "text": message, "target": field["id"]}], snap["snapshot_id"])
        if not ok:
            return self.fail(uid, err)
        field = next((i for i in snap["items"] if i["kind"] == "element" and i["role"] == "textfield"
                      and i["label"] == "Message"), field)
        send = [{"op": "keys", "keys": "return"}]
        cancel = [{"op": "press", "target": field["id"]}, {"op": "keys", "keys": "cmd+a"}, {"op": "keys", "keys": "delete"}]
        if self.gate(uid, "send", f"Send “{message}” to {title}", send, snap, cancel):
            return
        ok, err, _ = self.execute(uid, send, snap["snapshot_id"])
        return self.finish(uid, t_end, f"Sent to {title}") if ok else self.fail(uid, err)

    def _text_pick(self, uid, answer, names, rest, msg, t_end):
        i = pick_option(norm(answer), names)
        if i is None:
            return self.fail(uid, f"Didn't catch which one (heard “{answer}”)")
        self.text_flow(uid, rest, self.ui.snapshot(), t_end, chosen=names[i], message=msg)


def main():
    q = queue.Queue()
    ready = threading.Event()
    holder = {}

    def worker():
        brain = Brain(q)
        holder["brain"] = brain
        ready.set()
        while True:
            u = q.get()
            brain.cancel.clear()
            brain.busy = True
            try:
                brain.handle(u)
            except Exception as e:                      # keep serving
                log(f"!! {type(e).__name__}: {e}")
                emit("error", u.get("id"), {"message": type(e).__name__})
            finally:
                brain.busy = False

    threading.Thread(target=worker, daemon=True).start()
    ready.wait()
    load_settings()                                     # writes defaults on first run

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                u = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            except ValueError:
                self.send_response(400); self.end_headers(); return
            if not holder["brain"].try_stop(u):
                q.put(u)
            self.send_response(200)
            self.end_headers()

        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok": true, "mini_brain": true}')

        def log_message(self, *a):
            pass

    print(f"mini brain on http://127.0.0.1:{PORT}  (fast path + Bonsai planner on :8080; settings {SETTINGS_PATH})",
          flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
