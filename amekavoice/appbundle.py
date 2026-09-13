"""Make macOS call her Ameka when it asks for permission.

The permission sheets said "python" wants the microphone, and "python" wants
to control your computer — which is both alarming and meaningless. macOS names
whatever asked, and what asked was an interpreter.

So the daemon runs from an app bundle whose executable is a copy of that same
interpreter named Ameka, with an Info.plist that says who it is and what it
needs each permission for. Python finds the venv through pyvenv.cfg beside the
bundle's Contents, and the packages through PYTHONPATH, so nothing about the
install changes — only the name on the sheet, and the sentence under it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

BUNDLE = Path(os.path.expanduser("~/.ameka/Ameka Voice.app"))
EXECUTABLE = BUNDLE / "Contents" / "MacOS" / "Ameka"

INFO = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Ameka</string>
  <key>CFBundleDisplayName</key><string>Ameka</string>
  <key>CFBundleExecutable</key><string>Ameka</string>
  <key>CFBundleIdentifier</key><string>ai.ameka.voice</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>{version}</string>
  <key>LSUIElement</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Ameka listens for your voice so you can run your coding sessions by speaking.</string>
  <key>NSAppleEventsUsageDescription</key><string>Ameka brings the session you name to the front, so what you say reaches that one and no other.</string>
  <key>NSSpeechRecognitionUsageDescription</key><string>Ameka turns what you say into words, on this Mac.</string>
</dict></plist>
"""


def base_interpreter() -> Path:
    """The real interpreter behind this venv — the one that does the work.

    A framework build's `python3.x` is a stub that re-execs into
    Python.app/Contents/MacOS/Python, and macOS names the image it ends up
    running: copying the stub into the bundle still produced a sheet saying
    "Python". The app inside the framework is the interpreter itself.
    """
    stub = Path(getattr(sys, "_base_executable", None) or sys.executable).resolve()
    inside = Path(sys.base_prefix) / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    return inside if inside.exists() else stub


def site_packages() -> str:
    return sysconfig.get_paths()["purelib"]


def build(version: str = "1.0", log=print) -> Path | None:
    """(Re)build the bundle. Returns the executable to run, or None off macOS."""
    if sys.platform != "darwin":
        return None
    real = base_interpreter()
    if not real.exists():
        return None
    try:
        macos = BUNDLE / "Contents" / "MacOS"
        macos.mkdir(parents=True, exist_ok=True)
        shutil.copy2(real, EXECUTABLE)
        (BUNDLE / "Contents" / "Info.plist").write_text(INFO.format(version=version))
        # Beside Contents, not beside the binary: python expects the prefix one
        # level up from the executable, and complains on every start otherwise.
        (BUNDLE / "Contents" / "pyvenv.cfg").write_text(
            f"home = {real.parent}\ninclude-system-site-packages = false\n"
            f"version = {sys.version.split()[0]}\n")
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(BUNDLE)],
                       capture_output=True, timeout=60)
    except Exception as exc:
        log(f"could not build the Ameka app bundle ({exc!r}); macOS will say python instead")
        return None
    return EXECUTABLE


def ready(version: str = "1.0") -> Path | None:
    """The bundle's executable, rebuilt if it is missing or from another Python."""
    if sys.platform != "darwin":
        return None
    if EXECUTABLE.exists():
        try:
            if EXECUTABLE.stat().st_size == base_interpreter().stat().st_size:
                return EXECUTABLE
        except OSError:
            pass
    return build(version)


def env() -> dict:
    """What the bundle needs to find her code and her packages."""
    from . import selfupdate
    return {"PYTHONPATH": f"{site_packages()}:{selfupdate.INSTALLED.parent}"}
