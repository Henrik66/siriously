#!/usr/bin/env bash
# Record mono 16 kHz WAV from a mic via PortAudio (python sounddevice).   ./record.sh clips/A1.wav 20
#   MIC_NAME="MacBook Air Microphone" (default; any substring of the input device name)
#
# Why not ffmpeg? On this Mac the built-in mic runs at 96 kHz and ffmpeg's avfoundation input
# mislabels it: clips come out ~45% of the requested length and garbled. PortAudio/CoreAudio
# converts correctly (verified: 6.00 s requested -> 6.00 s recorded).
set -euo pipefail
OUT="${1:?usage: record.sh out.wav [seconds]}"; SECS="${2:-20}"; DEV="${MIC_NAME:-MacBook Air Microphone}"
mkdir -p "$(dirname "$OUT")"
echo ">>> Recording ${SECS}s from '$DEV' -> $OUT  (talk now)"
uv run -q --no-project --with sounddevice --with numpy python -I - "$OUT" "$SECS" "$DEV" <<'PY'
import sys, wave
import numpy as np, sounddevice as sd
out, secs, name = sys.argv[1], float(sys.argv[2]), sys.argv[3]
devs = [i for i, d in enumerate(sd.query_devices()) if name in d["name"] and d["max_input_channels"] > 0]
if not devs:
    sys.exit(f"no input device matching {name!r}: {[d['name'] for d in sd.query_devices()]}")
sr = 16000
x = sd.rec(int(secs * sr), samplerate=sr, channels=1, dtype="int16", device=devs[0]); sd.wait()
with wave.open(out, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(x.tobytes())
peak = int(np.abs(x).max()); rms = float(np.sqrt((x.astype(float) ** 2).mean()))
db = lambda v: 20 * np.log10(max(v, 1) / 32768)
print(f">>> done: {len(x)/sr:.2f}s, peak {db(peak):.1f} dBFS, rms {db(rms):.1f} dBFS")
PY
