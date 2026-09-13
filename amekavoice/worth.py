"""Is this worth interrupting him for, when nothing read the turn?

With a key, the model that wrote the briefing decides: it has read what the
session actually said and is far better placed than any rule. This is the other
path — no key, no network, a briefing assembled locally out of the last few
sentences — and something still has to decide whether he hears about it.

The bar is deliberately high. He is wearing headphones and working; a voice in
his ear costs him his place in whatever he was doing, and most of what a coding
session says while it works is narration.
"""
from __future__ import annotations

import re

# Things he would want stopping for.
ASKING = re.compile(r"\?\s*$|\b(shall i|should i|do you want|want me to|would you like"
                    r"|confirm|which one|proceed\?)\b", re.I)
WENT_WRONG = re.compile(r"\b(error|failed|failing|broke|broken|cannot|can't|blocked"
                        r"|stuck|refused|denied|timed out|crash)\w*\b", re.I)
GOT_DONE = re.compile(r"\b(finished|complete[ds]?|done|passed|fixed|merged|deployed"
                      r"|published|shipped|landed|working now|green)\b", re.I)
# Things that are just it thinking out loud on the way somewhere.
NARRATION = re.compile(r"^(ok|okay|right|sure|got it|understood|thanks|noted"
                       r"|let me|i'?ll|i am going to|i'?m going to|now i|next i"
                       r"|looking at|reading|checking|running|starting)\b", re.I)
# The same thing in the present tense: "I am now reading the controls file."
# A machine saying what it is in the middle of is not news either.
MID_TASK = re.compile(r"^(i'?m|i am|we'?re|we are)\b[^.]{0,30}?\b(reading|checking"
                      r"|running|looking|scanning|opening|inspecting|reviewing"
                      r"|starting|about to|going to|working on|continuing)\b", re.I)


EMPTY = {"a session finished", "a session ended", "session finished"}


def verdict(brief: dict, said: str = "") -> tuple[bool, str]:
    """(say it, why). Reasons are for the log, so they are short and plain.

    The order matters. The local summary invents a closing question for every
    briefing — "Want me to run the checks?" — so testing for a question first
    meant everything qualified, including a session narrating its way through
    the work. Only a question the session actually asked counts.
    """
    takeaway = (brief.get("takeaway") or "").strip()
    if not takeaway:
        return False, "nothing in it"
    # What the local summary says when it found nothing to say. It contains the
    # word "finished", which is otherwise a good reason to speak.
    if takeaway.rstrip(".").lower() in EMPTY:
        return False, "nothing in it"
    if NARRATION.match(takeaway) or MID_TASK.match(takeaway):
        return False, "it is narrating"
    if WENT_WRONG.search(takeaway):
        return True, "something went wrong"
    if ASKING.search(said or ""):
        return True, "it asked him something"
    if GOT_DONE.search(takeaway):
        return True, "something finished"
    if len(takeaway.split()) < 6:
        return False, "too slight to interrupt for"
    return True, "it said something substantial"
