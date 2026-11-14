# Siriously: 60-second demo script

PRD §1: wifi off → one-sentence multi-step task in a native app → a judge tries to hijack it and is
ignored → teach a new command live → replay it instantly. This script adds one safety beat (a delete
that waits for "yes") if time allows.

Every line below is also a step in `scripts/demo/rehearse.py`, so you can check it before you go on stage:

```bash
uv run python scripts/demo/rehearse.py            # in-process, fixtures (+ Bonsai if it is up)
uv run python scripts/demo/rehearse.py --live     # drives the real Mac through the running brain server
uv run python -m eval.demo_board                  # the scoreboard slide (newest eval/results/*.json)
```

`rehearse.py` exit codes: `0` = READY (every step passed), `1` = a step failed, `2` = some steps were
skipped (usually because no model is running).
Every demo line has a deterministic path, so the whole demo also runs with the model down
(`rehearse.py --no-model` → READY 7/7); the model only matters for free-form commands.

---

## 0. Before you walk up (T-10 min)

**Wifi-off checklist (do it in this order):**

- [ ] Bonsai server running on the Mac (`:8080`); `curl -s http://127.0.0.1:8080/health` answers.
- [ ] Brain server running (`siriously-brain`, `:8765`); `curl -s http://127.0.0.1:8765/health` shows
      `"bonsai": true`.
- [ ] Speech daemon running and enrolled to the presenter's voice; overlay visible.
- [ ] **Turn wifi off** (menu bar → Wi-Fi → off). Unplug ethernet too.
- [ ] Prove it: `curl -m 3 https://example.com` must FAIL. (The test `tests/test_offline.py` proves the
      brain never opens a non-loopback socket.)
- [ ] `uv run python scripts/demo/rehearse.py --live` → **READY: 7 pass**. If it says NOT READY, read the
      failing step and use the fallbacks below.
- [ ] Reset the stage: in Notes, delete the "standup" notes the rehearsal made; leave **Finder** in front.
      Old taught flows are fine ("call that standup" overwrites).
- [ ] Warm the planner once (the rehearsal does this). The first uncached call is slow.
- [ ] Optional slide: `uv run python -m eval.demo_board` (pass rate, holdout, latency).

---

## 1. The script

| # | You say (exact words) | The room sees | Notes |
|---|---|---|---|
| 0 | *(no voice)* "Wifi is off. Everything you'll see runs on this laptop." | Menu bar Wi-Fi icon off. | Point at it. |
| 1 | **"Open Notes and start a note called standup."** | Notes comes forward; a new note titled *standup* appears. Overlay: route, plan, actions. | One sentence, several steps. Fast path, no model needed for this exact wording. |
| 2 | *(hand the mic to a judge)* Judge: **"Delete everything."** | Nothing happens. Overlay: *rejected: not your voice*. | Voice lock. Nothing runs, nothing changes on screen. |
| 3 | **"Call that standup."** | Overlay: *saved flow "standup"*. | Teaches the last successful sequence. |
| 4 | **"Open Finder."** | Finder comes forward. | Walk away from Notes so the replay is visible. |
| 5 | **"Standup."** | Notes jumps forward and a new *standup* note appears, instantly. | Replay of the taught flow: no model, no planning. |
| 6 | *(if time)* **"Delete this note."** | Overlay asks to confirm. Nothing is deleted yet. | Safety: send/delete/quit always ask first. Deterministic fast path (Edit › Delete), no model. |
| 7 | **"Yes."** | The note is deleted. | |
| 8 | *(close)* "That's the number we track:" + scoreboard | `eval.demo_board`: pass rate, holdout, latency per route. | Optional slide. |

Speak at a normal pace and pause about half a second after each line. Don't talk over the overlay.

---

## 2. If something misfires (fallback lines)

Say **"stop"** at any time to abort the current burst, or **"undo"** to revert the last change. Both are
control words: a regex handles them, never the model, in under a millisecond.

| Step | Symptom | Say this instead |
|---|---|---|
| 1 | Notes opened but no new note | **"New note."** (fast path, menu File › New Note), then type nothing. The title is optional for the story. |
| 1 | Wrong app or nothing happened | **"Open Notes."**, then **"Start a note called standup."** (both pure fast path) |
| 1 | Slow (planner thinking) | Keep talking: "it reads the menu bar and picks from what's on screen; it can't invent buttons." |
| 2 | The judge's command *did* run | **"Stop."**, then **"Undo."** Say: "voice lock threshold is tuned for this room"; move on. |
| 3 | Overlay says "nothing to save" | Step 1 didn't finish: say **"Open Notes and start a note called standup"** again, then **"Call that standup."** |
| 5 | Replay didn't fire | **"Run standup."** (explicit flow call). If it still misses, redo step 1 and skip the teach story. |
| 6 | No confirmation shown, or it deleted right away | **"Undo."** and say "confirm is required before send, delete or quit". Skip 7. |
| 6 | It asked to confirm the wrong thing | **"No."** (cancels the pending action) and move on. |
| any | Overlay frozen / brain not responding | Switch to the recorded backup clip; the rehearsal output shows the same timeline. |

---

## 3. What to say about the numbers (if asked)

- **Fast path and control words:** sub-millisecond in the brain. The end-to-end latency is dominated by
  speech-to-text.
- **Planner (Bonsai 2 4B, on-device):** about 1.6 to 2 s from end of speech to first action on our dev
  box; it is re-measured on the Mac. A cached static prompt prefix and a small per-screen suffix keep it
  there.
- **Eval:** `eval/commands.yaml` (dev + held-out paraphrases), replayed through the full pipeline against
  recorded macOS fixtures. We report the held-out split separately so prompt tuning can't overfit.
- **Offline:** `tests/test_offline.py` blocks every non-loopback socket and runs the whole suite through it.
