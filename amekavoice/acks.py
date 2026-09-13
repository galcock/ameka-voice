"""The small noises a person makes so you know they heard you.

Advanced Voice Mode feels alive partly because it never leaves a silence: it
says "okay", "let me look", "still going". Ameka reads finished sentences, so
between your instruction and the session's answer there was nothing at all —
sometimes for minutes. These fill that, briefly and without repeating.
"""
from __future__ import annotations

import random

TOOK_IT = ["Okay.", "On it.", "Got it.", "Sent.", "Right.", "Will do.", "Done."]
PASSED_ON = ["Sent to {where}.", "{where} has it.", "Passed to {where}.", "Okay, {where}."]
STILL_GOING = ["Still going.", "Still working.", "Nothing back yet.",
               "It is still on it.", "Give it a moment."]
LOOKING = ["Let me look.", "One moment.", "Checking.", "Hold on."]

_last: dict[str, str] = {}


def _fresh(kind: str, options: list[str]) -> str:
    """Never the same one twice running — repetition is what sounds robotic."""
    choices = [o for o in options if o != _last.get(kind)] or options
    picked = random.choice(choices)
    _last[kind] = picked
    return picked


def took_it(where: str = "") -> str:
    if where and random.random() < 0.5:
        return _fresh("passed", PASSED_ON).format(where=where)
    return _fresh("took", TOOK_IT)


def still_going() -> str:
    return _fresh("waiting", STILL_GOING)


def looking() -> str:
    return _fresh("looking", LOOKING)
