#!/usr/bin/env python3
"""Stand-in for the brain (PRD §4.1): accepts POST /utterance on 127.0.0.1:8765 and logs it. Stdlib only.

    python3 mac/stub_brain.py            # then run: mac/ear/.build/release/siriously-ear run
Use it to test the speech daemon without the real brain, or to see the exact JSON the daemon sends.
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0)
        try:
            u = json.loads(body)
        except ValueError:
            self.send_response(400); self.end_headers(); return
        lag = (time.time() - u.get("t_speech_end", time.time())) * 1000
        sp = u.get("speaker", {})
        mark = "✓" if sp.get("verified") else "✗ REJECTED"
        print(f"{time.strftime('%H:%M:%S')} {self.path} {u.get('id')} {mark} score={sp.get('score')} "
              f"stt={u.get('stt_ms')}ms arrived {lag:.0f}ms after speech end: {u.get('text')!r}", flush=True)
        print("   " + json.dumps(u), flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok": true, "stub": true}')

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"stub brain on http://127.0.0.1:{PORT} (POST /utterance)", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
