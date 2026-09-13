"""Watch Codex sessions on disk so they brief you too.

Codex writes a rollout JSONL per session under ~/.codex/sessions/YYYY/MM/DD/.
Watching the files needs no change to your Codex config, so whatever you already
have wired to `notify` keeps working untouched.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

SESSIONS = Path(os.path.expanduser("~/.codex/sessions"))
POLL_SECONDS = 3.0
QUIET_SECONDS = 2.0        # a turn is finished once the file stops growing
MAX_AGE = 6 * 3600


def _text(payload: dict) -> str:
    parts = []
    for block in payload.get("content") or []:
        if isinstance(block, dict) and block.get("text"):
            if block.get("type") in ("output_text", "text", "summary_text"):
                parts.append(block["text"])
    return "\n".join(parts).strip()


def read_tail(path: Path) -> dict:
    """Last assistant message, the user turn before it, and the session's cwd."""
    assistant, user, cwd = "", "", ""
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return {"assistant": "", "user": "", "cwd": ""}

    for line in lines:
        if '"session_meta"' in line:
            try:
                meta = json.loads(line).get("payload") or {}
                cwd = meta.get("cwd") or meta.get("workspace") or cwd
            except json.JSONDecodeError:
                pass

    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = rec.get("payload") or {}
        if payload.get("type") != "message":
            continue
        role, body = payload.get("role"), _text(payload)
        if not body:
            continue
        if role == "assistant" and not assistant:
            assistant = body
        elif role == "user" and assistant:
            user = body
            break
    return {"assistant": assistant, "user": user, "cwd": cwd}


def latest_files() -> list[Path]:
    if not SESSIONS.exists():
        return []
    now = time.time()
    out = []
    for path in SESSIONS.rglob("rollout-*.jsonl"):
        try:
            if now - path.stat().st_mtime < MAX_AGE:
                out.append(path)
        except OSError:
            continue
    return out


def watch(on_turn, stop: threading.Event) -> None:
    """Call on_turn(path, tail) once per finished Codex turn."""
    seen: dict[str, float] = {p.name: p.stat().st_mtime for p in latest_files()}
    pending: dict[str, float] = {}

    while not stop.is_set():
        stop.wait(POLL_SECONDS)
        now = time.time()
        for path in latest_files():
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if seen.get(path.name) != mtime:
                seen[path.name] = mtime
                pending[str(path)] = mtime          # still writing; wait for quiet

        for key, mtime in list(pending.items()):
            if now - mtime < QUIET_SECONDS:
                continue
            del pending[key]
            path = Path(key)
            tail = read_tail(path)
            if tail["assistant"]:
                try:
                    on_turn(path, tail)
                except Exception:
                    pass
