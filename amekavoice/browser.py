"""Read and drive browser tabs through Chrome's DevTools Protocol.

Chrome hides web content from both AppleScript and the accessibility API unless
a human flips a menu item, which defeats the point. The DevTools port needs no
human: Ameka relaunches Chrome with it once and everything after that is
programmatic.
"""
from __future__ import annotations

import json
import os
import sys
import re
import subprocess
import time
import urllib.error
import urllib.request

PORT = 9222
CHROME = ("/Applications/Google Chrome.app" if sys.platform == "darwin" else
          os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "Google", "Chrome", "Application", "chrome.exe"))
CHROME_BIN = f"{CHROME}/Contents/MacOS/Google Chrome"
# Chrome 136+ refuses remote debugging on the default profile directory, so
# Ameka keeps its own. On APFS this is a clone: instant, and no extra disk.
DEFAULT_PROFILE = (os.path.expanduser("~/Library/Application Support/Google/Chrome") if sys.platform == "darwin"
                   else os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data"))
PROFILE = os.path.expanduser("~/.ameka/chrome")

# Where a finished answer lives in each product's DOM.
READERS = {
    "chatgpt.com": """
        (() => {
          const nodes = document.querySelectorAll('[data-message-author-role="assistant"]');
          const last = nodes[nodes.length - 1];
          const busy = !!document.querySelector('button[data-testid="stop-button"]');
          // The tab title is truncated by the browser; the sidebar entry is not.
          let title = document.title;
          const id = (location.pathname.split("/c/")[1] || "").split(/[?#]/)[0];
          if (id) {
            const link = document.querySelector('a[href*="' + id + '"]');
            // The sidebar entry carries the project badge on a second line.
            if (link) {
              const first = (link.innerText || link.textContent || "").split("\n")[0].trim();
              if (first.length > 2) title = first;
            }
          }
          return JSON.stringify({
            text: last ? last.innerText : "",
            busy,
            title,
            id: last ? (last.getAttribute("data-message-id") || "") : ""
          });
        })()
    """,
    "claude.ai": """
        (() => {
          const nodes = document.querySelectorAll('[data-testid="assistant-message"], .font-claude-message');
          const last = nodes[nodes.length - 1];
          const busy = !!document.querySelector('button[aria-label*="Stop"]');
          return JSON.stringify({
            text: last ? last.innerText : "",
            busy,
            title: document.title,
            id: String(nodes.length)
          });
        })()
    """,
}

COMPOSER = """
    (() => {
      const box = document.querySelector('#prompt-textarea, div[contenteditable="true"], textarea');
      if (!box) return "no-composer";
      box.focus();
      return "ok";
    })()
"""


# ------------------------------------------------------------------ plumbing --
def cdp_up(port: int = PORT) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.5):
            return True
    except Exception:
        return False


def chrome_running() -> bool:
    out = subprocess.run(["pgrep", "-f", "Google Chrome.app/Contents/MacOS"],
                         capture_output=True, text=True)
    return bool(out.stdout.strip())


def ensure_profile() -> bool:
    """Clone the real Chrome profile once, so logins and tabs carry over."""
    if os.path.isdir(os.path.join(PROFILE, "Default")):
        return True
    if not os.path.isdir(DEFAULT_PROFILE):
        return False
    os.makedirs(os.path.dirname(PROFILE), exist_ok=True)
    for flags in ("-Rc", "-R"):                     # -c is an APFS clone
        if subprocess.run(["cp", flags, DEFAULT_PROFILE, PROFILE],
                          capture_output=True).returncode == 0:
            return True
    return False


def enable_cdp(port: int = PORT, timeout: float = 60.0) -> bool:
    """Restart Chrome with the debugging port. Tabs come back on their own."""
    if cdp_up(port):
        return True
    ensure_profile()
    if chrome_running():
        subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to quit'],
                       capture_output=True)
        for _ in range(30):
            if not chrome_running():
                break
            time.sleep(0.5)
        else:
            subprocess.run(["pkill", "-f", "Google Chrome.app/Contents/MacOS"], capture_output=True)
            time.sleep(2)
    subprocess.Popen(
        [CHROME_BIN, f"--remote-debugging-port={port}", "--remote-allow-origins=*",
         f"--user-data-dir={PROFILE}", "--restore-last-session"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cdp_up(port):
            return True
        time.sleep(1.0)
    return False


def tabs(port: int = PORT) -> list[dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as resp:
            return [t for t in json.loads(resp.read().decode()) if t.get("type") == "page"]
    except Exception:
        return []


def ai_tabs(port: int = PORT) -> list[dict]:
    out = []
    for tab in tabs(port):
        url = tab.get("url", "")
        for host in READERS:
            if host in url:
                out.append({**tab, "host": host})
                break
    return out


def evaluate(tab: dict, expression: str, timeout: float = 8.0):
    """Run JavaScript in a tab and return the value."""
    import websocket

    # Chrome rejects a DevTools socket that carries an Origin header.
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=timeout,
                                     suppress_origin=True)
    try:
        ws.send(json.dumps({
            "id": 1, "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True, "awaitPromise": True},
        }))
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = json.loads(ws.recv())
            if message.get("id") == 1:
                return ((message.get("result") or {}).get("result") or {}).get("value")
    finally:
        ws.close()
    return None


def read_tab(tab: dict) -> dict:
    raw = evaluate(tab, READERS[tab["host"]])
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    data["url"] = tab.get("url", "")
    data["host"] = tab["host"]
    if data.get("title"):
        remember_title(tab.get("id", ""), data["title"])
    return data


def send_to_tab(tab: dict, text: str) -> bool:
    """Type into the composer and press return, without touching the keyboard."""
    import websocket

    if evaluate(tab, COMPOSER) != "ok":
        return False
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=10,
                                     suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": "Input.insertText", "params": {"text": text}}))
        time.sleep(0.25)
        for msg_id, kind in ((2, "keyDown"), (3, "keyUp")):
            ws.send(json.dumps({
                "id": msg_id, "method": "Input.dispatchKeyEvent",
                "params": {"type": kind, "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
                           "key": "Enter", "code": "Enter", "text": "\r"},
            }))
            time.sleep(0.05)
        time.sleep(0.4)
        return True
    finally:
        ws.close()


def project_name(tab: dict) -> str:
    """A short spoken name for a browser conversation.

    Chrome truncates a tab title, so "Targeted Registration" arrives as
    "Targeted Regist". The full name is read from the page when available and
    cached against the tab id.
    """
    title = tab.get("full_title") or _TITLES.get(tab.get("id", "")) or tab.get("title", "")
    title = re.sub(r"\s*[-|·]\s*(ChatGPT|Claude)\s*$", "", title).strip()
    title = re.sub(r"^\(\d+\)\s*", "", title)            # "(3) Some chat"
    if title and title.lower() not in ("chatgpt", "claude", "new chat"):
        return title[:60]
    return "ChatGPT" if "chatgpt" in tab.get("host", "") else "Claude web"


_TITLES: dict[str, str] = {}


def remember_title(tab_id: str, title: str) -> None:
    if tab_id and title:
        _TITLES[tab_id] = title
