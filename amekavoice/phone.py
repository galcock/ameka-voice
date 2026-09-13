"""The phone side: reach Ameka from an iPhone on the same network.

The daemon normally binds to localhost only. Turning this on binds it to the
LAN as well and protects every phone route with a shared token, so the Mac
keeps working while you are in another room with only your phone and AirPods.
"""
from __future__ import annotations

import io
import json
import secrets
import wave
from pathlib import Path

from . import config as cfgmod

TOKEN_PATH = cfgmod.STATE_DIR / "phone-token"


def new_token() -> str:
    """Throw the old key away and issue a fresh one."""
    cfgmod.ensure_dirs()
    value = secrets.token_urlsafe(18)
    TOKEN_PATH.write_text(value)
    TOKEN_PATH.chmod(0o600)
    return value


def token() -> str:
    """A stable secret for this Mac, made once."""
    cfgmod.ensure_dirs()
    if TOKEN_PATH.exists():
        value = TOKEN_PATH.read_text().strip()
        if value:
            return value
    value = secrets.token_urlsafe(18)
    TOKEN_PATH.write_text(value)
    TOKEN_PATH.chmod(0o600)
    return value


def lan_address() -> str:
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))          # no packet is sent
        return probe.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        probe.close()


def tailscale_address() -> str:
    """A Tailscale address follows the Mac anywhere, so the shortcut keeps
    working on cellular and never needs editing again."""
    import shutil
    import subprocess

    binary = shutil.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    try:
        out = subprocess.run([binary, "status", "--json"], capture_output=True,
                             text=True, timeout=6)
        if out.returncode != 0:
            return ""
        state = json.loads(out.stdout)
        me = state.get("Self") or {}
        name = (me.get("DNSName") or "").rstrip(".")
        if name and state.get("BackendState") == "Running":
            return name
        for address in me.get("TailscaleIPs") or []:
            if ":" not in address:
                return address
    except Exception:
        return ""
    return ""


def reachability(config) -> tuple[str, str]:
    """Where the phone should point, and how far that reaches."""
    port = int(config.section("server").get("port", 8765))
    tailnet = tailscale_address()
    if tailnet:
        return f"http://{tailnet}:{port}", "anywhere"
    return f"http://{lan_address()}:{port}", "same wifi"


def base_url(config) -> str:
    return reachability(config)[0]


def wav_bytes(samples, rate: int) -> bytes:
    """Float samples from the neural voice, as a WAV an iPhone will play."""
    import numpy as np

    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return buf.getvalue()


def shortcut_recipe(config) -> str:
    """The Shortcut to build, once. It loops, so one "Hey Siri" is a whole
    conversation rather than one exchange."""
    from .relay import DEFAULT_URL

    cfg = config.section("phone")
    url = cfg.get("relay_url") or DEFAULT_URL
    key = token()
    local, reach = reachability(config)
    scope = ("Works anywhere with signal — the Mac reaches out to the relay, so nothing\n"
             "has to reach in. No app, no VPN, no settings."
             if url else f"Only on this network: {local}")

    return f"""Ameka on your iPhone

{scope}

A Shortcut is a saved list of steps in Apple's Shortcuts app, which is already
on your phone. Nothing is installed. You build this once, then say
"Hey Siri, Ameka" and talk — it keeps listening until you say stop, so you say
her name once per conversation, not once per sentence.

Open Shortcuts, tap +, name it Ameka, and add these:

  1. Repeat  20 times
       2. Dictate Text
            Language: English · Stop Listening: After Pause
       3. If  Dictated Text  is  stop
            4. Stop This Shortcut
          End If
       5. Get Contents of URL
            URL      {url}
            Method   POST
            Headers  x-ameka-channel : {key}
                     x-ameka-role    : phone
            Request Body  JSON
                     action : send
                     text   : Dictated Text
       6. Get Contents of URL
            URL      {url}
            Method   POST
            Headers  x-ameka-channel : {key}
                     x-ameka-role    : phone
            Request Body  JSON
                     action : poll
       7. Get Dictionary Value   message   from  Contents of URL
       8. Get Dictionary Value   text      from  Dictionary Value
       9. Speak Text  (Dictionary Value)
     End Repeat

That is the whole thing. Say "Hey Siri, Ameka", then talk normally:
"what sessions are running", "switch to the api", "proceed", "stop listening".

Keep that channel line private — it is the key to this Mac. `ameka phone --new-key`
issues a fresh one if it ever gets out.
"""
