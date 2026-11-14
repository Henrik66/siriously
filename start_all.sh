#!/usr/bin/env bash
# Start the local AI stack for the hackathon, fully offline, from ~/hackathon.
#
#   ./start_all.sh                 # Bonsai 2 4B (PrismML fork, :8080) + Argmax STT server (:50060)
#   BONSAI=upstream ./start_all.sh # stock Homebrew llama.cpp + Q2_0 on :8080
#   BONSAI=mlx ./start_all.sh      # mlx-vlm server on :8081
#   SIZE=2B|4B|9B  STT=base.en|large-v3-v20240930_626MB  ./start_all.sh
#   ./start_all.sh stop            # stop everything this script started
#   ./start_all.sh status          # health-check only
#
# Logs: ~/hackathon/logs/run_*.log   PIDs: ~/hackathon/logs/*.pid
set -uo pipefail
EXT="$(cd "$(dirname "$0")" && pwd)"
DEMO="$EXT/Bonsai-demo"
ARGMAX_MODELS="$EXT/models/argmax"
LOGS="$EXT/logs"; mkdir -p "$LOGS"
BONSAI="${BONSAI:-fork}"          # fork | upstream | mlx
SIZE="${SIZE:-4B}"
STT="${STT:-base.en}"
STT_PORT="${STT_PORT:-50060}"

# Never phone home: everything below loads from local paths.
export HF_HOME="$EXT/hf" HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1

bonsai_port() { [ "$BONSAI" = mlx ] && echo 8081 || echo 8080; }

health() {   # name url -> 0 when healthy
  local code; code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 "$2" 2>/dev/null)
  [ "$code" = 200 ]
}

wait_healthy() {   # name url timeout_s
  local name="$1" url="$2" t="${3:-120}" i
  for ((i = 0; i < t; i++)); do
    if health "$name" "$url"; then echo "  ✅ $name healthy ($url) after ${i}s"; return 0; fi
    sleep 1
  done
  echo "  ❌ $name NOT healthy after ${t}s; see $LOGS/run_$name.log"; tail -5 "$LOGS/run_$name.log" | sed 's/^/     /'; return 1
}

start_bg() {   # name cmd...   (refuses if the port is taken)
  local name="$1"; shift
  nohup "$@" > "$LOGS/run_$name.log" 2>&1 &
  echo $! > "$LOGS/$name.pid"
  echo "  started $name (pid $!)"
}

stop_all() {
  for f in "$LOGS"/bonsai.pid "$LOGS"/argmax.pid; do
    [ -f "$f" ] || continue
    pid=$(cat "$f"); kill "$pid" 2>/dev/null && echo "stopped $(basename "$f" .pid) (pid $pid)"; rm -f "$f"
  done
  # llama-server is exec'd by the Bonsai launcher; catch anything still on our ports.
  for p in 8080 8081 "$STT_PORT"; do lsof -ti TCP:"$p" -sTCP:LISTEN | xargs kill 2>/dev/null; done
  return 0
}

status() {
  local ok=0
  health bonsai "http://127.0.0.1:8080/health" && echo "  ✅ bonsai (llama-server) :8080" || \
  { health bonsai "http://127.0.0.1:8081/v1/models" && echo "  ✅ bonsai (mlx-vlm) :8081" || { echo "  ❌ bonsai server not responding"; ok=1; }; }
  health argmax "http://127.0.0.1:$STT_PORT/health" && echo "  ✅ argmax STT :$STT_PORT" || { echo "  ❌ argmax STT not responding"; ok=1; }
  return $ok
}

case "${1:-start}" in
  stop) stop_all; exit 0 ;;
  status) status; exit $? ;;
  start) ;;
  *) echo "usage: $0 [start|stop|status]"; exit 2 ;;
esac

PORT=$(bonsai_port)
for p in "$PORT" "$STT_PORT"; do
  if lsof -ti TCP:"$p" -sTCP:LISTEN >/dev/null; then echo "Port $p is already in use. Run '$0 stop' first."; exit 1; fi
done

echo "== Bonsai 2 $SIZE via $BONSAI on :$PORT"
GGUF_DIR="$DEMO/models/bonsai2-gguf/$SIZE"
case "$BONSAI" in
  fork)
    [ -f "$GGUF_DIR/Ternary-Bonsai-2-$SIZE-PQ2_0.gguf" ] || { echo "missing $SIZE PQ2_0 model in $GGUF_DIR"; exit 1; }
    ( cd "$DEMO" && export BONSAI_MODEL="$SIZE" && start_bg bonsai ./scripts/start_llama_server.sh ) ;;
  upstream)
    MODEL="$GGUF_DIR/Ternary-Bonsai-2-$SIZE-Q2_0.gguf"
    [ -f "$MODEL" ] || { echo "missing $MODEL"; exit 1; }
    start_bg bonsai llama-server -m "$MODEL" -ngl 99 -fa on -c 16384 --jinja --host 127.0.0.1 --port 8080 ;;
  mlx)
    [ -d "$DEMO/models/Ternary-Bonsai-2-$SIZE-mlx-2bit" ] || { echo "missing MLX $SIZE model"; exit 1; }
    ( cd "$DEMO" && export BONSAI_MODEL="$SIZE" && start_bg bonsai ./scripts/start_mlx_server.sh ) ;;
  *) echo "BONSAI must be fork|upstream|mlx"; exit 2 ;;
esac

echo "== Argmax STT server ($STT) on :$STT_PORT"
STT_DIR="$ARGMAX_MODELS/models/argmaxinc/whisperkit-coreml/openai_whisper-$STT"
[ -d "$STT_DIR" ] || { echo "missing $STT_DIR"; exit 1; }
start_bg argmax whisperkit-cli serve --model-path "$STT_DIR" --download-tokenizer-path "$ARGMAX_MODELS/tokenizers" \
  --host 127.0.0.1 --port "$STT_PORT"

echo "== Health checks"
rc=0
if [ "$BONSAI" = mlx ]; then wait_healthy bonsai "http://127.0.0.1:8081/v1/models" 180 || rc=1
else wait_healthy bonsai "http://127.0.0.1:8080/health" 180 || rc=1; fi
wait_healthy argmax "http://127.0.0.1:$STT_PORT/health" 180 || rc=1

echo
echo "Bonsai  : http://127.0.0.1:$PORT/v1   (OpenAI chat; send sampling + thinking flags per request)"
echo "Argmax  : http://127.0.0.1:$STT_PORT/v1/audio/transcriptions   (OpenAI audio API)"
echo "Stop    : $0 stop"
exit $rc
