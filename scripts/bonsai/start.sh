#!/usr/bin/env bash
# Start llama-server with Bonsai 2 4B (Q2_0) in the background on 127.0.0.1:${PORT:-8080}, then health-check.
# Usage: scripts/bonsai/start.sh [cpu|vulkan|metal]   env: PORT, CTX (8192), THREADS, BONSAI_LLAMA_BUILD,
#        LLAMA_SERVER (use another binary, e.g. brew's `llama-server`), FOREGROUND=1
# One slot (-np 1) so cache_prompt + id_slot 0 always reuse the planner's static prefix.
set -euo pipefail

KIND="${1:-cpu}"
[ "$(uname -s)" = Darwin ] && KIND="metal"
BUILD="${BONSAI_LLAMA_BUILD:-b12383}"
PORT="${PORT:-8080}"
CTX="${CTX:-8192}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
EXE="${LLAMA_SERVER:-$REPO/models/llama.cpp/$BUILD/$KIND/llama-server}"
MODEL="$REPO/models/bonsai/Ternary-Bonsai-2-4B-Q2_0.gguf"
LOGDIR="$REPO/logs"
LOG="$LOGDIR/bonsai-server-$PORT.log"
PIDFILE="$LOGDIR/bonsai-server-$PORT.pid"

if [ -z "${THREADS:-}" ]; then
  if [ "$(uname -s)" = Darwin ]; then THREADS="$(sysctl -n hw.perflevel0.physicalcpu 2>/dev/null || sysctl -n hw.physicalcpu)"
  elif command -v lscpu >/dev/null; then THREADS="$(lscpu -p=Core | grep -v '^#' | sort -u | wc -l | tr -d ' ')"
  else THREADS="$(nproc)"; fi
fi
command -v "$EXE" >/dev/null || [ -x "$EXE" ] || { echo "missing $EXE; run scripts/bonsai/setup.sh" >&2; exit 1; }
[ -f "$MODEL" ] || { echo "missing $MODEL; run scripts/bonsai/setup.sh" >&2; exit 1; }
if curl -s -o /dev/null "http://127.0.0.1:$PORT/health"; then
  echo "port $PORT already serving; stop it with scripts/bonsai/stop.sh" >&2; exit 1
fi

args=(-m "$MODEL" -c "$CTX" --jinja --host 127.0.0.1 --port "$PORT" -t "$THREADS" -np 1 -fa on --alias bonsai --no-webui)
[ "$KIND" = cpu ] && args+=(-ngl 0) || args+=(-ngl 99)

echo "llama-server $BUILD ($KIND, $THREADS threads, ctx $CTX) -> http://127.0.0.1:$PORT/v1"
if [ "${FOREGROUND:-0}" = 1 ]; then exec "$EXE" "${args[@]}"; fi
mkdir -p "$LOGDIR"
nohup "$EXE" "${args[@]}" >"$LOG" 2>&1 &
echo $! >"$PIDFILE"
echo "pid $(cat "$PIDFILE"), log $LOG"
PORT="$PORT" WAIT_SECONDS="${WAIT_SECONDS:-120}" "$(dirname "$0")/health.sh" || { tail -n 20 "$LOG"; exit 1; }
