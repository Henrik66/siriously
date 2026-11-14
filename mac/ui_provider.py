# /// script
# requires-python = ">=3.11"
# dependencies = ["pyobjc-framework-ApplicationServices", "pyobjc-framework-Cocoa"]
# ///
"""MacUIProvider: snapshot of the frontmost app's actionable UI (PRD §4.2), via the macOS Accessibility API.

    uv run --script mac/ui_provider.py            # print a snapshot of the frontmost app (after 3 s)
    from mac.ui_provider import MacUIProvider     # used by the brain on the Mac

Requires: System Settings → Privacy & Security → Accessibility → the host terminal app enabled.
Item ids are scoped to one snapshot_id; `element_ref(snapshot_id, id)` returns the AXUIElement for the executor.
"""
from __future__ import annotations

import hashlib
import os
import sys
import time
import uuid
from collections import OrderedDict, deque

import AppKit
import ApplicationServices as AX
from CoreFoundation import CFEqual

# Roles worth offering to the planner (pressable, typeable, or a selectable list entry).
ACTIONABLE_ROLES = {
    "AXButton", "AXCheckBox", "AXRadioButton", "AXPopUpButton", "AXMenuButton", "AXTextField", "AXTextArea",
    "AXComboBox", "AXLink", "AXSlider", "AXDisclosureTriangle", "AXIncrementor", "AXCell", "AXRow",
    "AXColorWell", "AXSegmentedControl",
}
TEXT_INPUT_ROLES = {"AXTextField", "AXTextArea", "AXComboBox"}
# Window chrome buttons: label them by subrole (their AXHelp text is useless).
WINDOW_BUTTON_LABELS = {"AXCloseButton": "Close window", "AXMinimizeButton": "Minimize window",
                        "AXZoomButton": "Zoom window", "AXFullScreenButton": "Full screen"}
# Menu key equivalents that are not printable characters.
SPECIAL_KEYS = {"\x08": "delete", "\x7f": "delete", "\r": "return", "\x03": "enter", "\x1b": "esc",
                "\t": "tab", " ": "space", "\uf700": "up", "\uf701": "down", "\uf702": "left",
                "\uf703": "right", "\uf728": "forward-delete", "\uf729": "home", "\uf72b": "end",
                "\uf72c": "pageup", "\uf72d": "pagedown"}
SKIP_MENU_TOPS = {"Services"}
SUBROLE_NAMES = {"AXSearchField": "searchfield", "AXSwitch": "switch", "AXSecureTextField": "securefield",
                 "AXTabButton": "tab", "AXToggle": "toggle"}
# Menu item modifier bits (HIToolbox kMenu*Modifier).
MOD_SHIFT, MOD_OPTION, MOD_CONTROL, MOD_NO_COMMAND = 1, 2, 4, 8

MAX_VISIT = int(os.environ.get("SIRIOUSLY_AX_MAX_VISIT", 4000))
MAX_ELEMENTS = int(os.environ.get("SIRIOUSLY_AX_MAX_ELEMENTS", 200))
MAX_MENU_ITEMS = int(os.environ.get("SIRIOUSLY_AX_MAX_MENU_ITEMS", 400))
LABEL_MAX = 60
MAX_DIGEST_TEXTS = 400      # static texts hashed into the snapshot digest (change detection)


def _attr(el, name):
    err, val = AX.AXUIElementCopyAttributeValue(el, name, None)
    return val if err == 0 else None


def _actions(el):
    err, val = AX.AXUIElementCopyActionNames(el, None)
    return list(val) if err == 0 and val else []


def _frame(el):
    pos, size = _attr(el, "AXPosition"), _attr(el, "AXSize")
    if pos is None or size is None:
        return None
    ok1, pt = AX.AXValueGetValue(pos, AX.kAXValueCGPointType, None)
    ok2, sz = AX.AXValueGetValue(size, AX.kAXValueCGSizeType, None)
    if not (ok1 and ok2):
        return None
    return [round(pt.x), round(pt.y), round(sz.width), round(sz.height)]


def _intersects(a, b):
    return a and b and a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def _clean(s):
    if not isinstance(s, str):
        return None
    s = " ".join(s.split())
    return (s[: LABEL_MAX - 1] + "…") if len(s) > LABEL_MAX else (s or None)


def _text_below(el, depth=3):
    """First static-text value under an element (labels for rows/cells/icon groups)."""
    q = deque([(el, 0)])
    while q:
        node, d = q.popleft()
        if node is not el:
            role = _attr(node, "AXRole")
            if role in ("AXStaticText", "AXTextField"):
                v = _clean(_attr(node, "AXValue")) or _clean(_attr(node, "AXTitle"))
                if v:
                    return v
            for a in ("AXTitle", "AXDescription"):
                v = _clean(_attr(node, a))
                if v:
                    return v
        if d < depth:
            for ch in (_attr(node, "AXChildren") or [])[:12]:
                q.append((ch, d + 1))
    return None


def _label(el, role):
    sub = _attr(el, "AXSubrole")
    if sub in WINDOW_BUTTON_LABELS:
        return WINDOW_BUTTON_LABELS[sub]
    for a in ("AXTitle", "AXDescription", "AXPlaceholderValue"):
        v = _clean(_attr(el, a))
        if v:
            return v
    t = _attr(el, "AXTitleUIElement")
    if t is not None:
        v = _clean(_attr(t, "AXValue")) or _clean(_attr(t, "AXTitle"))
        if v:
            return v
    if role in ("AXRow", "AXCell", "AXLink", "AXButton", "AXRadioButton", "AXCheckBox", "AXPopUpButton", "AXMenuButton"):
        v = _text_below(el)
        if v:
            return v
    return _clean(_attr(el, "AXHelp"))


def _shortcut(item):
    char = _attr(item, "AXMenuItemCmdChar")
    if not isinstance(char, str) or not char:
        return None
    key = SPECIAL_KEYS.get(char) or (char.strip().lower() if char.strip() and char.isprintable() else None)
    if key is None:
        return None
    mods = _attr(item, "AXMenuItemCmdModifiers") or 0
    parts = []
    if mods & MOD_CONTROL:
        parts.append("ctrl")
    if mods & MOD_OPTION:
        parts.append("option")
    if mods & MOD_SHIFT:
        parts.append("shift")
    if not mods & MOD_NO_COMMAND:
        parts.append("cmd")
    parts.append(key)
    return "+".join(parts)


class MacUIProvider:
    def __init__(self, keep_snapshots: int = 16):
        if not AX.AXIsProcessTrusted():
            raise PermissionError("Accessibility access missing: System Settings → Privacy & Security → "
                                  "Accessibility → enable your terminal app.")
        self._refs: OrderedDict[str, dict[int, object]] = OrderedDict()
        self._snaps: OrderedDict[str, dict] = OrderedDict()
        self._keep = keep_snapshots
        self._electron_enabled: set[int] = set()

    # ---------------------------------------------------------------- public API (PRD §4.5)
    @staticmethod
    def frontmost_app():
        """Live frontmost app. NSWorkspace.frontmostApplication() goes stale in a CLI process without a
        run loop, so ask the Accessibility API (system-wide focused application) instead."""
        focused_app = _attr(AX.AXUIElementCreateSystemWide(), "AXFocusedApplication")
        if focused_app is not None:
            err, pid = AX.AXUIElementGetPid(focused_app, None)
            if err == 0:
                app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
                if app is not None:
                    return app
        return AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()

    @staticmethod
    def _bundle_id(name: str) -> str | None:
        path = AppKit.NSWorkspace.sharedWorkspace().fullPathForApplication_(name)
        b = AppKit.NSBundle.bundleWithPath_(path) if path else None
        return b.bundleIdentifier() if b is not None else None

    @staticmethod
    def activate(name: str, timeout: float = 8.0) -> bool:
        """Launch (if needed) and bring an app to the front. `open -a` alone is not enough: macOS'
        cooperative activation ignores activation requests from a background process, so we raise the
        app through the Accessibility API (AXFrontmost), which trusted processes may do.
        The pid is looked up by bundle id: NSWorkspace.runningApplications() is a KVO snapshot that only
        refreshes on the main run loop, so off the main thread it never shows a freshly launched app."""
        import subprocess
        subprocess.run(["open", "-a", name], check=True, capture_output=True)
        bid = MacUIProvider._bundle_id(name)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if bid:
                apps = list(AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bid))
            else:
                AppKit.NSRunLoop.currentRunLoop().runUntilDate_(AppKit.NSDate.dateWithTimeIntervalSinceNow_(0.05))
                apps = [a for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()
                        if a.localizedName() == name]
            if apps:
                pid = apps[0].processIdentifier()
                AX.AXUIElementSetAttributeValue(AX.AXUIElementCreateApplication(pid), "AXFrontmost", True)
                time.sleep(0.15)
                front = MacUIProvider.frontmost_app()
                if front is not None and front.processIdentifier() == pid:
                    return True
            time.sleep(0.1)
        return False

    def snapshot(self, include_menus: bool = True) -> dict:
        t0 = time.perf_counter()
        app = self.frontmost_app()
        pid = app.processIdentifier()
        app_el = AX.AXUIElementCreateApplication(pid)
        self._enable_electron_ax(app_el, pid)

        window = _attr(app_el, "AXFocusedWindow") or _attr(app_el, "AXMainWindow") or \
            next(iter(_attr(app_el, "AXWindows") or []), None)
        focused = _attr(app_el, "AXFocusedUIElement")
        snapshot_id = f"s-{uuid.uuid4().hex[:8]}"
        refs: dict[int, object] = {}
        items: list[dict] = []
        next_id = 1
        visited = 0
        truncated = False
        focused_id = None
        digest, n_texts = hashlib.blake2b(digest_size=8), 0

        if window is not None:
            wframe = _frame(window)
            q = deque([window])
            while q:
                el = q.popleft()
                visited += 1
                if visited > MAX_VISIT or len(items) >= MAX_ELEMENTS:
                    truncated = True
                    break
                role = _attr(el, "AXRole")
                if role == "AXStaticText" and n_texts < MAX_DIGEST_TEXTS:
                    v = _attr(el, "AXValue")            # content the planner doesn't see, e.g. a result or a
                    if isinstance(v, str):              # new chat message; lets the executor detect changes
                        digest.update(v.encode("utf-8", "replace") + b"\0")
                        n_texts += 1
                if role in ACTIONABLE_ROLES or (role and "AXPress" in _actions(el) and role != "AXWindow"):
                    fr = _frame(el)
                    if fr and fr[2] > 1 and fr[3] > 1 and (wframe is None or _intersects(fr, wframe)) \
                            and not _attr(el, "AXHidden"):
                        label = _label(el, role)
                        is_input = role in TEXT_INPUT_ROLES
                        if label or is_input:
                            sub = _attr(el, "AXSubrole")
                            item = {"id": next_id, "kind": "element",
                                    "role": SUBROLE_NAMES.get(sub) or role[2:].lower(),
                                    "label": label or "", "enabled": bool(_attr(el, "AXEnabled") is not False),
                                    "frame": fr}
                            if is_input:
                                item["value"] = _clean(_attr(el, "AXValue")) or ""
                            if focused is not None and CFEqual(el, focused):
                                focused_id = next_id
                            refs[next_id] = el
                            items.append(item)
                            next_id += 1
                    if role in ("AXRow", "AXCell", "AXButton", "AXLink"):
                        continue          # don't descend into a list row's internals
                for ch in _attr(el, "AXChildren") or []:
                    q.append(ch)

        if include_menus:
            for item, ref in self._menu_items(app_el, start_id=next_id):
                refs[item["id"]] = ref
                items.append(item)

        result = {
            "snapshot_id": snapshot_id,
            "ts": time.time(),
            "app": {"name": app.localizedName(), "bundle_id": app.bundleIdentifier(), "pid": pid},
            "window": _clean(_attr(window, "AXTitle")) if window is not None else None,
            "focused_id": focused_id,
            "items": items,
            "truncated": truncated,
            "digest": digest.hexdigest(),
            "stats": {"ms": round((time.perf_counter() - t0) * 1000), "visited": visited,
                      "elements": sum(1 for i in items if i["kind"] == "element"),
                      "menus": sum(1 for i in items if i["kind"] == "menu")},
        }
        self._refs[snapshot_id] = refs
        self._snaps[snapshot_id] = result
        while len(self._refs) > self._keep:
            self._refs.popitem(last=False)
            self._snaps.popitem(last=False)
        return result

    def list_apps(self) -> list[str]:
        dirs = ["/Applications", "/System/Applications", "/System/Applications/Utilities",
                os.path.expanduser("~/Applications")]
        names = set()
        for d in dirs:
            try:
                names.update(f[:-4] for f in os.listdir(d) if f.endswith(".app"))
            except FileNotFoundError:
                pass
        for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
            if a.activationPolicy() == AppKit.NSApplicationActivationPolicyRegular and a.localizedName():
                names.add(a.localizedName())
        return sorted(names, key=str.lower)

    def get_snapshot(self, snapshot_id: str) -> dict | None:
        """A recent snapshot dict by id (None if expired)."""
        return self._snaps.get(snapshot_id)

    def forget(self, snapshot_id: str) -> None:
        """Drop a snapshot (the executor discards its intermediate polling snapshots)."""
        self._refs.pop(snapshot_id, None)
        self._snaps.pop(snapshot_id, None)

    def element_ref(self, snapshot_id: str, item_id: int):
        """AXUIElement for an item id from a recent snapshot (None if expired or unknown)."""
        return self._refs.get(snapshot_id, {}).get(item_id)

    # ---------------------------------------------------------------- internals
    def _enable_electron_ax(self, app_el, pid):
        """Electron apps (Slack, VS Code, Discord) expose their tree only after AXManualAccessibility=true."""
        if pid in self._electron_enabled:
            return
        self._electron_enabled.add(pid)
        if AX.AXUIElementSetAttributeValue(app_el, "AXManualAccessibility", True) != 0:
            return                # not an Electron/Chromium app
        # Chromium builds its tree asynchronously: poll until the node count stops growing (max ~4 s).
        def nodes(el, depth=0):
            return 1 + (sum(nodes(c, depth + 1) for c in (_attr(el, "AXChildren") or [])) if depth < 40 else 0)
        last, stable, t0 = -1, 0, time.time()
        while time.time() - t0 < 4.0 and stable < 2:
            win = _attr(app_el, "AXFocusedWindow")
            n = nodes(win) if win is not None else 0
            stable = stable + 1 if n == last and n > 20 else 0
            last = n
            time.sleep(0.25)

    def _menu_items(self, app_el, start_id):
        bar = _attr(app_el, "AXMenuBar")
        out, next_id = [], start_id
        tops = (_attr(bar, "AXChildren") or [])[1:] if bar is not None else []   # skip the Apple menu

        def walk(menu, path, depth):
            nonlocal next_id
            for it in _attr(menu, "AXChildren") or []:
                if len(out) >= MAX_MENU_ITEMS:
                    return
                title = _clean(_attr(it, "AXTitle"))
                if not title:
                    continue                       # separator
                sub = _attr(it, "AXChildren") or []
                if title in SKIP_MENU_TOPS:
                    continue
                if sub and depth < 2:
                    walk(sub[0], path + [title], depth + 1)
                    continue
                out.append(({"id": next_id, "kind": "menu", "path": path + [title], "label": " › ".join(path + [title]),
                             "shortcut": _shortcut(it), "enabled": bool(_attr(it, "AXEnabled"))}, it))
                next_id += 1

        for top in tops:
            title = _clean(_attr(top, "AXTitle"))
            for menu in _attr(top, "AXChildren") or []:
                walk(menu, [title] if title else [], 0)
        return out


if __name__ == "__main__":
    import json
    delay = float(sys.argv[1]) if len(sys.argv) > 1 else 3
    print(f"Switch to the app you want to capture; snapshot in {delay:.0f}s...", file=sys.stderr)
    time.sleep(delay)
    snap = MacUIProvider().snapshot()
    print(json.dumps(snap, indent=1, ensure_ascii=False))
