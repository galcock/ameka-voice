"""Pull the last meaningful exchange out of a Claude Code transcript."""
from __future__ import annotations

import json
import re
from pathlib import Path

MAX_TEXT_BLOCKS = 6      # assistant messages to keep
MAX_LINES = 1200         # how far back to walk
NOISE = re.compile(
    r"<(command-name|command-message|command-args|local-command-[a-z]+|system-reminder|"
    r"task-notification|ci-monitor-event|user-prompt-submit-hook)>.*?</\1>",
    re.S,
)


def _texts(content) -> tuple[list[str], list[str]]:
    """Return (text blocks, tool names) for one message's content."""
    if isinstance(content, str):
        return ([content] if content.strip() else []), []
    if not isinstance(content, list):
        return [], []
    texts, tools = [], []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and (block.get("text") or "").strip():
            texts.append(block["text"])
        elif block.get("type") == "tool_use":
            tools.append(block.get("name", "tool"))
    return texts, tools


def read_claude_transcript(path: str | Path) -> dict:
    """Return the last assistant prose, the human message that prompted it, and
    the session's own title. Tool traffic is ignored."""
    p = Path(path)
    if not p.exists():
        return {"assistant": "", "user": "", "title": ""}

    assistant: list[str] = []
    tools: list[str] = []
    last_user = ""
    title = ""

    lines = p.read_text(errors="ignore").splitlines()[-MAX_LINES:]
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not title:
            for key in ("customTitle", "title"):
                value = rec.get(key)
                if isinstance(value, str) and value.strip():
                    title = value.strip()
                    break
        if rec.get("isSidechain") or rec.get("isMeta"):
            continue

        msg = rec.get("message") or {}
        role = msg.get("role") or rec.get("type")
        texts, used = _texts(msg.get("content"))

        if role == "assistant":
            if len(assistant) < MAX_TEXT_BLOCKS:
                assistant.extend(texts)
            tools.extend(used)
        elif role == "user" and texts:
            body = NOISE.sub(" ", "\n".join(texts)).strip()
            if body.startswith("<") and body.endswith(">"):
                continue                      # injected wrapper, not a human turn
            if body:
                last_user = body
                break

    prose = "\n\n".join(reversed(assistant)).strip()
    if not prose and tools:
        seen = list(dict.fromkeys(reversed(tools)))[:6]
        prose = "The session is still working; it just ran: " + ", ".join(seen) + "."
    return {"assistant": prose, "user": last_user, "title": title}
