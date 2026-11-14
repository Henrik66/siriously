# /// script
# requires-python = ">=3.11"
# dependencies = ["pyobjc-framework-ApplicationServices", "pyobjc-framework-Cocoa"]
# ///
"""Capture real UI snapshots (PRD §4.2) as eval fixtures for the brain.

    uv run --script mac/capture_fixtures.py                    # Finder, Notes, System Settings, Calendar, Slack
    uv run --script mac/capture_fixtures.py Notes Slack        # just these
Writes eval/fixtures/<slug>.json plus eval/fixtures/apps.json (list_apps()). Brings each app to the front.
NOTE: snapshots contain whatever is on screen (file names, note titles, messages). Review before sharing.
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import AppKit  # noqa: E402
from ui_provider import MacUIProvider  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "eval", "fixtures")
APPS = sys.argv[1:] or ["Finder", "Notes", "System Settings", "Calendar", "Slack"]
SETTLE = {"Slack": 3.0}


def bring_to_front(name):
    return MacUIProvider.activate(name)


def main():
    os.makedirs(OUT, exist_ok=True)
    ui = MacUIProvider()
    for name in APPS:
        if not bring_to_front(name):
            print(f"{name:<16} could not bring to front, skipped")
            continue
        time.sleep(SETTLE.get(name, 1.2))
        ui.snapshot(include_menus=False)            # warm-up (Electron tree build, AX caches)
        snap = ui.snapshot()
        slug = name.lower().replace(" ", "_")
        with open(os.path.join(OUT, f"{slug}.json"), "w") as f:
            json.dump(snap, f, indent=1, ensure_ascii=False)
        st = snap["stats"]
        print(f"{name:<16} {st['elements']:>4} elements  {st['menus']:>4} menu items  {st['ms']:>5} ms  "
              f"visited {st['visited']:>5}  window={snap['window']!r}{'  TRUNCATED' if snap['truncated'] else ''}")
    with open(os.path.join(OUT, "apps.json"), "w") as f:
        json.dump(ui.list_apps(), f, indent=1)
    subprocess.run(["open", "-a", "Terminal"])


if __name__ == "__main__":
    main()
