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
| `brain/`, `eval/` | *(to be added)* see PRD §4–5 |

## Not in git (machine-local, recreate as needed)

Models, virtual envs, logs, and third-party checkouts are gitignored. On the Mac they live next to this repo:

- `Bonsai-demo/`: `git clone https://github.com/PrismML-Eng/Bonsai-demo && cd Bonsai-demo && BONSAI_MODEL=4B BONSAI_OPENWEBUI=0 BONSAI_CODE_INTERPRETER=0 ./setup.sh`
- `argmax-oss-swift/`: `git clone --branch v1.1.1 https://github.com/argmaxinc/argmax-oss-swift` (needed only to build `smoke/argmax/swift`)
- `models/argmax/`: downloaded on first run of `smoke/argmax/argmax_cli_smoke.sh` and the Swift tests (always with explicit paths, never `~/Documents/huggingface`)
- CLI tools: `brew install whisperkit-cli llama.cpp`

**Off-Mac (teammate):** you only need a Bonsai 2 4B server for development. See PRD §8.
