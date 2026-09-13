"""She writes down what she learns about the person she works for.

His brain is a file he wrote once. Everything she sees afterwards — which
projects are real, which ones he abandoned, what he calls them out loud, what
he keeps asking for, what he never wants touched — is learned and then thrown
away at the end of the pass.

So: once an hour, from the sessions she has watched, she proposes a handful of
short durable facts, and appends the ones that are new to a section of the
file that is hers. Everything above that line is his, and is never edited. She
adds; she does not rewrite. Anything she gets wrong, he deletes — it is a
markdown file, and the section says so at the top.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import brain, profile

MARK = "<!-- ameka:learned -->"
HEADER = f"""
{MARK}
## What Ameka has learned

Written by Ameka from what she has watched, one line at a time. Everything
above this line is yours and she never touches it. Delete anything here that
is wrong or stale; she will not put it back unless she sees it again.
"""

STATE = Path(os.path.expanduser("~/.local/state/ameka/learned.json"))

EXTRACT = (
    "You watch the coding sessions of the person you work for, and you keep notes about their "
    "work that they did not write down themselves. From the SESSIONS BELOW ONLY, write facts "
    "that are durably true and that are NOT already in their notes in any wording.\n"
    "Good: what a project you have watched actually is and what it does, a name or a person "
    "that turned up in the work, a constraint or deadline the work revealed.\n"
    "Reject, always: anything restating their notes — their priorities, how they want to be "
    "spoken to, what they are working toward, a list of their projects. They wrote that; "
    "repeating it back is worse than silence. Also reject: what a session did today, anything "
    "about you, anything you are not sure of.\n"
    "Each fact is ONE short sentence naming the project it is about. At most three. An empty "
    'list is the right answer most of the time. Return ONLY JSON: {"facts": ["...", "..."]}'
)


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def _save(d: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d, indent=1))


def due(config, hours: float = 1.0) -> bool:
    behavior = config.section("behavior")
    if not behavior.get("learn", True) or not brain.available():
        return False
    every = float(behavior.get("learn_hours", hours) or 0)
    if every <= 0:
        return False
    return time.time() - float(_state().get("looked", 0)) >= every * 3600


def _known(text: str) -> set[str]:
    squash = lambda t: " ".join(re.sub(r"[^a-z0-9 ]+", " ", t.lower()).split())
    return {squash(line.lstrip("- ").strip()) for line in text.splitlines() if line.strip()}


def look(config, seen: dict, log=print) -> str:
    """One pass: propose facts, keep the new ones, append them. Returns a line
    for the log."""
    _save({**_state(), "looked": time.time()})
    path = profile.path_for(config)
    if path is None:
        return "no brain file to add to"
    existing = path.read_text(errors="ignore")
    # What the sessions have been doing, as her smart loops saw them.
    lines = []
    for project, (where, state, why, when) in list(seen.items())[:12]:
        lines.append(f"- {project} ({where}): {state}. {why}")
    if not lines:
        return "nothing watched yet"
    prompt = ("Sessions you have watched:\n" + "\n".join(lines)
              + "\n\nNotes you already have about them:\n" + existing[-3000:])
    got = brain.ask(EXTRACT, prompt, max_tokens=220, timeout=45)
    facts = [str(f).strip() for f in (got.get("facts") or []) if str(f).strip()] if got else []
    if not facts:
        return "nothing new worth writing down"
    # Word-for-word dedup let four restatements of his own manual through, in
    # different words. A fact whose content words are already almost all in the
    # file is something he has said; it does not go back to him as news.
    squash = lambda t: " ".join(re.sub(r"[^a-z0-9 ]+", " ", t.lower()).split())
    stop = {"the", "a", "an", "and", "or", "of", "to", "in", "for", "is", "are", "with", "that",
            "this", "on", "by", "as", "it", "user", "they", "their", "them", "project", "work"}
    already = set(squash(existing).split())
    have = _known(existing)

    def news(fact: str) -> bool:
        words = [w for w in squash(fact).split() if w not in stop and len(w) > 2]
        if not words or squash(fact) in have:
            return False
        return sum(w not in already for w in words) / len(words) >= 0.34

    fresh = [f for f in facts if len(f.split()) >= 4 and news(f)][:3]
    if not fresh:
        return "nothing new worth writing down"
    body = existing if MARK in existing else existing.rstrip() + "\n" + HEADER
    stamp = time.strftime("%Y-%m-%d")
    body = body.rstrip() + "\n" + "\n".join(f"- {f}  _(learned {stamp})_" for f in fresh) + "\n"
    try:
        path.write_text(body)
    except OSError as exc:
        return f"could not write to {path.name}: {exc!r}"
    profile.forget()
    return f"added {len(fresh)} to {path.name}: " + "; ".join(f[:60] for f in fresh)
