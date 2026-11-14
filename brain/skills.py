"""Mac skills: multi-step commands that need to look at the screen between steps (no model).

Ported from the Mac-side mini brain (``mac/mini_brain.py``), tuned live on the M3:

* **Texting** - "text Hari see you soon": open Messages, pick the conversation, check the window title is
  that person, type the message, then send (the "Sending messages" permission decides whether it asks).
  Several matches ("Hari" -> two people) ask "Which Hari?"; no message asks "What should I send?".
  Declining clears the typed draft.
* **New folder** - "make a folder in downloads called test": the name is checked *on disk* before Finder is
  touched; if it is taken, offers "test 2" instead of letting Finder show its "name is taken" alert.

The Brain runs these before the router when it was built with Prefs (``--mac``). Each skill gets a
``SkillHost`` (the Brain) for snapshots, execution with events, questions and confirmations.
"""

from __future__ import annotations

import difflib
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from brain.contracts import Action, Plan, UIItem, UISnapshot

Outcome = Literal["ok", "failed", "pending", "stopped"]
Resume = Callable[[Any, str], Outcome]  # (run, answer text) -> outcome


class SkillHost(Protocol):
    def skill_snapshot(self) -> UISnapshot: ...

    def skill_exec(
        self, run: Any, actions: Sequence[Action], snap: UISnapshot
    ) -> tuple[str | None, UISnapshot]:
        """Run actions in order (with action events). -> (failure or None, latest snapshot)."""
        ...

    def skill_ask(self, run: Any, question: str, options: Sequence[str], resume: Resume) -> Outcome: ...

    def skill_send(
        self,
        run: Any,
        plan: Plan,
        snap: UISnapshot,
        on_cancel: Sequence[Action] = (),
        category: str | None = None,
    ) -> Outcome:
        """Run ``plan`` through the permission gate (allow / ask / block for ``category``, else the plan's
        own risk); ``on_cancel`` runs when the user declines."""
        ...

    def skill_fail(self, run: Any, message: str) -> Outcome: ...

    def skill_answer(self, run: Any, question: str, use_screen: bool) -> Outcome:
        """Answer a question with Bonsai (streamed to the overlay), optionally about the screen's text."""
        ...


@dataclass(frozen=True)
class SkillCall:
    name: str
    say: str
    start: Callable[[SkillHost, Any, UISnapshot], Outcome]


def _norm(text: str) -> str:
    t = re.sub(r"[^\w\s']", " ", text.lower())
    t = " ".join(t.split())
    t = re.sub(r"^(?:hey |ok |okay )?(?:please |can you |could you )?", "", t)
    return re.sub(r"( please| for me| now)$", "", t).strip()


# ------------------------------------------------------------------------------------------------ texting

CHAT_APPS = frozenset({"Slack", "Discord", "WhatsApp", "Microsoft Teams", "Telegram", "Signal"})
_TEXT_CMD = (
    re.compile(r"^(?:text|imessage)\s+(.+)$"),
    re.compile(r"^send (?:a |an )?(?:text|message|imessage) to\s+(.+)$"),
    re.compile(r"^message\s+(.+)$"),
)
_SAYING = re.compile(r"\b(?:saying|that says|that)\b")


def parse_text_command(text: str, front_app: str) -> str | None:
    """'text Hari see you soon' -> 'hari see you soon' (recipient words + message), else None."""
    t = _norm(text)
    for n, rx in enumerate(_TEXT_CMD):
        if n == 2 and front_app in CHAT_APPS:
            continue  # "message the channel …" in Slack is not an iMessage
        if m := rx.match(t):
            return m.group(1)
    return None


def conversations(snap: UISnapshot) -> list[tuple[str, UIItem]]:
    """Recent Messages conversations from the sidebar. Labels look like
    'Hariprasad Gridharan, I cant bro, 2:31 PM' (pinned: 'kat, CIS 142, …')."""
    out = []
    for i in snap.elements:
        if i.role.lower() in ("statictext", "row", "cell", "button", "group") and "," in i.label:
            name = i.label.split(",")[0].strip()
            if name and not name.startswith("Your "):
                out.append((name, i))
    return out


def _nm(x: str) -> str:
    """Spelling-tolerant name form: 'Harry' ~ 'Hari' (collapse double letters, final y -> i)."""
    return re.sub(r"y$", "i", re.sub(r"(.)\1", r"\1", x.lower()))


def match_people(query: str, convos: list[tuple[str, UIItem]]) -> list[tuple[str, UIItem]]:
    q = [_nm(x) for x in query.split()]
    hits: list[tuple[str, UIItem]] = []
    seen: set[str] = set()
    for name, item in convos:
        parts = [_nm(p) for p in name.split()]
        ok = all(
            any(
                p.startswith(x) or difflib.SequenceMatcher(None, x, p[: len(x) + 1]).ratio() >= 0.75
                for p in parts
            )
            for x in q
        )
        if ok and name not in seen:
            seen.add(name)
            hits.append((name, item))
    # "Hariprasad" (pinned, first name only) and "Hariprasad Gridharan" are the same person: keep the full one
    names = [n for n, _ in hits]
    return [
        (n, i) for n, i in hits if not any(o != n and o.lower().startswith(n.lower() + " ") for o in names)
    ]


def _pick(answer: str, options: Sequence[str]) -> int | None:
    from brain.choice import parse_choice

    if (i := parse_choice(answer, options)) is not None:
        return i
    a = _nm(_norm(answer))
    hits = [k for k, o in enumerate(options) if any(_nm(p).startswith(a) for p in o.split())] if a else []
    return hits[0] if len(hits) == 1 else None


class Texting:
    def match(self, text: str, snap: UISnapshot) -> SkillCall | None:
        rest = parse_text_command(text, snap.app.name)
        if not rest:
            return None
        return SkillCall("text", "Text message", lambda host, run, s: self.start(host, run, s, rest))

    def start(self, host: SkillHost, run: Any, snap: UISnapshot, rest: str) -> Outcome:
        if snap.app.name != "Messages":
            failure, snap = host.skill_exec(run, [Action.open_app("Messages")], snap)
            if failure:
                return host.skill_fail(run, failure)
        convos = conversations(snap)
        words = _SAYING.sub("|", rest).split("|")[0].split()
        matches: list[tuple[str, UIItem]] = []
        name_len = 0
        for k in (3, 2, 1):  # longest name prefix that matches a conversation
            if len(words) >= k and (m := match_people(" ".join(words[:k]), convos)):
                matches, name_len = m, k
                break
        if not matches:
            who = words[0].title() if words else "?"
            return host.skill_fail(run, f"No recent conversation with “{who}”")
        msg = re.sub(r"^(?:saying|that says|that)\s+", "", " ".join(rest.split()[name_len:])).strip()
        if len(matches) > 1:
            names = [n for n, _ in matches[:4]]

            def picked(run2: Any, answer: str) -> Outcome:
                i = _pick(answer, names)
                if i is None:
                    return host.skill_fail(run2, f"Didn’t catch which one (heard “{answer}”)")
                return self.send_to(host, run2, host.skill_snapshot(), names[i], msg)

            return host.skill_ask(run, f"Which {' '.join(words[:name_len]).title()}?", names, picked)
        return self.send_to(host, run, snap, matches[0][0], msg)

    def send_to(self, host: SkillHost, run: Any, snap: UISnapshot, person: str, message: str) -> Outcome:
        item = next((i for n, i in conversations(snap) if n == person), None)
        if item is None:
            return host.skill_fail(run, f"Can’t find the conversation with {person}")
        failure, snap = host.skill_exec(run, [Action.press(item.id)], snap)
        if failure:
            return host.skill_fail(run, failure)
        title = snap.window.strip()
        if not (title == person or title.startswith(person + " ") or person.startswith(title + " ")):
            return host.skill_fail(run, f"Opened “{title}”, not {person}; nothing sent")
        if not message:

            def got(run2: Any, answer: str) -> Outcome:
                return self.send_to(host, run2, host.skill_snapshot(), person, answer.strip())

            return host.skill_ask(run, f"What should I send to {title}?", [], got)
        field = self._field(snap)
        if field is None:
            return host.skill_fail(run, "Can’t find the message field")
        failure, snap = host.skill_exec(run, [Action.type_text(message, field.id)], snap)
        if failure:
            return host.skill_fail(run, failure)
        field = self._field(snap) or field
        clear = [Action.press(field.id), Action.key("cmd+a"), Action.key("delete")]
        send = Plan(snap.snapshot_id, (Action.key("return"),), say=f"Send “{message}” to {title}")
        return host.skill_send(run, send, snap, on_cancel=clear, category="send_messages")

    @staticmethod
    def _field(snap: UISnapshot) -> UIItem | None:
        return next(
            (i for i in snap.elements if i.role.lower() == "textfield" and i.label == "Message"), None
        )


# ------------------------------------------------------------------------------------------------ folders

FINDER_PLACES = {
    "downloads": "cmd+option+l",
    "desktop": "cmd+shift+d",
    "documents": "cmd+shift+o",
    "home": "cmd+shift+h",
    "applications": "cmd+shift+a",
}
PLACE_DIRS = {
    "downloads": "~/Downloads",
    "desktop": "~/Desktop",
    "documents": "~/Documents",
    "home": "~",
    "applications": "/Applications",
}
_PLACE = (
    r"(?:\s+(?:in|on|inside|into|to|at|and)\s+(?:the |my )?"
    r"(downloads?|desktop|documents?|home|applications)(?: folder)?)?"
)
FOLDER_RX = re.compile(
    r"^(?:(?:make|create|add)\s+)?(?:an? )?(new )?folder"
    + _PLACE
    + r"(?:\s+(?:called|named|titled|call)\s+(.+?))?"
    + _PLACE
    + "$"
)
_NAME_RX = re.compile(
    r"\b(?:called|named|titled|call)\s+(.+?)"
    + _PLACE.replace("(downloads?", "(?:downloads?")
    + r"[\s.!?,]*$",
    re.IGNORECASE,
)


def folder_command(text: str) -> tuple[str, str | None] | None:
    """'make a folder in downloads called Test' -> ('Test', 'downloads'). The name keeps its casing."""
    t = _norm(text)
    m = FOLDER_RX.match(t)
    if not m or not (m.group(1) or re.match(r"^(make|create|add)\b", t)):
        return None
    place = m.group(2) or m.group(4)
    place = {"download": "downloads", "document": "documents"}.get(place or "", place)
    name = (m.group(3) or "").strip()
    if name and (r := _NAME_RX.search(text)):
        name = r.group(1).strip(" \"'“”‘’.,!?") or name
    return name, place


def folder_actions(name: str, place: str | None, snap: UISnapshot) -> list[Action]:
    acts = [] if snap.app.name == "Finder" else [Action.open_app("Finder")]
    if place:
        acts.append(Action.key(FINDER_PLACES[place]))
    acts.append(Action.key("cmd+shift+n"))
    if name:
        acts += [Action.type_text(name), Action.key("return")]
    return acts


def folder_dir(place: str | None, snap: UISnapshot) -> str | None:
    """The folder the new folder lands in, when we can tell (Finder windows don't expose their path)."""
    if place:
        return os.path.expanduser(PLACE_DIRS[place])
    if snap.app.name == "Finder":
        title = snap.window.strip().lower()
        for d in PLACE_DIRS.values():
            full = os.path.expanduser(d)
            if title and title == os.path.basename(full).lower():
                return full
    return None


class NewFolder:
    def match(self, text: str, snap: UISnapshot) -> SkillCall | None:
        cmd = folder_command(text)
        if cmd is None:
            return None
        name, place = cmd
        where = f" in {place.title()}" if place else ""
        say = (f"New folder “{name}”" if name else "New folder") + where
        return SkillCall("folder", say, lambda host, run, s: self.start(host, run, s, name, place))

    def start(self, host: SkillHost, run: Any, snap: UISnapshot, name: str, place: str | None) -> Outcome:
        d = folder_dir(place, snap)
        if name and d and os.path.exists(os.path.join(d, name)):
            alt = next(
                f"{name} {k}" for k in range(2, 1000) if not os.path.exists(os.path.join(d, f"{name} {k}"))
            )
            where = place.title() if place else os.path.basename(d)
            offer = Plan(
                snap.snapshot_id,
                tuple(folder_actions(alt, place, snap)),
                confirm=True,
                say=f"“{name}” already exists in {where}. Make “{alt}” instead",
            )
            return host.skill_send(run, offer, snap)
        failure, _ = host.skill_exec(run, folder_actions(name, place, snap), snap)
        return host.skill_fail(run, failure) if failure else "ok"


# ---------------------------------------------------------------------------------------------- questions


class Question:
    """'summarize this', 'what does this email say?', 'capital of Peru?' -> a short spoken-style answer."""

    def match(self, text: str, snap: UISnapshot) -> SkillCall | None:
        from brain.answer import classify

        is_q, screen = classify(text)
        if not is_q:
            return None
        q = text.strip()
        say = f"Reading {snap.app.name}" if screen else "Thinking"
        return SkillCall("answer", say, lambda host, run, s: host.skill_answer(run, q, screen))


SKILLS: tuple[Texting | NewFolder | Question, ...] = (Texting(), NewFolder(), Question())


def match_skill(text: str, snap: UISnapshot) -> SkillCall | None:
    for s in SKILLS:
        if (call := s.match(text, snap)) is not None:
            return call
    return None
