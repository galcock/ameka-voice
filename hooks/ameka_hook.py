#!/usr/bin/env python3
"""Claude Code hook -> Ameka Voice.

Wired to Stop and Notification. Reads the hook payload on stdin, records how to
reach this session (tmux pane / host app), and fires it at the local daemon.
Never blocks Claude Code: short timeout, always exits 0.
"""
import json
import os
import sys
import urllib.request

PORT = int(os.environ.get("AMEKA_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}/event"


def main() -> int:
    # A headless `claude -p` started by Ameka itself must not brief us about
    # the briefing it is writing.
    if os.environ.get("AMEKA_NO_HOOK"):
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}

    body = {
        "source": "claude-code",
        "session_id": payload.get("session_id", ""),
        "cwd": payload.get("cwd", os.getcwd()),
        "transcript_path": payload.get("transcript_path", ""),
        "text": payload.get("message", ""),
        "hook": payload.get("hook_event_name", ""),
        "tmux_pane": os.environ.get("TMUX_PANE", ""),
        "bundle_id": os.environ.get("__CFBundleIdentifier", ""),
        "app_name": os.environ.get("TERM_PROGRAM", ""),
    }
    if sys.platform == "win32" and not body["bundle_id"] and not body["app_name"]:
        # No bundle ids on Windows. The Claude app is where a session lives
        # unless a terminal says otherwise, and it is the window typed into.
        body["app_name"] = "Windows Terminal" if os.environ.get("WT_SESSION") else "Claude"
    req = urllib.request.Request(
        URL, data=json.dumps(body).encode(),
        headers={"content-type": "application/json"}, method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=1.5).read()
    except Exception:
        pass          # daemon not running is not an error worth interrupting you for
    return 0


if __name__ == "__main__":
    sys.exit(main())
