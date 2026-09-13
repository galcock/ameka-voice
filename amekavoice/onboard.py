"""First run: ask for what Ameka needs, check it was granted, say what is left.

Three permissions and two engines, in the order macOS wants them. Everything
that can be triggered from code is triggered here rather than described, so the
prompts appear while the person is still watching.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time

from . import audio, browser, config as cfgmod, engines, route, setup_local, tools

TICK, CROSS, DOT = "✓", "✗", "·"


def _say(mark: str, label: str, detail: str = "") -> None:
    print(f"  {mark} {label}" + (f" — {detail}" if detail else ""))


def microphone() -> bool:
    """Opening the mic is what makes macOS ask. Nothing else does."""
    if not audio.HAVE_SD:
        _say(CROSS, "Microphone", "audio library missing")
        return False
    try:
        import sounddevice as sd

        with sd.InputStream(samplerate=16000, channels=1, dtype="int16"):
            time.sleep(0.35)
    except Exception as exc:
        _say(CROSS, "Microphone", str(exc)[:60])
        return False

    import numpy as np

    try:
        import sounddevice as sd

        with sd.InputStream(samplerate=16000, channels=1, dtype="int16") as stream:
            block, _ = stream.read(4800)
        peak = float(np.abs(np.frombuffer(bytes(block), dtype="int16")).max()) / 32768
    except Exception:
        peak = 0.0
    if peak < 1e-4:
        _say(DOT, "Microphone", "granted, but the room is silent — say something to test")
    else:
        _say(TICK, "Microphone", f"live on {audio.default_input_name()}")
    return True


def accessibility() -> bool:
    """Ameka types your answer into a session, which needs Accessibility."""
    notes = route.preflight()
    blocked = [n for n in notes if "Accessibility" in n]
    if not blocked:
        _say(TICK, "Accessibility", "Ameka can type into your sessions")
        return True
    # Asking System Events to do something is what raises the prompt.
    if sys.platform != "darwin":
        return
    subprocess.run(["osascript", "-e",
                    'tell application "System Events" to get name of first process'],
                   capture_output=True)
    if not [n for n in route.preflight() if "Accessibility" in n]:
        _say(TICK, "Accessibility", "granted")
        return True
    _say(CROSS, "Accessibility",
         "open System Settings > Privacy & Security > Accessibility and switch on Ameka")
    return False


def voice_engines() -> bool:
    ok = engines.kokoro_available() and engines.whisper_available()
    if ok:
        _say(TICK, "Voice and hearing", "both running on this Mac, no account needed")
        return True
    print("  installing the on-device voice and hearing engines…")
    setup_local.run()
    ok = engines.kokoro_available() and engines.whisper_available()
    _say(TICK if ok else CROSS, "Voice and hearing",
         "ready" if ok else "incomplete — run: ameka setup-local")
    return ok


def sessions_found() -> int:
    from .sessions import live_sessions

    found = live_sessions()
    if found:
        _say(TICK, "Claude Code", f"{len(found)} session"
             f"{'s' if len(found) != 1 else ''}: "
             + ", ".join(sorted({s['project'] for s in found})))
    else:
        _say(DOT, "Claude Code", "no sessions open yet — Ameka will find them when you start one")
    return len(found)


def codex_found() -> int:
    from .codex_watch import latest_files

    count = len(latest_files())
    _say(TICK if count else DOT, "Codex",
         f"{count} recent session{'s' if count != 1 else ''}" if count
         else "nothing recent — Ameka watches for new ones")
    return count


def browser_found(connect: bool) -> int:
    if browser.cdp_up():
        tabs = browser.ai_tabs()
        _say(TICK, "Browser", f"{len(tabs)} ChatGPT or Claude tab"
             f"{'s' if len(tabs) != 1 else ''} connected")
        return len(tabs)
    if not connect:
        _say(DOT, "Browser", 'not connected — say "connect the browser" any time')
        return 0
    print("  connecting Chrome (it restarts; your tabs come back)…")
    if browser.enable_cdp():
        tabs = browser.ai_tabs()
        _say(TICK, "Browser", f"{len(tabs)} tab{'s' if len(tabs) != 1 else ''} connected")
        return len(tabs)
    _say(CROSS, "Browser", "Chrome did not come back with its port open")
    return 0


def run(connect_browser: bool = False) -> int:
    print("Setting Ameka up. Nothing here leaves your Mac.\n")
    print("Permissions")
    mic = microphone()
    axe = accessibility()

    print("\nEngines")
    eng = voice_engines()

    print("\nWhat you use, and how Ameka reaches it")
    for tool in tools.detect():
        if not tool["present"]:
            _say(DOT, tool["name"], "not on this Mac — nothing to set up")
            continue
        if tool["name"] == "Claude Code":
            sessions_found()
        elif tool["name"] == "Codex":
            codex_found()
        elif tool["name"] == "Browser":
            browser_found(connect_browser)
        else:
            _say(TICK, tool["name"], tool["detail"])
    if not tools.available():
        print("\n  No AI tools found yet. Ameka will pick them up as soon as you")
        print("  open Claude Code, Codex, or a ChatGPT or Claude tab.")

    print("\nDelivery")
    _say(TICK if shutil.which("tmux") or engines.find_bin("tmux") else DOT, "tmux",
         "sessions can be answered by name" if engines.find_bin("tmux")
         else "optional — brew install tmux to answer several sessions by name")

    ready = mic and axe and eng
    print("\n" + ("Ready. Put your AirPods in and finish a session — Ameka will speak."
                  if ready else
                  "Almost. Fix the lines marked ✗ above, then run: ameka doctor"))
    return 0 if ready else 1
