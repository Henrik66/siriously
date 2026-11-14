# Mac integration guide (brain ⇄ speech daemon ⇄ UIProvider/Executor ⇄ overlay)

For Henrik, integrating on the M3 tonight (H+12). Everything here is checked against the code on
`feat/brain`; file:line pointers are given where the behaviour matters. Spec: `PRD.md` §4–6.

```
start_all.sh (Bonsai :8080 + Argmax STT :50060)
   → uv run siriously-brain --mac (brain :8765, warms Bonsai in the background)
      → speech daemon POSTs /utterance        → overlay connects WS /events
```

---

## 1. One-time setup (with wifi ON)

```bash
cd ~/hackathon/siriously            # this repo
uv sync                             # creates .venv from uv.lock (Python ≥ 3.11)
uv run pytest -q                    # should be all green (≈ 530 tests, 1 live test skipped)
```

**pyobjc is not in `pyproject.toml` yet.** `brain.mac` will need it (Accessibility + AppKit). Until the
coordinator adds a macOS-only dependency (e.g. `pyobjc-framework-ApplicationServices; sys_platform == 'darwin'`
and `pyobjc-framework-Cocoa`), install it into the venv with
`uv pip install pyobjc-framework-ApplicationServices pyobjc-framework-Cocoa`.
Note that a later plain `uv sync` removes packages that aren't in the lock (it is exact). `uv run` doesn't.

Give the terminal (or the Python binary in `.venv/bin`) **Accessibility** permission:
System Settings › Privacy & Security › Accessibility.

---

## 2. What `brain/mac.py` must export

`brain/server.py:369` (`make_io`) does exactly this:

```python
from brain.mac import MacExecutor, MacUIProvider
provider = MacUIProvider()     # no arguments
executor = MacExecutor()       # no arguments
```

`brain/mac.py` (or a `brain/mac/` package) is your file. Both classes need **no-arg constructors**.

**The executor needs the provider's id → AXUIElement map**, but `make_io` builds them separately. Until
`make_io` passes the provider in (requested from the coordinator), share state at module level:

```python
_SHARED: "MacUIProvider | None" = None

class MacUIProvider:
    def __init__(self) -> None:
        global _SHARED
        self._snapshots: OrderedDict[str, dict[int, AXUIElementRef]] = OrderedDict()  # LRU, ~16
        _SHARED = self

class MacExecutor:
    def __init__(self, provider: "MacUIProvider | None" = None) -> None:
        self.provider = provider or _SHARED or MacUIProvider()
```

### 2.1 `UIProvider` (contracts.py:591)

| Method | Must do |
|---|---|
| `snapshot() -> UISnapshot` | Frontmost app. Visible, **actionable**, enabled-or-not items (the brain hides disabled ones from the model). Elements as `kind="element"` with `role`/`label`/`value`; the menu bar as `kind="menu"` with `path=("File","New Note")` and `shortcut="cmd+n"` (lowercase, `+`-joined, modifiers `ctrl opt shift cmd`). Set `focused_id`. Keep it fast: the router runs right after it. |
| `list_apps() -> list[str]` | Installed app display names ("Notes", "System Settings"). Called **once** at startup (`build_brain`). Fast path and `open_app` match against it. |

**Snapshot id scoping (important).** Contractually, ids are scoped to one `snapshot_id`. But:

1. **A burst runs on its base snapshot id.** `Brain._run_plan` (pipeline.py:330) calls
   `executor.run(plan.snapshot_id, action)` for **every** action of a 1–6-action burst, even after
   action 1 changed the UI. So `MacExecutor.run` must resolve `(snapshot_id, id)` against the
   map of the snapshot the plan was made on. Keep an LRU of the last ~16 snapshots' maps. If the
   element is gone (`kAXErrorInvalidUIElement`), return `ok=False, error="stale element …"`. That
   triggers a re-plan on a fresh snapshot.
2. **Make ids stable within an app where you cheaply can** (e.g. a per-app registry
   `{(kind, role, path or label, ordinal): id}` kept for the provider's lifetime). This isn't required
   at runtime, but two things depend on it:
   - Captured fixtures must keep one id = one item across states (`brain/fixtures.py:202`
     rejects them otherwise).
   - Stable ids make stale-burst failures rarer.

   Menu items are the easy win: key them by `path`.

### 2.2 `Executor` (contracts.py:598)

`run(snapshot_id, action) -> ActionResult`, `undo() -> bool` (best effort cmd+z).

| op | Expected behaviour |
|---|---|
| `open_app{app}` | Activate/launch the app and **block until it is frontmost** (≤ ~1.5 s). The planner emits bursts like `open_app Notes → wait 500 → keys cmd+n → type "standup"`; if `open_app` returns early, cmd+n lands in the previous app. |
| `press{target}` | `AXPress` on the element. |
| `menu{target}` | `AXPress` on the menu item (open parent menus if needed). Prefer this over clicking. |
| `type{text, target?}` | With `target`: focus it (`AXFocused=true`), then type. Without: type into the focused element. Do not press return; the plan says so explicitly. |
| `keys{keys}` | Post the key combo (`cmd+n`, `shift+cmd+n`, `return`, `escape`, `tab`, `cmd+,`). |
| `scroll{direction, amount}` | Scroll the focused/main scroll area by `amount` lines. |
| `wait{ms}` | Sleep. |

**`ActionResult` semantics the brain relies on** (`pipeline.py:65` `expects_change`, `:291` `_exec_one`):

- `ok=False` → failure → re-plan with `error` text in the prompt (≤ `SIRIOUSLY_MAX_REPLANS`, default 2).
  Put a short human-readable reason in `error` ("no element 12 in s-0192", "menu item disabled").
- `changed` matters **only** for `press`, `menu`, `open_app`, and `keys` combos containing `+`.
  For those, `ok=True, changed=False` is treated as **"had no effect"** and triggers a re-plan.
  So compute `changed` honestly: compare a cheap before/after fingerprint (frontmost app, window title,
  focused element, item count, focused value). `type`, `scroll`, `wait` and bare keys (`return`) are
  never judged on `changed`.
- `snapshot`: **return the fresh post-action snapshot whenever you have it.** Re-plans and flow replay
  use `result.snapshot` and only call `provider.snapshot()` when it is `None` (pipeline.py:349, :384).
  It saves one AX walk per step.
- Never raise from `run` if you can help it. The brain catches exceptions and treats them as `ok=False`,
  but the error text is less useful.

---

## 3. Running the brain

```bash
uv run siriously-brain --mac                 # on macOS, --mac is the default when no --fixture is given
uv run siriously-brain --fixture notes       # off-Mac / dry run on fixtures (FakeExecutor)
uv run siriously-brain --mac --no-model      # control + taught flows + fast path only (no Bonsai)
uv run siriously-brain --mac --log-level debug
```

Exit code 2 with a message means a setup error, e.g. `brain.mac` failed to import.

**Environment variables** (`brain/config.py`):

| Variable | Default | Meaning |
|---|---|---|
| `SIRIOUSLY_BONSAI_URL` | `http://127.0.0.1:8080/v1` | Bonsai llama-server (OpenAI API). mlx-vlm on :8081 isn't supported by the planner (it needs llama-server's `json_schema` grammar + `cache_prompt`/`id_slot`). |
| `SIRIOUSLY_BONSAI_MODEL` | `bonsai` | Model name sent; llama-server ignores it with a single model. |
| `SIRIOUSLY_BONSAI_TIMEOUT_S` | `6.0` | Per planner call (connect + full reply). |
| `SIRIOUSLY_BONSAI_SAMPLING` | `greedy` | `greedy` (temperature 0) or `card` (model-card thinking-off profile). |
| `SIRIOUSLY_HOST` / `SIRIOUSLY_PORT` | `127.0.0.1` / `8765` | Brain HTTP/WS bind. No auth: keep it on loopback. |
| `SIRIOUSLY_FLOWS` | `~/.siriously/flows.json` | Taught flows ("call that standup"). |
| `SIRIOUSLY_UTTERANCE_TIMEOUT_S` | `8.0` | Global budget per utterance (checked between actions). |
| `SIRIOUSLY_MAX_REPLANS` | `2` | Re-plans after a failed/no-effect action. |

**Health:** `curl -s 127.0.0.1:8765/health` → `{"ok": true, "bonsai": true, "flows": 0}`.
`"bonsai": false` means :8080 is down; the brain still runs control words, flows and the fast path.

**Dev CLI** (`brain/cli.py`):
- `uv run python -m brain.cli --post "open notes"` sends to the running brain and prints the Route.
- `uv run python -m brain.cli --watch --history 20` prints the event stream.

---

## 4. Speech daemon → brain: `POST /utterance`

```http
POST http://127.0.0.1:8765/utterance          (?wait=true blocks and returns the Route)
Content-Type: application/json

{"id": "u-0042", "text": "open notes and start a note called standup",
 "t_speech_end": 1731600000.12, "stt_ms": 150,
 "speaker": {"verified": true, "score": 0.61, "name": "henrik"}}
```

- `speaker` is **required**. `t_speech_end` is Unix epoch **seconds** (float) at end of speech. The brain
  uses it for every event's `ms_since_speech_end`; send `0` if unknown.
- Reply `202 {"accepted": true, "id": "u-0042", "stopped": false}` immediately; bad payloads give
  `422 {"error", "code": "BAD_UTTERANCE"}`.
- Utterances run **one at a time** on a worker thread. **A verified "stop" cuts in immediately**
  (`stopped: true`): it aborts the running burst between actions.
- `verified: false` → the brain only emits `rejected`. Nothing runs.
- `id` should be unique per utterance. The overlay groups events by it.

---

## 5. Brain → overlay: `WS ws://127.0.0.1:8765/events`

Each message: `{"type", "utterance_id", "data", "ms_since_speech_end"}`. `?history=N` replays the last N
events on connect (also `GET /events/history?n=50`). A slow client loses its oldest queued events. The
brain never blocks on it.

| type | data |
|---|---|
| `heard` | `{text, speaker}` |
| `rejected` | `{text, score}` (unverified speaker; the only event for that utterance) |
| `route` | `{kind: control\|flow\|fastpath\|planner\|dictation\|none, reason, ms, control?, flow?}`. For `planner`, `ms` ≈ the Bonsai call time. |
| `plan` | `{plan: Plan, source: fastpath\|planner\|dictation\|replan\|flow-fallback}` |
| `confirm` | `{plan, reason}`: waiting for "yes" / "no" (the next utterance) |
| `action` | `{i, action, ok, changed, error}`, one per executed action |
| `done` | `{route, n_actions, ms}` (+ `saved, n_steps` after "call that …") |
| `error` | `{message}` (gave up, timeout, stopped, planner/server error, "nothing to undo"…) |

Typical sequences:

```
planner     heard → route{planner} → plan{source:planner} → action×N → done
fast path   heard → route{fastpath} → plan{source:fastpath} → action → done
re-plan     … → action{ok:false | changed:false} → plan{source:replan} → action… → done | error
confirm     heard → route → plan → confirm{reason}      ‖ next utterance "yes": heard → route{control} → action… → done
ask         heard → route{planner} → plan{plan.ask:[4,9]} → done{n_actions:0}  (overlay numbers ids 4 and 9)
stop        heard → route{control:stop} → done   and the interrupted utterance ends with error{"stopped"}
teach       heard → route{flow} → done{saved:"standup", n_steps}
no match    heard → route{none, reason} → error{message}
```

Known gap: after an `ask`, saying "two" / "the second one" is **not** handled yet (reported to the coordinator).

---

## 6. Start order and warmup

1. `./start_all.sh` starts Bonsai (fork, PQ2_0, :8080) and Argmax STT (:50060) and waits until both are healthy.
   `./start_all.sh status` checks them later. The default `BONSAI=fork` and `BONSAI=upstream` both work
   with the planner.
2. `uv run siriously-brain --mac`. On startup it runs `Brain.warmup()` **in a background thread**: one
   1-token request that puts the planner's ~890-token static prompt prefix into llama-server's cache.
   **Wait for the log line `planner warm (… ms)`** before the first planner command. The first
   uncached call costs several seconds and can exceed the 6 s timeout. The brain re-warms only on restart,
   so **restart the brain if you restart Bonsai**.
3. Start the speech daemon and point it at `/utterance`. Start the overlay and connect it to `/events`.
4. Smoke test: `uv run python -m brain.cli --post "new note"` with Notes frontmost.

---

## 7. Latency budget (planner path)

**Measured off-Mac** (Windows, Ryzen AI 7 350, Radeon 860M iGPU via Vulkan, fork PQ2_0, warm cache):

| Step | Value |
|---|---|
| Static prefix | ~890 tokens, served from cache (`cached_tokens=891` every call) |
| Dynamic suffix | ~110–150 tokens: ≤ 15 items, labels ≤ 40 chars; budget-trimmed in `brain/planner/prompt.py` |
| Prompt processing | ~700–800 ms for the suffix |
| Decode | 15–42 output tokens at ~25 ms/token, plus grammar overhead: 0.9–2.7 s |
| **Per plan** | **1.2–2.3 s** |

**Mac projection** from PRD §7 (LPM): prompt 100–150 tok/s and decode 15 tok/s. The suffix costs
≈ 1.0–1.5 s and decode ≈ 1–2.7 s, so **≈ 2–4 s per plan, over the 2.0 s target**. Plugged in (no LPM)
should help. Measure it: the `route` event's `ms` for `kind=planner` is the model call.

Levers, cheapest first:
1. Most commands should never reach the model: check `route.kind` in the overlay; fast-path misses are bugs.
2. Fewer shown items: `BonsaiPlanner(k=10)`. Shorter decode: single-action plans are about 15 tokens.
3. `SIRIOUSLY_BONSAI_TIMEOUT_S` stays ≥ 6 so a slow plan doesn't become an error.

Other measured numbers (same machine, `smoke/bonsai_smoke.py`):

| Server | TTFT | Decode | Prompt |
|---|---|---|---|
| fork PQ2_0 Vulkan | 308 ms | 24 tok/s | 106 tok/s |
| stock b12383 Q2_0 Vulkan | 386 ms | 11.5 tok/s | 74 tok/s |
| CPU | 898 ms | 20 tok/s | 31 tok/s |

---

## 8. Capturing real fixtures from `MacUIProvider`

The eval (`uv run python -m eval.run_eval`) replays `eval/commands.yaml` against
`eval/fixtures/<name>.json`. Real captures make the pass rate meaningful. Format (`brain/fixtures.py`):

```json
{"name": "notes", "note": "captured on M3, macOS 26.5", "initial": "s-notes-0",
 "snapshots": {"s-notes-0": {UISnapshot.to_dict()}, "s-notes-1": {...}},
 "transitions": [{"from": "s-notes-0", "action": {"op": "menu", "target": 101}, "to": "s-notes-1"}]}
```

Rules enforced on load (`validate_fixture`):
- each snapshot key == its `snapshot_id`;
- `focused_id` is an item;
- transition targets exist in the `from` snapshot;
- **an id keeps the same kind/role/menu path in every state** (see §2.1).

Capture script: no file needed, run it from the repo with Notes frontmost.

```bash
uv run python - <<'EOF'
import json, time
from pathlib import Path
from brain.contracts import Action
from brain.mac import MacExecutor, MacUIProvider

name = "notes-real"
steps = [Action.menu(101), Action.type_text("standup")]   # EDIT: real ids, from a first dry snapshot
p, x = MacUIProvider(), MacExecutor()
snaps, trans = {}, []

def grab():
    s = p.snapshot(); snaps[s.snapshot_id] = s.to_dict(); return s.snapshot_id   # keep the provider's ids

cur = initial = grab()
for act in steps:
    r = x.run(cur, act); time.sleep(0.5)
    print(act.to_dict(), "->", r.ok, r.changed, r.error)
    nxt = grab()
    trans.append({"from": cur, "action": act.to_dict(), "to": nxt}); cur = nxt

fx = {"name": name, "note": "captured " + time.strftime("%Y-%m-%d %H:%M"), "initial": initial,
      "snapshots": snaps, "transitions": trans}
Path(f"eval/fixtures/{name}.json").write_text(json.dumps(fx, indent=1, ensure_ascii=False))
print("wrote", name, len(snaps), "states")
EOF
uv run python -c "from brain.fixtures import load_fixture; print(load_fixture('notes-real').name, 'OK')"
```

First print one snapshot (`uv run python -c "from brain.mac import MacUIProvider; import json;
print(json.dumps(MacUIProvider().snapshot().to_dict(), indent=1))"`) to pick the ids for `steps`.
If the load check complains "id N changes identity across states", ids aren't stable within the app (§2.1).

Also refresh the installed-app list the eval uses:
`uv run python -c "import json; from brain.mac import MacUIProvider; print(json.dumps(MacUIProvider().list_apps()))" > /tmp/apps.json`.
Then hand it to the eval owner (`siriously-57`), who owns `eval/fixtures/apps.json` and `eval/commands.yaml`.
To point the eval at a separate folder: `--fixtures path/to/dir`.

---

## 9. Offline checklist (demo runs with wifi OFF)

- [ ] `uv sync` (and pyobjc) done while online. Afterwards always `uv run --offline …` or `export UV_OFFLINE=1`.
- [ ] Bonsai GGUF + Argmax models on disk. `start_all.sh` exports `HF_HUB_OFFLINE=1`.
- [ ] `./start_all.sh status` shows both servers healthy with wifi off.
- [ ] `curl 127.0.0.1:8765/health` → `"bonsai": true`, and the `planner warm` log line appeared.
- [ ] Overlay loads no CDN fonts/scripts.
- [ ] No HTTP proxy env vars pointing off-box. The planner bypasses proxies anyway; `curl` doesn't.
- [ ] `uv run --offline python -m eval.run_eval --bonsai-url http://127.0.0.1:8080/v1` runs and prints the
      pass rate. Show it in the demo.
- [ ] Flows taught during rehearsal live in `~/.siriously/flows.json`. Copy or delete it before the demo as intended.
- [ ] Accessibility permission granted to the exact Python/terminal you demo from.

---

## 10. Troubleshooting

| Symptom | Likely cause |
|---|---|
| `route{none, reason: "planner error: timeout…"}` on the first command | Cache not warm yet (wait for `planner warm`), or Bonsai was restarted after the brain. |
| `planner error: down…` | :8080 not up. `./start_all.sh status`. |
| `planner error: bad_output: … not a shown id` | Should not happen (grammar-constrained). Capture the `--log-level debug` output and the snapshot. |
| Every `press` is followed by `plan{source: replan}` | `changed` is always false: fix the before/after fingerprint. |
| Second action of a burst fails "stale" | Executor isn't resolving ids against the burst's **base** `snapshot_id` (§2.1). |
| `cmd+n` after `open_app` goes to the wrong app | `open_app` returned before the app was frontmost. |
