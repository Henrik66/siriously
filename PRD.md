# PRD: Siriously, an offline voice remote for macOS ("Brain" workstream)

**Owner of this doc's scope:** teammate (non-Mac). **Mac side:** Henrik.
**Event:** Cornell Local AI Hackathon. Building Sat Nov 14 12:15 → submissions Sun Nov 15 12:00. **The demo must run with wifi off.**
**Status:** v1, 2026-11-14. Numbers marked *(LPM)* were measured on Henrik's Mac on battery with Low Power Mode on; they are worst-case and will be re-measured.

---

## 1. Product in one paragraph

You talk to your Mac and it does the thing, in **any app**, without the internet. "Open Notes and start a note called standup" runs as one utterance. It **only obeys the enrolled voice**: a judge shouting "delete everything" is ignored. It can be **taught once** ("call that standup") and then replays the flow without the LLM. Speech runs on Argmax (WhisperKit + SpeakerKit); reasoning runs on **PrismML Bonsai 2 4B**, all on-device.

**60-second demo:** wifi off → one-sentence multi-step task in a native app → judge tries to hijack it and gets ignored → teach a new command live → replay it instantly.

## 2. Design principles (non-negotiable)

1. **The model is the last resort.** Order: control words (stop/undo/cancel) → taught flows → fast-path matcher → Bonsai planner. Most commands should never reach the model.
2. **Call, don't click.** Prefer menu-bar items and keyboard shortcuts over pressing elements. **Never use raw screen coordinates.**
3. **Pick one of N.** The model never invents targets. It chooses ids from a short numbered list, and the JSON schema makes any other id impossible to emit.
4. **Bursts, not single steps.** One model call returns 1–6 actions. Re-read the screen only when something unexpected happens.
5. **Show, then act.** Every decision is emitted as an event for the overlay. Confirm before send/delete/quit.
6. **Offline at runtime.** No cloud APIs, no CDN assets, no runtime downloads. Everything installed ahead of time.

## 3. Architecture and ownership

```
 mic ──► [Speech daemon]  (Henrik, Swift: WhisperKit STT + SpeakerKit voice-lock)
              │  POST /utterance  (JSON, §4.1)
              ▼
         [Brain]  (TEAMMATE, Python)  ── WS /events ──► [Overlay] (Henrik)
          router → control | flows | fast-path | planner(Bonsai :8080)
              │  in-process Python interfaces (§4.5)
              ▼
         [UIProvider + Executor]  (Henrik, Python + pyobjc, macOS Accessibility API)
```

- **Teammate owns `brain/` and `eval/`**: router, fast-path matcher, Bonsai planner, execution loop, flow memory, safety rules, event stream, eval harness, plus fakes for developing off-Mac.
- **Henrik owns** the speech daemon, the voice-lock threshold, the Mac UIProvider/Executor (Accessibility tree, `AXManualAccessibility` for Electron apps like Slack/VS Code), the overlay, and the Mac fixtures.
- Final demo: **everything runs on Henrik's Mac in one Python process** (plus the Swift speech daemon and the Bonsai server). Keep `brain/` pure Python with OS-agnostic dependencies.

## 4. Contracts (build these first; both sides code against them)

All ids are integers scoped to one `snapshot_id`. Keep these in `brain/contracts.py` as dataclasses, with matching JSON Schema.

### 4.1 Utterance (speech daemon → brain): `POST http://127.0.0.1:8765/utterance`
```json
{
  "id": "u-0042",
  "text": "open notes and start a note called standup",
  "t_speech_end": 1731600000.12,
  "stt_ms": 150,
  "speaker": {"verified": true, "score": 0.61, "name": "henrik"}
}
```
If `speaker.verified` is false, the brain **does nothing** except emit a `rejected` event. Henrik decides `verified` (threshold calibration is his).

### 4.2 UISnapshot (UIProvider → brain)
```json
{
  "snapshot_id": "s-0192",
  "app": {"name": "Notes", "bundle_id": "com.apple.Notes"},
  "window": "Notes",
  "focused_id": 7,
  "items": [
    {"id": 1,   "kind": "element", "role": "button",   "label": "New Note", "enabled": true},
    {"id": 7,   "kind": "element", "role": "textarea", "label": "Note body", "value": "", "enabled": true},
    {"id": 101, "kind": "menu",    "path": ["File", "New Note"], "shortcut": "cmd+n", "enabled": true}
  ],
  "truncated": false
}
```
Henrik's provider already filters to **visible, actionable** items. The brain does the relevance ranking (§5.3).

**Implemented: `mac/ui_provider.py`** (`MacUIProvider.snapshot()`, `list_apps()`, `activate(name)`, `element_ref(snapshot_id, id)`). What real snapshots look like, so the contracts must accept them:
- One id counter per snapshot: elements first (1..N), then menu items (N+1..).
- Elements carry `frame: [x, y, w, h]` (screen points, for the overlay ring), and `value` for text inputs.
- `role` is the lowercase AX role or subrole: `button, menubutton, popupbutton, checkbox, radiobutton, tab, row, cell, textfield, textarea, searchfield, link, switch, statictext…`. Window chrome appears as `button "Close window" / "Minimize window" / "Full screen"`.
- Menu items have `label: "File › New Note"`, `path`, `shortcut` (e.g. `cmd+n`, `shift+cmd+delete`, `cmd+up`; null if none) and `enabled`. The Services submenu is excluded.
- Extra top-level fields: `ts`, `stats {ms, visited, elements, menus}`. `focused_id` may be null.
- Measured: **47–68 ms per snapshot**. Finder 32 elements / 201 menu items, Notes 34/205, System Settings 28/113, Calendar 22/116, Slack 110/142 (Electron tree enabled automatically).
- Real fixtures (`eval/fixtures/*.json`) contain Henrik's on-screen content, so they are shared privately, not in this public repo.
- On the Mac, the brain needs `pyobjc-framework-ApplicationServices` and `pyobjc-framework-Cocoa`. Add them with a `; sys_platform == 'darwin'` marker so Windows/Linux installs still work.

### 4.3 Plan (brain → executor)
```json
{"snapshot_id": "s-0192", "confirm": false, "say": "New note: standup",
 "actions": [
   {"op": "menu",  "target": 101},
   {"op": "type",  "text": "standup"},
   {"op": "keys",  "keys": "return"}
 ]}
```
**Ops (closed set):** `open_app{app}`, `press{target}`, `menu{target}`, `type{text, target?}`, `keys{keys}` (e.g. `cmd+n`, `return`, `tab`), `scroll{direction: up|down, amount}`, `wait{ms}`.

### 4.4 ActionResult (executor → brain)
```json
{"ok": true, "changed": true, "error": null, "snapshot": { "...UISnapshot after the action..." }}
```

### 4.5 Python interfaces (in-process)
```python
class UIProvider(Protocol):
    def snapshot(self) -> UISnapshot: ...          # frontmost app
    def list_apps(self) -> list[str]: ...           # installed app names, for open_app matching

class Executor(Protocol):
    def run(self, snapshot_id: str, action: Action) -> ActionResult: ...
    def undo(self) -> bool: ...                     # best effort (cmd+z)
```
Teammate ships `FixtureUIProvider` (loads `eval/fixtures/*.json`) and `FakeExecutor` (applies scripted state transitions from fixtures). Henrik ships `MacUIProvider` / `MacExecutor`.

### 4.6 Events (brain → overlay): `WS ws://127.0.0.1:8765/events`
`{"type": "heard|rejected|route|plan|action|done|error|confirm", "utterance_id": "...", "data": {...}, "ms_since_speech_end": 412}`

## 5. Functional requirements (teammate)

### 5.1 Router (R1)
Classify each verified utterance, in this order:
1. **Control:** `stop`, `cancel`, `undo`, `yes/confirm`, `no`. Regex only, never the model, always under 50 ms.
2. **Taught flow:** exact or fuzzy match on a flow name ("standup").
3. **Fast path:** §5.2.
4. **Planner:** §5.3.
5. **Dictation** (stretch): if the planner returns `{"mode": "dictation"}`, type the cleaned text into the focused field.

### 5.2 Fast-path matcher (R2)
No model involved. Patterns: `open|launch|switch to <app>` (fuzzy against `list_apps()`), `new tab|close tab|next tab|previous tab`, `scroll up|down`, `press <key>`, `undo`, `copy|paste|select all`, and **menu items by name** ("File, New Note" / "new note" → fuzzy against `kind=menu` labels when the score is high). Target: **≤ 30 ms** in the brain. Use `rapidfuzz`.

### 5.3 Planner (R3): Bonsai 2 4B
- **Prompt = static prefix + small dynamic suffix.**
  - The static prefix (role, op grammar, 6–8 few-shot examples) is byte-identical on every call, so llama-server's prompt cache skips it.
  - The dynamic suffix:
    ```
    APP: Notes | WINDOW: Notes
    1 button "New Note"
    7 textarea "Note body"
    101 menu "File › New Note" (cmd+n)
    USER: open notes and start a note called standup
    ```
- **Element budget:** pre-rank items by fuzzy overlap with the utterance (plus always-useful menus) and send the **top ~15**. Truncate labels to 40 characters. Keep the dynamic suffix **under ~150 tokens**. Prompt processing is the bottleneck (§7).
- **Output:** schema-constrained JSON (`response_format: json_schema`). Build the schema **per call**:
  - `target` is an `enum` of the ids actually shown, so hallucinated ids are impossible.
  - `op` is an enum.
  - `actions` has `maxItems: 6`.
  - Short keys keep output under ~30 tokens.
- **Request settings:**
  - `chat_template_kwargs: {"enable_thinking": false}`. Required; thinking adds seconds.
  - Start with `temperature: 0`, and A/B against the model card's thinking-off profile (`temperature 0.7, top_p 0.8, top_k 20, min_p 0, presence_penalty 1.5`) using the eval harness.
  - `cache_prompt: true` (the default). Optionally `id_slot: 0` to keep cache hits on one slot.
- **Ambiguity:** if two candidates tie, return `{"ask": [id, id]}` and the overlay numbers them for the user.

### 5.4 Execution loop (R4)
Run the actions of a burst in order. After any action with `changed=false` when a change was expected, or `ok=false`: re-snapshot and re-plan with the error in the prompt. Allow **at most 2 re-plans**, then give up with an `error` event. Global timeout: 8 s per utterance.

### 5.5 Flow memory: "teach once" (R5)
- "call that <name>" saves the **last successful action sequence**.
- Save targets as **descriptors** (`role + label + menu path + app bundle_id`), not ids.
- Replay resolves the descriptors against a fresh snapshot. If any descriptor fails to resolve, fall back to the planner with the flow as a hint.
- Store in `~/.siriously/flows.json`.

### 5.6 Safety (R6)
- Confirm before any action whose target label or menu path matches `send|delete|remove|quit|erase|empty trash|sign out` (`confirm: true`, then wait for "yes").
- `stop` aborts the current burst immediately. `undo` calls `Executor.undo()`.
- Unverified speaker: ignore it and emit `rejected`.

### 5.7 Eval harness (R7): **first deliverable**
- `eval/commands.yaml`: **30+ realistic commands**, each with `fixture`, `utterance`, and `expect` (an acceptable action sequence, or `route: fastpath|planner|control`).
- `eval/run_eval.py` replays them as text through router + planner against fixtures and prints **pass rate, median and p90 latency per route**. It must run offline against a local Bonsai server.
- Every change must move this number. Show the number in the demo.

## 6. Non-functional requirements

| Requirement | Target |
|---|---|
| End of speech → first action, fast path | ≤ 1.0 s on Henrik's Mac |
| End of speech → first action, planner path | ≤ 2.0 s on Henrik's Mac (tight; see §7) |
| Eval pass rate | ≥ 85% on 30 commands |
| Runtime network access | none (wifi off). No CDN fonts or scripts in any UI |
| Language and tooling | Python 3.11+, `uv` with a lockfile, minimal deps (`rapidfuzz`, `websockets` or `fastapi`/`uvicorn`, `pyyaml`). Stdlib HTTP client is fine |
| Portability | `brain/` and `eval/` run unchanged on Windows, Linux and macOS |

## 7. Measured facts (Henrik's Mac: M3, 16 GB, macOS 26.5, plugged in, Low Power Mode off)

| What | Number | Notes |
|---|---|---|
| Bonsai 2 **4B** time to first token | 172–215 ms | ~30-token prompt; fork PQ2_0 / upstream Q2_0 / MLX all similar |
| Bonsai 2 **4B** decode | ~36–41 tok/s | llama-bench tg128 41; server streaming 36–37 |
| Bonsai 2 **4B** prompt processing | ~320–330 tok/s | llama-bench pp512. 150 dynamic tokens ≈ 0.45 s → planner call ≈ **1.2 s** (150 in / 25 out) |
| Bonsai 2 **2B** (fork PQ2_0) | TTFT 84 ms · decode 86 tok/s · prompt 863 tok/s | planner call ≈ **0.5 s**. A one-line switch if 4B is too slow; check quality on the eval |
| Battery + Low Power Mode | everything ~2.5–3× slower | demo plugged in, Low Power Mode = Never |
| JSON-schema output | works on llama-server (grammar) and mlx-vlm (llguidance) | 6/6 fields correct in our test |
| Factual reliability at 4B, thinking off | wrong on 2 of 3 simple definition questions | use it to choose and format, not for facts |
| Argmax STT server, other models | large-v3 turbo: 0.8–1.1 s for a 9 s clip (accurate); `base.en`: **empty text on real mic speech** | OpenAI `/v1/audio/transcriptions` on :50060. Budget ~0.3–0.5 s for STT end to end |
| SpeakerKit voiceprint (voice lock) | 256-d per speaker per run; ~0.3 s per clip | **Real mic, 2 people, 10 short commands:** enrolling with long paragraphs **fails** (clip length dominates the embedding). Enrolling with **short phrases** (like-for-like, mean-centered, score = sim(Henrik) − sim(other), threshold ≈ −0.3) gave **10/10 correct**, margin +0.59 (leave-one-out). Small sample; the other voice was farther from the mic. Henrik owns the threshold; the brain only reads `speaker.verified` |
| STT `small.en` on real commands | **10/10 commands transcribed exactly**; ~0.43 s for a 9 s clip via server | chosen STT model for the speech daemon |
| Bonsai 4B server memory | ~1.3 GB | fine alongside STT on 16 GB |

## 8. Your local setup (Windows/Linux)

Bonsai 2 4B runs off-Mac with the same OpenAI API:
- **Option A (PrismML demo):** `git clone https://github.com/PrismML-Eng/Bonsai-demo`, then `$env:BONSAI_MODEL="4B"; .\setup.ps1; .\scripts\start_llama_server.ps1` → `http://127.0.0.1:8080/v1`. Use `BONSAI_OPENWEBUI=0` to skip extras.
- **Option B (sample repo):** `local-ai-hackathon-samples/bonsai-models/gguf/start.ps1` (or `start.sh 4B`) downloads the fork release and `PQ2_0` 4B and serves :8080.
- **Option C (stock llama.cpp ≥ b11556):** the `Ternary-Bonsai-2-4B-Q2_0.gguf` file with `llama-server -m … -ngl 99 -fa on -c 16384 --jinja --port 8080`. Older builds load it but output garbage silently.
- Always send `chat_template_kwargs.enable_thinking=false` and the sampling fields on every request; the server does not read them from the model.
- Your latency ≠ the Mac's. Develop for correctness on your machine; latency targets are judged on Henrik's Mac.

## 9. Milestones (hours from 12:15 Sat)

| By | Deliverable |
|---|---|
| H+2 | `contracts.py` + JSON Schemas, `FixtureUIProvider`/`FakeExecutor`, Bonsai running locally, eval harness running on 5 stub cases |
| H+3 | **Henrik delivers:** 5 real fixtures (Finder, Notes, System Settings, Calendar, Slack) + `list_apps()` output |
| H+5 | Router + fast path + planner v1; eval baseline on 30 commands |
| H+8 | Execution loop with re-plan, safety rules, event stream, `POST /utterance` server |
| H+10 | Flow memory (teach + replay) |
| H+12 | **Integration on Henrik's Mac** (MacExecutor + speech daemon); latency measured there |
| H+12–20 | Tune prompts and element ranking on real snapshots; eval ≥ 85%; dictation (stretch) |
| Sun 10:00 | **Feature freeze.** Rehearse the demo with wifi off, twice |

## 10. Out of scope

Web apps in the browser, screenshots/vision, raw coordinates, long open-ended multi-app chores (more than 6 steps), spoken replies to every command, any cloud service.

## 11. Open questions

1. Which 30 commands and which 3–4 apps for the demo? (They decide the fast paths.)
2. Bonsai size: 4B is the default. 2B or 9B is a one-line switch if the latency or quality numbers say so after re-measuring plugged in.
3. Slack cannot send with wifi off. Do the wifi-off part of the demo in native apps.
