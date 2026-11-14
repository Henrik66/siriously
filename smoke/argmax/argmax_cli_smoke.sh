#!/usr/bin/env bash
# Argmax OSS CLI smoke tests (whisperkit-cli == argmax-cli v1.1.1 from Homebrew).
# All models live under $EXTERNAL/models/argmax. Re-runnable offline once models exist.
#
#   ./argmax_cli_smoke.sh                 # STT file latency for each model + diarization
#   STT_MODELS="base.en" ./argmax_cli_smoke.sh
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; SMOKE="$(dirname "$HERE")"; EXT="$(dirname "$SMOKE")"
M="$EXT/models/argmax"; TOK="$M/tokenizers"; CLIPS="$SMOKE/clips"; OUT="$SMOKE/results"
mkdir -p "$M" "$TOK" "$CLIPS" "$OUT"
CLI="${CLI:-whisperkit-cli}"
STT_MODELS="${STT_MODELS:-base.en large-v3-v20240930_626MB}"
LOG="$OUT/argmax_cli_$(date +%Y%m%d-%H%M%S).log"
now() { perl -MTime::HiRes=time -e 'printf "%.3f", time'; }
say_log() { echo "$*" | tee -a "$LOG"; }

REF="The quick brown fox jumps over the lazy dog. Local models keep your data on your own laptop."
if [ ! -f "$CLIPS/ref_say.wav" ]; then
  say -o "$CLIPS/ref_say.aiff" "$REF" && ffmpeg -loglevel error -y -i "$CLIPS/ref_say.aiff" -ac 1 -ar 16000 "$CLIPS/ref_say.wav"
fi
DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$CLIPS/ref_say.wav")
say_log "== STT file latency (clip ${DUR}s) | $($CLI --version)"

model_dir() { find "$M" -maxdepth 6 -type d -name "openai_whisper-$1" 2>/dev/null | head -1; }

for model in $STT_MODELS; do
  for pass in cold warm; do
    dir="$(model_dir "$model")"
    if [ -n "$dir" ]; then src=(--model-path "$dir"); else src=(--model "$model" --download-model-path "$M"); fi
    t0=$(now)
    text=$($CLI transcribe "${src[@]}" --download-tokenizer-path "$TOK" --audio-path "$CLIPS/ref_say.wav" 2>>"$LOG" | tail -1)
    rc=$?; t1=$(now)
    acc=$(python3 -I - "$REF" "$text" <<'PY'
import re, sys
norm = lambda s: re.sub(r"[^a-z ]", "", s.lower()).split()
ref, hyp = norm(sys.argv[1]), set(norm(sys.argv[2]))
print(f"{sum(w in hyp for w in ref)}/{len(ref)} words")
PY
)
    say_log "$(printf '%-28s %-4s rc=%s  %6.2fs  RTF %.3f  %s  | %s' "$model" "$pass" "$rc" "$(echo "$t1-$t0" | bc)" "$(echo "($t1-$t0)/$DUR" | bc -l)" "$acc" "$text")"
  done
done

# ---- diarization: real 2-speaker clip if recorded (clips/AB.wav), else a synthetic one from two macOS voices
if [ ! -f "$CLIPS/AB.wav" ] && [ ! -f "$CLIPS/AB_say.wav" ]; then
  V1="${V1:-Samantha}"; V2="${V2:-Daniel}"
  i=0; : > "$CLIPS/ab_list.txt"
  for line in "$V1|Good morning everyone, let's start the standup." "$V2|Sure. Yesterday I finished the speech pipeline." \
              "$V1|Great, any blockers on the diarization work?" "$V2|Only that the drive was full, but that is fixed now." \
              "$V1|Perfect. Let's demo it at noon."; do
    i=$((i+1)); v="${line%%|*}"; t="${line#*|}"
    say -v "$v" -o "$CLIPS/ab_$i.aiff" "$t" && ffmpeg -loglevel error -y -i "$CLIPS/ab_$i.aiff" -ac 1 -ar 16000 "$CLIPS/ab_$i.wav"
    echo "file '$CLIPS/ab_$i.wav'" >> "$CLIPS/ab_list.txt"
  done
  ffmpeg -loglevel error -y -f concat -safe 0 -i "$CLIPS/ab_list.txt" -c copy "$CLIPS/AB_say.wav"
  rm -f "$CLIPS"/ab_*.aiff "$CLIPS"/ab_[0-9].wav "$CLIPS/ab_list.txt"
fi
AB="$CLIPS/AB.wav"; [ -f "$AB" ] || AB="$CLIPS/AB_say.wav"
spk_dir="$(find "$M" -maxdepth 5 -type d -name speakerkit-coreml 2>/dev/null | head -1)"
if [ -n "$spk_dir" ]; then dsrc=(--model-path "$spk_dir"); else dsrc=(--download-model-path "$M"); fi
say_log "== Diarization on $(basename "$AB") ($(ffprobe -v error -show_entries format=duration -of csv=p=0 "$AB")s)"
t0=$(now)
$CLI diarize "${dsrc[@]}" --audio-path "$AB" --rttm-path "$OUT/$(basename "$AB" .wav).rttm" --verbose >>"$LOG" 2>&1
rc=$?; t1=$(now)
say_log "diarize rc=$rc in $(echo "$t1-$t0" | bc)s; speakers in RTTM: $(awk '{print $8}' "$OUT/$(basename "$AB" .wav).rttm" 2>/dev/null | sort -u | tr '\n' ' ')"
awk '{printf "  %-10s %6.2f -> %6.2f\n", $8, $4, $4+$5}' "$OUT/$(basename "$AB" .wav).rttm" 2>/dev/null | tee -a "$LOG"
echo "log: $LOG"
