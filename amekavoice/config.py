"""Config loading for Ameka Voice.

Config lives at ~/.config/ameka/config.toml. Multiple ChatGPT (OpenAI) and
Claude (Anthropic) accounts are supported: each is a named entry under
[accounts.openai.<name>] / [accounts.anthropic.<name>], and each subsystem
(voice, listen, summarize) picks which named account it uses.
"""
from __future__ import annotations

import json
import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .identity import delivery
from typing import Any

CONFIG_DIR = Path(os.path.expanduser("~/.config/ameka"))
# AMEKA_CONFIG lets you point at a scratch config (testing, second machine).
CONFIG_PATH = Path(os.path.expanduser(os.environ.get("AMEKA_CONFIG") or (CONFIG_DIR / "config.toml")))
STATE_DIR = Path(os.path.expanduser("~/.local/state/ameka"))
LOG_PATH = STATE_DIR / "ameka.log"
MUTE_FLAG = STATE_DIR / "muted"
PORT_DEFAULT = 8765

DEFAULTS: dict[str, Any] = {
    "server": {"host": "127.0.0.1", "port": PORT_DEFAULT},
    # Off by default: turning it on opens the port to your network, so every
    # phone route carries a token.
    # The phone aims itself without moving the microphone in your ear.
    "phone": {"enabled": False, "relay_url": "", "separate_binding": True},
    "voice": {
        "engine": "auto",            # auto | kokoro | openai | say
        "kokoro_voice": "am_michael",
        "pronounce": {},   # {"Kubectl": "cube cuttle"} — respell what it says wrong
        "account": "default",
        "model": "gpt-4o-mini-tts",
        "audio_model": "gpt-audio",   # the Advanced Voice Mode family
        "voice": "ballad",
        "speed": 1.05,
        "instructions": "",   # blank uses the built-in identity
        "output_device": "",
        "fallback_say_voice": "Daniel",
    },
    "listen": {
        "engine": "auto",            # auto | whisper | openai
        "threads": 4,
        "account": "default",
        "stt_model": "gpt-4o-mini-transcribe",
        "prompt": "",   # blank uses the built-in product vocabulary
        "input_device": "",
        "window_seconds": 90.0,       # cap on one utterance
        "room_window_seconds": 25,
        "room_speech_over": 5.0,
        "idle_wait_seconds": 12.0,    # how long a single wait-for-speech lasts
        "stream_max_idle_seconds": 240.0,   # retire an idle mic stream rather than trust it
        # Measured on AirPods: the room is 0.00005, Ameka's own voice leaking back is
        # 0.0002, and speech is around 0.010. The old floor of 0.055 was five times
        # above the voice it was meant to detect, so it never fired.
        "barge_over_leakage": 4.0,
        "barge_floor": 0.004,
        "barge_seconds": 0.12,
        "barge_ceiling": 0.05,        # the bar never rises above this, whatever the room does
        "barge_settle_seconds": 0.6,  # ignore the first moment, before sound starts      # a long thought is not a runaway recording
        "silence_seconds": 1.6,   # quiet for this long means you have finished

        "min_speech_seconds": 0.24,
        # How much sound before the trigger is kept. Too little and a sentence
        # arrives having started in the middle.
        "preroll_seconds": 0.66,
        "onset_seconds": 0.045,
        "min_confidence": 0.35,
        "earcon": True,
    },
    "summarize": {
        # auto = free local extraction unless you have configured a key.
        # local | claude-cli | openai | anthropic
        "provider": "auto",
        "account": "default",
        "model": "gpt-4o-mini",
        "cli_model": "haiku",
        "max_input_chars": 12000,
    },
    "behavior": {
        "reply_affirm": "proceed",
        "chat": False,
        "default_target": "",   # blank: whichever AI tool this Mac actually has
        # One real word is a real answer: "Finished." "Correctly." Noise is caught by
        # pitch, the speech band and the stopword list, not by counting words.
        "chat_min_words": 1,
        # Off: everything you say reaches the session. Non-speech is already
        # stopped acoustically, so this only ever discarded real words.
        "greeting": "",           # silent on start; restarts should not announce themselves
        "announce_project": True,
        # Switching windows by hand moves the microphone with you.
        "follow_focus": True,
        # Saying "switch to X" brings X to the front, so you can see it.
        "reveal_on_switch": True,
        "continuation_grace_seconds": 1.4,
        # How long to wait when the sentence plainly has not finished.
        "unfinished_grace_seconds": 3.5,
        # "always" reads out every finished turn, which is the point of having
        # it in your ear: staying quiet means reading the screen to find out
        # what happened. "worthwhile" limits it to failures, blocks and real
        # next steps.
        # Her judgement, not a rule. She reads the turn and says whether
        # it is worth interrupting him for; "always" threw that away and
        # read out every finished turn, including the ones that had
        # already decided not to notify him.
        "speak_when": "worthwhile",
        # "full" reads the whole closing point; "short" reads one sentence.
        "briefing": "full",
        # Talking over Ameka stops it mid-sentence.
        "barge_in": True,
        # Say "okay" the moment something is sent, and "still going" if a session
        # is quiet for a long time. Silence is what makes it feel like a machine.
        "acknowledge": True,
        "still_working_after": 45.0,
        # Wakes itself after this long asleep, so a mangled name can never strand
        # it. 0 turns the safety net off.
        "sleep_timeout_minutes": 30,
        "watch_codex": True,
        "watch_browser": True,
        "browser_poll": 4.0,
        "codex_target": "com.openai.chat",
        # Every pause is a window where you can speak and not be heard. There is no
        # reason for one after sending.
        "chat_pause_after_send": 0.0,
        # One briefing per session per window. At four seconds a busy
        # session earned a spoken briefing every four seconds, and she
        # talked over her own last sentence about the same thing.
        # Projects she must not send to or start. Empty by default: this
        # is a thing you tell her, not a thing she decides.
        # What she may touch. Empty means everything; naming any means
        # only those, which cannot go stale when a new project appears.
        # Where her own code lives. Complaints about how she behaves are
        # bug reports, and they belong where they can be acted on.
        "own_project": "Ameka",
        # Where her code is written. Set it and she installs her own
        # updates and restarts into them; empty and she never does,
        # which is what an install that is not being worked on wants.
        "source_dir": "",
        # How often she reads her own log for signs she is malfunctioning,
        # and how long before the same fault is worth mentioning again.
        "selfwatch_minutes": 10,
        "selfwatch_repeat_hours": 4,
        "focus": [],
        "hands_off": [],
        "dedupe_seconds": 45.0,
        "max_queue": 8,
        "auto_listen": True,
        "confirm_freeform": True,
    },
    # Words the recogniser gets wrong, and what you actually said. Anyone can
    # add their own: [glossary] then "what you hear" = "what you meant".
    "glossary": {},
    "accounts": {"openai": {}, "anthropic": {}},
}


PLACEHOLDERS = {"sk-REPLACE-ME", "REPLACE-ME", "", "sk-..."}


@dataclass
class Account:
    name: str
    api_key: str = ""
    base_url: str = ""
    label: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.api_key) and self.api_key not in PLACEHOLDERS


@dataclass
class Config:
    raw: dict[str, Any] = field(default_factory=dict)

    def get(self, *path, default=None):
        node: Any = self.raw
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def section(self, name: str) -> dict[str, Any]:
        merged = dict(DEFAULTS.get(name, {}))
        merged.update(self.raw.get(name, {}) or {})
        return merged

    def account(self, provider: str, name: str) -> Account:
        table = (self.raw.get("accounts", {}) or {}).get(provider, {}) or {}
        entry = table.get(name)
        if entry is None:
            # Fall back to the single configured account, then to env vars.
            if len(table) == 1:
                name, entry = next(iter(table.items()))
            else:
                entry = {}
        key = entry.get("api_key", "")
        if not key and entry.get("api_key_env"):
            key = os.environ.get(entry["api_key_env"], "")
        if not key:
            env = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
            key = os.environ.get(env, "")
        # base_url is only ever taken from this file. Inheriting a proxy from the
        # shell (ANTHROPIC_BASE_URL etc.) would silently send keys somewhere else.
        base = entry.get("base_url", "")
        return Account(name=name, api_key=key, base_url=base, label=entry.get("label", ""))

    def accounts(self, provider: str) -> dict[str, Any]:
        return (self.raw.get("accounts", {}) or {}).get(provider, {}) or {}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> Config:
    raw: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        raw = tomllib.loads(CONFIG_PATH.read_text())
    return Config(raw=_deep_merge(DEFAULTS, raw))


def ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def has_tmux() -> bool:
    return shutil.which("tmux") is not None


def set_value(section: str, key: str, value) -> None:
    """Write one key into the user's config file, keeping everything else."""
    path = CONFIG_PATH
    text = path.read_text() if path.exists() else ""
    line = f'{key} = {json.dumps(value)}'
    head = f"[{section}]"
    if head in text:
        before, after = text.split(head, 1)
        body, *rest = after.split("\n[", 1)
        lines = body.split("\n")
        for i, ln in enumerate(lines):
            if ln.strip().startswith(key + " ") or ln.strip().startswith(key + "="):
                lines[i] = line
                break
        else:
            lines.insert(1, line)
        text = before + head + "\n".join(lines) + ("\n[" + rest[0] if rest else "")
    else:
        text = text.rstrip("\n") + f"\n\n{head}\n{line}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
