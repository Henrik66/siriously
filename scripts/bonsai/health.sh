#!/usr/bin/env bash
# Health check for the local Bonsai llama-server: /health ok + a 1-token chat with thinking off.
# Usage: scripts/bonsai/health.sh   env: PORT (8080), WAIT_SECONDS (0 = check once). Exit 0 = healthy.
set -euo pipefail
PORT="${PORT:-8080}"
BASE="http://127.0.0.1:$PORT"
deadline=$(( $(date +%s) + ${WAIT_SECONDS:-0} ))
until curl -sf "$BASE/health" | grep -q '"ok"'; do
  if [ "$(date +%s)" -ge "$deadline" ]; then echo "UNHEALTHY: $BASE/health not ok"; exit 1; fi
  sleep 0.5
done
body='{"model":"bonsai","max_tokens":1,"temperature":0,"messages":[{"role":"user","content":"Say OK."}],"chat_template_kwargs":{"enable_thinking":false}}'
start=$(python3 -c 'import time;print(time.time())')
curl -sf -m 60 -H 'Content-Type: application/json' -d "$body" "$BASE/v1/chat/completions" >/dev/null \
  || { echo "UNHEALTHY: chat completion failed"; exit 1; }
ms=$(python3 -c "import time;print(int((time.time()-$start)*1000))")
echo "HEALTHY: $BASE/v1 1-token chat ${ms} ms"
