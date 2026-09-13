"""Deliver a reply back into the session that spoke to you.

Preference order:
  1. tmux pane recorded by the hook (exact, silent, no focus stealing)
  2. AppleScript keystroke into the host app the hook ran under
"""
from __future__ import annotations

import shlex
import shutil
import subprocess
import time

from . import host

BUNDLE_NAMES = {
    "com.anthropic.claudefordesktop": "Claude",
    "com.anthropic.claude": "Claude",
    "com.apple.Terminal": "Terminal",
    "com.googlecode.iterm2": "iTerm2",
    "com.microsoft.VSCode": "Code",
    "dev.warp.Warp-Stable": "Warp",
    "com.github.wez.wezterm": "WezTerm",
    "net.kovidgoyal.kitty": "kitty",
}


def _osascript(script: str) -> tuple[bool, str]:
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return proc.returncode == 0, (proc.stderr or proc.stdout).strip()


def _applescript_literal(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def deliver(target: dict, text: str) -> tuple[bool, str]:
    from .engines import find_bin

    tab_id = target.get("cdp_tab_id")
    if tab_id:
        from . import browser
        for tab in browser.ai_tabs():
            if tab["id"] == tab_id:
                try:
                    if browser.send_to_tab(tab, text):
                        return True, f"browser:{browser.project_name(tab)}"
                except Exception as exc:
                    return False, f"browser send failed ({str(exc)[:60]})"
        return False, "that browser tab is gone"

    pane = target.get("tmux_pane")
    tmux = find_bin("tmux")
    if pane and tmux:
        ok = True
        for args in (["send-keys", "-t", pane, "-l", text], ["send-keys", "-t", pane, "Enter"]):
            proc = subprocess.run([tmux, *args], capture_output=True, text=True)
            ok = ok and proc.returncode == 0
            time.sleep(0.05)
        if ok:
            return True, f"tmux:{pane}"

    bundle = target.get("bundle_id") or ""
    app = BUNDLE_NAMES.get(bundle) or target.get("app_name") or ""
    if host.IS_WIN:
        # No tmux and no AppleScript here. Typing goes to whichever session is
        # in front, so bring the app forward and paste, the way a person would.
        title = "Claude" if "claude" in (bundle + app).lower() or not app else app
        if not host.focus_window(title):
            return False, f"no {title} window to type into"
        time.sleep(0.4)
        if not host.paste_and_enter(text):
            return False, "could not paste into the window"
        return True, f"paste:{title}"
    literal = _applescript_literal(text)

    if bundle:
        activate = f'tell application id "{_applescript_literal(bundle)}" to activate'
    elif app:
        activate = f'tell application "{_applescript_literal(app)}" to activate'
    else:
        return False, "no route recorded for this session"

    # Typing character by character races the Return that follows it: on a long
    # line the text was still arriving when Enter fired, so it sat in the box
    # unsent. Pasting puts the whole line in at once.
    saved = _clipboard_read()
    _clipboard_write(text)
    script = (
        f'{activate}\n'
        'delay 0.35\n'
        'tell application "System Events"\n'
        '  keystroke "v" using command down\n'
        '  delay 0.25\n'
        '  key code 36\n'
        'end tell'
    )
    ok, err = _osascript(script)
    time.sleep(0.15)
    if saved is not None:
        _clipboard_write(saved)
    if not ok:
        # Fall back to typing it, which is slower but needs no clipboard.
        fallback = (
            f'{activate}\n'
            'delay 0.3\n'
            'tell application "System Events"\n'
            f'  keystroke "{literal}"\n'
            f'  delay {max(0.3, min(2.0, len(text) / 120)):.2f}\n'
            '  key code 36\n'
            'end tell'
        )
        ok, err = _osascript(fallback)
        if not ok:
            return False, f"applescript failed ({err[:120]})"
        return True, f"keystroke:{app or bundle}"
    return True, f"paste:{app or bundle}"


def _clipboard_read():
    try:
        proc = subprocess.run(["pbpaste"], capture_output=True, timeout=4)
        return proc.stdout if proc.returncode == 0 else None
    except Exception:
        return None


def _clipboard_write(data) -> None:
    try:
        payload = data if isinstance(data, bytes) else str(data).encode()
        subprocess.run(["pbcopy"], input=payload, timeout=4)
    except Exception:
        pass


def reveal(target: dict) -> bool:
    """Bring the session you just named to the front, so you can see it."""
    if host.IS_WIN:
        return host.focus_window("Claude") if target.get("bundle_id") or target.get("app_session") else False
    tab_id = target.get("cdp_tab_id")
    if tab_id:
        from . import browser

        try:
            for tab in browser.ai_tabs():
                if tab["id"] == tab_id:
                    urllib_open = f"http://127.0.0.1:{browser.PORT}/json/activate/{tab_id}"
                    import urllib.request

                    urllib.request.urlopen(urllib_open, timeout=4).read()
                    subprocess.run(["osascript", "-e",
                                    'tell application "Google Chrome" to activate'],
                                   capture_output=True)
                    return True
        except Exception:
            return False
        return False

    bundle = target.get("bundle_id")
    app = BUNDLE_NAMES.get(bundle) or target.get("app_name")
    if app:
        ok, _ = _osascript(f'tell application "{_applescript_literal(app)}" to activate')
        return ok
    return False


def describe(target: dict) -> str:
    if target.get("cdp_tab_id"):
        return target.get("label") or "a browser tab"
    if target.get("tmux_pane"):
        return f"tmux pane {target['tmux_pane']}"
    bundle = target.get("bundle_id") or target.get("app_name") or "unknown app"
    return BUNDLE_NAMES.get(bundle, bundle)


FRONT_SCRIPT = """
tell application "System Events"
  set frontApp to name of first application process whose frontmost is true
end tell
set tabURL to ""
if frontApp is "Google Chrome" then
  try
    tell application "Google Chrome" to set tabURL to URL of active tab of front window
  end try
end if
return frontApp & "\n" & tabURL
"""


def frontmost() -> tuple[str, str]:
    """(app name, active browser tab url). Cheap enough to check every turn."""
    if host.IS_WIN:
        return "", ""
    proc = subprocess.run(["osascript", "-e", FRONT_SCRIPT],
                          capture_output=True, text=True, timeout=8)
    if proc.returncode != 0:
        return "", ""
    lines = (proc.stdout or "").splitlines()
    app = lines[0].strip() if lines else ""
    url = lines[1].strip() if len(lines) > 1 else ""
    return app, url


def preflight() -> list[str]:
    """Report anything that will stop delivery from working."""
    if host.IS_WIN:
        return []
    notes = []
    ok, err = _osascript('tell application "System Events" to return name of first process')
    if not ok:
        notes.append(
            "Accessibility permission missing: System Settings > Privacy & Security > "
            "Accessibility > enable your terminal app. (" + err[:80] + ")"
        )
    from .engines import find_bin
    if not find_bin("tmux"):
        notes.append("tmux not installed (optional; enables silent delivery without focus)")
    return notes


def shell_quote(text: str) -> str:
    return shlex.quote(text)
