#!/usr/bin/env bash
# Re-runnable Bonsai 2 benchmark across runtimes and sizes (plugged in, Low Power Mode off).
#   ./bench_bonsai.sh            # default: 4B + 2B on fork, upstream, and 4B on MLX
# Uses ports 8090 (llama) / 8091 (MLX) so it never collides with start_all.sh (8080/8081).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; EXT="$(dirname "$HERE")"; DEMO="$EXT/Bonsai-demo"
LOG="$EXT/logs/bench_$(date +%Y%m%d-%H%M%S).log"; mkdir -p "$EXT/logs"
SIZES="${SIZES:-4B 2B}"
say_log() { echo "$*" | tee -a "$LOG"; }
wait_up() { for _ in $(seq 1 180); do curl -sf --max-time 1 "$1" >/dev/null 2>&1 && return 0; sleep 1; done; return 1; }
stop_port() { lsof -ti TCP:"$1" -sTCP:LISTEN | xargs kill 2>/dev/null; sleep 2; }

say_log "== $(date) | power: $(pmset -g batt | head -1 | sed "s/Now drawing from //") | lowpowermode=$(pmset -g | awk '/lowpowermode/{print $2}')"

[ -n "${SKIP_BENCH:-}" ] || say_log "== llama-bench (pp512 / tg128, -ngl 99 -fa 1, 3 reps)"
for s in $([ -n "${SKIP_BENCH:-}" ] && echo "" || echo "$SIZES"); do
  G="$DEMO/models/bonsai2-gguf/$s"
  [ -f "$G/Ternary-Bonsai-2-$s-PQ2_0.gguf" ] && "$DEMO/bin/mac/llama-bench" -m "$G/Ternary-Bonsai-2-$s-PQ2_0.gguf" -ngl 99 -fa 1 -p 512 -n 128 -r 3 2>/dev/null | grep -E "pp512|tg128" | sed "s/^/fork     /" | tee -a "$LOG"
  [ -f "$G/Ternary-Bonsai-2-$s-Q2_0.gguf" ] && llama-bench -m "$G/Ternary-Bonsai-2-$s-Q2_0.gguf" -ngl 99 -fa 1 -p 512 -n 128 -r 3 2>/dev/null | grep -E "pp512|tg128" | sed "s/^/upstream /" | tee -a "$LOG"
done

run_smoke() {  # label backend url
  python3 -I "$HERE/bonsai_smoke.py" --label "$1" --backend "$2" --base-url "$3" --runs 5 --max-tokens 256 2>&1 \
    | grep -E "^\[a\]|^\[b\]|MEDIAN|ALL PASS|FAILED" | tee -a "$LOG"
}

for s in $SIZES; do
  G="$DEMO/models/bonsai2-gguf/$s"
  if [ -f "$G/Ternary-Bonsai-2-$s-PQ2_0.gguf" ]; then
    say_log "== server: fork $s PQ2_0"
    ( cd "$DEMO" && BONSAI_MODEL="$s" PORT=8090 nohup ./scripts/start_llama_server.sh > "$EXT/logs/bench_fork_$s.log" 2>&1 & )
    wait_up http://127.0.0.1:8090/health && run_smoke "$s-PQ2_0-fork" llama http://127.0.0.1:8090/v1; stop_port 8090
  fi
  if [ -f "$G/Ternary-Bonsai-2-$s-Q2_0.gguf" ]; then
    say_log "== server: upstream $s Q2_0"
    nohup llama-server -m "$G/Ternary-Bonsai-2-$s-Q2_0.gguf" -ngl 99 -fa on -c 16384 --jinja --host 127.0.0.1 --port 8090 > "$EXT/logs/bench_upstream_$s.log" 2>&1 &
    wait_up http://127.0.0.1:8090/health && run_smoke "$s-Q2_0-upstream" llama http://127.0.0.1:8090/v1; stop_port 8090
  fi
  if [ -d "$DEMO/models/Ternary-Bonsai-2-$s-mlx-2bit" ]; then
    say_log "== server: mlx $s"
    ( cd "$DEMO" && BONSAI_MODEL="$s" PORT=8091 nohup ./scripts/start_mlx_server.sh > "$EXT/logs/bench_mlx_$s.log" 2>&1 & )
    wait_up http://127.0.0.1:8091/v1/models && run_smoke "$s-mlx" mlx http://127.0.0.1:8091/v1; stop_port 8091
  fi
done
say_log "== done -> $LOG"
