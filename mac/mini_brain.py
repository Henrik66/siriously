# /// script
# requires-python = ">=3.11"
# dependencies = ["pyobjc-framework-ApplicationServices", "pyobjc-framework-Cocoa", "pyobjc-framework-Quartz"]
# ///
"""Mini brain: fast-path-only stand-in for the real brain (PRD §5.2), to test the Mac side end to end.
The real brain (router + Bonsai planner) is the teammate's; this only exists so voice -> action works today.

    uv run --script mac/mini_brain.py            # 127.0.0.1:8765, then: mac/ear/.build/release/siriously-ear run

Handles: open/launch/switch to <app> · scroll up/down · undo · new tab / close tab / next tab / previous tab ·
close window · copy / paste / select all · "new note called X" · "type X" · "click/press <label>" ·
menu items by name ("new folder", "show sidebar"). Anything else is logged as "-> planner (not in mini brain)".
Rejected voices are ignored. Keystrokes are never sent to a terminal (executor guard), so switch to a real app first.
"""
import difflib
import json
import os
import queue
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from executor import MacExecutor  # noqa: E402
from ui_provider import MacUIProvider  # noqa: E402

PORT = 8765
KEYS = {"new tab": "cmd+t", "close tab": "cmd+w", "close window": "cmd+w", "close this window": "cmd+w",
        "next tab": "ctrl+tab", "previous tab": "ctrl+shift+tab", "copy": "cmd+c", "copy that": "cmd+c",
        "paste": "cmd+v", "paste that": "cmd+v", "select all": "cmd+a", "save": "cmd+s", "zoom in": "cmd+=",
        "zoom out": "cmd+-", "go back": "cmd+[", "go forward": "cmd+]", "new window": "cmd+n",
        "press enter": "return", "press return": "return", "enter": "return", "escape": "esc", "cancel": "esc",
        "find": "cmd+f", "search": "cmd+f"}
FILLER = re.compile(r"^(hey |ok |okay )?(siri(ously)?[, ]+)?(please |can you |could you )?")


def norm(text):
    t = text.lower().strip()
    t = re.sub(r"[^\w\s']", " ", t)
    t = " ".join(t.split())
    t = FILLER.sub("", t)
    return re.sub(r"( please| for me| now)$", "", t).strip()


def best(query, labels, cutoff):
    m = difflib.get_close_matches(query, labels, n=1, cutoff=cutoff)
    return m[0] if m else None


def plan(t, snap):
    """Return (description, [actions]) or (None, None)."""
    k = re.sub(r"^(?:go to |switch to |open |do )?(?:the |a )?", "", t)
    if k in KEYS:
        return k, [{"op": "keys", "keys": KEYS[k]}]
    if m := re.match(r"^(?:open|launch|start|switch to|go to|bring up|show)\s+(?:the\s+)?(?:app\s+)?(.+?)(?:\s+app)?$", t):
        target = m.group(1)
        if not re.match(r"^(a |an |new |my )", target):
            return f"open {target}", [{"op": "open_app", "app": target}]
    if m := re.match(r"^scroll\s+(up|down)(?:\s+(a bit|a little|a lot|more))?", t):
        amt = {"a bit": 2, "a little": 2, "a lot": 12, "more": 8}.get(m.group(2) or "", 5)
        return f"scroll {m.group(1)}", [{"op": "scroll", "direction": m.group(1), "amount": amt}]
    if re.match(r"^undo( that| it)?$", t):
        return "undo", [{"op": "keys", "keys": "cmd+z"}]
    if m := re.match(r"^(?:start |create |make )?(?:a )?new note(?: called| named| titled)?\s*(.*)$", t):
        title = m.group(1).strip()
        acts = [{"op": "open_app", "app": "Notes"}, {"op": "keys", "keys": "cmd+n"}]   # executor settles; no wait
        if title:
            acts.append({"op": "type", "text": title})
        return f"new note {title!r}", acts
    if m := re.match(r"^(?:type|write|dictate)\s+(.+)$", t):
        return "type", [{"op": "type", "text": m.group(1)}]
    els = {i["label"].lower(): i for i in snap["items"] if i["kind"] == "element" and i["label"]}
    if m := re.match(r"^(?:click|press|tap|hit|select)\s+(?:on\s+)?(?:the\s+)?(.+?)(?:\s+button)?$", t):
        lbl = best(m.group(1), list(els), 0.6)
        if lbl:
            return f"press {els[lbl]['label']!r}", [{"op": "press", "target": els[lbl]["id"]}]
        t = m.group(1)                                   # fall through: maybe it's a menu item
    menus = {i["path"][-1].lower(): i for i in snap["items"] if i["kind"] == "menu" and i["enabled"]}
    lbl = best(t, list(menus), 0.85)
    if lbl:
        return f"menu {menus[lbl]['label']}", [{"op": "menu", "target": menus[lbl]["id"]}]
    return None, None


def worker(q):
    ui = MacUIProvider()
    ex = MacExecutor(ui)
    while True:
        u = q.get()
        sp = u.get("speaker", {})
        heard = time.time()
        lag = (heard - u.get("t_speech_end", heard)) * 1000
        if not sp.get("verified"):
            print(f"{time.strftime('%H:%M:%S')} ✗ ignored (voice lock, score {sp.get('score')}): {u.get('text')!r}", flush=True)
            continue
        t = norm(u.get("text", ""))
        snap = ui.snapshot()
        desc, acts = plan(t, snap)
        if not acts:
            print(f"{time.strftime('%H:%M:%S')} ✓ {u.get('text')!r} -> planner (not in mini brain)  [in {snap['app']['name']}]", flush=True)
            continue
        print(f"{time.strftime('%H:%M:%S')} ✓ {u.get('text')!r} -> {desc}  [in {snap['app']['name']}]", flush=True)
        sid = snap["snapshot_id"]
        for a in acts:
            r = ex.run(sid, a)
            sid = r["snapshot"]["snapshot_id"]
            status = "ok" if r["ok"] else f"ERROR {r['error']}"
            print(f"     {json.dumps(a, ensure_ascii=False):<52} {status}  changed={r['changed']}  {r['ms']} ms", flush=True)
            if not r["ok"]:
                break
        total = (time.time() - u.get("t_speech_end", heard)) * 1000
        print(f"     done {total:.0f} ms after you stopped talking (daemon delivered at {lag:.0f} ms)", flush=True)


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

    print(f"mini brain on http://127.0.0.1:{PORT}  (fast path only; Ctrl-C to stop)", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
