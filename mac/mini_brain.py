# /// script
# requires-python = ">=3.11"
# dependencies = ["pyobjc-framework-ApplicationServices", "pyobjc-framework-Cocoa", "pyobjc-framework-Quartz"]
# ///
"""Mini brain: Mac-side stand-in for the real brain (PRD §5), to test voice -> action end to end.
The real brain is the teammate's; this one is deliberately small: fast path + Bonsai planner + confirmations.

    STT=none ./start_all.sh                      # Bonsai 2 4B on :8080 (for the planner)
    uv run --script mac/mini_brain.py            # 127.0.0.1:8765, then: mac/ear/.build/release/siriously-ear run

Routing, in order (PRD §5.1):
  1. pending confirmation: "yes" runs it, anything else cancels it
  2. fast path (instant): open/switch to <installed app> · scroll up/down · undo · tabs · close window · copy/paste ·
     "new note called X" · "type X" · "click <label>" · menu items by exact-ish name
  3. planner (mac/planner.py, Bonsai 2 4B, ~1.5-2 s per call): anything else. Up to 3 rounds: it re-plans after
     a failed step, and after opening another app when the plan still needs that app's buttons.
Risky plans (send/post/delete/trash/quit/buy..., or typing + Enter in a chat/mail app) wait for a spoken "yes".
Rejected voices are ignored. Keystrokes never go to a terminal (executor guard), so have a real app in front.
Events for the overlay go to UDP :8766 in the PRD §4.6 shape.
"""
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
_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

KEYS = {"new tab": "cmd+t", "close tab": "cmd+w", "close window": "cmd+w", "close this window": "cmd+w",
        "next tab": "ctrl+tab", "previous tab": "ctrl+shift+tab", "copy": "cmd+c", "copy that": "cmd+c",
        "paste": "cmd+v", "paste that": "cmd+v", "select all": "cmd+a", "save": "cmd+s", "zoom in": "cmd+=",
        "zoom out": "cmd+-", "go back": "cmd+[", "go forward": "cmd+]", "new window": "cmd+n",
        "press enter": "return", "press return": "return", "enter": "return", "escape": "esc",
        "find": "cmd+f", "search": "cmd+f"}
FILLER = re.compile(r"^(hey |ok |okay )?(siri(ously)?[, ]+)?(please |can you |could you )?")
YES_WORDS = {"yes", "yeah", "yep", "yup", "sure", "confirm", "confirmed", "ok", "okay", "absolutely", "correct",
             "affirmative"}
NO_WORDS = {"no", "nope", "cancel", "stop", "don't", "dont", "never", "nah", "wait"}
CONFIRM_SLACK = 0.5      # a "yes" may score this much below the voice-lock threshold (one-word clips score low)
CHAT_APPS = {"Slack", "Messages", "Mail", "Discord", "WhatsApp", "Microsoft Teams", "Telegram", "Signal", "Outlook"}
# Irreversible or outward-facing only. Move to Trash (undoable) and quitting an app (it asks about unsaved work) don't ask.
RISKY = re.compile(r"\b(send|post|reply|forward|delete|empty|erase|shut ?down|restart|log ?out|"
                   r"buy|purchase|pay|order|uninstall|format|unsubscribe|submit)\b")
RISKY_KEYS = {"cmd+shift+delete", "cmd+option+shift+delete", "cmd+option+esc", "cmd+shift+q", "cmd+option+delete"}
PAST = {"sent": "Send", "moved": "Move", "deleted": "Delete", "created": "Create", "opened": "Open", "closed": "Close",
        "emptied": "Empty", "posted": "Post", "replied": "Reply", "forwarded": "Forward", "searched": "Search",
        "sending": "Send", "deleting": "Delete", "moving": "Move", "creating": "Create", "searching": "Search",
        "opening": "Open", "posting": "Post", "replying": "Reply"}


def is_yes(t):
    w = t.split()
    if not w or w[0] in NO_WORDS:
        return False
    return w[0] in YES_WORDS or t.startswith(("do it", "go ahead", "send it")) or \
        (len(w) <= 8 and any(x in YES_WORDS for x in w) and not any(x in NO_WORDS for x in w))


def tidy_say(say):
    """Imperative, curly quotes: "Sent 'hello'" -> "Send “hello”"."""
    say = re.sub(r"['\"]([^'\"]+)['\"]", r"“\1”", (say or "").strip())
    w = say.split(" ", 1)
    if w and w[0].lower() in PAST:
        say = PAST[w[0].lower()] + (" " + w[1] if len(w) > 1 else "")
    return say


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


def norm(text):
    t = text.lower().strip()
    t = re.sub(r"[^\w\s']", " ", t)
    t = " ".join(t.split())
    t = FILLER.sub("", t)
    return re.sub(r"( please| for me| now)$", "", t).strip()


def best(query, labels, cutoff):
    m = difflib.get_close_matches(query, labels, n=1, cutoff=cutoff)
    return m[0] if m else None


def fast_path(t, snap, apps):
    """(description, [actions]) for instant commands, else (None, None)."""
    k = re.sub(r"^(?:go to |switch to |open |do )?(?:the |a )?", "", t)
    if k in KEYS:
        return k.capitalize(), [{"op": "keys", "keys": KEYS[k]}]
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
    if m := re.match(r"^(?:make |create )?(?:a )?new folder(?: called| named)?\s*(.*)$", t):
        name = m.group(1).strip()
        acts = [] if snap["app"]["name"] == "Finder" else [{"op": "open_app", "app": "Finder"}]
        acts += [{"op": "keys", "keys": "cmd+shift+n"}]
        if name:
            acts += [{"op": "type", "text": name}, {"op": "keys", "keys": "return"}]
        return (f"New folder “{name}”" if name else "New folder"), acts
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
    """App the request is about, if it says so: "... in Safari", "open the bluetooth settings", "text hari hello".
    Only explicit mentions (alias, or app name after in/on/using/with/from/open/to) so 'the weather' != Weather."""
    for pat, app in APP_ALIASES:
        if re.search(pat, t) and app in apps:
            return app
    for name in sorted(apps, key=len, reverse=True):
        n = re.escape(name.lower())
        if re.search(rf"\b(in|on|using|with|from|open|launch|to)\s+(the\s+)?{n}( app)?\b", t):
            return name
    return None


ORDINALS = {"one": 0, "first": 0, "1": 0, "two": 1, "second": 1, "2": 1, "to": 1, "too": 1, "three": 2, "third": 2,
            "3": 2, "four": 3, "fourth": 3, "4": 3}
FILLER_ONLY = re.compile(r"^(um+|uh+|hmm+|mm+|ah+|oh+|huh|okay so|so)$")


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
        ok = all(any(p.startswith(x) or difflib.SequenceMatcher(None, x, p[:len(x) + 1]).ratio() >= 0.75 for p in parts)
                 for x in q)
        if ok:
            hits.append((name, item))
    seen, uniq = set(), []
    for name, item in hits:
        if name not in seen:
            seen.add(name); uniq.append((name, item))
    return uniq


TEXT_CMD = [re.compile(r"^(?:text|imessage)\s+(.+)$"),
            re.compile(r"^send (?:a |an )?(?:text|message|imessage) to\s+(.+)$"),
            re.compile(r"^message\s+(.+)$")]


def parse_text_command(t, front_app):
    """'text hari hello' -> 'hari hello' (name + message still joined; split once we can see conversations)."""
    for n, rx in enumerate(TEXT_CMD):
        if n == 2 and front_app in CHAT_APPS - {"Messages"}:
            continue                                  # "message the channel …" in Slack is not an iMessage
        if m := rx.match(t):
            return m.group(1)
    return None


def needs_confirm(text, snap, actions):
    labels = {i["id"]: i["label"] for i in snap["items"]}
    apps = {snap["app"]["name"]} | {a.get("app", "") for a in actions if a["op"] == "open_app"}
    if RISKY.search(text.lower()):
        return True
    for n, a in enumerate(actions):
        if a["op"] in ("press", "menu") and RISKY.search(labels.get(a.get("target"), "").lower()):
            return True
        if a["op"] == "keys" and a["keys"].lower() in RISKY_KEYS:
            return True
        if a["op"] == "keys" and a["keys"].lower() == "return" and apps & CHAT_APPS \
                and any(b["op"] == "type" for b in actions[:n]):
            return True
    return False


class Brain:
    def __init__(self):
        self.ui = MacUIProvider()
        self.ex = MacExecutor(self.ui)
        self.apps = self.ui.list_apps()
        self.pending = None          # confirmation: {"uid", "say", "actions", "sid", "t", "cancel"}
        self.awaiting = None         # follow-up question: {"say", "options", "resume": fn(answer, uid, t_end), "t"}

    def execute(self, uid, actions, sid, total=None, offset=0):
        """Run actions in order. -> (ok, error, last_snapshot)"""
        labels = {i["id"]: i["label"] for i in (self.ui.get_snapshot(sid) or {"items": []})["items"]}
        snap = None
        for n, a in enumerate(actions, 1):
            emit("action", uid, {"index": offset + n, "of": total or len(actions), "action": a,
                                 "label": labels.get(a.get("target"))})
            r = self.ex.run(sid, a)
            snap, sid = r["snapshot"], r["snapshot"]["snapshot_id"]
            log(f"     {json.dumps(a, ensure_ascii=False):<60} {'ok' if r['ok'] else 'ERROR ' + r['error']}"
                f"  changed={r['changed']}  {r['ms']} ms")
            if not r["ok"]:
                return False, r["error"], snap
        return True, None, snap

    def handle(self, u):
        uid, text, t_end = u.get("id"), u.get("text", ""), u.get("t_speech_end")
        sp = u.get("speaker", {})
        t = norm(text)
        if not sp.get("verified"):
            score, thr = sp.get("score"), sp.get("threshold")
            near = score is not None and thr is not None and score >= thr - CONFIRM_SLACK
            if not (self.pending and is_yes(t) and near):
                log(f"✗ ignored (voice lock, score {score}): {text!r}")
                return
            log(f"   accepting short confirmation {text!r} (score {score:.2f}, threshold {thr:.2f})")

        # 1. pending confirmation
        if self.pending:
            p, self.pending = self.pending, None
            if time.time() - p["t"] < CONFIRM_TIMEOUT and is_yes(t):
                log(f"✓ {text!r} -> confirmed: {p['say']}")
                emit("plan", uid, {"say": p["say"], "steps": len(p["actions"])})   # overlay follows this utterance
                ok, err, _ = self.execute(uid, p["actions"], p["sid"])
                if ok:
                    emit("done", uid, {}, t_end)
                else:
                    emit("error", uid, {"message": err})
                return
            log(f"   pending '{p['say']}' cancelled by {text!r}")
            if p.get("cancel"):
                self.execute(uid, p["cancel"], p["sid"])
            emit("done", uid, {"say": "Cancelled"})
            if not t or t.split()[0] in NO_WORDS or t.startswith("never mind"):
                return

        # 1b. answer to a follow-up question
        if self.awaiting:
            a, self.awaiting = self.awaiting, None
            if time.time() - a["t"] < CONFIRM_TIMEOUT + 5 and t and t.split()[0] not in NO_WORDS:
                log(f"✓ {text!r} -> answer to {a['say']!r}")
                a["resume"](text, uid, t_end)
                return
            log(f"   question {a['say']!r} dropped")
            emit("done", uid, {"say": "Cancelled"})
            return

        if not t or FILLER_ONLY.match(t) or all(w in NO_WORDS for w in t.split()):
            log(f"   ignored filler {text!r}")
            return

        snap = self.ui.snapshot()
        # 2a. texting skill (deterministic: the recipient is read back from the screen, never assumed)
        rest = parse_text_command(t, snap["app"]["name"])
        if rest:
            log(f"✓ {text!r} -> text message flow  [in {snap['app']['name']}]")
            return self.text_flow(uid, rest, snap, t_end)
        # 2b. fast path
        desc, acts = fast_path(t, snap, self.apps)
        if acts:
            log(f"✓ {text!r} -> {desc}  [fast path, in {snap['app']['name']}]")
            if needs_confirm(text, snap, acts):
                return self.ask(uid, desc, acts, snap)
            emit("plan", uid, {"say": desc, "steps": len(acts)})
            ok, err, _ = self.execute(uid, acts, snap["snapshot_id"])
            emit("done", uid, {}, t_end) if ok else emit("error", uid, {"message": err})
            log(f"     done {(time.time() - t_end) * 1000:.0f} ms after you stopped talking")
            return

        # 3. planner. If the request names another app, open it first so the planner sees the right screen.
        log(f"✓ {text!r} -> planner  [in {snap['app']['name']}]")
        emit("route", uid, {"route": "planner"})
        done, error, steps = [], None, 0
        target_app = mentioned_app(t, self.apps)
        if target_app and target_app != snap["app"]["name"]:
            ok, err, snap2 = self.execute(uid, [{"op": "open_app", "app": target_app}], snap["snapshot_id"])
            if not ok:
                emit("error", uid, {"message": err})
                return
            snap, steps = snap2, 1
            done.append(f"open_app {target_app}")
        for rnd in range(MAX_ROUNDS):
            try:
                p = planner.plan(text, snap, done, error)
            except Exception as e:                      # server down, timeout, bad JSON
                log(f"     planner failed: {type(e).__name__}: {e}")
                emit("error", uid, {"message": "Planner unavailable (is Bonsai running?)"})
                return
            acts = p["actions"]
            if p.get("ask") and not acts:
                log(f"     planner asks: {p['ask']!r}")
                original = text
                return self.ask_question(uid, p["ask"], [],
                                         lambda ans, uid2, t2: self.handle({"id": uid2, "text": f"{original} ({ans})",
                                                                            "t_speech_end": t2,
                                                                            "speaker": {"verified": True}}))
            log(f"     plan r{rnd + 1} ({p['ms']} ms, {p['new_prompt_tokens']} new tokens): {p['say']!r} "
                f"{json.dumps(acts, ensure_ascii=False)}")
            if not acts:
                if rnd == 0:
                    emit("route", uid, {"route": "none", "say": p["say"] or "Can't do that"})
                else:
                    emit("done", uid, {}, t_end)
                return
            if needs_confirm(text if rnd == 0 else "", snap, acts):
                say = p["say"]
                if snap["app"]["name"] in CHAT_APPS and snap.get("window"):
                    typed = next((a["text"] for a in acts if a["op"] == "type"), None)
                    say = f"Send “{typed}” to {snap['window']}" if typed else f"{say} (in {snap['window']})"
                return self.ask(uid, say, acts, snap)
            emit("plan", uid, {"say": tidy_say(p["say"]), "steps": steps + len(acts)})
            # After open_app, ids of the old app are meaningless: stop there and re-plan in the new app.
            cut = len(acts)
            for k, a in enumerate(acts):
                if a["op"] == "open_app" and any("target" in b for b in acts[k + 1:]):
                    cut = k + 1
                    break
            ok, err, snap2 = self.execute(uid, acts[:cut], snap["snapshot_id"], total=steps + len(acts), offset=steps)
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
            emit("done", uid, {}, t_end)
            log(f"     done {(time.time() - t_end) * 1000:.0f} ms after you stopped talking")
            return
        emit("error", uid, {"message": error or "Gave up after 3 tries"})

    def ask_question(self, uid, say, options, resume):
        self.awaiting = {"say": say, "options": options, "resume": resume, "t": time.time()}
        log(f"     asks: {say!r} {options}")
        emit("ask", uid, {"say": say, "options": options})

    def text_flow(self, uid, rest, snap, t_end, chosen=None, message=None):
        """text <name> <message> in Messages, with the recipient verified from the window title."""
        if snap["app"]["name"] != "Messages":
            ok, err, snap = self.execute(uid, [{"op": "open_app", "app": "Messages"}], snap["snapshot_id"])
            if not ok:
                return emit("error", uid, {"message": err})
        convos = conversations(snap)
        if chosen is None:
            words = re.sub(r"\b(saying|that says|that)\b", "|", rest).split("|")[0].split()
            matches, name_len = [], 0
            for k in (3, 2, 1):                          # longest name prefix that matches a conversation
                if len(words) >= k and (m := match_people(" ".join(words[:k]), convos)):
                    matches, name_len = m, k
                    break
            if not matches:
                return emit("error", uid, {"message": f"No recent conversation with “{words[0].title() if words else '?'}”"})
            msg = re.sub(r"^(saying|that says|that)\s+", "", " ".join(rest.split()[name_len:])).strip()
            if len(matches) > 1:
                names = [n for n, _ in matches[:4]]
                return self.ask_question(uid, f"Which {' '.join(words[:name_len]).title()}?", names,
                                         lambda ans, uid2, t2: self._text_pick(uid2, ans, names, rest, msg, t2))
            chosen, message = matches[0][0], msg
        # open the conversation (re-find it in the current snapshot) and verify the window title
        item = next((i for n, i in conversations(snap) if n == chosen), None)
        if item is None:
            return emit("error", uid, {"message": f"Can't find the conversation with {chosen}"})
        ok, err, snap = self.execute(uid, [{"op": "press", "target": item["id"]}], snap["snapshot_id"])
        if not ok:
            return emit("error", uid, {"message": err})
        if (snap.get("window") or "").strip() != chosen:
            return emit("error", uid, {"message": f"Opened “{snap.get('window')}”, not {chosen}; nothing sent"})
        if not message:
            return self.ask_question(uid, f"What should I send to {chosen}?", [],
                                     lambda ans, uid2, t2: self.text_flow(uid2, rest, self.ui.snapshot(), t2,
                                                                          chosen=chosen, message=ans.strip()))
        field = next((i for i in snap["items"] if i["kind"] == "element" and i["role"] == "textfield"
                      and i["label"] == "Message"), None)
        if field is None:
            return emit("error", uid, {"message": "Can't find the message field"})
        ok, err, snap = self.execute(uid, [{"op": "type", "text": message, "target": field["id"]}], snap["snapshot_id"])
        if not ok:
            return emit("error", uid, {"message": err})
        field = next((i for i in snap["items"] if i["kind"] == "element" and i["role"] == "textfield"
                      and i["label"] == "Message"), field)
        # cancel = focus the message field first, then clear it (never ⌘A+Delete on the sidebar)
        self.ask(uid, f"Send “{message}” to {snap.get('window')}", [{"op": "keys", "keys": "return"}], snap,
                 cancel=[{"op": "press", "target": field["id"]}, {"op": "keys", "keys": "cmd+a"},
                         {"op": "keys", "keys": "delete"}])

    def _text_pick(self, uid, answer, names, rest, msg, t_end):
        i = pick_option(norm(answer), names)
        if i is None:
            return emit("error", uid, {"message": f"Didn't catch which one (heard “{answer}”)"})
        self.text_flow(uid, rest, self.ui.snapshot(), t_end, chosen=names[i], message=msg)

    def ask(self, uid, say, acts, snap, cancel=None):
        say = tidy_say(say)
        self.pending = {"uid": uid, "say": say, "actions": acts, "sid": snap["snapshot_id"], "t": time.time(),
                        "cancel": cancel}
        log(f"     needs confirmation: {say!r} {json.dumps(acts, ensure_ascii=False)}")
        emit("confirm", uid, {"say": (say or "Do it").rstrip("?") + "?"})


def worker(q):
    brain = Brain()
    while True:
        u = q.get()
        try:
            brain.handle(u)
        except Exception as e:                          # keep serving
            log(f"!! {type(e).__name__}: {e}")
            emit("error", u.get("id"), {"message": f"{type(e).__name__}"})


def main():
    q = queue.Queue()
    threading.Thread(target=worker, args=(q,), daemon=True).start()

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                q.put(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)))))
                self.send_response(200)
            except ValueError:
                self.send_response(400)
            self.end_headers()

        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok": true, "mini_brain": true}')

        def log_message(self, *a):
            pass

    print(f"mini brain on http://127.0.0.1:{PORT}  (fast path + Bonsai planner on :8080; Ctrl-C to stop)", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
