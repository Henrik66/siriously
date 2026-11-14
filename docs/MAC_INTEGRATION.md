# Mac integration guide (brain ⇄ speech daemon ⇄ UIProvider/Executor ⇄ overlay)

For the M3 demo machine and the people wiring the Mac side. Everything here is checked against the code on `feat/brain`; file pointers are given where the behaviour matters. Spec: `PRD.md` sections 4 to 6. Architecture: `docs/ARCHITECTURE.md`.

```
scripts/run_demo.sh start
   -> start_all.sh          Bonsai :8080 (llama-server) + Argmax STT :50060, health-checked
   -> siriously-brain --mac  brain :8765, warms Bonsai in a background thread
        ^ POST /utterance   from the speech daemon (Swift, WhisperKit + SpeakerKit)
        v WS /events        to the overlay, and to /console (debug page)
```

`brain/mac.py` (the Mac UIProvider and MacExecutor) is Henrik's module. It is **not in `feat/brain` yet**. `--mac` needs it and exits with code 2 without it (section 3).

---

## 1. One-time setup (with wifi ON)

```bash
cd ~/hackathon/siriously            # this repo
uv sync                             # .venv from uv.lock (Python >= 3.11); pyobjc is a darwin-only dependency
uv run pytest -q                    # 1003 passed on the dev box
```

`pyobjc-framework-ApplicationServices` and `pyobjc-framework-Cocoa` are in `pyproject.toml` for macOS. `uv sync` is exact: it removes anything not in the lock. `uv run` does not.

Grant the terminal (or the Python binary in `.venv/bin`) **Accessibility** permission: System Settings › Privacy & Security › Accessibility. Without it the Mac provider sees no elements.

---

## 2. What `brain/mac.py` must export

`brain/server.py` (`make_io`) does this:

```python
from brain.mac import MacExecutor, MacUIProvider
provider = MacUIProvider()                  # no arguments
try:
    executor = MacExecutor(provider)        # the executor resolves ids through the provider's map
except TypeError:
    executor = MacExecutor()                # fallback for an executor that keeps no provider reference
```

So `MacExecutor` should accept an optional provider (`def __init__(self, provider: "MacUIProvider | None" = None)`) and use its snapshot-to-element map. Without the argument it may keep its own reference, but must still resolve ids from the snapshots the provider produced.

### 2.1 `UIProvider`

| Method | Must do |
|---|---|
| `snapshot() -> UISnapshot` | Frontmost app. Visible, **actionable** items, enabled or not (the brain hides disabled ones from the model). Elements as `kind="element"` with `role`, `label`, `value`. The menu bar as `kind="menu"` with `path=("File","New Note")` and `shortcut="cmd+n"` (lowercase, `+`-joined; modifiers `ctrl opt shift cmd`). Set `focused_id`. **Fill `app.bundle_id`:** the confirm re-check and flow descriptors compare it. Keep it fast: the router runs right after it. |
| `list_apps() -> list[str]` | Installed app display names ("Notes", "System Settings"). Called **once**, in `build_brain` at startup. Fast path and `open_app` match against it. |

**Snapshot id scoping.** Ids are scoped to one `snapshot_id`:

1. **A burst runs on its base snapshot id.** For every action of a burst the brain calls `executor.run(plan.snapshot_id, action)`, even after action 1 changed the UI. `MacExecutor.run` must resolve `(snapshot_id, id)` against the map of the snapshot the plan was made on. Keep an LRU of the last ~16 snapshots' maps. If an element is gone (`kAXErrorInvalidUIElement`), return `ok=False, error="stale element ..."`. That triggers a re-plan on a fresh snapshot.
2. **Make ids stable within an app where you cheaply can.** A per-app registry keyed by `(kind, role, path or label, ordinal)` kept for the provider's lifetime works. Menu items are the easy win: key them by `path`. This is not required at runtime, but captured fixtures must keep one id = one item across states (`brain/fixtures.py:223` rejects a change), and stable ids make stale-burst failures rarer.

### 2.2 `Executor`

`run(snapshot_id, action) -> ActionResult`, `undo() -> bool` (best-effort Cmd+Z; return `False` when there is nothing to undo).

| op | Expected behaviour |
|---|---|
| `open_app{app}` | Activate or launch the app and **block until it is frontmost** (≤ ~1.5 s). The planner emits bursts like `open_app Notes → wait 500 → keys cmd+n → type "standup"`. If `open_app` returns early, `cmd+n` lands in the previous app. If the app is already frontmost, return `ok=True`: that is success. |
| `press{target}` | `AXPress` on the element. |
| `menu{target}` | `AXPress` on the menu item, opening parent menus if needed. Prefer this over clicking. |
| `type{text, target?}` | With `target`: focus it (`AXFocused=true`), then type. Without: type into the focused element. Do not press return; the plan says so explicitly. |
| `keys{keys}` | Post the combo (`cmd+n`, `shift+cmd+n`, `return`, `escape`, `tab`, `cmd+,`). |
| `scroll{direction, amount}` | Scroll the focused or main scroll area by `amount` lines. |
| `wait{ms}` | Sleep. |

**`ActionResult` semantics the brain relies on** (`brain/pipeline.py`, `_exec_one`):

- `ok=False` is a failure. The brain re-plans with `error` in the prompt, up to `SIRIOUSLY_MAX_REPLANS` (default 2). Put a short reason in `error`: "no element 12 in s-0192", "menu item disabled".
- `changed` matters for `press`, `menu`, `open_app`, and `keys` combos containing `+`. For those, `ok=True, changed=False` is "no visible effect". Before counting it as a failure, the brain re-snapshots and compares the content (frontmost app, window, focused element, items, values). So a false `changed=false` costs one snapshot, not a re-plan. Still compute it honestly: compare a before/after fingerprint. `type`, `scroll`, `wait`, and bare keys (`return`) are never judged on `changed`.
- Pressing an element that is already selected, or `open_app` for the frontmost app, counts as success.
- Return `snapshot` (the fresh post-action snapshot) whenever you have it. Re-plans and flow replay use it and only call `provider.snapshot()` when it is `None`. That saves one AX walk per step.
- Never raise if you can help it. The brain turns exceptions into `ok=False`, but the error text is then less useful.

---

## 3. Running the brain

```bash
scripts/run_demo.sh start                    # the whole stack, with health checks and the warm-up wait (recommended)
uv run siriously-brain --mac                 # brain only; on macOS, --mac is the default when no --fixture is given
uv run siriously-brain --mac --no-model      # control words, taught flows, fast path only (no Bonsai)
uv run siriously-brain --fixture finder      # off-Mac: FixtureUIProvider + FakeExecutor
uv run python -m brain.server --mac --port 8765 --log-level debug
```

`siriously-brain` flags: `--fixture NAME` or `--mac` (mutually exclusive), `--no-model`, `--host`, `--port`, `--log-level {debug,info,warning,error}`. A setup failure (for example, `brain.mac` cannot be imported) prints the reason and exits with code 2.

`scripts/run_demo.sh` env: `BRAIN_PORT` (8765), `BRAIN_ARGS` (extra brain arguments, e.g. `--no-model`), `WAIT_SECONDS` (180). `start` reuses a healthy stack or brain and only stops what it started. Logs go to `logs/brain.log`, and the pid to `logs/brain.pid`.

**Settings** (`brain/config.py`, plus the server's access variables):

| Variable | Default | Meaning |
|---|---|---|
| `SIRIOUSLY_BONSAI_URL` | `http://127.0.0.1:8080/v1` | Bonsai llama-server (OpenAI API). The planner needs llama-server's `json_schema` grammar, `cache_prompt` and `id_slot`, so mlx-vlm on :8081 is not supported. |
| `SIRIOUSLY_BONSAI_MODEL` | `bonsai` | Model name sent. llama-server ignores it with a single model. |
| `SIRIOUSLY_BONSAI_TIMEOUT_S` | `6.0` | Per planner call (connect and full reply). |
| `SIRIOUSLY_BONSAI_SAMPLING` | `greedy` | `greedy` (temperature 0) or `card` (model-card thinking-off profile). |
| `SIRIOUSLY_HOST` / `SIRIOUSLY_PORT` | `127.0.0.1` / `8765` | Bind address. Keep it on loopback. |
| `SIRIOUSLY_TOKEN` | unset | Shared secret. When set, every request except `GET /health` and the console's static files must send it (section 5). |
| `SIRIOUSLY_ALLOWED_ORIGINS` | empty | Comma-separated browser origins that may connect. `null` is never accepted. The brain's own console origin is always allowed. |
| `SIRIOUSLY_ALLOWED_HOSTS` | empty | Extra `Host` names. Loopback names are always allowed. |
| `SIRIOUSLY_FLOWS` | `~/.siriously/flows.json` | Taught flows ("call that standup"). |
| `SIRIOUSLY_UTTERANCE_TIMEOUT_S` | `8.0` | Global budget per utterance, checked between actions and before each planner call. |
| `SIRIOUSLY_MAX_REPLANS` | `2` | Re-plans after a failed or no-effect action. |
| `SIRIOUSLY_MIN_PLAN_BUDGET_S` | `2.5` | A planner call (re-plan or continuation) starts only if at least `max(this, 1.25 x measured planner time)` remains. |
| `SIRIOUSLY_CONFIRM_TTL_S` | `20.0` | A pending "are you sure?" expires after this. A late "yes" is refused. |

**Health:**

```bash
curl -s --noproxy '*' 127.0.0.1:8765/health
# {"ok": true, "bonsai": true, "flows": 0, "confirm_ttl_s": 20.0}
```

`"bonsai": false` means :8080 is down. Control words, taught flows and the fast path still work.

**Dev CLI** (`brain/cli.py`):

- `uv run python -m brain.cli --fixture finder "open notes"`: in-process, fakes. Add `--no-model`, `--unverified`, or `--repl` (one utterance per stdin line).
- `uv run python -m brain.cli --post "new note"`: POSTs to the running server (`?wait=true`) and prints the Route. Add `--repl` for line by line.
- `uv run python -m brain.cli --watch --history 20`: prints the event stream, replaying the last 20 events first.
- With `SIRIOUSLY_TOKEN` set, `--post` and `--watch` send `X-Siriously-Token` themselves.

---

## 4. Speech daemon → brain: `POST /utterance`

```http
POST http://127.0.0.1:8765/utterance          (?wait=true blocks until handled and returns the Route)
Content-Type: application/json
X-Siriously-Token: <only when SIRIOUSLY_TOKEN is set>

{"id": "u-0042", "text": "open notes and start a note called standup",
 "t_speech_end": 1731600000.12, "stt_ms": 150,
 "speaker": {"verified": true, "score": 0.61, "name": "henrik"}}
```

- **Required:** `id`, `text`, and `speaker` (an object with a boolean `verified`). `t_speech_end` is Unix epoch seconds (float) at end of speech; send `0` if unknown. The brain uses it for every event's `ms_since_speech_end`. `stt_ms` is optional.
- **`text` is at most 2000 characters.** Longer input is refused with 422 `BAD_UTTERANCE`, because it is not a voice command.
- **`Content-Type: application/json` is required.** Anything else gets 415 `UNSUPPORTED_MEDIA_TYPE`.
- **Replies.** Without `?wait=true`: `202 {"accepted": true, "id": "u-0042", "stopped": false}`. With `?wait=true`: the Route, `200`. Other statuses: `422 BAD_UTTERANCE` (invalid JSON or contract violation), `504 TIMEOUT` (no result within the utterance budget plus 2 s; not-yet-started work is cancelled), `409 CANCELLED` (cancelled by a stop before it ran), `500 BRAIN_ERROR`.
- **Serial execution.** Utterances run one at a time on a worker thread.
- **Stop cuts in.** A verified stop phrase ("stop", "stop it", "wait", "hold on", up to three words) is handled on the request thread right away: it aborts the running burst between actions and cancels queued utterances (each gets an `error`, "cancelled by stop"). The reply has `"stopped": true`.
- **Unverified speaker.** `verified: false` produces only a `rejected` event. Nothing runs.
- **`id` must be unique** per utterance. The overlay groups events by it.
- **Host and Origin.** The `Host` header must be loopback (or listed in `SIRIOUSLY_ALLOWED_HOSTS`), else 403 `FORBIDDEN_HOST`. A request with an `Origin` header must come from an allowed origin, else 403 `FORBIDDEN_ORIGIN`. Native clients send no `Origin` header, and their `Host` is `127.0.0.1:8765` (or `localhost:8765`).

---

## 5. Brain → overlay: `WS ws://127.0.0.1:8765/events`

Optional `?history=N` (0 to 200) replays the last N events on connect. `GET /events/history?n=50` returns the same history as JSON. A slow client loses its oldest queued events. The brain never blocks on it.

**Authentication** when `SIRIOUSLY_TOKEN` is set:

- Native clients send the header `X-Siriously-Token: <token>`.
- Browsers cannot set WS headers, so they offer subprotocols instead: `token.<base64url(token), no padding>` in `Sec-WebSocket-Protocol`. Offer `siriously` too to get the console protocol: the first message is `{"hello": {"replay": N}}`, and the next N messages are history replay. The server answers `siriously` only when it was offered, and never echoes the token.
- `?token=` in the URL is refused with 401 `UNAUTHORIZED`. URLs end up in logs.

**Message shape:** `{"type", "utterance_id", "data", "ms_since_speech_end"}`.

| type | data |
|---|---|
| `heard` | `{text, speaker}` |
| `rejected` | `{text, score}`: unverified speaker; the only event for that utterance |
| `route` | `{kind: control\|flow\|fastpath\|planner\|dictation\|none, reason, ms, control?, flow?, source?}`. `source: "rest"` marks the second half of "open X and <rest>". For `planner`, `ms` is the Bonsai call time. |
| `plan` | `{plan, source, targets?, ask?, labels?}`. `source` is `fastpath`, `planner`, `dictation`, `replan`, `continue` (the rest of a burst after navigation), or `flow-fallback`. `targets` maps id to a readable name, e.g. `{"101": "File › New Note"}`. |
| `confirm` | `{plan, reason}`: waiting for "yes" or "no" (the next utterance). Expires after `confirm_ttl_s`. |
| `action` | `{i, action, ok, changed, error, label?}`, one per executed action. `label` is the readable target name. |
| `done` | `{route, n_actions, ms, usage?, saved?, n_steps?}`. `usage` sums the planner calls of this utterance: `{prompt_tokens, cached_tokens, completion_tokens, ms, calls}`. `saved` and `n_steps` appear after "call that X". |
| `error` | `{message}`: gave up, timeout, stopped, cancelled, planner or server error, "nothing to undo", "nothing to confirm", and similar. |

Typical sequences:

```
planner     heard -> route{planner} -> plan{source:planner} -> action x N -> done
fast path   heard -> route{fastpath} -> plan{source:fastpath} -> action -> done
compound    heard -> route{fastpath, reason:compound} -> plan -> action(open) -> route{source:rest} -> plan -> action... -> done
re-plan     ... -> action{ok:false | changed:false} -> plan{source:replan} -> action... -> done | error
confirm     heard -> route -> plan -> confirm{reason}     ||  next "yes": heard -> route{control} -> action... -> done
            (a "yes" after the TTL, or after any other utterance, gives error "nothing to confirm")
ask         heard -> route{planner} -> plan{ask:[4,9], labels} -> done{n_actions:0}
            next utterance "two" / "the second one" / a label: route{planner, ambiguity answer} -> action -> done
stop        heard -> route{control:stop} -> done; the interrupted utterance ends with error{"cancelled by stop"} or error{"stopped"}
teach       heard -> route{flow, teach} -> done{saved:"standup", n_steps}
no match    heard -> route{none, reason} -> error{message}
```

**Overlay notes.** Every HTTP response carries `X-Frame-Options: DENY`, a CSP with `frame-ancestors 'none'`, `nosniff`, and `Referrer-Policy: no-referrer`. The console at `/console` is static and has no CDN assets. The overlay must load no CDN fonts or scripts either.

---

## 6. Start order and warm-up

1. `scripts/run_demo.sh start`. This runs `start_all.sh` (Bonsai on :8080 and Argmax STT on :50060, waiting until both are healthy), then starts the brain with `--mac` and waits for `bonsai:true` and the warm-up line. Or do it by hand: `./start_all.sh`, then `uv run siriously-brain --mac`.
2. **Warm-up.** On startup the brain runs `Brain.warmup()` in a background thread: one 1-token request that puts the planner's static prefix (system rules and few-shot turns) into llama-server's prompt cache. Wait for the log line **`planner warm (N ms)`** before the first planner command. The first uncached call can take seconds and exceed the 6 s timeout. The warm-up runs once per brain process, so **restart the brain if you restart Bonsai**.
3. Start the speech daemon, pointed at `/utterance`. Start the overlay, connected to `/events`.
4. Smoke test with Notes frontmost: `uv run python -m brain.cli --post "new note"`.

---

## 7. Latency budget

**Targets (PRD section 6), judged on the Mac:** end of speech to first action, fast path ≤ 1.0 s; planner ≤ 2.0 s.

**Dev-box numbers** (Windows, Bonsai 2 4B on an AMD iGPU via Vulkan, greedy; not the Mac): planner call median 1.94 s, p90 2.33 s; prompt about 1234 tokens of which about 1050 are cached; about 17 completion tokens. Fast-path matcher 0.02 ms on a 300-item snapshot. End of speech to first action: fast path median 0.28 ms, p90 0.62 ms; planner median 1.6 s, p90 2.1 s.

**Mac numbers** are what count. Measure with `scripts/bench/planner_bench.py`, plugged in, Low Power Mode off:

```bash
uv run python scripts/bench/planner_bench.py                 # SIRIOUSLY_BONSAI_URL or :8080
uv run python scripts/bench/planner_bench.py --repeat 3      # more planner calls per case
uv run python scripts/bench/planner_bench.py --no-model      # fast path only, no Bonsai needed
```

Flags: `--bonsai-url`, `--no-model`, `--repeat`, `--ttft-runs` (default 5), `--only` (case-id glob, repeatable), `--out`, `--no-write`. It writes `eval/results/bench-<timestamp>.json` unless `--no-write`, and prints a verdict table against the PRD targets. The `route` event with `kind=planner` carries the same call time (`ms`). The `done.usage` field carries the tokens.

**Levers, cheapest first:**

1. Run plugged in with Low Power Mode off. The SETUP_REPORT numbers below are Low Power Mode on battery.
2. Most commands should never reach the model. Check `route.kind` in the overlay: a `planner` route for a command that should be fast is a bug in the fast path or the eval, so fix it there.
3. MTP speculative decoding on the PrismML fork, server-side only. Greedy speculative decoding gives identical output, so accuracy is unchanged, and decode is about 45% of a plan. Use `Ternary-Bonsai-2-4B-PQ2_0-MTP-Q8_0.gguf` with `--spec-type draft-mtp --spec-draft-n-max 2`, per the model card's USAGE.md. Untested: whether the fork combines MTP with `json_schema` grammars. Verify with `planner_bench.py` and `python -m eval.run_eval --e2e` before using it in the demo.
4. Keep `SIRIOUSLY_BONSAI_TIMEOUT_S` at 6 or more, so a slow plan does not become an error.

**Measured, do not do:** shrinking the prompt costs accuracy. Same-snapshot A/Bs on the dev box against HEAD at 74/77:

| Change | Prompt tokens | Accuracy |
|---|---|---|
| Fewer items (k=10) | — | lower |
| Drop quotes around labels | -22 | 71/77 |
| Drop shortcuts and quotes | -62 | 68/77, prompt time only -7% |
| Fixed, utterance-independent item block (to cache/prefill per screen) | — | 67/77 |

The model needs relevance-ordered items with quoted labels and shortcuts. Keep k=15 and the quoted format.

Other measurements from SETUP_REPORT (M3, Low Power Mode on, battery, worst case): time to first token 270 to 355 ms, decode 14 to 16 tokens/s, prompt processing 78 to 103 tokens/s in the smoke tests (PRD section 7 estimates 100 to 150). At that rate a 300-token suffix costs 2 to 3 s, which is why the suffix is capped at 150 tokens and the prefix is cached.

---

## 8. Capturing real fixtures from `MacUIProvider`

The eval replays `eval/commands.yaml` against `eval/fixtures/<name>.json`. Real captures make the pass rate meaningful. Format (`brain/fixtures.py`):

```json
{"name": "notes", "note": "captured on M3, macOS 26.5", "initial": "s-notes-0",
 "snapshots": {"s-notes-0": {UISnapshot.to_dict()}, "s-notes-1": {...}},
 "transitions": [{"from": "s-notes-0", "action": {"op": "menu", "target": 101}, "to": "s-notes-1"}]}
```

`load_fixture` / `validate_fixture` enforce these rules:
- each snapshot key equals its `snapshot_id`;
- `focused_id` is one of the snapshot's items;
- transition targets exist in the `from` snapshot;
- an id keeps the same kind, role, and menu path in every state (section 2.1).

Print one snapshot first, to choose the ids for the steps:

```bash
uv run python -c "from brain.mac import MacUIProvider; import json; print(json.dumps(MacUIProvider().snapshot().to_dict(), indent=1))"
```

Capture script (run from the repo with Notes frontmost; edit `steps` to the real ids):

```bash
uv run python - <<'EOF'
import json, time
from pathlib import Path
from brain.contracts import Action
from brain.mac import MacExecutor, MacUIProvider

name = "notes-real"
steps = [Action.menu(101), Action.type_text("standup")]   # EDIT: real ids, from the snapshot above
p, x = MacUIProvider(), MacExecutor(MacUIProvider())      # the executor must share the provider's id map
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

If the load check reports "id N changes identity across states", the ids are not stable within the app (section 2.1).

Refresh the installed-app list the eval uses:

```bash
uv run python -c "import json; from brain.mac import MacUIProvider; print(json.dumps(MacUIProvider().list_apps()))" > /tmp/apps.json
```

Hand that file to the eval owner, who maintains `eval/fixtures/apps.json` and `eval/commands.yaml`. To point the eval at a separate fixture folder: `uv run python -m eval.run_eval --fixtures path/to/dir`.

---

## 9. Offline checklist (the demo runs with wifi OFF)

- [ ] `uv sync` (and pyobjc) done while online. Afterwards use `uv run --offline …` or `export UV_OFFLINE=1`.
- [ ] Bonsai GGUF and Argmax models on disk. `start_all.sh` exports `HF_HOME`, `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, and `HF_HUB_DISABLE_TELEMETRY=1`.
- [ ] `scripts/run_demo.sh status` shows both servers healthy, and the offline check passes (exit 0, no "WIFI IS ON" banner).
- [ ] `curl -s --noproxy '*' 127.0.0.1:8765/health` shows `"bonsai": true`, and `logs/brain.log` has the `planner warm` line.
- [ ] The overlay loads no CDN fonts or scripts.
- [ ] No proxy environment variables point off-box. The brain's own HTTP clients bypass proxies; `curl` needs `--noproxy '*'`.
- [ ] `uv run --offline python -m eval.run_eval --bonsai-url http://127.0.0.1:8080/v1` runs and prints the pass rate. Add `--min-pass 0.85` to make it exit 1 below 85%. Show the scoreboard with `uv run python -m eval.demo_board`.
- [ ] Flows taught during rehearsal live in `~/.siriously/flows.json`. Keep or delete them before the demo, as intended.
- [ ] `uv run python scripts/demo/rehearse.py --live` prints READY 7/7 with wifi off.
- [ ] Accessibility permission is granted to the exact Python binary or terminal you demo from.

---

## 10. Troubleshooting

| Symptom | Likely cause |
|---|---|
| `route{none, reason: "planner error: timeout…"}` on the first command | Cache not warm yet (wait for `planner warm`), or Bonsai was restarted after the brain. |
| `planner error: down…` | :8080 is not up. `scripts/run_demo.sh status` or `./start_all.sh status`. |
| `planner error: bad_output: … not a shown id` | Should not happen; the grammar forbids it. Capture the `--log-level debug` output and the snapshot. |
| Every `press` is followed by `plan{source: replan}` | `changed` is always false and the content re-check finds nothing. Check the executor's before/after fingerprint and that `result.snapshot` is set. |
| A second action of a burst fails "stale" | The executor is not resolving ids against the burst's base `snapshot_id` (section 2.1). |
| `cmd+n` after `open_app` goes to the wrong app | `open_app` returned before the app was frontmost (section 2.2). |
| Exit code 2 with a `brain.mac` message | `brain/mac.py` is missing or failed to import (pyobjc not installed: run `uv sync`). Off-Mac, use `--fixture <name>`. |
| HTTP 415 `UNSUPPORTED_MEDIA_TYPE` | The client did not send `Content-Type: application/json`. |
| HTTP 403 `FORBIDDEN_ORIGIN` or `FORBIDDEN_HOST` | The request carries an `Origin` header, or a `Host` header that is not loopback. Native clients should not send `Origin`. Use `SIRIOUSLY_ALLOWED_ORIGINS` or `SIRIOUSLY_ALLOWED_HOSTS` only if you really need them. |
| HTTP 401 `UNAUTHORIZED` | `SIRIOUSLY_TOKEN` is set and the token is missing or wrong, or it was sent in the URL (`?token=` is refused). |
| `error{"nothing to confirm"}` on "yes" | The confirm expired (`confirm_ttl_s`, 20 s by default), or another utterance dropped it. Say the command again. |
| `error{"cannot confirm: … nothing was executed"}` | The frontmost app changed, or the target is gone since the confirm was shown. Nothing ran. |
| HTTP 504 `TIMEOUT` on `?wait=true` | The utterance exceeded its budget. If it had not started, it was cancelled. |
| No events on connect | Connect with `?history=N` to replay the last N events. |
