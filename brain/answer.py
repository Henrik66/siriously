"""Questions instead of commands: "summarize this", "what does this email say?", "capital of Peru?".

Bonsai answers in one or two short sentences, streamed to the overlay as it writes. When the question is
about the screen ("this", "the page", "summarize", ...), the front window's readable text (Accessibility,
~0.2 s) goes into the prompt. Nothing on screen is clicked or typed. Runs on llama-server slot 1 so the
planner's cached prefix on slot 0 stays warm.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from collections.abc import Callable
from typing import Any

from brain.config import Settings

_POLITE = re.compile(r"^(?:can|could|would|will) you (?:please )?")
_ASK_VERBS = re.compile(
    r"^(?:summari[sz]e|explain|read|tell me|translate|define|describe|tldr|tl dr|"
    r"give me (?:a|the) (?:summary|gist))\b"
)
_WH = re.compile(
    r"^(?:what|whats|what's|who|whos|who's|whom|whose|when|where|why|how|which|is|are|was|were|does|do|did|"
    r"should|has|have)\b"
)
_SCREEN_WORDS = re.compile(
    r"\b(?:this|that|it|these|those|here|screen|page|email|mail|message|messages|note|document|doc|article|"
    r"window|tab|site|website|chat|thread|text|summari[sz]e|explain|read)\b"
)
# "what's open", "is wifi on" style questions can still be commands for the planner; these never are:
_NOT_QUESTIONS = re.compile(r"^(?:what time is it|whats the time)$")

SYSTEM = (
    "You are Siriously, an offline voice assistant on a Mac. Answer the question in at most two short "
    "sentences (under 40 words), as plain spoken text: no markdown, no lists, no preamble. If screen text "
    "is given and the question is about it, answer only from that text; if it does not contain the answer, "
    "say so in a few words."
)


def _norm(text: str) -> str:
    t = re.sub(r"[^\w\s']", " ", text.lower())
    return " ".join(t.split())


def classify(text: str) -> tuple[bool, bool]:
    """(is a question for Bonsai to answer, needs the screen text). Polite commands ("can you open notes")
    are not questions; "can you summarize this" is."""
    t = _norm(text)
    if not t or _NOT_QUESTIONS.match(t):
        return False, False
    polite = _POLITE.match(t)
    rest = t[polite.end() :] if polite else t
    if _ASK_VERBS.match(rest):
        return True, True
    if polite:
        return False, False  # "can you open notes?" is a command
    if _WH.match(t) and (text.strip().endswith("?") or len(t.split()) >= 3):
        return True, bool(_SCREEN_WORDS.search(t))
    return False, False


def stream_answer(
    question: str,
    screen: dict[str, Any] | None,
    settings: Settings,
    on_text: Callable[[str], bool],
    max_tokens: int = 90,
) -> str:
    """Ask Bonsai; ``on_text(text_so_far)`` after each chunk (return False to stop). -> the full answer."""
    if screen and screen.get("text"):
        user = (
            f"App: {screen.get('app', '')}\nWindow: {screen.get('window', '')}\n"
            f"Screen text:\n{screen['text']}\n\nQuestion: {question}"
        )
    else:
        user = question
    body = {
        "model": settings.bonsai_model,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "cache_prompt": True,
        "id_slot": 1,  # the planner keeps slot 0's prompt cache
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        settings.bonsai_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback only, never a proxy
    text = ""
    last = 0.0
    with opener.open(req, timeout=30) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                delta = json.loads(data)["choices"][0].get("delta", {}).get("content") or ""
            except (ValueError, KeyError, IndexError):
                continue
            text += delta
            now = time.monotonic()
            if delta and now - last > 0.12:
                last = now
                if not on_text(text.strip()):
                    break
    return text.strip()
