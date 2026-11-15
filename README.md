# Siriously

Talk to your Mac and it does the thing, in any app, with the wifi off. It only obeys your voice, and you can teach it a flow once and replay it instantly.

Built for the Cornell Local AI Hackathon (Nov 14-15, 2026) on PrismML **Bonsai 2** (reasoning) and Argmax **WhisperKit + SpeakerKit** (speech and voice lock). Everything runs on-device.

Docs: [Architecture](docs/ARCHITECTURE.md) · [Mac integration](docs/MAC_INTEGRATION.md) · [Demo script](scripts/demo/DEMO_SCRIPT.md) · [PRD](PRD.md) · [Mac setup report](SETUP_REPORT.md)

## The 60-second demo

Wifi off. Then:

1. **"Open Notes and start a note called standup."** Notes comes forward and a new note titled *standup* appears. One sentence, several steps, no model call.
2. **A judge says "Delete everything."** Nothing happens. The overlay shows *rejected: not your voice*.
3. **"Call that standup."** The last successful run is saved as a flow.
4. **"Open Finder." Then "Standup."** The saved flow replays instantly.

Optional: **"Delete this note."** asks for confirmation first. **"Yes."** deletes it.

## Why it's different

- **The model is the last resort.** Order: control words (stop, undo, cancel) → taught flows → deterministic fast path → Bonsai planner. Control words and fast-path commands never reach the model. A taught flow reaches it only if a step stops resolving on screen, and then the planner gets just the remaining steps.
- **Bonsai picks one of N.** The planner sees at most 15 on-screen items, each with an id. The per-call JSON schema makes every target an enum of those ids, so the model cannot name anything that is not on screen.
- **Call, don't click.** Actions are menu items, keyboard shortcuts and element presses. The protocol has no screen coordinates.
- **Voice-locked.** Only verified speech runs. An unverified utterance produces a `rejected` event and nothing else.
- **Confirm before destructive.** Send, delete, remove, quit, erase, empty trash and sign out ask for "yes" first. The confirmation expires after 20 s, and the target is re-resolved on a fresh snapshot before anything runs.
- **Offline.** The brain listens on loopback only. `tests/test_offline.py` blocks every non-loopback socket and runs the suite through that block.

## Numbers (Windows dev box)

Bonsai 2 4B on an AMD iGPU via Vulkan, greedy decoding. These are dev-box numbers. The latency targets (fast path ≤ 1.0 s, planner ≤ 2.0 s from end of speech) are judged on the Mac.

| Measure | Result | Notes |
|---|---|---|
| End-to-end eval: 77 cases through the full pipeline, 7 macOS app fixtures | **74/77 = 96.1%** | dev 45/45; holdout 29/32 = 90.6% |
| Router only (no execution) | 71/74 = 95.9% | planner-category cases 36/39 |
| Before planner tuning and the deterministic fast paths | 33/42 = 78.6% | earlier, smaller case set |
| End of speech → first action, fast path | median 0.28 ms, p90 0.62 ms | most commands |
| End of speech → first action, planner | median 1.6 s, p90 2.1 s | Bonsai 2 4B |
| Planner call alone | median 1.94 s, p90 2.33 s | ~1234-token prompt, ~1050 cached, ~17 output tokens |
| Demo rehearsal (`scripts/demo/rehearse.py`) | READY 7/7 | every step deterministic; passes with `--no-model` |
| Tests | 1062 passed | ruff and mypy --strict clean |

The end-to-end latency in the eval runs from `Brain.handle()` entry to the first action. The Mac's speech-to-text adds to it: `base.en` took 147 ms for a 6.1 s clip on the M3.

## Quickstart

Settings are `SIRIOUSLY_*` environment variables (`brain/config.py`). Full list: [docs/MAC_INTEGRATION.md](docs/MAC_INTEGRATION.md#3-running-the-brain) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

### Mac (demo machine)

Models and servers must already be installed (see "Not in git" below and `SETUP_REPORT.md`).

```bash
uv sync                                         # once, online: creates .venv from uv.lock
scripts/run_demo.sh start                       # Bonsai :8080, Argmax STT :50060, brain :8765 (--mac)
# console: http://127.0.0.1:8765/console        (static page, no CDN)
uv run python scripts/demo/rehearse.py --live   # expect READY 7/7; run it again with wifi off
uv run python -m brain.cli --watch --history 20 # live event stream in a terminal
scripts/run_demo.sh status                      # health; exits 3 if the internet is reachable
scripts/run_demo.sh stop
```

Taught flows are stored in `~/.siriously/flows.json` (override with `SIRIOUSLY_FLOWS`). Clear or keep that file before the demo, as you intend.

### Dev box (Windows, no Mac)

```powershell
uv sync
.\scripts\bonsai\setup.ps1                  # once, online: Bonsai 2 4B (Q2_0) + llama.cpp into models\
.\scripts\bonsai\start.ps1                  # llama-server on 127.0.0.1:8080 (Vulkan; -Backend cpu for CPU)
uv run siriously-brain --fixture finder     # brain on 127.0.0.1:8765 with the Finder fixture
# console: http://127.0.0.1:8765/console
uv run python scripts/demo/rehearse.py --no-model   # the 7 demo steps, in-process, no server needed
uv run python -m eval.run_eval --e2e        # router vs end-to-end scores -> eval/results/ (planner cases need :8080; add --no-model to skip)
uv run python -m eval.demo_board            # scoreboard from the newest results file
```

`.\scripts\run_demo.ps1` does the Bonsai start, the brain start, the warm-up wait and the offline check in one command.

## Repo layout

| Path | What |
|---|---|
| `PRD.md` | Product and engineering spec: contracts, requirements, measured facts, milestones |
| `docs/ARCHITECTURE.md` | Pipeline, routing order, execution loop, safety model, latency budget, eval design |
| `docs/MAC_INTEGRATION.md` | Mac wiring: UIProvider/Executor contract, event stream, start order, fixture capture, offline checklist |
| `docs/BUILD_PLAN.md` | Workstream ownership and pinned interfaces (coordination doc) |
| `scripts/demo/DEMO_SCRIPT.md` | The 60-second demo script with fallback lines |
| `SETUP_REPORT.md` | Mac environment setup: runtimes tested, measured numbers |
| `start_all.sh` | Mac: starts Bonsai 2 (:8080) and the Argmax STT server (:50060) offline, with health checks (`start` / `status` / `stop`) |
| `scripts/run_demo.sh`, `scripts/run_demo.ps1` | One-command demo launcher for the Mac and the dev box: stack, brain, warm-up wait, offline check |
| `scripts/bonsai/` | Off-Mac Bonsai server: `setup`, `start`, `health`, `stop` (`.ps1` and `.sh`), stock llama.cpp with Q2_0 |
| `scripts/demo/` | `rehearse.py` (wraps `eval/rehearse.py`): the demo step by step, in-process or `--live` |
| `scripts/bench/planner_bench.py` | Latency bench: planner calls on fixture snapshots, fast-path timing, verdict against the PRD targets |
| `brain/` | The brain (Python). `server.py` (HTTP/WS, access guard), `pipeline.py` (execution loop, safety, flows), `router.py`, `control.py`, `fastpath.py`, `flows.py`, `choice.py`, `safety.py`, `contracts.py`, `config.py`, `events.py`, `fixtures.py`, `cli.py`, `planner/` (Bonsai planner), `console/` (debug page) |
| `brain/mac.py` | Mac UIProvider and MacExecutor (pyobjc, Accessibility). Not in this branch yet: `--mac` exits with code 2 until it is added |
| `eval/` | Eval harness: `commands.yaml` (77 cases, dev and holdout splits), `fixtures/` (7 app fixtures + `apps.json`), `results/`, `run_eval.py`, `demo_board.py`, `rehearse.py` |
| `smoke/` | Bonsai and Argmax smoke tests (stdlib; work against any OpenAI-compatible server), test clips and results |
| `tests/` | pytest suite (1062 tests) |
| `pyproject.toml`, `uv.lock` | Dependencies, locked |

## Ownership

- **Brain and eval** (`brain/`, `eval/`): teammate.
- **Speech daemon, Mac UIProvider/MacExecutor, overlay, Mac fixtures**: Henrik.

## Not in git (machine-local, recreate as needed)

Models, virtual environments, logs and third-party checkouts are gitignored (`.gitignore`: `/models/`, `/logs/`, `/Bonsai-demo/`, `/argmax-oss-swift/`, `/local-ai-hackathon-samples/`, `/hf/`, `.venv*/`, `*.gguf`, `*.safetensors`, `*.mlmodelc/`, `*.mlpackage/`). On the Mac they live next to this repo, in `~/hackathon/`:

- `Bonsai-demo/`: `git clone https://github.com/PrismML-Eng/Bonsai-demo && cd Bonsai-demo && BONSAI_MODEL=4B BONSAI_OPENWEBUI=0 BONSAI_CODE_INTERPRETER=0 ./setup.sh`
- `argmax-oss-swift/`: `git clone --branch v1.1.1 https://github.com/argmaxinc/argmax-oss-swift` (needed only to build `smoke/argmax/swift`)
- `models/argmax/`: downloaded on first run of `smoke/argmax/argmax_cli_smoke.sh` and the Swift tests (always with explicit paths, never `~/Documents/huggingface`)
- CLI tools: `brew install whisperkit-cli llama.cpp`

On the dev box, `scripts/bonsai/setup.*` puts the model and llama.cpp into `models\` inside the repo (gitignored).

**Off-Mac (teammate):** you only need a Bonsai 2 4B server for development. See PRD section 8.

## Note on the git history

The repo was private during the hackathon and made public for judging. Before publishing (Nov 14, evening) the history was rewritten once to remove personal data:

- Raw UI snapshots captured from Henrik's own Mac (`eval/fixtures/mac/*.json`, and the same files at `eval/fixtures/*.json` in the earliest commits) were dropped from every commit. They held real note titles, Slack workspaces and messages, calendar entries and folder names. The eval uses hand-built fixtures with fictional content, which are unchanged.
- A real contact's name used as an example in the texting tests was replaced with a made-up one.

Nothing else changed: same 48 commits, same messages, authors and timestamps, same code. Raw captures are now gitignored.
