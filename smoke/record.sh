#!/usr/bin/env bash
# Record mono 16 kHz WAV from a mic.   ./record.sh clips/A1.wav 20
# List devices:  ffmpeg -f avfoundation -list_devices true -i ""     Pick one with MIC=<index>.
set -euo pipefail
OUT="${1:?usage: record.sh out.wav [seconds]}"; SECS="${2:-20}"; DEV="${MIC:-0}"
mkdir -p "$(dirname "$OUT")"
echo ">>> Recording ${SECS}s from audio device :${DEV} -> $OUT  (talk now)"
ffmpeg -hide_banner -loglevel error -y -f avfoundation -i ":${DEV}" -t "$SECS" -ac 1 -ar 16000 "$OUT"
echo ">>> done: $(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT")s, peak $(ffmpeg -hide_banner -i "$OUT" -af volumedetect -f null - 2>&1 | grep -o 'max_volume: .*')"
