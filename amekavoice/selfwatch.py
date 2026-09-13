"""She reads her own log and reports what is going wrong with her.

Everything that fails in here leaves a line behind — a capture that ran a
minute to catch three words, a session she could not bring forward, a
traceback, the same sentence spoken twice. Nobody was reading any of it. He
was: he listened to her misbehave, worked out what was wrong from the outside,
and told her, which is the slowest possible path from a fault to a fix.

So she reads it herself and tells the session that holds her code, with the
lines that show it. Not a guess about what is wrong — the evidence, so
whoever picks it up starts where she left off.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

STATE = Path(os.path.expanduser("~/.local/state/ameka/selfwatch.json"))

# What a fault looks like from the inside. Each is something she can observe
# about herself without being told, and each has been a real bug today.
# Her own words, echoed back. Everything she says and sends is logged, so her
# last report is sitting in the log containing the word "Traceback", and she
# read it as fifteen crashes. A report that guarantees the next report is worse
# than no report at all.
MINE = re.compile(r"^\d\d:\d\d:\d\d (sent|speaking:|spoke |brief |understood:|heard:)")

SYMPTOMS: list[tuple[str, str, re.Pattern]] = [
    # A failed start is not a crash in the running program: it is a bad edit,
    # caught before deploy, already fixed by the time anyone reads this.
    ("crash", "Something threw and the turn died",
     re.compile(r"\berror: \w+(Error|Exception)\(")),
    ("deaf", "Recording for a long time and finding almost nothing in it",
     re.compile(r"captured (\d+(?:\.\d+)?)s .*<- fewer words")),
    # Only a capture with a sentence's worth of speech in it. Sub-second
    # blips that transcribe to nothing are the guard working, and she filed
    # nine of them as deafness on a laptop microphone at midnight.
    ("mute", "Capturing audio that turns into no words at all",
     re.compile(r"captured .*\(([1-9]\d*\.\d+|0\.9\d*)s talking\) -> 0 words")),
    ("blind", "Asked to bring a session forward and could not find it",
     re.compile(r"could not find .* on screen")),
    ("relay", "The phone relay is not answering",
     re.compile(r"relay poll failed")),
    # The one he had to catch for me. Seven captures running were two point
    # seven seconds long holding a fifth of a second of speech, which is the
    # shape of ending his sentence for him, and I read past it every time
    # because nothing in the log says "cut off" — it has to be recognised.
    # Friction, not failure. He should not have to notice that she got slower.
    ("slow", "Taking a long time to turn what he said into words",
     re.compile(r"captured \d\d+\.\ds ")),
    ("clipped", "Ending his sentences early — barely any speech in a capture",
     re.compile(r"captured \d+(?:\.\d+)?s \(0\.[0-3]s talking\)")),
    # The one nobody caught for twenty hours. Speech runs on the main loop, so
    # a write that never returned took listening, self-check and self-update
    # down with it, and from outside she looked fine: the status endpoint is
    # its own thread and kept answering "speaking". There was no line to find
    # because nothing had finished; now there is one, ninety seconds in.
    ("stuck", "Her voice hung and the whole loop waited on it",
     re.compile(r"voice hung: ")),
    # She clicked a session's row and the pane showed a different session,
    # or she could not reach the row at all. Once is the app being slow;
    # twice in ten minutes is her typing path being wrong for that session.
    ("disk", "The disk is full — she cannot write, and neither can anything else",
     re.compile(r"No space left on device|ENOSPC|\[Errno 28\]")),
    ("nowindow", "An app she works in is running with no window, so she cannot reach it",
     re.compile(r"app has no window open")),
    ("misaimed", "Could not get the right session on screen before typing",
     re.compile(r"(?:the session on screen is not it|could not bring .* on screen)")),
]
# Two things were on this list that should never have been: hearing her own
# voice and rejecting room noise. Both are guards doing exactly their job, and
# counting a working guard as a fault is how a report stops being believed.
# How many times a thing has to happen before it is worth anybody's attention.
# One rejected capture is a cough; thirty in ten minutes is a fault.
FLOORS = {"crash": 1, "deaf": 4, "mute": 6, "blind": 3, "relay": 20,
          "clipped": 5, "slow": 4, "stuck": 1, "misaimed": 2, "disk": 1, "nowindow": 2}


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def _save(state: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state))
    except Exception:
        pass


def _tail(path: Path, offset: int) -> tuple[list[str], int]:
    """Lines written since last time, and where to start next time."""
    try:
        size = path.stat().st_size
    except OSError:
        return [], 0
    if offset > size:                      # rotated or truncated
        offset = 0
    try:
        with path.open("r", errors="replace") as fh:
            fh.seek(offset)
            text = fh.read()
            return text.splitlines(), fh.tell()
    except OSError:
        return [], offset


def look(log_path: Path, window: int = 400_000) -> tuple[list[dict], int]:
    """What has gone wrong since the last look. Returns findings and an offset."""
    state = _state()
    offset = int(state.get("offset", 0))
    lines, new_offset = _tail(log_path, offset)
    if offset == 0 and len(lines) > 4000:      # first run: only recent history
        lines = lines[-4000:]
    hits: dict[str, list[str]] = {}
    for line in lines:
        if MINE.match(line):
            continue
        for key, _, pattern in SYMPTOMS:
            if pattern.search(line):
                hits.setdefault(key, []).append(line.strip())
                break
    findings = []
    for key, about, _ in SYMPTOMS:
        seen = hits.get(key, [])
        if len(seen) >= FLOORS[key]:
            findings.append({"key": key, "about": about, "count": len(seen),
                             "evidence": seen[-4:]})
    return findings, new_offset


def due(config) -> bool:
    behavior = config.section("behavior")
    every = float(behavior.get("selfwatch_minutes", 10) or 0)
    if every <= 0:
        return False
    return time.time() - float(_state().get("looked", 0)) >= every * 60


def mark_looked(offset: int) -> None:
    state = _state()
    state["looked"] = time.time()
    state["offset"] = offset
    _save(state)


def worth_saying(config, findings: list[dict]) -> list[dict]:
    """What to raise now, and what to raise again because nothing changed.

    Saying a thing once and never mentioning it again is how a fault report
    becomes a note in a log nobody reads. Saying it every ten minutes is how it
    becomes noise. So a fault already raised waits, and when it comes back it
    comes back saying how long it has been going on — which is the part that
    makes somebody act on it.
    """
    hours = float(config.section("behavior").get("selfwatch_repeat_hours", 4))
    state = _state()
    told = state.get("told", {})
    now = time.time()
    fresh = []
    for finding in findings:
        first = float(told.get(finding["key"], 0))
        if first and now - first < hours * 3600:
            continue
        if first:
            ago = (now - first) / 3600
            finding["about"] += (f" — and this is the second time, {ago:.0f} "
                                 f"hours after I first reported it")
            finding["again"] = True
        fresh.append(finding)
        told[finding["key"]] = now
    state["told"] = told
    _save(state)
    return fresh


def write_up(findings: list[dict]) -> str:
    """The report, as evidence rather than a theory."""
    parts = ["I have been watching my own log and something is wrong with me.",
             ""]
    for finding in findings:
        parts.append(f"{finding['about']} — {finding['count']} times since I "
                     f"last looked.")
        for line in finding["evidence"]:
            parts.append(f"    {line}")
        parts.append("")
    parts.append("Those lines are from ~/.local/state/ameka/ameka.log. Please "
                 "work out what is causing it and fix it.")
    return "\n".join(parts).strip()
