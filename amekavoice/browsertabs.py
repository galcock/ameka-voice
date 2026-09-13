"""Every AI conversation open in whatever browser he uses, with no debug port.

Chrome, Safari, Arc, Brave, Edge — each answers AppleScript with the title and
address of every tab it has open. That is enough to see a ChatGPT or Claude
conversation and to bring it forward. Reading what it says takes the
accessibility tree of the front window, so a tab is read and typed into only
while it is on screen, for the few seconds its smart loop takes — the way a
person would look at it.
"""
from __future__ import annotations

import re
import subprocess

from . import host

CHROMIUM = ["Google Chrome", "Brave Browser", "Microsoft Edge", "Arc", "Chromium", "Vivaldi"]
AI_SITES = {
    "chatgpt.com": "ChatGPT", "chat.openai.com": "ChatGPT", "claude.ai": "Claude", "gemini.google.com": "Gemini",
    "copilot.microsoft.com": "Copilot", "grok.com": "Grok", "perplexity.ai": "Perplexity",
}


def _running(app: str) -> bool:
    try:
        out = subprocess.run(["osascript", "-e", f'tell application "System Events" to (name of processes) contains "{app}"'],
                             capture_output=True, text=True, timeout=8).stdout.strip()
        return out == "true"
    except Exception:
        return False


def _list(app: str, chromium: bool) -> list[dict]:
    if chromium:
        script = f'''
tell application "{app}"
  set out to ""
  set wi to 0
  repeat with w in windows
    set wi to wi + 1
    set ti to 0
    repeat with t in tabs of w
      set ti to ti + 1
      set out to out & wi & "|||" & ti & "|||" & (URL of t) & "|||" & (title of t) & linefeed
    end repeat
  end repeat
  return out
end tell'''
    else:
        script = f'''
tell application "{app}"
  set out to ""
  set wi to 0
  repeat with w in windows
    set wi to wi + 1
    set ti to 0
    repeat with t in tabs of w
      set ti to ti + 1
      set out to out & wi & "|||" & ti & "|||" & (URL of t) & "|||" & (name of t) & linefeed
    end repeat
  end repeat
  return out
end tell'''
    try:
        out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("|||", 3)
        if len(parts) < 4:
            continue
        w, t, url, title = parts
        site = next((name for dom, name in AI_SITES.items() if dom in url), "")
        if not site:
            continue
        rows.append({"browser": app, "window": int(w), "index": int(t), "url": url.strip(),
                     "title": title.strip() or site, "site": site})
    return rows


def ai_tabs() -> list[dict]:
    """Every AI conversation open in every running browser."""
    if not host.IS_MAC:
        return []
    found = []
    for app in CHROMIUM:
        if _running(app):
            found.extend(_list(app, chromium=True))
    if _running("Safari"):
        found.extend(_list("Safari", chromium=False))
    return found


def key(tab: dict) -> str:
    m = re.search(r"/c/([0-9a-f-]{20,})", tab.get("url", ""))
    ident = m.group(1) if m else re.sub(r"[^a-z0-9]+", "-", tab.get("url", "").lower())[:60]
    return f"browser:{ident}"


def focus(tab: dict) -> bool:
    """Bring the tab to the front — the browser, its window, that tab."""
    app, w, t = tab["browser"], tab["window"], tab["index"]
    if app == "Safari":
        script = f'''
tell application "Safari"
  activate
  set index of window {w} to 1
  set current tab of window 1 to tab {t} of window 1
end tell'''
    else:
        script = f'''
tell application "{app}"
  activate
  set index of window {w} to 1
  set active tab index of window 1 to {t}
end tell'''
    try:
        return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10).returncode == 0
    except Exception:
        return False


def read(tab: dict) -> str:
    """What the conversation says, from the front window's accessibility tree.
    The tab has to be in front; focus() first."""
    from . import sessions as session_tools
    helper = session_tools.AMEKA_AX
    if not helper.exists():
        return ""
    try:
        out = subprocess.run([str(helper), "read", tab["browser"]], capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return ""
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    return "\n".join(lines[-80:])


def send(tab: dict, text: str) -> tuple[bool, str]:
    """Focus the tab, put the cursor in its composer, paste, enter."""
    from . import sessions as session_tools, route
    if not focus(tab):
        return False, "could not bring the tab forward"
    import time
    time.sleep(0.8)
    helper = session_tools.AMEKA_AX
    if helper.exists():
        try:
            out = subprocess.run([str(helper), "field", tab["browser"]], capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception:
            out = ""
        if not out.startswith("yes"):
            return False, "no message box on that page"
    return route.deliver({"app_name": tab["browser"]}, text)
