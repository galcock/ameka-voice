"""Install the local engines so Ameka needs no API key and no network.

  whisper.cpp + ggml-base.en   hearing you      ~148 MB
  Kokoro-82M (onnx) + voices   speaking to you  ~354 MB
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from .engines import KOKORO_MODEL, KOKORO_VOICES, MODELS, whisper_model

WHISPER_MODEL = MODELS / "ggml-small.en.bin"

DOWNLOADS = {
    WHISPER_MODEL: "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin",
    KOKORO_MODEL: ("https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
                   "model-files-v1.0/kokoro-v1.0.onnx"),
    KOKORO_VOICES: ("https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
                    "model-files-v1.0/voices-v1.0.bin"),
}


def _fetch(dest: Path, url: str) -> bool:
    if dest.exists() and dest.stat().st_size > 1_000_000:
        print(f"  have {dest.name} ({dest.stat().st_size // 1_000_000} MB)")
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  downloading {dest.name} …", end="", flush=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as fh:
            total = int(resp.headers.get("content-length") or 0)
            got = 0
            while chunk := resp.read(1 << 20):
                fh.write(chunk)
                got += len(chunk)
                if total:
                    print(f"\r  downloading {dest.name} … {got * 100 // total}%",
                          end="", flush=True)
        tmp.rename(dest)
        print(f"\r  downloaded {dest.name} ({dest.stat().st_size // 1_000_000} MB)   ")
        return True
    except Exception as exc:
        print(f"\r  FAILED {dest.name}: {str(exc)[:80]}")
        tmp.unlink(missing_ok=True)
        return False


def run(skip_brew: bool = False) -> int:
    ok = True
    print("Local engines — no API key, nothing leaves this Mac.\n")

    print("hearing (whisper.cpp)")
    if shutil.which("whisper-cli"):
        print("  have whisper-cli")
    elif sys.platform == "win32":
        try:
            import pywhispercpp  # noqa: F401
            print("  have pywhispercpp")
        except Exception:
            print("  installing pywhispercpp (whisper.cpp for Python) …")
            rc = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "pywhispercpp"],
                                capture_output=True).returncode
            if rc:
                print("  ! pip install pywhispercpp failed — she cannot hear you until it is installed")
                ok = False
            else:
                print("  installed pywhispercpp")
    elif skip_brew:
        print("  ! whisper-cli missing (brew install whisper-cpp)")
        ok = False
    elif shutil.which("brew"):
        print("  installing whisper-cpp with Homebrew (a few minutes) …")
        rc = subprocess.run(["brew", "install", "whisper-cpp"], capture_output=True).returncode
        if rc or not shutil.which("whisper-cli"):
            print("  ! brew install failed — run it yourself: brew install whisper-cpp")
            ok = False
        else:
            print("  installed whisper-cli")
    else:
        print("  ! Homebrew not found. Install it, then: brew install whisper-cpp")
        ok = False
    ok &= _fetch(WHISPER_MODEL, DOWNLOADS[WHISPER_MODEL])

    print("\nspeaking (Kokoro neural voice)")
    try:
        import kokoro_onnx  # noqa: F401
        print("  have kokoro-onnx")
    except Exception:
        print("  installing kokoro-onnx …")
        rc = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                             "kokoro-onnx", "soundfile"], capture_output=True).returncode
        if rc:
            print("  ! pip install failed — the macOS voice will be used instead")
            ok = False
        else:
            print("  installed kokoro-onnx")
    for dest in (KOKORO_MODEL, KOKORO_VOICES):
        ok &= _fetch(dest, DOWNLOADS[dest])

    print("\nthinking (her own brain, llama.cpp)")
    from . import brain as brain_mod
    if brain_mod.runtime_available():
        print("  have llama.cpp runtime")
    else:
        print("  installing llama-cpp-python (a few minutes; it compiles for this machine) …")
        env = dict(os.environ)
        if sys.platform == "darwin":
            env["CMAKE_ARGS"] = "-DGGML_METAL=on"
        cmd = [sys.executable, "-m", "pip", "install", "--quiet", "llama-cpp-python>=0.3.30"]
        if sys.platform == "win32":
            # A prebuilt CPU wheel where one exists — a PC rarely has a compiler.
            cmd += ["--prefer-binary", "--extra-index-url", "https://abetlen.github.io/llama-cpp-python/whl/cpu"]
        rc = subprocess.run(cmd, capture_output=True, env=env).returncode
        if rc and sys.platform == "win32":
            rc = subprocess.run(cmd[:6], capture_output=True, env=env).returncode
        if rc:
            print("  ! llama-cpp-python did not install — briefings use the rule-based extractor until it does")
            ok = False
        else:
            print("  installed llama.cpp runtime")
    if brain_mod.runtime_available():
        print("  " + brain_mod.refresh(log=lambda m: print("  " + m), force=not brain_mod.BRAIN.exists()))
    print(f"\nmodels live in {MODELS}")
    print("done." if ok else "finished with gaps — see the lines marked ! above")
    return 0 if ok else 1
