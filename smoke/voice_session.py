#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["sounddevice", "numpy"]
# ///
"""Guided voice session: records everything for the voice-lock go/no-go, then scores it.

Run in a terminal tab (live prompts):   cd ~/hackathon/smoke && ./voice_session.py
Needs: a second voice (teammate, or a YouTube clip playing near the laptop) for parts 3-4.

Records (MacBook Air Microphone, 16 kHz mono) into clips/real/session/:
  1. henrik_enroll_1/2  : you read two short paragraphs (enrollment)
  2. henrik_cmd_1..5    : you say 5 short commands (what the voice lock will actually see)
  3. other_enroll_1     : second voice reads a paragraph
  4. other_cmd_1..5     : second voice says 5 commands ("hijack" attempts)
  5. convo              : 30 s two-person conversation (diarization check)
Then: every clip diarized in its own run → voiceprints → accept/reject table + verdict,
and each command transcribed with small.en (STT accuracy on real commands).
"""
import json
import math
import os
import subprocess
import sys
import time
import wave

import numpy as np
import sounddevice as sd

HERE = os.path.dirname(os.path.abspath(__file__))
EXT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "clips", "real", "session")
SYNTH = os.path.join(HERE, "clips", "synth")
MODELS = os.path.join(EXT, "models", "argmax")
EMBED = os.path.join(HERE, "argmax", "swift", ".build", "release", "speaker-embed")
STT_DIR = os.path.join(MODELS, "models", "argmaxinc", "whisperkit-coreml", "openai_whisper-small.en")
MIC = os.environ.get("MIC_NAME", "MacBook Air Microphone")
SR = 16000

P1 = ("Siriously is a voice remote for the Mac that runs completely offline. It listens for my voice, "
      "understands what I want, and then clicks through the apps for me, using small models that fit on a laptop.")
P2 = ("This morning I made coffee, checked my messages, and walked to the hackathon in the cold. The plan is "
      "simple: get speech working first, then the planner, then a demo that works with the wifi switched off.")
P3 = ("Hi, I'm the second voice for this test. I'm reading a few sentences so the system learns what I sound "
      "like, and can check that it never confuses me with Henrik. Everything stays on the laptop.")
HENRIK_CMDS = ["Open Notes and start a note called standup", "Switch to Safari", "Scroll down",
               "Move today's screenshots into a folder called demo", "Undo that"]
OTHER_CMDS = ["Delete everything on the desktop", "Open System Settings", "Close this window",
              "Send a message to Henrik", "Empty the trash"]


def mic_index():
    for i, d in enumerate(sd.query_devices()):
        if MIC in d["name"] and d["max_input_channels"] > 0:
            return i
    sys.exit(f"No input device matching {MIC!r}")


def record(name, secs, prompt):
    path = os.path.join(OUT, f"{name}.wav")
    print(f"\n   {prompt}")
    for c in (3, 2, 1):
        print(f"   {c}...", end=" ", flush=True)
        time.sleep(0.7)
    print(f"\n   🔴 RECORDING {secs}s ", end="", flush=True)
    x = sd.rec(int(secs * SR), samplerate=SR, channels=1, dtype="int16", device=DEV)
    for _ in range(int(secs)):
        time.sleep(1)
        print("▮", end="", flush=True)
    sd.wait()
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(x.tobytes())
    rms = float(np.sqrt((x.astype(float) ** 2).mean()))
    print(f"  ✅ saved ({20 * math.log10(max(rms, 1) / 32768):.0f} dBFS)")
    return path


def wait(msg):
    input(f"\n>>> {msg}  [press Enter when ready] ")


def cos(a, b):
    a, b = np.asarray(a), np.asarray(b)
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def dominant(clip):
    if not clip["speakingSeconds"]:
        return None
    sid = max(clip["speakingSeconds"], key=clip["speakingSeconds"].get)
    return clip["centroids"].get(sid)


def transcribe(path):
    r = subprocess.run(["whisperkit-cli", "transcribe", "--model-path", STT_DIR,
                        "--download-tokenizer-path", os.path.join(MODELS, "tokenizers"), "--audio-path", path],
                       capture_output=True, text=True)
    return (r.stdout.strip().splitlines() or [""])[-1].strip()


def analyse(report_path):
    rep = json.load(open(report_path))
    clips = {c["label"].split("#")[0] if c["role"] == "test" else c["label"]: c for c in rep["clips"]}
    enroll = [c for c in rep["clips"] if c["role"] == "enroll"]
    prints = [(c["label"].split("#")[0], dominant(c)) for c in enroll if dominant(c)]
    mu = np.mean([v for _, v in prints], axis=0)            # background mean: all enrolled voices
    names = sorted({n for n, _ in prints})
    proto = {n: np.mean([v for m, v in prints if m == n], axis=0) for n in names}

    rows = []
    for c in rep["clips"]:
        if c["role"] != "test" or not c["label"].startswith(("henrik_cmd", "other_cmd")):
            continue
        v = dominant(c)
        who = "henrik" if c["label"].startswith("henrik") else "other"
        if v is None:
            rows.append((c["label"], who, None, None, None, "no speech detected")); continue
        raw = cos(v, proto["henrik"])
        cen = {n: cos(np.asarray(v) - mu, proto[n] - mu) for n in names}
        best = max(cen, key=cen.get)
        rows.append((c["label"], who, raw, cen["henrik"], best, ""))

    print("\n================ VOICE LOCK: does a short command match Henrik's voiceprint? ================")
    print(f"{'clip':<14} {'truth':<7} {'raw cos':>8} {'centered':>9}  nearest   transcript (small.en)")
    for label, who, raw, cen_h, best, note in rows:
        t = transcribe(os.path.join(OUT, f"{label}.wav"))
        if raw is None:
            print(f"{label:<14} {who:<7} {'-':>8} {'-':>9}  {note:<9} {t}")
        else:
            print(f"{label:<14} {who:<7} {raw:8.3f} {cen_h:9.3f}  {best:<9} {t}")

    for kind, idx in (("raw", 2), ("centered", 3)):
        h = [r[idx] for r in rows if r[1] == "henrik" and r[idx] is not None]
        o = [r[idx] for r in rows if r[1] == "other" and r[idx] is not None]
        if h and o:
            margin = min(h) - max(o)
            thr = (min(h) + max(o)) / 2
            print(f"{kind:>9}: henrik min {min(h):.3f} | other max {max(o):.3f} | margin {margin:+.3f} | threshold ~{thr:.3f}")
    h = [r[3] for r in rows if r[1] == "henrik" and r[3] is not None]
    o = [r[3] for r in rows if r[1] == "other" and r[3] is not None]
    if h and o:
        m = min(h) - max(o)
        verdict = "WORKS" if m >= 0.10 else ("PARTIAL" if m > 0 else "NO")
        print(f"\nVERDICT (centered): {verdict}. All 5 of your commands are accepted and all 5 hijacks rejected "
              f"only if the margin is > 0 (got {m:+.3f}).")
    if "convo" in clips:
        c = clips["convo"]
        print(f"\nConversation: {c['speakerCount']} speakers found, talk time "
              + ", ".join(f"spk{k}={v:.1f}s" for k, v in sorted(c["speakingSeconds"].items())))
        for sid, v in sorted(c["centroids"].items()):
            cen = {n: cos(np.asarray(v) - mu, proto[n] - mu) for n in names}
            print(f"   spk{sid} -> {max(cen, key=cen.get)}  ({', '.join(f'{n} {s:.2f}' for n, s in cen.items())})")


def main():
    global DEV
    os.makedirs(OUT, exist_ok=True)
    DEV = mic_index()
    if len(sys.argv) > 1 and sys.argv[1] == "--analyse-only":
        return run_embed_and_analyse()
    print("Siriously voice session: about 4 minutes. Speak at normal volume, about 50 cm from the laptop.")
    wait("PART 1/5, Henrik: read the paragraph shown after the countdown")
    record("henrik_enroll_1", 15, f'Read: "{P1}"')
    record("henrik_enroll_2", 15, f'Read: "{P2}"')
    wait("PART 2/5, Henrik: 5 short commands, one per countdown, say it once naturally")
    for i, c in enumerate(HENRIK_CMDS, 1):
        record(f"henrik_cmd_{i}", 4, f'Say: "{c}"')
    wait("PART 3/5, SECOND VOICE: hand over (or start the YouTube clip). Henrik stays silent")
    record("other_enroll_1", 15, f'Second voice reads: "{P3}"  (YouTube: just let it play)')
    wait("PART 4/5, SECOND VOICE: 5 'hijack' commands (YouTube: just let it play)")
    for i, c in enumerate(OTHER_CMDS, 1):
        record(f"other_cmd_{i}", 4, f'Second voice says: "{c}"')
    wait("PART 5/5, BOTH: 30 s conversation, take turns of at least 3 s each")
    record("convo", 30, "Talk with each other naturally, taking turns.")
    run_embed_and_analyse()


def run_embed_and_analyse():
    report = os.path.join(HERE, "results", f"voice_session_{time.strftime('%Y%m%d-%H%M%S')}.json")
    args = [EMBED, "--models", MODELS, "--out", report]
    for n in (1, 2):
        args += ["--enroll", f"henrik={OUT}/henrik_enroll_{n}.wav"]
    args += ["--enroll", f"other={OUT}/other_enroll_1.wav"]
    for v in ("samantha", "daniel", "karen"):          # extra background voices for mean-centering
        for n in (1, 2):
            args += ["--enroll", f"{v}={SYNTH}/{v}_{n}.wav"]
    for who in ("henrik", "other"):
        for i in range(1, 6):
            args += ["--test", f"{who}_cmd_{i}={OUT}/{who}_cmd_{i}.wav"]
    args += ["--test", f"convo={OUT}/convo.wav@2"]
    print("\nScoring (each clip diarized in its own run)...")
    subprocess.run(args, check=True, capture_output=True)
    analyse(report)
    print(f"\nDetails: {report}")


if __name__ == "__main__":
    main()
