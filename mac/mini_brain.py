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
YES = re.compile(r"^(yes|yeah|yep|yup|sure|confirm|confirmed|do it|go ahead|ok|okay|send it|go)( please)?$")
CHAT_APPS = {"Slack", "Messages", "Mail", "Discord", "WhatsApp", "Microsoft Teams", "Telegram", "Signal", "Outlook"}
RISKY = re.compile(r"\b(send|post|reply|forward|delete|trash|empty|erase|remove|quit|shut ?down|restart|log ?out|"
                   r"buy|purchase|pay|order|uninstall|format|unsubscribe|submit)\b")
RISKY_KEYS = {"cmd+q", "cmd+delete", "cmd+backspace", "cmd+shift+delete", "cmd+option+esc", "cmd+shift+q"}


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
        self.pending = None          # {"uid", "say", "actions", "sid", "t", "text"}

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
        if not sp.get("verified"):
            log(f"✗ ignored (voice lock, score {sp.get('score')}): {text!r}")
            return
        t = norm(text)

        # 1. pending confirmation
        if self.pending:
            p, self.pending = self.pending, None
            if time.time() - p["t"] < CONFIRM_TIMEOUT and YES.match(t):
                log(f"✓ {text!r} -> confirmed: {p['say']}")
                emit("plan", uid, {"say": p["say"], "steps": len(p["actions"])})   # overlay follows this utterance
                ok, err, _ = self.execute(uid, p["actions"], p["sid"])
                if ok:
                    emit("done", uid, {}, t_end)
                else:
                    emit("error", uid, {"message": err})
                return
            log(f"   pending '{p['say']}' cancelled by {text!r}")
            emit("done", uid, {"say": "Cancelled"})
            if re.match(r"^(no|nope|cancel|stop|never mind|nevermind|don't)\b", t):
                return

        snap = self.ui.snapshot()
        # 2. fast path
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

        # 3. planner
        log(f"✓ {text!r} -> planner  [in {snap['app']['name']}]")
        emit("route", uid, {"route": "planner"})
        done, error, steps = [], None, 0
        for rnd in range(MAX_ROUNDS):
            try:
                p = planner.plan(text, snap, done, error)
            except Exception as e:                      # server down, timeout, bad JSON
                log(f"     planner failed: {type(e).__name__}: {e}")
                emit("error", uid, {"message": "Planner unavailable (is Bonsai running?)"})
                return
            acts = p["actions"]
            log(f"     plan r{rnd + 1} ({p['ms']} ms, {p['new_prompt_tokens']} new tokens): {p['say']!r} "
                f"{json.dumps(acts, ensure_ascii=False)}")
            if not acts:
                if rnd == 0:
                    emit("route", uid, {"route": "none", "say": p["say"] or "Can't do that"})
                else:
                    emit("done", uid, {}, t_end)
                return
            if needs_confirm(text if rnd == 0 else "", snap, acts):
                return self.ask(uid, p["say"], acts, snap)
            emit("plan", uid, {"say": p["say"], "steps": steps + len(acts)})
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
            if cut < len(acts):
                done.append(f"open_app {acts[cut - 1]['app']}")
                error = None
                continue
            emit("done", uid, {}, t_end)
            log(f"     done {(time.time() - t_end) * 1000:.0f} ms after you stopped talking")
            return
        emit("error", uid, {"message": error or "Gave up after 3 tries"})

    def ask(self, uid, say, acts, snap):
        self.pending = {"uid": uid, "say": say, "actions": acts, "sid": snap["snapshot_id"], "t": time.time()}
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
