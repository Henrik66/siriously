#!/usr/bin/env python3
"""Call the Argmax OSS local server (OpenAI Audio API) from Python. Stdlib only.

Start the server first, e.g.:
  whisperkit-cli serve --model-path <...>/openai_whisper-base.en --download-tokenizer-path <...> --port 50060
Then:
  python3 argmax_server_smoke.py [--url http://localhost:50060/v1] [--file clip.wav] [--runs 3]

Equivalent with the OpenAI SDK (pip install openai):
  OpenAI(base_url="http://localhost:50060/v1", api_key="x").audio.transcriptions.create(file=open(f,"rb"), model="base.en")
"""
import argparse
import json
import os
import statistics
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))


def multipart(fields, file_path):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    with open(file_path, "rb") as f:
        data = f.read()
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{os.path.basename(file_path)}"\r\n'
                  f"Content-Type: audio/wav\r\n\r\n").encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def transcribe(url, path, model, stream):
    fields = {"model": model, "response_format": "verbose_json", "language": "en"}
    if stream:
        fields["stream"] = "true"
    body, ctype = multipart(fields, path)
    req = urllib.request.Request(url + "/audio/transcriptions", data=body, headers={"Content-Type": ctype})
    t0 = time.perf_counter()
    first = None
    text = ""
    with urllib.request.urlopen(req, timeout=300) as r:
        if not stream:
            out = json.load(r)
            return time.perf_counter() - t0, None, out.get("text", "")
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            first = first or time.perf_counter() - t0
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            ev = json.loads(payload)
            text = ev.get("text") or text + ev.get("delta", "")
    return time.perf_counter() - t0, first, text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:50060/v1")
    ap.add_argument("--file", default=os.path.join(os.path.dirname(HERE), "clips", "ref_say.wav"))
    ap.add_argument("--model", default="base.en")
    ap.add_argument("--runs", type=int, default=3)
    a = ap.parse_args()

    totals = []
    for i in range(a.runs):
        dt, _, text = transcribe(a.url, a.file, a.model, stream=False)
        totals.append(dt)
        print(f"run {i+1}: {dt*1000:.0f} ms  | {text.strip()}")
    dt, first, text = transcribe(a.url, a.file, a.model, stream=True)
    print(f"stream: first event {first*1000 if first else float('nan'):.0f} ms, done {dt*1000:.0f} ms | {text.strip()}")
    print(f"MEDIAN non-stream latency: {statistics.median(totals)*1000:.0f} ms for {os.path.basename(a.file)}")


if __name__ == "__main__":
    main()
