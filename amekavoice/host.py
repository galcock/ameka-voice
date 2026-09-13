"""What this machine is, and the handful of things that differ by it.

Everything here has a macOS answer and a Windows answer. The rest of the
package asks these questions rather than calling osascript or launchctl
directly, so a Mac-only call site cannot be reached on a PC by accident.

The first port was written blind, with no Windows machine to run it on. So
each function fails softly — a False, an empty string, a logged reason — and
never with a traceback, because on the machine that finally runs it the log
is the only witness.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
NAME = "Windows" if IS_WIN else "macOS" if IS_MAC else sys.platform


# ------------------------------------------------------------- processes --
def pid_alive(pid: int) -> bool:
    """Is this process still running? Without touching it.

    On macOS a signal of zero is a probe. On Windows os.kill treats any
    signal it does not recognise as TerminateProcess — so the same line that
    lists sessions on a Mac would have killed every Claude Code session on a
    PC the first time she looked.
    """
    if pid <= 0:
        return False
    if not IS_WIN:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    SYNCHRONIZE, STILL_ACTIVE = 0x00100000, 259
    kernel32 = ctypes.windll.kernel32                                    # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(SYNCHRONIZE | 0x1000, False, int(pid))  # + QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return True
    finally:
        kernel32.CloseHandle(handle)


# ----------------------------------------------------------------- voice --
def speak_system(text: str, voice: str = "") -> bool:
    """The voice the operating system ships with. The last resort, everywhere."""
    if IS_MAC:
        cmd = ["say"] + (["-v", voice] if voice else []) + [text]
        try:
            subprocess.run(cmd, check=False, timeout=90)
            return True
        except Exception:
            return False
    if IS_WIN:
        escaped = text.replace("'", "''")
        script = ("Add-Type -AssemblyName System.Speech; "
                  "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                  + (f"try {{ $s.SelectVoice('{voice}') }} catch {{}}; " if voice else "")
                  + f"$s.Speak('{escaped}')")
        try:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           check=False, timeout=90, creationflags=_no_window())
            return True
        except Exception:
            return False
    return False


# ------------------------------------------------------------- clipboard --
def clipboard_read() -> str | None:
    try:
        if IS_MAC:
            return subprocess.run(["pbpaste"], capture_output=True, timeout=4).stdout.decode("utf-8", "replace")
        if IS_WIN:
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard -Raw"],
                                 capture_output=True, timeout=6, creationflags=_no_window()).stdout
            return out.decode("utf-8", "replace")
    except Exception:
        pass
    return None


def clipboard_write(text: str) -> bool:
    try:
        if IS_MAC:
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), timeout=4)
            return True
        if IS_WIN:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", "Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
                           input=text.encode("utf-8"), timeout=6, creationflags=_no_window())
            return True
    except Exception:
        pass
    return False


# --------------------------------------------------------------- windows --
def _no_window() -> int:
    return 0x08000000 if IS_WIN else 0            # CREATE_NO_WINDOW


def focus_window(title_contains: str) -> bool:
    """Bring the first top-level window whose title contains the text to the front. Windows only."""
    if not IS_WIN:
        return False
    user32 = ctypes.windll.user32                                        # type: ignore[attr-defined]
    found = []
    wanted = title_contains.lower()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)   # type: ignore[attr-defined]
    def each(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if wanted in buf.value.lower():
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(each, 0)
    if not found:
        return False
    hwnd = found[0]
    SW_RESTORE = 9
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    # Windows refuses SetForegroundWindow to a process that is not the one
    # receiving input; pressing a null key first is the documented workaround.
    _key(0xA4, up=False); _key(0xA4, up=True)          # VK_MENU tap
    user32.SetForegroundWindow(hwnd)
    return True


def _key(vk: int, up: bool) -> None:
    KEYEVENTF_KEYUP = 0x0002
    ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP if up else 0, 0)  # type: ignore[attr-defined]


def paste_and_enter(text: str) -> bool:
    """Put text into the focused window as a paste, then press Enter. Windows only."""
    if not IS_WIN:
        return False
    import time
    saved = clipboard_read()
    if not clipboard_write(text):
        return False
    time.sleep(0.15)
    VK_CONTROL, VK_V, VK_RETURN = 0x11, 0x56, 0x0D
    _key(VK_CONTROL, up=False); _key(VK_V, up=False)
    _key(VK_V, up=True); _key(VK_CONTROL, up=True)
    time.sleep(0.25)
    _key(VK_RETURN, up=False); _key(VK_RETURN, up=True)
    time.sleep(0.15)
    if saved is not None:
        clipboard_write(saved)
    return True


# --------------------------------------------------------------- startup --
def install_startup(command: list[str], keepalive: list[str] | None = None,
                    label: str = "AmekaVoice") -> tuple[bool, str]:
    """Run at login and keep running. Windows: a scheduled task at logon, plus
    one every five minutes running `keepalive`, which starts her only if she is
    not answering — the nearest thing to launchd's KeepAlive without a service."""
    if not IS_WIN:
        return False, "not this platform"
    def quote(parts): return " ".join(f'"{c}"' if " " in c else c for c in parts)
    results = []
    for name, schedule, parts in ((label, ["/sc", "onlogon"], command),
                                  (f"{label}Keepalive", ["/sc", "minute", "/mo", "5"], keepalive or command)):
        try:
            done = subprocess.run(["schtasks", "/create", "/f", "/tn", name, "/tr", quote(parts), *schedule, "/rl", "limited"],
                                  capture_output=True, text=True, timeout=30, creationflags=_no_window())
            results.append(f"{name}: {'ok' if done.returncode == 0 else (done.stderr or done.stdout).strip()[:120]}")
        except Exception as exc:
            results.append(f"{name}: {exc!r}")
    ok = all(": ok" in r for r in results)
    return ok, "; ".join(results)


def already_running(port: int) -> bool:
    """Is a daemon answering on the port? Used by the keepalive task."""
    import urllib.request
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
        return True
    except Exception:
        return False


def mac_only(default):
    """Make a macOS-only function return `default` everywhere else."""
    import functools

    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            if not IS_MAC:
                return default() if callable(default) else default
            return fn(*args, **kwargs)
        return inner
    return wrap


def home_bin() -> Path:
    return Path.home() / (".ameka" if IS_WIN else ".local/bin")


def accessibility_trusted() -> bool:
    """Is THIS process allowed to drive other apps?

    `ameka doctor` asked this from the command line, where the answer is about
    the terminal, not about her. The daemon has to answer for itself: it is a
    different binary with a different grant, and when it is missing every smart
    loop decides what to do and then cannot reach the session to do it.
    """
    if not IS_MAC:
        return True
    try:
        import ctypes
        import ctypes.util
        ax = ctypes.CDLL(ctypes.util.find_library("ApplicationServices"))
        return bool(ax.AXIsProcessTrusted())
    except Exception:
        return True
