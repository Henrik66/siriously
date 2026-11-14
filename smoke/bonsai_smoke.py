#!/usr/bin/env python3
"""Bonsai 2 smoke test against a local OpenAI-compatible server.

  (a) chat completion: prints the reply
  (b) schema-constrained JSON extraction: validates structure and content
  (c) time-to-first-token and decode tokens/sec over N streamed runs (after 1 warm-up)

Standard library only, so it runs in any Python 3.9+ with no network or venv.

Usage:
  python3 bonsai_smoke.py --label 4B-PQ2_0                       # llama-server on :8080
  python3 bonsai_smoke.py --label 4B-mlx --backend mlx --base-url http://127.0.0.1:8081/v1
  python3 bonsai_smoke.py --label 4B-Q2_0-lmstudio --backend lmstudio --base-url http://127.0.0.1:1234/v1
Results are written to ./results/bonsai_<label>_<timestamp>.json next to this script.
"""
import argparse
import datetime
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def http_json(url, body=None, timeout=600):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def sse_events(resp):
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            return
        yield json.loads(payload)


def sampling(backend, thinking=False):
    """Bonsai 2 small-model profiles from Bonsai-demo/SMALL-MODELS.md."""
    if thinking:
        s = {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 1.0}
    else:
        s = {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0, "presence_penalty": 1.5}
    if backend == "mlx":  # native mlx-vlm server
        s.update(enable_thinking=thinking, presence_context_size=64, repetition_penalty=1.0)
    else:  # llama-server (PrismML fork or upstream) and LM Studio's llama.cpp engine
        s.update(chat_template_kwargs={"enable_thinking": thinking}, repeat_penalty=1.0, repeat_last_n=64)
    return s


def pick_model(base):
    data = http_json(base + "/models", timeout=10).get("data", [])
    if not data:
        sys.exit("No models listed at /v1/models. Is the server up and the model loaded?")
    loaded = [m for m in data if m.get("loaded")]
    return (loaded or data)[0]["id"]


# ---------------------------------------------------------------- (a) chat
def test_chat(base, model, backend):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "In one sentence: what is a ternary-weight language model?"}],
        "max_tokens": 200,
        **sampling(backend),
    }
    t0 = time.perf_counter()
    r = http_json(base + "/chat/completions", body)
    dt = time.perf_counter() - t0
    text = (r["choices"][0]["message"].get("content") or "").strip()
    print(f"\n[a] chat reply ({dt:.2f}s):\n    {text}\n")
    return {"ok": bool(text), "reply": text, "seconds": round(dt, 3), "usage": r.get("usage")}


# ---------------------------------------------------------------- (b) JSON
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "city": {"type": "string"},
        "phone": {"type": "string"},
        "topic": {"type": "string"},
        "day": {"type": "string", "enum": DAYS},
        "attendees": {"type": "integer"},
    },
    "required": ["name", "city", "phone", "topic", "day", "attendees"],
    "additionalProperties": False,
}
SOURCE = ("Hey, this is Maria Chen calling from Ithaca. Can we move the robotics demo to "
          "Saturday? We'll have 4 people. My number is 607-555-0142.")
PY_TYPES = {"string": str, "integer": int}


def schema_errors(obj):
    errs = []
    if not isinstance(obj, dict):
        return ["top level is not an object"]
    for k in SCHEMA["required"]:
        if k not in obj:
            errs.append(f"missing key {k!r}")
    for k, v in obj.items():
        spec = SCHEMA["properties"].get(k)
        if spec is None:
            errs.append(f"unexpected key {k!r}")
            continue
        t = PY_TYPES[spec["type"]]
        if not isinstance(v, t) or isinstance(v, bool):
            errs.append(f"{k!r} should be {spec['type']}, got {type(v).__name__}")
        if "enum" in spec and v not in spec["enum"]:
            errs.append(f"{k!r}={v!r} not in enum")
    return errs


def content_errors(obj):
    errs = []
    if "maria" not in str(obj.get("name", "")).lower():
        errs.append(f"name={obj.get('name')!r}")
    if "ithaca" not in str(obj.get("city", "")).lower():
        errs.append(f"city={obj.get('city')!r}")
    if re.sub(r"\D", "", str(obj.get("phone", ""))) != "6075550142":
        errs.append(f"phone={obj.get('phone')!r}")
    if obj.get("day") != "Saturday":
        errs.append(f"day={obj.get('day')!r}")
    if obj.get("attendees") != 4:
        errs.append(f"attendees={obj.get('attendees')!r}")
    return errs


def test_json(base, model, backend):
    messages = [
        {"role": "system", "content": "Extract the requested fields. Reply with JSON only."},
        {"role": "user", "content": f"Extract name, city, phone, topic, day and attendees from:\n\n{SOURCE}"},
    ]
    modes = [
        ("json_schema", {"type": "json_schema", "json_schema": {"name": "contact", "strict": True, "schema": SCHEMA}}),
        ("json_object", {"type": "json_object", "schema": SCHEMA}),
        ("prompt_only", None),
    ]
    for mode, rf in modes:
        body = {"model": model, "messages": messages, "max_tokens": 300, **sampling(backend)}
        if rf:
            body["response_format"] = rf
        try:
            t0 = time.perf_counter()
            r = http_json(base + "/chat/completions", body)
            dt = time.perf_counter() - t0
        except urllib.error.HTTPError as e:
            print(f"[b] response_format={mode} rejected (HTTP {e.code}); trying next mode")
            continue
        raw = (r["choices"][0]["message"].get("content") or "").strip()
        try:
            obj = json.loads(raw)
            parse_err = None
        except json.JSONDecodeError as e:
            obj, parse_err = None, str(e)
        s_errs = [f"JSON parse error: {parse_err}"] if parse_err else schema_errors(obj)
        c_errs = [] if parse_err else content_errors(obj)
        ok = not s_errs and not c_errs
        print(f"[b] JSON extraction via {mode} ({dt:.2f}s): {'PASS' if ok else 'FAIL'}")
        print(f"    raw: {raw}")
        for e in s_errs + c_errs:
            print(f"    - {e}")
        return {"ok": ok, "mode": mode, "constrained": mode == "json_schema", "seconds": round(dt, 3),
                "raw": raw, "schema_errors": s_errs, "content_errors": c_errs}
    return {"ok": False, "mode": None, "error": "all response_format modes rejected"}


# ---------------------------------------------------------------- (c) speed
PERF_PROMPT = "Write about 200 words describing a sunrise over Cayuga Lake."


def stream_once(base, model, backend, max_tokens):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": PERF_PROMPT}],
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
        "seed": 42,
        "cache_prompt": False,  # llama-server: measure a fresh prompt every run (ignored elsewhere)
        **sampling(backend),
    }
    req = urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    first = last = None
    chunks = 0
    usage = timings = None
    with urllib.request.urlopen(req, timeout=600) as resp:
        for ev in sse_events(resp):
            usage = ev.get("usage") or usage
            timings = ev.get("timings") or timings
            for ch in ev.get("choices", []):
                d = ch.get("delta") or {}
                if d.get("content") or d.get("reasoning_content") or d.get("reasoning"):
                    now = time.perf_counter()
                    first = first or now
                    last = now
                    chunks += 1
    if first is None:
        raise RuntimeError("stream produced no tokens")
    n = (usage or {}).get("completion_tokens") or (timings or {}).get("predicted_n") or chunks
    return {
        "ttft_s": round(first - t0, 4),
        "total_s": round(last - t0, 4),
        "completion_tokens": n,
        "client_decode_tps": round((n - 1) / (last - first), 2) if last > first and n > 1 else None,
        "server_decode_tps": round(timings["predicted_per_second"], 2) if timings and timings.get("predicted_per_second") else None,
        "server_prompt_tps": round(timings["prompt_per_second"], 2) if timings and timings.get("prompt_per_second") else None,
    }


def test_speed(base, model, backend, runs, max_tokens):
    print(f"\n[c] speed: 1 warm-up + {runs} runs, max_tokens={max_tokens}, thinking off")
    warm = stream_once(base, model, backend, max_tokens)
    print(f"    warm-up: TTFT {warm['ttft_s']*1000:.0f} ms, {warm['client_decode_tps']} tok/s")
    rows = []
    for i in range(runs):
        r = stream_once(base, model, backend, max_tokens)
        rows.append(r)
        print(f"    run {i+1}: TTFT {r['ttft_s']*1000:.0f} ms | decode {r['client_decode_tps']} tok/s (client), "
              f"{r['server_decode_tps']} tok/s (server) | {r['completion_tokens']} tokens")

    def med(key):
        vals = [r[key] for r in rows if r[key] is not None]
        return round(statistics.median(vals), 3) if vals else None

    summary = {"ttft_ms_median": round(med("ttft_s") * 1000, 1), "client_decode_tps_median": med("client_decode_tps"),
               "server_decode_tps_median": med("server_decode_tps"), "server_prompt_tps_median": med("server_prompt_tps")}
    print(f"    MEDIAN: TTFT {summary['ttft_ms_median']} ms | decode {summary['client_decode_tps_median']} tok/s "
          f"(server {summary['server_decode_tps_median']}) | prompt {summary['server_prompt_tps_median']} tok/s")
    return {"ok": True, "warmup": warm, "runs": rows, "summary": summary}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8080/v1")
    ap.add_argument("--backend", choices=["llama", "mlx", "lmstudio"], default="llama")
    ap.add_argument("--label", required=True, help="e.g. 4B-PQ2_0, used in the results filename")
    ap.add_argument("--model", help="model id (default: first loaded model from /v1/models)")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--max-tokens", type=int, default=256)
    a = ap.parse_args()
    base = a.base_url.rstrip("/")

    model = a.model or pick_model(base)
    print(f"server {base} | backend {a.backend} | model {model}")
    out = {"label": a.label, "backend": a.backend, "base_url": base, "model": model,
           "timestamp": datetime.datetime.now().isoformat(timespec="seconds")}
    out["chat"] = test_chat(base, model, a.backend)
    out["json"] = test_json(base, model, a.backend)
    out["speed"] = test_speed(base, model, a.backend, a.runs, a.max_tokens)

    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    path = os.path.join(HERE, "results", f"bonsai_{a.label}_{datetime.datetime.now():%Y%m%d-%H%M%S}.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    ok = out["chat"]["ok"] and out["json"]["ok"] and out["speed"]["ok"]
    print(f"\n{'ALL PASS' if ok else 'SOME CHECKS FAILED'} -> {path}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
