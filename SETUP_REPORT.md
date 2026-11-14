# Hackathon Setup Report

Cornell Local AI Hackathon ("Own Your AI"): check-in Sat Nov 14, kickoff 11:45, building from 12:15, submissions due Sun Nov 15 at noon.
Sponsors: PrismML (Bonsai 2), Argmax (WhisperKit / SpeakerKit / TTSKit), LM Studio.
Log started 2026-11-13. Updated after every phase.

## Status board

| Component | Status | Notes |
|---|---|---|
| Storage location | 🟡 | external drive lost → everything in `~/hackathon` on the internal disk |
| Command Line Tools | 🟢 | 26.6, Swift 6.3.3, macOS SDK 26.5 |
| Power state | 🟢 | plugged in, Low Power Mode = Never (since 12:30 Sat). Low Power Mode numbers below are marked |
| Bonsai 2 4B (llama.cpp fork, PQ2_0) | 🟢 | all smoke checks pass; ~15.6 tok/s (Low Power Mode) |
| Bonsai 2 4B (stock llama.cpp, Q2_0) | 🟢 | all checks pass; fork not required |
| Bonsai 2 4B (MLX / mlx-vlm) | 🟢 | all checks pass; ~13.9 tok/s (Low Power Mode) |
| Bonsai 2B | 🟢 | PQ2_0 + Q2_0 downloaded; fork: TTFT 84 ms, 86 tok/s, ALL PASS. 9B not downloaded |
| LM Studio | ⏳ | app installed; needs one GUI launch |
| WhisperKit STT | 🟢 | **small.en: 10/10 real commands exact**, ~0.43 s per 9 s clip (server). base.en fails on real mic (empty). Live `--stream` mode not tested |
| SpeakerKit diarization | 🟢 | synthetic 2-voice clip correct; real 2-person clip pending |
| Speaker embeddings (go/no-go) | 🟡 | **PARTIAL**: long-paragraph enrollment fails on real voices (clip length dominates). Short-phrase enrollment + centering separated 10/10 commands (margin +0.59), but the sample is small and mic distance is a confound |
| TTSKit | 🟢 | 0.6B: first audio 133 ms, RTF 1.2 (Low Power Mode); audible playback pending |
| Argmax local server | 🟢 | OpenAI audio API from Python, 147 ms median, SSE first event 46 ms |
| start_all.sh | 🟢 | starts Bonsai + Argmax, both health checks pass, stop works |
| UI provider (`mac/ui_provider.py`) | 🟢 | AX snapshots in 47–68 ms; Electron (Slack) supported; fixtures captured to `eval/fixtures/` |
| Executor (`mac/executor.py`) | 🟢 | self-test 18/18 (Calculator + TextEdit): press, menu, type (Unicode), keys, scroll, open_app, undo, terminal / wrong-app guard |
| Speech daemon (`mac/ear`, Swift) | 🟢 | mic → VAD → small.en + voice lock in parallel → POST /utterance. Live: delivered ~0.8 s after you stop talking (600 ms end-of-speech + ~200 ms STT/lock). Models load 0.7 s |
| Voice lock (live, hackathon rooms) | 🟢 | 23 own / 15 impostor clips incl. room chatter; cut-off −0.49; all room chatter since recalibration rejected (−0.8 to −1.8). Re-enroll in the demo room (`siriously-ear enroll`) |
| Voice → action (mini brain) | 🟢 | `mac/mini_brain.py` fast path: new note 4.1 s, undo/close/new tab 1.2–1.4 s, open app 0.75 s (running) to 2.8 s (launch), all from end of speech |
| Offline verification | ⏳ | needs wifi off |

---

## Phase 0: Machine check

| Item | Value |
|---|---|
| CPU / GPU | Apple M3 (arm64), 10-core GPU, Metal 4 |
| macOS | 26.5.2 (25F84). Pending OS updates (26.7.3, 27.2.1) deliberately **not** installed before the event |
| RAM | 16 GB (limits concurrent LLM + STT + TTS) |
| Internal disk | 5.9 GB free at start → 10 GB after cleanup → 8.2 GB after LM Studio + CLIs → **17 GiB** after clearing Chrome cache (2.4 GB) and APFS reclaiming deleted space; still 17 GiB after the CLT update |
| External drive | **lost (2026-11-14).** `$EXTERNAL` = `/Users/hg/hackathon` on the internal APFS disk, outside the iCloud-synced Desktop/Documents. `HF_HOME=$HOME/hackathon/hf` appended to `~/.zshrc` (backup: `~/.zshrc.bak-hackathon`). |
| Spotify cache | cleared 2026-11-14 at the user's request: quit Spotify, deleted `~/Library/Caches/com.spotify.client/Data` (11 GB streaming cache). Downloads live in `Application Support/Spotify/PersistentCache/Storage` (per Spotify's `storage.last-location` pref) and were untouched. Space was first held by 8 hourly Time Machine local snapshots (purgeable); macOS released it within minutes → 17 GiB free with Bonsai installed. |
| Command Line Tools | **26.6** (`sudo softwareupdate -i "Command Line Tools for Xcode 26.6-26.6"`, run by user; first attempt failed: "Not enough free disk space: 10.86 GB required"). Swift 6.3.3, Apple clang 21.0.0, macOS SDK 26.5. No `metal` compiler (CLT-only; not needed for Bonsai 2 wheels or Argmax CoreML). brew still notes that CLT 27.0 exists; ignored. |
| Homebrew | 7.0.12 |
| Python | 3.12.10 (python.org framework, first on PATH); brew 3.12 / 3.13 / 3.14; uv available |
| Node / git / ffmpeg | v20.20.2 (nvm) / 2.50.1 / 7.1.1 |
| Full Xcode | not installed and not needed (see Bonsai and Argmax notes) |

### Cleanup done (approved)
- Old app-updater downloads (Badlion, Slack ShipIt, Granola, Lunar), pnpm metadata cache, `uv cache clean` (1.8 GiB), `pip cache purge`, `brew cleanup -s` (310 MB), `~/Library/Developer/CoreSimulator/Devices` (2.4 GB, no Xcode installed).
- **Measured: 4.5 GB freed** (5.9 → 10 GiB free).
- Chrome cache `~/Library/Caches/Google/Chrome` cleared after the user quit Chrome: 2.4 GB freed.
- Not approved: Spotify cache (11.9 GB in `~/Library/Caches/com.spotify.client`; clear via Spotify → Settings → Storage → Clear cache if space gets tight).

### Installed (internal disk, small)
| Tool | Version | How |
|---|---|---|
| whisperkit-cli (= argmax-cli) | 1.1.1, built with `BUILD_ALL=1` → `transcribe`, `tts`, `diarize`, `serve` | `brew install whisperkit-cli` (bottle, no deps) |
| hf (Hugging Face CLI) | 2.4.0 | `uv tool install huggingface_hub` → `~/.local/bin/hf` |
| LM Studio | 0.4.28 (1.6 GB app) | `brew install --cask lm-studio`, not launched yet |

---

## Research findings (done before the drive arrived)

### Bonsai 2 (PrismML)
- Repo: https://github.com/PrismML-Eng/Bonsai-demo (updated 2026-11-13). PrismML also maintains forks: `llama.cpp`, `mlx`, `mlx-lm`, `mlx-swift`.
- Sizes 2B / 4B / 9B / 27B. Bonsai 2 is **ternary** (~1.75–2 bits/weight). The 1-bit `Q1_0` format belongs to the older Bonsai 1 family.
- Three GGUF packings of the same weights:
  - `PQ2_0`: demo default, faster prompt processing. **PrismML llama.cpp fork only.**
  - `PTQ1_0`: smallest. **Fork only.**
  - `Q2_0`: **works on upstream llama.cpp ≥ b11556.** Older builds load it but give wrong output with no warning.
- Homebrew `llama.cpp` is v0.9.0 (commit `cd959ba`), 554 commits ahead of b11556 → stock brew llama.cpp should run `Q2_0`.
- Fork binaries: the demo pins `prism-b10828-a153697`; the latest fork release is `prism-b12128-d7b2896` (2026-10-31), with macOS arm64 tarballs.
- MLX: Bonsai 2 runs on native `mlx-vlm==0.7.8` (+ `mlx==0.32.3`, `mlx-lm==0.32.0`, `transformers==5.19.0`) from prebuilt wheels. **No Xcode needed.** `mlx_lm.server` cannot load these packs (`prism_hadamard_qwen35` architecture). Only the legacy Bonsai 1 MLX fork build needs full Xcode + Metal Toolchain.
- LM Studio bundles its own engines. Support depends on its bundled llama.cpp / MLX version. **Unconfirmed; must test** (`Q2_0` GGUF).
- File sizes (HF): 4B `PQ2_0` 1.15 GB, `Q2_0` 1.21 GB, `PTQ1_0` 0.95 GB, MLX 1.95 GB · 9B `PQ2_0` 2.42 GB, `Q2_0` 2.56 GB, MLX 3.61 GB (one 3.46 GB file) · 2B `PQ2_0` 0.52 GB.
- Small-model sampling (send on every request; servers do not read min_p/presence_penalty from the model):
  - thinking off: temp 0.7, top_p 0.8, top_k 20, min_p 0, presence_penalty 1.5, penalty window 64
  - thinking on: temp 1.0, top_p 0.95, top_k 20, min_p 0, presence_penalty 1.0
  - llama-server: `chat_template_kwargs.enable_thinking`. mlx-vlm server: top-level `enable_thinking` + `presence_context_size: 64`.
- Thinking is **on by default**. For real-time features and JSON, send thinking off.

### Argmax OSS (argmax-oss-swift v1.1.1, 2026-10-08)
- WhisperKit (Whisper STT), SpeakerKit (Pyannote v4 community-1 diarization), TTSKit (Qwen3-TTS 0.6B / 1.7B), all CoreML.
- CLI `argmax-cli` (Homebrew `whisperkit-cli` is the same binary): `transcribe [--stream]`, `diarize`, `tts`, `serve` (OpenAI Audio API, port 50060: `/v1/audio/transcriptions`, `/v1/audio/translations`, SSE streaming).
- **Default model download location is `~/Documents/huggingface`. On this Mac, `~/Documents` is iCloud-synced.** HF_HOME is ignored (Argmax removed that lookup), and overriding HOME does not help. Fix: always pass explicit paths (`--download-model-path`, `--download-tokenizer-path`, `--model-path`; `downloadBase:` / `tokenizerFolder:` in Swift). `tts` CLI has no download-base flag, so TTS goes through a Swift test with `downloadBase`.
- Building from source needs Swift 6.2 for the full manifest (`Package@swift-6.2.swift`); README says Xcode 16+. CLT 26.6 provides Swift 6.2+; full Xcode should not be required (to verify).
- **Speaker embeddings:** `DiarizationResult.speakerCentroidEmbeddings: [Int: [Float]]` is public (added 2026-06-02, PR #463, in v1.1.0+). Raw embedder space, pre-PLDA. Helpers: `centroidCosineDistance(between:and:)`, `nearestSpeakerCentroid(to:)`. The doc comment explicitly says it is for "linking the same speaker across independent diarize(...) calls", with **no built-in same-speaker threshold** (calibrate it yourself). `centroidSource: .finalAssignment | .trainableOnly`. Not exposed by the CLI → needs Swift.
- **OSS vs Pro:** Pro-only = real-time transcription *with speakers* (Sortformer streaming diarization), custom vocabulary, WebSocket real-time local server (Deepgram-compatible), Android. OSS has: streaming STT, file diarization, TTS, OpenAI-compatible file transcription server.

### Hackathon sample repo (iBrushC/local-ai-hackathon-samples @ 5c72a8d, 2026-11-09)
- `bonsai-models/`: a single-page chat UI (`shared/index.html`) plus two launchers. `gguf/start.sh` downloads the **latest** fork release + `PQ2_0` and runs `llama-server -ngl 99 -fa on -c 32768 --port 8080`. `mlx/start.sh` runs `mlx_vlm.server` on 8080. Same sampling presets as above. Matches our plan.
- `argmax-pro-sdk/`: "Meeting Scribe" CLI using the **Argmax Pro SDK** (private Swift registry, `axst_` secret token to download + `ax_` API key at runtime). Shows LiveTranscriber, real-time speaker labels (`SpeakerKitPro` + Sortformer, up to 8 speakers), custom vocabulary (Parakeet v3), file transcription + Pyannote. **Explicitly untested by the author** (`TODO(verify)`, SDK version unclear: 3.0.6 vs 2.0.9). Pro activates the license online on first use, then caches it in the Keychain for offline runs.
- `windows-shim/`: Python stand-in, not relevant on a Mac.

---

## Phase 1: Bonsai (2026-11-14)

**Setup:** `cd ~/hackathon/Bonsai-demo && BONSAI_MODEL=4B BONSAI_OPENWEBUI=0 BONSAI_CODE_INTERPRETER=0 ./setup.sh` (log: `~/hackathon/logs/bonsai_setup_4B.log`). Installed fork binaries `prism-b10828-a153697` → `bin/mac` (29 MB), `.venv` (164 MB), `.venv-vlm` (783 MB: mlx 0.32.3, mlx-vlm 0.7.8, uv-managed Python 3.11). Repo @ `de058d0`.
Extra: `brew install llama.cpp` → upstream 0.9.0 = build 12110 (commit cd959ba), 21 MB.

**Model files**
| File | Size | Path |
|---|---|---|
| 4B PQ2_0 (fork only) | 1.15 GB | `~/hackathon/Bonsai-demo/models/bonsai2-gguf/4B/Ternary-Bonsai-2-4B-PQ2_0.gguf` |
| 4B Q2_0 (upstream ≥ b11556) | 1.21 GB | `~/hackathon/Bonsai-demo/models/bonsai2-gguf/4B/Ternary-Bonsai-2-4B-Q2_0.gguf` |
| 4B vision projector | 0.37 GB | `~/hackathon/Bonsai-demo/models/bonsai2-gguf/4B/Ternary-Bonsai-2-4B-mmproj-Q8_0.gguf` |
| 4B MLX 2-bit | 1.95 GB | `~/hackathon/Bonsai-demo/models/Ternary-Bonsai-2-4B-mlx-2bit/` |

**Which runtimes work (Bonsai 2 4B)**
| Runtime | File | Result |
|---|---|---|
| PrismML fork `llama-server` (b10828) | PQ2_0 | ✅ chat, JSON schema, streaming |
| PrismML fork `llama-bench` | Q2_0 | ✅ loads and runs |
| **Stock Homebrew llama.cpp 0.9.0** (b12110) | Q2_0 | ✅ chat, JSON schema, streaming: **no fork needed** |
| mlx-vlm 0.7.8 server (`.venv-vlm`) | MLX 2-bit | ✅ chat, JSON schema (enforced via llguidance), streaming |
| LM Studio 0.4.28 | Q2_0 | ⏳ needs one GUI launch before `lms` works |

**Launch commands** (OpenAI-compatible, base URL `http://127.0.0.1:<port>/v1`)
- Fork (default, vision on): `cd ~/hackathon/Bonsai-demo && BONSAI_MODEL=4B ./scripts/start_llama_server.sh` → :8080, ctx 16384, 4 slots
- Stock llama.cpp: `llama-server -m ~/hackathon/Bonsai-demo/models/bonsai2-gguf/4B/Ternary-Bonsai-2-4B-Q2_0.gguf -ngl 99 -fa on -c 16384 --jinja --host 127.0.0.1 --port 8082`
- MLX: `cd ~/hackathon/Bonsai-demo && BONSAI_MODEL=4B ./scripts/start_mlx_server.sh` → :8081
- Smoke test: `python3 ~/hackathon/smoke/bonsai_smoke.py --label <name> [--backend llama|mlx|lmstudio] [--base-url ...]`

**Measured. ⚠️ Battery + Low Power Mode ON (worst case; rerun pending after fixing power)**
| Backend / file | (a) chat | (b) JSON | TTFT median | decode tok/s median | prompt tok/s |
|---|---|---|---|---|---|
| Fork llama-server, PQ2_0 (5 runs) | ✅ | ✅ json_schema | 355 ms | 15.6 | 78 |
| Upstream llama-server, Q2_0 (3 runs) | ✅ | ✅ json_schema | 270 ms | 14.9 | 103 |
| mlx-vlm, MLX 2-bit (3 runs) | ✅ | ✅ json_schema | 355 ms | 13.9 | 95 |

`llama-bench` (pp512 / tg128, `-ngl 99 -fa 1`, 3 reps, same power state): fork PQ2_0 151.5 / 12.8 · fork Q2_0 149.0 / 15.3 · upstream Q2_0 142.7 / 17.3 tok/s. Metal (MTL0: Apple M3) confirmed.
Results JSON: `~/hackathon/smoke/results/bonsai_*.json`.

**Observations**
- All three runtimes give valid schema-constrained JSON (all six fields correct).
- 4B with thinking off produced a **wrong** one-sentence definition of "ternary-weight model" on 2 of 3 runs. Fine for extraction and formatting; don't trust it for facts.
- Prompt caching was disabled in the timed runs (`cache_prompt: false`) so TTFT reflects a fresh prompt.

## Phase 2: Argmax (2026-11-14, Low Power Mode on battery)

**Install:** `brew install whisperkit-cli` (v1.1.1 = argmax-cli with `transcribe/tts/diarize/serve`). Source clone `~/hackathon/argmax-oss-swift` @ v1.1.1 (`1bbc57c`). Swift smoke package `~/hackathon/smoke/argmax/swift` (`speaker-embed`, `tts-smoke`) **builds with CLT 26.6 only, no Xcode** (`swift build -c release`, 163 s; tools-version 6.0 needed for `.macOS(.v15)`, Swift 5 language mode).
**Models (1.8 GB) under `~/hackathon/models/argmax/models/argmaxinc/`:** `whisperkit-coreml/openai_whisper-base.en`, `whisperkit-coreml/openai_whisper-large-v3-v20240930_626MB`, `speakerkit-coreml`, `ttskit-coreml` (Qwen3-TTS 0.6B). Tokenizers: `~/hackathon/models/argmax/tokenizers/` (TTS: `Qwen3-0.6B`). Verified nothing was written to iCloud `~/Documents/huggingface`.
**Pro vs OSS:** everything below uses the open-source SDK, no API key. Pro-only: real-time transcription *with speakers* (Sortformer), custom vocabulary, WebSocket real-time server.

| Test | Command | Result |
|---|---|---|
| STT file, base.en | `smoke/argmax/argmax_cli_smoke.sh` | warm **0.86 s** for 6.1 s clip (RTF 0.14), 18/18 words; cold 26 s (download + CoreML compile) |
| STT file, large-v3 turbo 626 MB | same | warm **3.41 s** (RTF 0.56), 18/18 words; cold 131 s |
| (a) Streaming mic STT | `whisperkit-cli transcribe --stream --model-path …` | ⏳ needs you at the mic |
| (b) Diarization | `whisperkit-cli diarize` on synthetic 2-voice standup (Samantha/Daniel, 13.9 s) | ✅ 2 speakers, turn order B-A-B-A-B correct; 16.3 s incl. model load (in-process: 0.3 s) |
| (c) Speaker embeddings | `swift/.build/release/speaker-embed` | 🟡 see below; real voices pending |
| (d) TTS | `swift/.build/release/tts-smoke` | ✅ first audio **133 ms**, 10.3 s to make 8.6 s of audio (RTF 1.2); load + warm-up 196 s first time (download + compile). Saved `smoke/results/tts_smoke.wav`. Audible `--play` pending |
| (e) Local server from Python | `whisperkit-cli serve … --port 50060` + `smoke/argmax/argmax_server_smoke.py` | ✅ median **147 ms** per 6.1 s clip; SSE first event **46 ms**. `/health` = 200 |
| Chain: STT → Bonsai JSON | both servers up | ✅ 1.1 s STT (first request, cold) + 3.3 s LLM (JSON schema). llama-server footprint 1.28 GB |

### (c) Speaker-embedding go/no-go: evidence so far (synthetic voices)
- `DiarizationResult.speakerCentroidEmbeddings` → **256-d** vector per speaker per run. Each clip below was diarized in a **separate** `diarize()` call. Results: `smoke/results/speaker_embed_synthetic.json`.
- Raw cosine: same voice across runs **0.975–0.976**; different voices 0.822–0.943. Margin only **+0.033** (the raw embeddings share a large common component).
- **Mean-centered cosine** (subtract the mean voiceprint): same voice ≥ **0.544**, different ≤ **0.088**. Margin **+0.456**.
- Naming speakers inside a 2-voice clip (diarized separately) against enrolled voiceprints: both speakers matched correctly. Raw: Daniel 0.884 vs 0.816 runner-up, Samantha 0.898 vs 0.810. Centered: 0.637 vs −0.370 and 0.622 vs −0.073.
- Caveats: macOS `say` voices are easier than real people. The mean for centering must come from a **background set** (with 2 enrolled people, centering on their own mean forces their prints to be opposites). Decide by nearest voiceprint plus a margin over the runner-up, not one fixed raw threshold.
- Verdict so far: **PARTIAL → promising**. Final verdict needs real mic recordings (two takes of you + a second voice + a 2-person clip).

### Plugged-in rerun (12:33, AC power, Low Power Mode off): `smoke/bench_bonsai.sh`
| Model / runtime | TTFT | decode tok/s | llama-bench pp512 / tg128 |
|---|---|---|---|
| 4B fork PQ2_0 | 215 ms | 37.3 | 330 / 41.5 |
| 4B upstream Q2_0 | 172 ms | 35.6 | 321 / 41.1 |
| 4B MLX | 180 ms | 37.1 | — |
| 2B fork PQ2_0 | 84 ms | 85.7 | 863 / 87.6 |
| 2B upstream Q2_0 | 113 ms | 50.4 | 831 / 72.7 |
All ALL PASS (chat + JSON schema). Low Power Mode cost ~2.5–3×.

### STT model choice on real mic speech (9 s clip, `whisperkit-cli serve`)
small.en 431 ms ✅ accurate · large-v3 turbo (1.5 GB) 802 ms ✅ · large-v3 626 MB 1.1 s ✅ · base.en ❌ empty output (even after loudness normalization). Mic capture: ffmpeg avfoundation mislabels the 96 kHz built-in mic (clips ~45% of the requested length and garbled), so `smoke/record.sh` and `voice_session.py` use PortAudio (sounddevice) instead.

### (c) Speaker embeddings: real voices (`smoke/voice_session.py`, 12:51)
- Data: Henrik 2×15 s paragraphs + 5 commands; second voice 1×15 s + 5 commands; 30 s conversation. Built-in mic, hackathon room.
- **Long-paragraph enrollment vs 4 s commands: NO.** All commands nearest to "other". The embedding similarity is dominated by clip length: commands vs commands 0.81–0.97 regardless of speaker; paragraph vs paragraph 0.69–0.89; across lengths 0.41–0.67.
- **Short-phrase enrollment (leave-one-out over commands):** raw 10/10 but margin +0.031. Centered on the mean of short clips, score = sim(Henrik) − sim(other): Henrik min −0.025 vs other max −0.613 → threshold ≈ −0.3 separates **10/10, margin +0.59**.
- Confounds: n = 10 from one session; the second voice was farther from the mic (−37 vs −28 dBFS, SNR proxy 22 vs 31–35 dB), so the score may partly measure distance.
- **Verdict: PARTIAL.** Usable for the demo if enrollment uses 8–10 short phrases and the threshold is calibrated on the day. Plan B: a dedicated short-utterance speaker-verification model.
- STT side: small.en transcribed all 10 commands exactly.

## Phase 3: Hackathon sample repo
Cloned to `~/hackathon/local-ai-hackathon-samples` @ `5c72a8d`. Summary is in "Research findings" above. What it changes:
- `bonsai-models/gguf/start.sh` downloads the **latest** fork release (`prism-b12128`) and its own copy of PQ2_0 (another 1.15 GB) and serves on :8080 with `-c 32768`. Our setup already covers this; to reuse our files, run the fork `llama-server` directly with our model path and `--path ~/hackathon/local-ai-hackathon-samples/bonsai-models/shared` for its chat page. **Both use port 8080; don't run them together.**
- `bonsai-models/shared/index.html` is a handy zero-build chat UI for any of our servers (it has a server-URL box).
- `argmax-pro-sdk/` needs a Pro key (`ax_…`) plus a registry token (`axst_…`), is untested by its author, and needs internet once for license activation. Still pending: whether you'll get a key.

## Phase 4: Offline verification
_Pending: wifi off, then rerun everything._ `~/hackathon/start_all.sh` is written and tested online: `./start_all.sh` (fork 4B + Argmax small.en), `BONSAI=upstream|mlx`, `SIZE=…`, `STT=…`, `./start_all.sh status`, `./start_all.sh stop`. It sets `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1` and loads only local paths.

## Phase 5: Final status and recommendation
_pending_

## Pending checklist (saved 2026-11-14, user will come back to it)
1. Plug in the charger and set System Settings → Battery → Low Power Mode → **Never**, then rerun all benchmarks.
2. LM Studio: open it once, click **Skip** on the welcome screen, then test Bonsai 2 Q2_0 in it (import without copying).
3. Approve extra sizes: 2B (0.5 GB) and/or 9B PQ2_0 (2.4 GB); 9B MLX (3.46 GB file) needs an explicit OK.
4. Mic session (~5 min): live streaming captions; two 20 s takes of the user's voice; 20 s of a second voice; a 30 s two-person conversation → real-voice embedding verdict.
5. Wifi off → Phase 4 offline rerun of everything.
6. Argmax Pro key (if one is handed out): activate while online.

## Open risks (running list)
0. **Low Power Mode + battery** throttles everything. Plug in and set Low Power Mode to Never for the event.
1. Internal disk: ~16 GiB free after setup. Swap was already at 3.1 GB used during testing; close Chrome tabs and other heavy apps during demos.
2. Argmax defaults would download into iCloud-synced `~/Documents`; every Argmax call must pass explicit paths (`--model-path`, `downloadBase:`).
3. Speaker-name captions in real time are a Pro feature. The OSS route is chunked diarization + centroid matching (to be measured).
4. If an Argmax Pro key is handed out at the event, activation needs internet once.
5. LM Studio compatibility with Bonsai 2 unverified.
