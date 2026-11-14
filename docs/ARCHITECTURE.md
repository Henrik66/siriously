# Architecture

Siriously turns one spoken sentence into actions in the frontmost Mac app, on-device. This document describes how the pieces fit at branch `feat/brain`. The spec is [`PRD.md`](../PRD.md) (contracts in section 4, requirements in section 5). The Mac side is in [`MAC_INTEGRATION.md`](MAC_INTEGRATION.md).

## 1. Pipeline

```
 mic
  |
  v
[Speech daemon]  Swift, WhisperKit (STT) + SpeakerKit (voice lock)
  |  POST /utterance  {id, text, t_speech_end, stt_ms, speaker{verified, score, name}}
  v
[Brain server]  brain/server.py, 127.0.0.1:8765
  |  AccessGuard: Host, Origin, token, JSON-only POST    SecurityHeaders: on every response
  |  Dispatcher: one worker thread; a verified "stop" cuts in on the request thread
  v
[Brain.handle]  brain/pipeline.py
  |  unverified speaker ----------------------------> event: rejected          (nothing runs)
  |  stop ------------------------------------------> stop() + done (cuts into the running burst)
  |  snapshot = UIProvider.snapshot()
  |  Router.route(text, snapshot)                    brain/router.py
  |     control    -> cancel / undo / yes / no      (regex, brain/control.py)
  |     filler     -> none                          (silence hallucinations: "Thank you.")
  |     teach      -> "call that <name>"            (saves the last successful run)
  |     flows      -> taught flow replay            (brain/flows.py)
  |     compound   -> "open <app> and <rest>"       (open_app now, then route <rest>)
  |     fastpath   -> deterministic matcher         (brain/fastpath.py, no model)
  |     planner    -> Bonsai 2 4B, one of N ids     (brain/planner/, :8080)
  v
[Execution loop]  _exec_plan / _run_flow / _continue / _replan
  |  safety gate: confirm_reason(plan)  -----------> event: confirm  --> "yes" / "no" (TTL 20 s)
  |  per action: Executor.run(base snapshot_id, action) -> ActionResult
  |  failed or no visible effect ---------------> re-plan (max 2, time-budgeted)
  |  navigation (app/window changed) ------------> wait for the new screen, plan the rest
  |  checked between actions: stop generation, 8 s utterance budget
  v
[UIProvider + Executor]  brain/mac.py (Mac: Accessibility API)  or  brain/fixtures.py (fakes)
  |
  v  every step emits an event
[EventBus]  bounded per-client queues, drop-oldest, never blocks the brain
  +--> WS /events  --> overlay (native, Henrik)
  +--> WS /events  --> /console (static page, same origin, debug timeline)
```

Everything above the executor is pure Python with OS-agnostic dependencies (`rapidfuzz`, `fastapi`/`uvicorn`, `websockets`, `pyyaml`). The Mac-specific part is one module behind two interfaces, so the same brain runs on Windows and Linux against fixtures.

## 2. Components

| Module | Role |
|---|---|
| `brain/contracts.py` | Dataclasses and JSON validation for every shape that crosses a boundary: `Utterance`, `UISnapshot`, `Action`, `Plan`, `ActionResult`, `Event`, `Route`, `TargetDescriptor`. Also the `UIProvider`, `Executor`, `Planner`, `EventSink` protocols. |
| `brain/config.py` | `Settings`, read from `SIRIOUSLY_*` environment variables when constructed. |
| `brain/control.py` | Control words (`stop`, `cancel`, `undo`, `yes`, `no`), regex only. |
| `brain/router.py` | Classifies an utterance in the fixed order in section 4. |
| `brain/fastpath.py` | Deterministic matcher: apps, standard commands, keys, scroll, menus and buttons by name, and three templates (create, web search, delete). Also `same_app`, the one app-identity check. |
| `brain/flows.py` | Taught flows: `parse_teach`, `FlowStore` (`~/.siriously/flows.json`), descriptor resolution against a fresh snapshot. |
| `brain/choice.py` | Parses answers to an ambiguity question ("one", "the second one", "the last", or a label). |
| `brain/pipeline.py` | `Brain`: the execution loop, confirm/stop/undo, re-plans, continuation, compound rest, flow replay and planner fallback, events. `build_brain` wires the parts. |
| `brain/safety.py` | `confirm_reason`: which plans need a "yes" (destructive labels, menu paths, shortcuts). |
| `brain/planner/` | `BonsaiPlanner`: ranking (`rank.py`), prompt (`prompt.py`), per-call schema and validation (`schema.py`), stdlib HTTP client (`client.py`). |
| `brain/server.py` | HTTP and WebSocket front door, access guard, dispatcher, runtime assembly (`make_io`, `build_runtime`), `siriously-brain` entry point. |
| `brain/events.py` | `EventBus`: history ring and per-client bounded queues. |
| `brain/cli.py` | Dev CLI: run utterances in-process, `--post` to a running server, `--watch` the event stream. |
| `brain/fixtures.py` | `FixtureUIProvider` and `FakeExecutor` over `eval/fixtures/*.json`. |
| `brain/console/` | Offline debug console at `/console` (timeline, confirm and question banners, stats, eval board). |
| `eval/` | Eval harness. See section 8. |

## 3. Contracts

All ids are integers scoped to one `snapshot_id`. The shapes are in `brain/contracts.py` and match PRD section 4.

- **Utterance** (speech daemon to brain): `id`, `text` (at most 2000 characters, else 422 `BAD_UTTERANCE`), `t_speech_end` (epoch seconds, or 0), `stt_ms`, `speaker` (required, with `verified`). An unverified speaker produces only a `rejected` event.
- **UISnapshot** (provider to brain): `snapshot_id`, `app` (`name`, `bundle_id`), `window`, `focused_id`, `items`, `truncated`. Items are `element` (role, label, value, enabled) or `menu` (path such as `("File", "New Note")`, shortcut such as `cmd+n`). The provider filters to visible, actionable items.
- **Action ops** (closed set): `open_app{app}`, `press{target}`, `menu{target}`, `type{text, target?}`, `keys{keys}`, `scroll{direction, amount}`, `wait{ms}`.
- **Plan**: `snapshot_id`, `actions` (at most 6, `MAX_ACTIONS`), `confirm`, `say`, `ask` (ambiguity: two to four ids for the user to choose), `mode` (`actions` or `dictation`).
- **ActionResult**: `ok`, `changed`, `error`, `snapshot` (the fresh snapshot after the action, when the executor has it).
- **TargetDescriptor**: `kind`, `role`, `label`, `path`, `bundle_id`. Flows store these, never ids.
- **Route**: `kind` (`control`, `flow`, `fastpath`, `planner`, `dictation`, `none`, `rejected`), plus `control`, `flow`, `teach`, `reason`, `ms`, `rest`.
- **Event**: `type` (`heard`, `rejected`, `route`, `plan`, `action`, `confirm`, `done`, `error`), `utterance_id`, `data`, `ms_since_speech_end`.

The executor and provider have no visibility into the planner or the router. The brain never asks the executor for coordinates; the closed op set has no coordinate fields.

## 4. Routing order

`Router._decide` checks in this order. The first match wins, and the model runs only at step 7.

1. **Control words.** Whole-utterance regex, under 1 ms: `stop` (also "wait", "hold on", up to three words), `cancel`, `undo`, `yes` (with a short benign tail such as "yes, delete it"), `no`.
2. **Filler.** Empty text and known silence hallucinations ("Thank you.", "[BLANK_AUDIO]") produce `none`. They never cancel a pending confirm.
3. **Teach.** "call that standup", "name that standup", "remember that as standup" save the last successful run as flow `standup`.
4. **Flow replay.** Exact name, "run / do / play <name>", or fuzzy match (score at least 90, at most five words).
5. **Compound.** "open Notes and start a note called standup": `open_app` runs first, then the rest is routed on the new screen (`route` event with `source: "rest"`). Two apps ("open pages and numbers") become two `open_app` actions.
6. **Fast path.** Under 30 ms, no model. Only short utterances with no clause words ("and", "then", "also"). Templates cover "start / create / make / add a [new] <noun> called <name>" (the matching New menu item, type the name, Return), "search for X" and "go to <domain>" in a browser (address bar), and "delete this note" (the menu-bar Delete item, always confirmed).
7. **Planner.** Bonsai 2 4B on the ranked items. Returns `planner`, `dictation`, `ask`, or an error that becomes `route{none}`.

## 5. Execution loop

- **Bursts.** One planner call returns 1 to 6 actions. The loop runs them in order and re-binds later targets by descriptor when the UI changes, so a burst does not depend on stale ids.
- **What counts as failure.** `ok=false`, or no visible effect for `press`, `menu`, `open_app`, and `keys` combos with `+`. The brain re-snapshots and compares content, so `changed` is not trusted alone. Pressing an item that is already selected, or `open_app` for the frontmost app, counts as success.
- **Re-plan.** On failure the planner is called again with the error text and the list of completed steps (`SIRIOUSLY_MAX_REPLANS`, default 2). Repeated actions are dropped before running. A re-plan is taken only when the remaining budget allows another planner call.
- **Continuation.** If a burst ends by navigating (`open_app`, or a press that changed the window), the brain waits for the new screen and plans only the remaining steps. A continuation answer of `{"a":[]}` means nothing is left.
- **Flow replay.** Each step resolves by descriptor against a fresh snapshot. A later step that does not resolve is polled for about 1 s. If it still does not resolve, the planner plans the remaining steps with the flow as a hint.
- **Ambiguity.** `{"ask":[id,id]}` numbers the candidates on the overlay. The next utterance may choose one ("two", "the second one", or a label) and that candidate runs.
- **Budgets.** `SIRIOUSLY_UTTERANCE_TIMEOUT_S` (default 8 s) is checked between actions and before every planner call. The planner call is capped to the remaining budget.

## 6. Safety model

**Voice lock.** The brain honours `speaker.verified` and nothing else. Speaker verification is the speech daemon's job (SpeakerKit voiceprint, Henrik's threshold). An unverified utterance emits `rejected` and nothing runs, so a judge's "delete everything" changes nothing on screen.

**Confirm before destructive.** `confirm_reason` asks for a "yes" when:
- a press or menu target's label or menu leaf matches `send|delete|remove|quit|erase|empty trash|sign out|log out|move to trash|discard`;
- a shortcut is destructive (`cmd+q`, `cmd+delete`, `cmd+backspace`, `cmd+option+esc`), or the menu item that owns the shortcut in this app is destructive;
- the plan is flagged `confirm`.

When confirmation is required the brain emits `confirm` and stores the pending plan.
- It expires after `SIRIOUSLY_CONFIRM_TTL_S` (default 20 s). A late "yes" is refused with "nothing to confirm".
- Any utterance other than `yes`, `no`, `cancel`, or `stop` drops the pending confirm.
- On "yes" the brain takes a **fresh snapshot** and re-binds every target by descriptor. If the frontmost app changed, or a target is no longer on screen, the action is refused with "nothing was executed". It does not fall back to the old ids.

**Stop.** `stop` never waits for the utterance lock. It is handled on the request thread:
- it bumps a generation counter, clears any pending confirm or question, and cancels queued utterances with an `error` event ("cancelled by stop");
- the running burst checks the generation between actions and ends with `error: "stopped"`;
- an utterance spoken before the stop, even if still queued, is cancelled too.

**Undo.** `undo` calls `Executor.undo()` (best effort, Cmd+Z on the Mac). If it returns false the brain emits "nothing to undo".

**HTTP and WebSocket guard** (`brain/server.py`). The brain drives the user's Mac, and any web page can reach loopback, so:
- The Host header must be loopback, the bind host, or listed in `SIRIOUSLY_ALLOWED_HOSTS`. This blocks DNS rebinding.
- Any request or WS upgrade with an `Origin` is refused (403 `FORBIDDEN_ORIGIN`) unless the origin is in `SIRIOUSLY_ALLOWED_ORIGINS`. `null` is never accepted. The console's own origin is allowed.
- `POST /utterance` and `POST /text` require `Content-Type: application/json` (415 otherwise), so a page cannot send a simple cross-origin POST.
- With `SIRIOUSLY_TOKEN` set, every request except `GET /health` and the console's static files needs the token: `X-Siriously-Token`, or for WS the `Sec-WebSocket-Protocol` values `siriously` and `token.<base64url>`. Comparison is constant time. The token is never accepted in a URL (`?token=` is refused with 401) and never logged.
- Every HTTP response carries `X-Frame-Options: DENY`, a CSP with `frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, and `Referrer-Policy: no-referrer`. The console has no inline script or style.
- The default bind is `127.0.0.1`. A non-loopback bind without a token logs a warning.
- Runtime makes no non-loopback connections. `tests/test_offline.py` blocks them and runs the suite through that block.

## 7. Latency budget and prompt cache

**Targets (PRD section 6), judged on the Mac:** end of speech to first action is at most 1.0 s for the fast path and at most 2.0 s for the planner.

**What the eval measures.** In the e2e eval, `first_action_ms` runs from `Brain.handle()` entry to the first `action` event. It excludes STT. On the Mac, `base.en` takes 147 ms for a 6.1 s clip (measured with the Argmax server, SETUP_REPORT), which is added on top.

**Dev-box numbers** (Windows, Bonsai 2 4B on an AMD iGPU via Vulkan, greedy decoding; not the Mac):

| Path | Result |
|---|---|
| Fast path, end of speech to first action | median 0.28 ms, p90 0.62 ms (most commands) |
| Planner, end of speech to first action | median 1.6 s, p90 2.1 s |
| Planner call (`planner_bench.py`) | median 1.94 s, p90 2.33 s; prompt about 1234 tokens, about 1050 cached; about 17 completion tokens |
| Fast-path matcher on a 300-item snapshot | 0.02 ms |

Mac numbers are the ones that count. Re-measure with `scripts/bench/planner_bench.py` on the Mac, plugged in, Low Power Mode off. Earlier Mac smoke tests on battery with Low Power Mode on (SETUP_REPORT) were worst case: prompt processing 78 to 103 tokens/s and decode 14 to 16 tokens/s.

**Prompt design.** The planner is bounded by prompt processing, so the design keeps the variable part small:
- **Static prefix.** System rules plus eight few-shot turns, byte-identical on every call. The server's prompt cache (`cache_prompt: true`, `id_slot: 0`, one slot via `-np 1`) skips it. `BonsaiPlanner.warmup()` primes the cache at startup, and the brain logs `planner warm (N ms)`.
- **Dynamic suffix, 150 tokens or less.** The top 15 items by fuzzy overlap with the utterance, elements first and then menus. Labels are truncated to 40 characters. Items are dropped from the bottom until the suffix fits. The suffix holds the app, window, optional installed-app list, the items, optional HINT or ERROR, and `USER:`.
- **Per-call schema.** Every target is an `enum` of the ids actually shown, `op` is a fixed set, `actions` has `maxItems: 6`, and `open` is an enum of installed apps that match the utterance (the frontmost app is excluded). The schema is sent as `response_format: json_schema`, so the server's grammar makes other ids impossible to emit.
- **Request settings.** `chat_template_kwargs.enable_thinking=false` (thinking adds seconds), greedy `temperature: 0` by default, and the model-card thinking-off profile is available as `SIRIOUSLY_BONSAI_SAMPLING=card` for A/B tests in the eval.
- **Validation.** The reply is validated again after parsing (ids, apps, key syntax, wait and scroll ranges). An `open_app` for the frontmost app is dropped. When the UI changes mid-burst, a `press` or `menu` target is re-bound only by its descriptor (role, label, menu path), never to a different item.
- **One request at a time.** The planner holds a single lock, which matches the one server slot.

## 8. Eval

`eval/commands.yaml` holds 77 cases. Each case names a fixture, an utterance, and what it should do: a route, a control word, a teach or flow name, `confirm`, or an acceptable action sequence (optional steps allowed, `menu` and its shortcut are equivalent, `wait` is ignored).

**Fixtures.** `eval/fixtures/*.json` describe seven macOS apps (Finder, Notes, System Settings, Calendar, Slack, TextEdit, Safari) plus `apps.json`, the installed-app list. A fixture holds snapshots keyed by `snapshot_id` and transitions between them, and ids stay stable within a fixture. `FakeExecutor` applies the transitions, so a plan can be scored on the screen it reaches.

**Modes.**
- `router` decides with `Router` and executes the plan on `FakeExecutor`. It re-plans across screens itself, up to `--bursts` (default 3), so it measures whether the planner can carry a task across screens.
- `e2e` runs the full `Brain.handle()` (`build_brain` over the fixture provider). Each case is scored on the events, the effective actions, and the final screen. This is the number that matters for the demo.

The router mode runs 74 cases. The e2e mode runs all 77, because some cases declare `modes: [e2e]` and need the full pipeline (for example, a confirm followed by "yes").

**Splits.** `dev` is for prompt and ranking tuning. `holdout` is 32 cases of paraphrases that are reported separately and never used for tuning, so the held-out number cannot be overfit.

**Results.** Each run writes `eval/results/<timestamp>.json`. `eval.demo_board` reads the newest file and prints the scoreboard, and `GET /eval/latest` serves the same headline read-only. An earlier run over 42 cases, before planner tuning and the deterministic fast paths, scored 33/42 (78.6%).

## 9. Known limits

- Bonsai 2 4B chooses and formats; it is not a source of facts. SETUP_REPORT measured wrong one-sentence definitions on 2 of 3 runs with thinking off. The planner's reply schema only allows actions, `ask`, or dictation, so there is no answer-a-question route.
- The Mac provider and executor (`brain/mac.py`, pyobjc) are not in this branch yet. Without them `--mac` exits with a setup error (code 2). `--fixture` runs the same brain against the fakes.
- Dictation (`mode: "dictation"`) is implemented as a route but is not part of the demo.
- Out of scope (PRD section 10): web apps beyond the address-bar templates, screenshots or vision, raw coordinates, and chores longer than six steps.
- Single user, loopback only. The token is optional on loopback and required before binding anywhere else.
