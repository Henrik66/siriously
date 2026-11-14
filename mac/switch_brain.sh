#!/bin/zsh
# Swap which brain answers on 127.0.0.1:8765 (the speech daemon posts there; the overlay listens to both).
#   mac/switch_brain.sh mini     Henrik's mini brain (wake word, permissions menu, texting, folder checks)
#   mac/switch_brain.sh team     Hari's brain (brain/: flows, compound commands, eval) on brain/mac.py
#   mac/switch_brain.sh status
# Bonsai (:8080) and the speech daemon keep running. Logs: logs/mini_brain.log, logs/brain.log
cd "${0:A:h}/.." || exit 1
stop() { pkill -f "mac/mini_brain.py"; pkill -f "siriously-brain"; sleep 0.8; }
case "$1" in
  mini)
    stop; nohup uv run -q --script mac/mini_brain.py >> logs/mini_brain.log 2>&1 &
    sleep 3; tail -1 logs/mini_brain.log ;;
  team)
    stop; nohup uv run --offline siriously-brain --mac >> logs/brain.log 2>&1 &
    for i in {1..30}; do sleep 1; curl -s --noproxy '*' -m 1 127.0.0.1:8765/health >/dev/null && break; done
    curl -s --noproxy '*' 127.0.0.1:8765/health; echo
    echo "Wake word, follow-ups and menu permissions: on (brain/addressing.py)." ;;
  status)
    pgrep -fl "mac/mini_brain.py|siriously-brain" | grep -v "uv run" || echo "no brain running"
    curl -s --noproxy '*' -m 1 127.0.0.1:8765/health; echo ;;
  *) echo "usage: $0 mini|team|status"; exit 2 ;;
esac
