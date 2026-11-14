# Siriously demo video: shot list

Siriously's UI is native macOS (an AppKit status card in the top-right corner and a SwiftUI menu bar
panel), so Playwright cannot capture it directly. `scene/index.html` rebuilds those two surfaces in HTML
from `mac/overlay/Sources/siriously-overlay/main.swift` (same 320 px card, 12 px corners, popover material,
13 / 11.5 pt type, icons, copy and states) on a made-up desktop with clean, fictional app windows (no real
contacts or files). Every frame is a pure function of time, so the video is identical on every render.

| # | Shot | Length | What moves inside the UI | Focus (sharp band) |
|---|------|--------|---------------------------|--------------------|
| 1 | **Menu bar panel (hero)** | 3.2 s | The Siriously panel drops from the waveform icon; the Listening switch flips on and the status line changes to "Listening for “Siriously”"; the header waveform starts moving; Recent fills in row by row; "Sending messages" slides from Ask to Allow. | Panel header and Recent list |
| 2 | **Text a friend** | 3.8 s | The card shows the words as they're spoken ("Text Maya I'm running late"); Messages opens Maya's chat and the message types into the field; the card asks "Send “I'm running late” to Maya Chen?"; after "Yes" the blue bubble slides into the chat and the card ticks "Sent · 2.1 s". | Status card + chat input |
| 3 | **Summarize this** | 3.6 s | Over a Safari article, "Summarize this" appears live, the card reads the page (animated dots), then Bonsai's answer streams in word by word, growing the card; the follow-up line starts draining. | Status card |
| 4 | **Voice lock** | 2.6 s | Someone else says "Siriously, delete everything": the card shows a lock, "Not your voice", the quote types in, the lock pulses, Finder's files stay untouched. | Status card |
| 5 | **End card** | 2.6 s | "Siriously" with a live waveform, then "Talk to your Mac." / "Fully offline.", then the on-device stack: Bonsai 2 4B · WhisperKit · SpeakerKit. | Wordmark |

Total about 15.8 s at 60 fps, with a 12-frame fade through black between shots.
Camera: each shot is tilted (rotateX ~45–52°, rotateZ alternating ±20–28°) and drifts slowly along the
plane. Knobs are in `config.mjs`.
