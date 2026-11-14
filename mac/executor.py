# /// script
# requires-python = ">=3.11"
# dependencies = ["pyobjc-framework-ApplicationServices", "pyobjc-framework-Cocoa", "pyobjc-framework-Quartz"]
# ///
"""MacExecutor: runs one Plan action (PRD §4.3) on the real Mac UI and reports what changed (§4.4).

    from mac.ui_provider import MacUIProvider
    from mac.executor import MacExecutor
    ui = MacUIProvider(); ex = MacExecutor(ui)
    snap = ui.snapshot()
    res = ex.run(snap["snapshot_id"], {"op": "press", "target": 12})
    # -> {"ok": bool, "changed": bool, "error": str|None, "snapshot": <UISnapshot after>, "ms": int}

    uv run --script mac/executor.py --selftest     # drives Calculator + TextEdit (~25 s, hands off!)

Ops: open_app{app} press{target} menu{target} type{text, target?} keys{keys} scroll{direction, amount?} wait{ms}
- press/menu act on the exact element through Accessibility (AXPress), so they can't land in another app.
  Rows/cells get selected; elements without AXPress fall back to a click at their center.
- type/keys/scroll synthesize input events. They are only sent while the expected app is frontmost
  (re-checked while typing) and NEVER to a terminal app (override: SIRIOUSLY_ALLOW_TERMINAL=1).
  The expected app is the app of the newest snapshot: the one passed in, or the executor's own last result
  (so `open_app Notes` followed by `keys cmd+n` on the plan's original snapshot goes to Notes).
- "changed" compares a signature of the UI before and after (elements, window, focus, text digest, focused
  value/selection), polling until the UI settles (bounded per op). The returned snapshot has fresh ids.
"""
from __future__ import annotations

import difflib
import os
import re
import sys
import time
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import AppKit  # noqa: E402
import ApplicationServices as AX  # noqa: E402
import Quartz  # noqa: E402
from ui_provider import TEXT_INPUT_ROLES, MacUIProvider, _actions, _attr, _frame  # noqa: E402

TERMINAL_BUNDLES = {
    "com.apple.Terminal", "com.googlecode.iterm2", "dev.warp.Warp-Stable", "dev.warp.Warp",
    "com.mitchellh.ghostty", "net.kovidgoyal.kitty", "org.alacritty", "io.alacritty",
    "com.github.wez.wezterm", "co.zeit.hyper", "com.raphaelamorim.rio", "com.termius-dmg.mac",
}
ALLOW_TERMINAL = os.environ.get("SIRIOUSLY_ALLOW_TERMINAL") == "1"

# How long to wait for the UI to settle after each op (seconds).
SETTLE = {"open_app": 2.5, "press": 1.0, "menu": 1.0, "type": 0.6, "keys": 1.0, "scroll": 0.5, "wait": 0.0}
TYPE_DELAY = 0.006          # between typed characters (Electron drops input when faster)
SCROLL_LINES = 3            # lines per scroll unit
MAX_TYPE_CHARS = 2000

AX_ERR_CANNOT_COMPLETE = -25204   # action sent, app busy (e.g. it opened a modal): treat as dispatched
AX_ERR_INVALID_ELEMENT = -25202

# ------------------------------------------------------------------------------------------- key parsing
MOD_FLAGS = {"cmd": Quartz.kCGEventFlagMaskCommand, "shift": Quartz.kCGEventFlagMaskShift,
             "option": Quartz.kCGEventFlagMaskAlternate, "ctrl": Quartz.kCGEventFlagMaskControl,
             "fn": Quartz.kCGEventFlagMaskSecondaryFn}
MOD_ALIASES = {"command": "cmd", "⌘": "cmd", "meta": "cmd", "super": "cmd", "control": "ctrl", "⌃": "ctrl",
               "alt": "option", "opt": "option", "⌥": "option", "⇧": "shift"}
# US ANSI virtual key codes (HIToolbox kVK_*).
KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13,
    "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "return": 36,
    "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47,
    "tab": 48, "space": 49, "`": 50, "delete": 51, "esc": 53, "f17": 64, "f18": 79, "f19": 80, "f20": 90,
    "f5": 96, "f6": 97, "f7": 98, "f3": 99, "f8": 100, "f9": 101, "f11": 103, "f13": 105, "f16": 106,
    "f14": 107, "f10": 109, "f12": 111, "f15": 113, "help": 114, "home": 115, "pageup": 116,
    "forward-delete": 117, "f4": 118, "end": 119, "f2": 120, "pagedown": 121, "f1": 122, "left": 123,
    "right": 124, "down": 125, "up": 126,
}
KEY_ALIASES = {
    "enter": "return", "ret": "return", "↩": "return", "escape": "esc", "backspace": "delete", "del": "delete",
    "⌫": "delete", "forwarddelete": "forward-delete", "fwd-delete": "forward-delete", "⌦": "forward-delete",
    "spacebar": "space", " ": "space", "page-up": "pageup", "pgup": "pageup", "page-down": "pagedown",
    "pgdn": "pagedown", "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
    "↑": "up", "↓": "down", "←": "left", "→": "right", "comma": ",", "period": ".", "dot": ".", "slash": "/",
    "backslash": "\\", "minus": "-", "hyphen": "-", "equal": "=", "equals": "=", "semicolon": ";",
    "quote": "'", "apostrophe": "'", "backtick": "`", "grave": "`", "leftbracket": "[", "rightbracket": "]",
}
SHIFTED = {"+": "=", "plus": "=", "?": "/", "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6",
           "&": "7", "*": "8", "(": "9", ")": "0", "_": "-", ":": ";", '"': "'", "<": ",", ">": ".", "{": "[",
           "}": "]", "|": "\\", "~": "`"}
# Real hardware sets these on navigation keys; some apps look at them.
ARROW_FLAGS = Quartz.kCGEventFlagMaskNumericPad | Quartz.kCGEventFlagMaskSecondaryFn
NAV_FLAGS = {"up": ARROW_FLAGS, "down": ARROW_FLAGS, "left": ARROW_FLAGS, "right": ARROW_FLAGS,
             "home": Quartz.kCGEventFlagMaskSecondaryFn, "end": Quartz.kCGEventFlagMaskSecondaryFn,
             "pageup": Quartz.kCGEventFlagMaskSecondaryFn, "pagedown": Quartz.kCGEventFlagMaskSecondaryFn,
             "forward-delete": Quartz.kCGEventFlagMaskSecondaryFn, "help": Quartz.kCGEventFlagMaskSecondaryFn}


def parse_keys(spec: str) -> tuple[int, int]:
    """'cmd+shift+n' -> (keycode, flags). Also: 'return', 'cmd+,', 'cmd++', 'ctrl+tab', 'cmd+?', 'f5'."""
    s = (spec or "").strip().lower()
    if not s:
        raise ValueError("empty key spec")
    if s.endswith("++"):
        parts = (s[:-2].split("+") if s[:-2] else []) + ["+"]
    elif s == "+":
        parts = ["+"]
    else:
        parts = s.split("+") if "+" in s else s.split()
    parts = [p.strip() for p in parts]
    *mods, key = parts
    flags = 0
    for m in mods:
        m = MOD_ALIASES.get(m, m)
        if m not in MOD_FLAGS:
            raise ValueError(f"unknown modifier {m!r} in {spec!r}")
        flags |= MOD_FLAGS[m]
    key = KEY_ALIASES.get(key, key)
    if key in SHIFTED:
        key, flags = SHIFTED[key], flags | Quartz.kCGEventFlagMaskShift
    if key not in KEYCODES:
        raise ValueError(f"unknown key {key!r} in {spec!r}")
    return KEYCODES[key], flags | NAV_FLAGS.get(key, 0)


def match_app(query: str, apps: list[str]) -> str | None:
    """Best installed/running app for a spoken name: exact > prefix > whole word > fuzzy."""
    q = " ".join((query or "").lower().replace(".app", "").split())
    if not q:
        return None
    q = {"settings": "system settings", "preferences": "system settings",
         "system preferences": "system settings", "chrome": "google chrome", "vscode": "visual studio code",
         "vs code": "visual studio code", "code": "visual studio code"}.get(q, q)
    low = {a.lower(): a for a in apps}
    if q in low:
        return low[q]
    for test in (lambda a: a.startswith(q), lambda a: q in re.split(r"[^a-z0-9]+", a) or f" {q} " in f" {a} "):
        hits = sorted((a for a in low if test(a)), key=len)
        if hits:
            return low[hits[0]]
    close = difflib.get_close_matches(q, list(low), n=1, cutoff=0.72)
    return low[close[0]] if close else None


class ExecError(Exception):
    pass


def _range(v):
    if v is None:
        return None
    ok, r = AX.AXValueGetValue(v, AX.kAXValueCFRangeType, None)
    if not ok:
        return None
    return (r.location, r.length) if hasattr(r, "location") else tuple(r)    # pyobjc may give a tuple


class MacExecutor:
    def __init__(self, ui: MacUIProvider):
        self.ui = ui
        self._last: dict | None = None          # snapshot returned by the previous run()
        self._moved = False                      # set by scroll when a scrollbar actually moved
        self._src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStatePrivate)

    # --------------------------------------------------------------------------------------- public API
    def run(self, snapshot_id: str | None, action: dict) -> dict:
        t0 = time.perf_counter()
        op = (action or {}).get("op")
        before = self.ui.get_snapshot(snapshot_id) if snapshot_id else None
        ref = max((s for s in (before, self._last) if s), key=lambda s: s["ts"], default=None)
        expect = ref["app"] if ref else self._front_info()
        try:
            pre = self._pre_signature(ref)
        except Exception:
            pre = None                                      # unknown "before": any result counts as changed
        need_window = False
        self._moved = False
        try:
            if op == "open_app":
                name = match_app(action.get("app", ""), self.ui.list_apps())
                if not name:
                    raise ExecError(f"no installed app matches {action.get('app')!r}")
                if not MacUIProvider.activate(name):
                    raise ExecError(f"could not bring {name} to the front")
                need_window = True
            elif op in ("press", "menu"):
                el, item = self._element(snapshot_id, before, action.get("target"))
                if op == "menu" or item.get("kind") == "menu":
                    self._press_menu(el)
                else:
                    self._press(el, expect)
            elif op == "type":
                text = action.get("text")
                if not isinstance(text, str) or not text:
                    raise ExecError("type needs non-empty text")
                if len(text) > MAX_TYPE_CHARS:
                    raise ExecError(f"text longer than {MAX_TYPE_CHARS} characters")
                if action.get("target") is not None:
                    el, _ = self._element(snapshot_id, before, action.get("target"))
                    self._focus(el, expect)
                self._type(text, expect)
            elif op == "keys":
                try:
                    code, flags = parse_keys(action.get("keys", ""))
                except ValueError as e:
                    raise ExecError(str(e)) from None
                self._guard(expect)
                self._tap(code, flags)
            elif op == "scroll":
                self._scroll(action.get("direction", "down"), action.get("amount", 5), expect)
            elif op == "wait":
                time.sleep(min(max(float(action.get("ms", 500)), 0), 10_000) / 1000)
            else:
                raise ExecError(f"unknown op {op!r}")
            ok, error = True, None
        except ExecError as e:
            ok, error = False, str(e)
        except Exception as e:                              # never let one action kill the brain
            ok, error = False, f"{type(e).__name__}: {e}"
        snap, changed = self._settle(pre, 0.0 if not ok or self._moved else SETTLE.get(op, 0.8), need_window)
        changed = changed or self._moved
        self._last = snap
        return {"ok": ok, "changed": changed if ok else False, "error": error, "snapshot": snap,
                "ms": round((time.perf_counter() - t0) * 1000)}

    def undo(self) -> bool:
        """Best effort: cmd+z in the frontmost app (never a terminal)."""
        try:
            info = self._front_info()
            self._guard(info)
            self._tap(*parse_keys("cmd+z"))
            return True
        except ExecError:
            return False

    # ------------------------------------------------------------------------------------- targets
    def _element(self, snapshot_id, before, target):
        if target is None:
            raise ExecError("missing target id")
        if before is None:
            raise ExecError(f"snapshot {snapshot_id!r} unknown or expired; take a new snapshot")
        try:
            target = int(target)
        except (TypeError, ValueError):
            raise ExecError(f"target must be an item id, got {target!r}") from None
        el = self.ui.element_ref(snapshot_id, target)
        item = next((i for i in before["items"] if i["id"] == target), None)
        if el is None or item is None:
            raise ExecError(f"no item {target} in snapshot {snapshot_id}")
        err, _ = AX.AXUIElementCopyAttributeValue(el, "AXRole", None)
        if err == AX_ERR_INVALID_ELEMENT:
            raise ExecError(f"item {target} ({item.get('label')!r}) no longer exists")
        return el, item

    def _press(self, el, expect):
        role = _attr(el, "AXRole")
        if role in TEXT_INPUT_ROLES:
            return self._focus(el, expect)
        acts = _actions(el)
        if "AXPress" in acts:
            AX.AXUIElementSetMessagingTimeout(el, 2.0)
            err = AX.AXUIElementPerformAction(el, "AXPress")
            if err in (0, AX_ERR_CANNOT_COMPLETE):
                return
            raise ExecError(f"AXPress failed ({err})")
        if role in ("AXRow", "AXCell"):
            row = el if role == "AXRow" else (_attr(el, "AXParent") or el)
            if AX.AXUIElementSetAttributeValue(row, "AXSelected", True) == 0 and _attr(row, "AXSelected"):
                return
        for a in ("AXPick", "AXConfirm", "AXOpen"):
            if a in acts and AX.AXUIElementPerformAction(el, a) in (0, AX_ERR_CANNOT_COMPLETE):
                return
        self._click_center(el, expect)

    def _press_menu(self, el):
        if _attr(el, "AXEnabled") is False:
            raise ExecError("menu item is disabled right now")
        AX.AXUIElementSetMessagingTimeout(el, 2.0)
        err = AX.AXUIElementPerformAction(el, "AXPress")
        if err not in (0, AX_ERR_CANNOT_COMPLETE):
            raise ExecError(f"menu press failed ({err})")

    def _focus(self, el, expect):
        AX.AXUIElementSetAttributeValue(el, "AXFocused", True)
        time.sleep(0.05)
        if _attr(el, "AXFocused"):
            return
        self._click_center(el, expect)
        time.sleep(0.05)

    # ------------------------------------------------------------------------------------- input events
    def _front_info(self) -> dict:
        front = MacUIProvider.frontmost_app()
        if front is None:
            raise ExecError("no frontmost app")
        return {"name": front.localizedName(), "bundle_id": front.bundleIdentifier(),
                "pid": front.processIdentifier()}

    def _guard(self, expect):
        """Keystrokes go to whatever is frontmost, so only send them to the app the plan is about."""
        if expect is None:
            raise ExecError("no target app")
        if expect.get("bundle_id") in TERMINAL_BUNDLES and not ALLOW_TERMINAL:
            raise ExecError(f"refusing to send input to {expect['name']} (a terminal)")
        front = MacUIProvider.frontmost_app()
        if front is None or front.processIdentifier() != expect["pid"]:
            name = front.localizedName() if front is not None else "another app"
            raise ExecError(f"{expect['name']} is not in front ({name} is); input not sent")
        if front.bundleIdentifier() in TERMINAL_BUNDLES and not ALLOW_TERMINAL:
            raise ExecError(f"refusing to send input to {front.localizedName()} (a terminal)")

    def _post(self, code, down, flags, text=None):
        ev = Quartz.CGEventCreateKeyboardEvent(self._src, code, down)
        Quartz.CGEventSetFlags(ev, flags)
        if text is not None:
            Quartz.CGEventKeyboardSetUnicodeString(ev, len(text.encode("utf-16-le")) // 2, text)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

    def _tap(self, code, flags=0):
        self._post(code, True, flags)
        self._post(code, False, flags)

    def _type(self, text, expect):
        self._guard(expect)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        for i, ch in enumerate(text):
            if i and i % 8 == 0:
                self._guard(expect)                 # stop if the user switched apps mid-typing
            if ch == "\n":
                self._tap(KEYCODES["return"])
            elif ch == "\t":
                self._tap(KEYCODES["tab"])
            else:
                self._post(0, True, 0, ch)
                self._post(0, False, 0, ch)
            time.sleep(TYPE_DELAY)

    def _click_center(self, el, expect):
        fr = _frame(el)
        if not fr:
            raise ExecError("element has no on-screen frame to click")
        self._guard(expect)
        x, y = fr[0] + fr[2] / 2, fr[1] + fr[3] / 2
        orig = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        for t in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
            ev = Quartz.CGEventCreateMouseEvent(self._src, t, (x, y), Quartz.kCGMouseButtonLeft)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.03)
        time.sleep(0.05)
        Quartz.CGWarpMouseCursorPosition(orig)

    def _scroll(self, direction, amount, expect):
        direction = str(direction).lower()
        if direction not in ("up", "down", "left", "right"):
            raise ExecError(f"scroll direction must be up/down/left/right, got {direction!r}")
        try:
            amount = max(1, min(int(amount), 30))
        except (TypeError, ValueError):
            amount = 5
        self._guard(expect)
        x, y, area = self._scroll_point(expect["pid"])
        bars = lambda: tuple(_attr(_attr(area, b), "AXValue") if area is not None and _attr(area, b) is not None  # noqa: E731
                             else None for b in ("AXVerticalScrollBar", "AXHorizontalScrollBar"))
        pos0 = bars()
        dy = {"up": SCROLL_LINES, "down": -SCROLL_LINES}.get(direction, 0)
        dx = {"left": SCROLL_LINES, "right": -SCROLL_LINES}.get(direction, 0)
        orig = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        Quartz.CGWarpMouseCursorPosition((x, y))           # wheel events go to the window under the cursor
        time.sleep(0.02)
        try:
            for _ in range(amount):
                try:
                    ev = Quartz.CGEventCreateScrollWheelEvent(self._src, Quartz.kCGScrollEventUnitLine, 2, dy, dx)
                except TypeError:
                    ev = Quartz.CGEventCreateScrollWheelEvent2(self._src, Quartz.kCGScrollEventUnitLine, 2, dy, dx, 0)
                Quartz.CGEventSetLocation(ev, (x, y))
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                time.sleep(0.015)
            time.sleep(0.05)
        finally:
            Quartz.CGWarpMouseCursorPosition(orig)
        self._moved = bars() != pos0

    def _scroll_point(self, pid):
        """Center of the largest scroll area in the focused window (else the window center)."""
        app = AX.AXUIElementCreateApplication(pid)
        win = _attr(app, "AXFocusedWindow") or _attr(app, "AXMainWindow")
        if win is None:
            raise ExecError("no window to scroll")
        wf = _frame(win)
        best, best_el, best_area, seen = None, None, 0, 0
        q = deque([win])
        while q and seen < 1500:
            el = q.popleft()
            seen += 1
            if _attr(el, "AXRole") == "AXScrollArea":
                fr = _frame(el)
                if fr and fr[2] * fr[3] > best_area:
                    best, best_el, best_area = fr, el, fr[2] * fr[3]
            q.extend(_attr(el, "AXChildren") or [])
        fr = best or wf
        if not fr:
            raise ExecError("window has no frame")
        return fr[0] + fr[2] / 2, fr[1] + fr[3] / 2, best_el

    # ------------------------------------------------------------------------------- change detection
    @staticmethod
    def _probe(pid):
        """Live state not fully in the snapshot: focused value (full length) and selection, window count."""
        app = AX.AXUIElementCreateApplication(pid)
        f = _attr(app, "AXFocusedUIElement")
        val = _attr(f, "AXValue") if f is not None else None
        sel = _range(_attr(f, "AXSelectedTextRange")) if f is not None else None
        val = hash(val) if isinstance(val, str) else (val if isinstance(val, (int, float, bool)) else None)
        return len(_attr(app, "AXWindows") or []), val, sel

    def _signature(self, snap):
        els = tuple((i["role"], i["label"], i.get("value"), i["enabled"], tuple(i["frame"])) for i in snap["items"]
                    if i["kind"] == "element")
        return (snap["app"]["pid"], snap["window"], snap["focused_id"], snap.get("digest"), els,
                self._probe(snap["app"]["pid"]))

    def _pre_signature(self, ref):
        front = MacUIProvider.frontmost_app()
        if ref is not None and front is not None and ref["app"]["pid"] == front.processIdentifier() \
                and time.time() - ref["ts"] < 3.0:
            return self._signature(ref)
        snap = self.ui.snapshot()
        self.ui.forget(snap["snapshot_id"])
        return self._signature(snap)

    def _settle(self, pre, timeout, need_window=False):
        """Poll until the UI differs from `pre` and then holds still, or the timeout passes."""
        t0, changed, last, snap = time.time(), False, None, None
        while True:
            time.sleep(0.08 if changed else 0.1)
            if snap is not None:
                self.ui.forget(snap["snapshot_id"])
            snap = self.ui.snapshot()
            sig = self._signature(snap)
            changed = changed or sig != pre
            if changed and sig == last and (snap["window"] is not None or not need_window):
                break
            last = sig
            if time.time() - t0 >= timeout:
                break
        return snap, changed


# ----------------------------------------------------------------------------------------- self-test
def _find(snap, *labels, kind="element", role=None):
    for lbl in labels:
        for i in snap["items"]:
            if i["kind"] == kind and i["label"] == lbl and (role is None or i["role"] == role):
                return i
    return None


def _calc_display(pid):
    app = AX.AXUIElementCreateApplication(pid)
    q, texts, inp = deque([_attr(app, "AXFocusedWindow")]), [], None
    while q:
        el = q.popleft()
        if el is None:
            continue
        if _attr(el, "AXIdentifier") == "StandardInputView":
            inp = el
        if _attr(el, "AXRole") == "AXStaticText":
            texts.append((el, str(_attr(el, "AXValue") or "")))
        q.extend(_attr(el, "AXChildren") or [])
    clean = lambda s: s.replace("‎", "").replace(",", "").strip()  # noqa: E731
    if inp is not None:
        q, vals = deque([inp]), []
        while q:
            el = q.popleft()
            if _attr(el, "AXRole") == "AXStaticText":
                vals.append(str(_attr(el, "AXValue") or ""))
            q.extend(_attr(el, "AXChildren") or [])
        if vals:
            return clean(vals[-1])
    return clean(texts[-1][1]) if texts else None


def selftest():
    print("MacExecutor self-test: drives Calculator and TextEdit for ~25 s.")
    print("Hands OFF the keyboard and mouse until it prints the summary.", flush=True)
    for c in (3, 2, 1):
        print(f"  {c}...", flush=True)
        time.sleep(1)
    ui = MacUIProvider()
    ex = MacExecutor(ui)
    origin = MacUIProvider.frontmost_app()
    running = {a.localizedName() for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()}
    results = []

    def check(name, cond, detail=""):
        results.append((name, bool(cond)))
        print(f"  {'PASS' if cond else 'FAIL'}  {name:<44} {detail}", flush=True)

    def run(snap, action):
        r = ex.run(snap["snapshot_id"] if snap else None, action)
        tag = "ok" if r["ok"] else f"ERR {r['error']}"
        print(f"        {str(action)[:70]:<72} {tag:<4} changed={r['changed']!s:<5} {r['ms']} ms", flush=True)
        return r

    try:
        # 0. safety: the guard refuses terminals and apps that are not in front
        term = {"name": "Terminal", "bundle_id": "com.apple.Terminal", "pid": -1}
        try:
            ex._guard(term)
            check("guard refuses terminal apps", False)
        except ExecError as e:
            check("guard refuses terminal apps", "terminal" in str(e), str(e))
        check("parse_keys", parse_keys("cmd+shift+n") == (45, MOD_FLAGS["cmd"] | MOD_FLAGS["shift"])
              and parse_keys("cmd++")[0] == 24 and parse_keys("Return") == (36, 0)
              and match_app("settings", ui.list_apps()) == "System Settings")

        # 1. Calculator: open_app (fuzzy), press buttons, keys, type
        r = run(None, {"op": "open_app", "app": "calculator"})
        s = r["snapshot"]
        check("open_app 'calculator'", r["ok"] and s["app"]["name"] == "Calculator", f"{r['ms']} ms")
        pid = s["app"]["pid"]
        r = run(s, {"op": "keys", "keys": "esc"})
        s = r["snapshot"]
        for lbl in ("7", "Multiply", "6", "Equals"):
            it = _find(s, lbl)
            if it is None:
                check(f"find Calculator button {lbl!r}", False)
                break
            r = run(s, {"op": "press", "target": it["id"]})
            s = r["snapshot"]
        d = _calc_display(pid)
        check("press 7 × 6 = (AXPress)", d == "42", f"display={d!r}")
        check("press reports changed", r["changed"])
        r = run(s, {"op": "keys", "keys": "esc"})
        d = _calc_display(pid)
        check("keys esc clears", d == "0", f"display={d!r}")
        r = run(r["snapshot"], {"op": "type", "text": "123+321="})
        d = _calc_display(pid)
        check("type '123+321='", d == "444", f"display={d!r}")
        calc_snap = r["snapshot"]

        # 2. guard: user switches away -> keystrokes for Calculator must not be sent
        MacUIProvider.activate("Finder")
        time.sleep(0.3)
        r = run(calc_snap, {"op": "keys", "keys": "esc"})
        check("keys refused when app not in front", not r["ok"] and "not in front" in (r["error"] or ""),
              r["error"] or "")

        # 3. TextEdit: press New Document, type into target, menu Select All, arrows, scroll, undo, close
        r = run(None, {"op": "open_app", "app": "textedit"})
        s = r["snapshot"]
        check("open_app 'textedit'", r["ok"] and s["app"]["name"] == "TextEdit")
        te_pid = s["app"]["pid"]
        nd = _find(s, "New Document")
        r = run(s, {"op": "press", "target": nd["id"]}) if nd else run(s, {"op": "keys", "keys": "cmd+n"})
        s = r["snapshot"]
        area = next((i for i in s["items"] if i["kind"] == "element" and i["role"] == "textarea"), None)
        check("new document window", r["changed"] and area is not None, f"window={s['window']!r}")
        text = "Hello from the executor.\nCafé, naïve, ✓ 42"
        r = run(s, {"op": "type", "text": text, "target": area["id"]})
        s = r["snapshot"]
        area_el = ui.element_ref(s["snapshot_id"], next(i["id"] for i in s["items"] if i["role"] == "textarea"))
        val = _attr(area_el, "AXValue")
        check("type into target text area", val == text, f"{len(val or '')}/{len(text)} chars")
        sel_all = _find(s, "Edit › Select All", kind="menu")
        r = run(s, {"op": "menu", "target": sel_all["id"]})
        rng = _range(_attr(area_el, "AXSelectedTextRange"))
        check("menu Edit › Select All", rng == (0, len(text)), f"selection={rng}")
        r = run(r["snapshot"], {"op": "keys", "keys": "right"})
        rng = _range(_attr(area_el, "AXSelectedTextRange"))
        check("keys right (collapses selection)", rng == (len(text), 0), f"selection={rng}")
        r = run(r["snapshot"], {"op": "type", "text": "\n" + "\n".join(f"line {i}" for i in range(1, 61))})
        sb = _attr(_attr(area_el, "AXParent"), "AXVerticalScrollBar")
        v0 = _attr(sb, "AXValue") if sb is not None else None
        r = run(r["snapshot"], {"op": "scroll", "direction": "up", "amount": 6})
        v1 = _attr(sb, "AXValue") if sb is not None else None
        check("scroll up", v0 is not None and v1 is not None and v1 < v0, f"scrollbar {v0} -> {v1}")
        r = run(r["snapshot"], {"op": "scroll", "direction": "down", "amount": 3})
        v2 = _attr(sb, "AXValue") if sb is not None else None
        check("scroll down", v1 is not None and v2 is not None and v2 > v1, f"scrollbar {v1} -> {v2}")
        before_val = _attr(area_el, "AXValue") or ""
        ok = ex.undo()                       # first undo may revert an auto-capitalization, not the typing
        time.sleep(0.3)
        after_val = _attr(area_el, "AXValue") or ""
        check("undo() (cmd+z)", ok and after_val != before_val,
              f"{len(before_val)} -> {len(after_val)} chars, text changed={after_val != before_val}")
        r = run(r["snapshot"], {"op": "keys", "keys": "cmd+w"})
        s = r["snapshot"]
        btn = _find(s, "Delete", "Don’t Save", "Don't Save", role="button")
        check("cmd+w shows save sheet", btn is not None, f"buttons={[i['label'] for i in s['items'] if i.get('role') == 'button'][:8]}")
        if btn:
            r = run(s, {"op": "press", "target": btn["id"]})
            gone = all(_attr(w, "AXTitle") != s["window"]
                       for w in (_attr(AX.AXUIElementCreateApplication(te_pid), "AXWindows") or []))
            check("press Delete closes the document", r["ok"] and gone)
    finally:
        if "TextEdit" not in running:          # all TextEdit documents are ours: discard them
            import subprocess
            subprocess.run(["osascript", "-e", 'tell application "TextEdit" to close every document saving no'],
                           capture_output=True)
        for name, bid in (("TextEdit", "com.apple.TextEdit"), ("Calculator", "com.apple.calculator")):
            if name not in running:      # by bundle id: the NSWorkspace list is stale off the main run loop
                for a in AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bid):
                    a.terminate()
        if origin is not None:
            MacUIProvider.activate(origin.localizedName())
    passed = sum(ok for _, ok in results)
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    print(__doc__)
