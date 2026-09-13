"""Work out which AI tools this Mac actually has.

Not everyone has the same ones. Someone might use only ChatGPT in a browser,
someone else only Codex, someone else only Claude Code. Ameka wires up what is
there, says so plainly, and never fails because something is missing.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import host
from .engines import find_bin

HOME = Path(os.path.expanduser("~"))


def _app(name: str) -> bool:
    if host.IS_MAC:
        return Path(f"/Applications/{name}.app").is_dir()
    if host.IS_WIN:
        # Installed per user under Programs, or machine-wide under Program Files.
        roots = [os.environ.get("LOCALAPPDATA", ""), os.environ.get("PROGRAMFILES", ""),
                 os.environ.get("PROGRAMFILES(X86)", "")]
        wanted = name.lower().replace(" ", "")
        for root in filter(None, roots):
            for base in (Path(root) / "Programs", Path(root)):
                try:
                    for entry in base.iterdir():
                        if entry.is_dir() and wanted in entry.name.lower().replace(" ", ""):
                            return True
                except OSError:
                    continue
    return False


def claude_code() -> dict:
    """The CLI, the desktop app, or evidence of past sessions."""
    cli = find_bin("claude")
    app = _app("Claude")
    projects = (HOME / ".claude" / "projects").is_dir()
    return {
        "name": "Claude Code",
        "present": bool(cli or app or projects),
        "cli": cli,
        "bundle_id": "com.anthropic.claudefordesktop" if app else "",
        "how": "hook" if (cli or projects) else ("app" if app else ""),
        "detail": ", ".join(filter(None, [
            "command line" if cli else "",
            "desktop app" if app else "",
            "past sessions" if projects and not cli else "",
        ])) or "not found",
    }


def codex() -> dict:
    cli = find_bin("codex")
    home = (HOME / ".codex").is_dir()
    sessions = (HOME / ".codex" / "sessions").is_dir()
    return {
        "name": "Codex",
        "present": bool(cli or home),
        "cli": cli,
        "bundle_id": "com.openai.chat" if _app("ChatGPT") else "",
        "how": "watch" if sessions else ("cli" if cli else ""),
        "detail": ", ".join(filter(None, [
            "command line" if cli else "",
            "session history" if sessions else "",
        ])) or "not found",
    }


def chrome() -> dict:
    return {
        "name": "Browser",
        "present": _app("Google Chrome"),
        "bundle_id": "com.google.Chrome",
        "how": "devtools",
        "detail": "Google Chrome" if _app("Google Chrome") else "not found",
    }


def chatgpt_app() -> dict:
    return {
        "name": "ChatGPT app",
        "present": _app("ChatGPT"),
        "bundle_id": "com.openai.chat",
        "how": "keystroke",
        "detail": "ChatGPT desktop app" if _app("ChatGPT") else "not found",
    }


def detect() -> list[dict]:
    return [claude_code(), codex(), chatgpt_app(), chrome()]


def available() -> list[dict]:
    return [tool for tool in detect() if tool["present"]]


def default_target() -> str:
    """Where a spoken line goes when nothing else has claimed it."""
    for tool in detect():
        if tool["present"] and tool.get("bundle_id"):
            return tool["bundle_id"]
    return ""


def summary() -> str:
    found = [tool["name"] for tool in available()]
    if not found:
        return "no AI tools found yet"
    return ", ".join(found)
