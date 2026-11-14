# Siriously

Talk to your Mac and it does the thing, in any app, with the wifi off. It only obeys the enrolled voice, and you can teach it a flow once and replay it.
Built for the Cornell Local AI Hackathon (Nov 14–15, 2026) on PrismML **Bonsai 2** (reasoning) and Argmax **WhisperKit + SpeakerKit** (speech + voice lock). Everything runs on-device.

**Start here: [`PRD.md`](PRD.md)**, the spec, contracts, and milestones for the brain workstream.

## Layout

| Path | What |
|---|---|
| `PRD.md` | Product + engineering spec (brain workstream, interfaces, measured numbers) |
| `SETUP_REPORT.md` | How the Mac environment was set up and verified, with measurements |
| `start_all.sh` | Mac only: starts Bonsai 2 (`:8080`) + Argmax STT server (`:50060`) offline, with health checks (`start` / `status` / `stop`) |
| `smoke/bonsai_smoke.py` | Bonsai smoke test: chat, schema-constrained JSON, TTFT and tok/s. Stdlib only; works against any OpenAI-compatible server |
| `smoke/argmax/` | Argmax smoke tests: CLI STT + diarization script, Python client for the STT server, Swift package (`speaker-embed` voiceprint test, `tts-smoke`) |
| `smoke/clips/`, `smoke/results/` | Test audio (synthetic macOS voices) and results |
| `brain/` | The brain (Python): router (control → taught flows → fast path → Bonsai planner), execution loop with re-plans + confirm, flow memory, HTTP `/utterance` + WS `/events` server, CLI. Contracts in `brain/contracts.py` |
| `brain/planner/` | Bonsai 2 planner: item ranking, cached prompt prefix, per-call JSON schema (ids as enums), stdlib client |
| `eval/` | Eval harness: `eval/commands.yaml` replayed against `eval/fixtures/*.json`; prints pass rate + latency per route |
| `scripts/bonsai/` | Off-Mac Bonsai server: `setup` / `start` / `health` / `stop` (`.ps1` and `.sh`), stock llama.cpp + `Q2_0` |
| `docs/MAC_INTEGRATION.md` | Mac integration guide: `brain.mac` contract, event stream, start order, fixture capture, offline checklist |

## Running the brain

```bash
uv sync                                                    # once, online
./start_all.sh                                             # Mac: Bonsai :8080 + Argmax STT :50060
uv run siriously-brain --mac                               # Mac: brain on 127.0.0.1:8765 (warms Bonsai)
uv run siriously-brain --fixture notes                     # anywhere: fixtures + fake executor
uv run python -m brain.cli --post "open notes and start a note called standup"
uv run python -m eval.run_eval --bonsai-url http://127.0.0.1:8080/v1   # pass rate + latency
```

Off-Mac Bonsai: `scripts/bonsai/setup.ps1` then `scripts/bonsai/start.ps1` (or the `.sh` versions).
Settings are `SIRIOUSLY_*` env vars (`brain/config.py`). Mac wiring: [`docs/MAC_INTEGRATION.md`](docs/MAC_INTEGRATION.md).

## Not in git (machine-local, recreate as needed)

Models, virtual envs, logs, and third-party checkouts are gitignored. On the Mac they live next to this repo:

- `Bonsai-demo/`: `git clone https://github.com/PrismML-Eng/Bonsai-demo && cd Bonsai-demo && BONSAI_MODEL=4B BONSAI_OPENWEBUI=0 BONSAI_CODE_INTERPRETER=0 ./setup.sh`
- `argmax-oss-swift/`: `git clone --branch v1.1.1 https://github.com/argmaxinc/argmax-oss-swift` (needed only to build `smoke/argmax/swift`)
- `models/argmax/`: downloaded on first run of `smoke/argmax/argmax_cli_smoke.sh` and the Swift tests (always with explicit paths, never `~/Documents/huggingface`)
- CLI tools: `brew install whisperkit-cli llama.cpp`

**Off-Mac (teammate):** you only need a Bonsai 2 4B server for development. See PRD §8.
