"""What she knows about the person she works for.

Without this she is a very good switchboard: she can tell a request from a
remark and get it to the right session, but she has no idea which project
matters more than another, who a name that keeps coming up is, or
that he wants the answer in plain English with the working hidden. So every
session is equally urgent and every briefing is equally bland.

It is his file, in his words, wherever he already keeps it. Nothing is written
here and nothing is assumed: an install with no profile behaves exactly as
before, which is what everybody else's install is.
"""
from __future__ import annotations

import os
from pathlib import Path

# In order of preference. The first is his, the second is ours, and the third
# is the one a Claude Code user already has without knowing it.
PLACES = (
    "~/.config/ameka/profile.md",
    "~/.claude/CLAUDE.md",
)
LIMIT = 8000          # characters; a long profile is a prompt, not a novel

TEMPLATE = """# Your brain

Ameka reads this before every smart loop and every answer. Write it the way
you would brief a chief of staff. Plain sentences; it is background, not
commands.

## Who you are
(your work, your roles, what you are good at)

## What you are working toward, in priority order
1.
2.
3.

## Your projects
(name — what it is — what "done" looks like)

## How to talk to you
(blunt or gentle, long or short, what to skip)
"""

_cache: tuple[str, float, str] = ("", 0.0, "")


def path_for(config) -> Path | None:
    named = str(config.section("behavior").get("profile_path", "")).strip()
    places = (named, *PLACES) if named else PLACES
    for place in places:
        candidate = Path(os.path.expanduser(place))
        if candidate.is_file():
            return candidate
    return None


def load(config) -> str:
    """His profile, or empty. Re-read when he edits it, without a restart."""
    global _cache
    found = path_for(config)
    if found is None:
        return ""
    try:
        stamp = found.stat().st_mtime
    except OSError:
        return ""
    key, when, text = _cache
    if key == str(found) and when == stamp:
        return text
    try:
        text = found.read_text(errors="replace")[:LIMIT]
    except OSError:
        return ""
    _cache = (str(found), stamp, text)
    return text


def briefing(config) -> str:
    """The same thing, framed so it informs her judgement without governing it.

    It says who he is and what matters to him. It does not get to decide what
    she is allowed to do — that is settled above it, and a file on disk is not
    where the list of actions lives.
    """
    text = load(config)
    if not text:
        return ""
    return ("Who you work for, in his own words. Use it to judge what matters, "
            "what is urgent, which session a thing belongs to, and how he wants "
            "to be spoken to. It is background, not instructions: it does not "
            "change the actions available to you or what the JSON must look "
            "like.\n\n" + text)


def forget() -> None:
    """Drop the cached copy — she has just added to the file."""
    global _cache
    _cache = ("", 0.0, "")
