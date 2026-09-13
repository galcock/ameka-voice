"""Local speech engines. No API keys, no network, no per-word cost.

TTS  : Kokoro-82M through onnxruntime — a real neural voice, ~1s for a sentence.
STT  : whisper.cpp — ~0.25s for a short command once the model is warm.
Both fall back to the macOS built-ins if the models are not installed.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

from . import corrections, host

MODELS = Path(os.path.expanduser("~/.ameka/models"))
KOKORO_MODEL = MODELS / "kokoro-v1.0.onnx"
KOKORO_VOICES = MODELS / "voices-v1.0.bin"
# Bigger models hear proper nouns the small one guesses at: base.en turns Codex
# into "codecs" and Claude into "cloud", small.en gets both right for about half
# a second more. The best model present wins.
WHISPER_MODELS = ("ggml-medium.en.bin", "ggml-small.en.bin", "ggml-base.en.bin")


def whisper_model() -> Path:
    for name in WHISPER_MODELS:
        candidate = MODELS / name
        if candidate.exists():
            return candidate
    return MODELS / WHISPER_MODELS[-1]


WHISPER_MODEL = MODELS / "ggml-base.en.bin"

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

# Words a neural voice reads wrong. Respelled the way they should sound.
PRONOUNCE = {
    "ameka": "Ah-meeka",
    "ameka.ai": "Ah-meeka dot A I",
    "tmux": "tee mux",
    "cwd": "working directory",
    "repo": "repo",
    "async": "a sink",
    "kokoro": "Kokoro",
}


SLASH = re.compile(r"(?<=\w)\s*/\s*(?=\w)")


def respell(text: str, extra: dict | None = None) -> str:
    # "ChatGPT/Claude" is read as one mangled word unless the slash becomes a word.
    text = SLASH.sub(" and ", text)
    table = dict(PRONOUNCE)
    table.update({k.lower(): v for k, v in (extra or {}).items()})
    def swap(match):
        return table.get(match.group(0).lower(), match.group(0))
    pattern = re.compile(r"\b(" + "|".join(sorted((re.escape(k) for k in table), key=len, reverse=True)) + r")\b", re.I)
    return pattern.sub(swap, text)

# whisper invents these out of silence; they are never a real command.
HALLUCINATIONS = {
    "", "you", "thank you.", "thanks for watching!", "thank you for watching.",
    "[blank_audio]", "(silence)", ".", "bye.", "so", "okay.", "[ silence ]",
    "thanks for watching.", "please subscribe", "subtitles by the amara.org community",
}


def kokoro_available() -> bool:
    if not (KOKORO_MODEL.exists() and KOKORO_VOICES.exists()):
        return False
    try:
        import kokoro_onnx  # noqa: F401
        return True
    except Exception:
        return False


EXTRA_BINS = ("/opt/homebrew/bin", "/usr/local/bin")


def find_bin(name: str) -> str:
    """shutil.which, plus the Homebrew paths launchd strips out of PATH."""
    found = shutil.which(name)
    if found:
        return found
    for folder in EXTRA_BINS:
        candidate = os.path.join(folder, name)
        if os.access(candidate, os.X_OK):
            return candidate
    return ""


def pywhisper_available() -> bool:
    """whisper.cpp through its Python binding — the same engine and the same
    model files, installed by pip. What a Windows machine uses, since the
    whisper-cli binary is a Homebrew thing."""
    try:
        import pywhispercpp  # noqa: F401
        return True
    except Exception:
        return False


def whisper_available() -> bool:
    return (bool(find_bin("whisper-cli")) or pywhisper_available()) and whisper_model().exists()


_PYWHISPER = None
_PYWHISPER_PATH = ""


def _pywhisper_transcribe(path: str, threads: int, prompt: str) -> str:
    global _PYWHISPER, _PYWHISPER_PATH
    from pywhispercpp.model import Model
    wanted = str(whisper_model())
    if _PYWHISPER is None or _PYWHISPER_PATH != wanted:
        _PYWHISPER = Model(wanted, n_threads=threads, print_realtime=False, print_progress=False)
        _PYWHISPER_PATH = wanted
    params = {"language": "en", "no_context": True}
    if prompt:
        params["initial_prompt"] = prompt
    try:
        segments = _PYWHISPER.transcribe(path, **params)
    except TypeError:                              # an older binding without those params
        segments = _PYWHISPER.transcribe(path)
    return " ".join(getattr(s, "text", str(s)) for s in segments)


# Every PortAudio stream that is open right now, in either direction. A write,
# a read, or a stop() on a device that has gone away can sit there forever — it
# did, for twenty hours, with the main loop waiting on it — and nothing inside
# that thread can get out. abort() from another thread does: unlike stop() it
# does not wait for the device to drain, which is the wait that never ends.
#
# A stream stays registered until it is closed, not merely until it has been
# written. The first version let go at the end of the write and the hang was in
# the close, so "0 streams aborted" and a thread still holding the microphone.
_ACTIVE: list = []


class active:
    """Register a stream for as long as it is open."""

    def __init__(self, stream):
        self.stream = stream

    def __enter__(self):
        _ACTIVE.append(self.stream)
        return self.stream

    def __exit__(self, *_):
        try:
            _ACTIVE.remove(self.stream)
        except ValueError:
            pass


def abort_streams() -> int:
    """Break every open stream, in and out. Safe from any thread. Returns how many."""
    n = 0
    for stream in list(_ACTIVE):
        try:
            stream.abort()
            n += 1
        except Exception:
            pass
    return n


class KokoroVoice:
    """Loads once, then speaks sentence by sentence so playback starts early."""

    def __init__(self, voice: str = "am_michael", speed: float = 1.0, device=None):
        self.voice, self.speed, self.device = voice, speed, device
        self._model = None
        self._lock = threading.Lock()

    def load(self):
        with self._lock:
            if self._model is None:
                from kokoro_onnx import Kokoro
                self._model = Kokoro(str(KOKORO_MODEL), str(KOKORO_VOICES))
        return self._model

    def voices(self) -> list[str]:
        return sorted(self.load().get_voices())

    def synth(self, text: str):
        return self.load().create(text, voice=self.voice, speed=self.speed, lang="en-us")

    def speak(self, text: str, extra_pronounce: dict | None = None,
              should_stop=None) -> bool:
        """Speak, checking every tenth of a second whether to shut up.

        Returns True if it was interrupted. A sentence is far too coarse a unit
        to stop on — by the time one ends you have been talked over for seconds.
        """
        import sounddevice as sd

        text = respell(text, extra_pronounce)
        chunks = [s for s in SENTENCE_SPLIT.split(text.strip()) if s.strip()] or [text]
        pipeline: queue.Queue = queue.Queue(maxsize=4)

        def produce():
            for chunk in chunks:
                try:
                    pipeline.put(self.synth(chunk))
                except Exception:
                    break
            pipeline.put(None)

        threading.Thread(target=produce, daemon=True).start()
        stream = None
        interrupted = False
        try:
            while True:
                item = pipeline.get()
                if item is None:
                    break
                audio, rate = item
                if stream is None:
                    stream = sd.OutputStream(samplerate=rate, channels=1,
                                             dtype="float32", device=self.device)
                    _ACTIVE.append(stream)
                    stream.start()
                block = max(1, int(rate * 0.1))
                samples = audio.astype("float32")
                for start in range(0, samples.size, block):
                    if should_stop is not None and should_stop():
                        interrupted = True
                        break
                    stream.write(samples[start:start + block])
                if interrupted:
                    break
        finally:
            # Still registered while it stops: stop() is the call that hangs.
            if stream is not None:
                try:
                    stream.abort() if interrupted else stream.stop()
                except Exception:
                    pass
                try:
                    _ACTIVE.remove(stream)
                except ValueError:
                    pass
                stream.close()
        return interrupted


SPECIAL_TOKEN = re.compile(r"^\[_.*_\]$")


def whisper_transcribe(wav_bytes: bytes, threads: int = 4, prompt: str = "",
                       min_confidence: float = 0.35, glossary: dict | None = None) -> str:
    """Run whisper.cpp over a 16 kHz mono WAV and return the text.

    The decoder's own token probabilities are the cheapest signal that it was
    guessing at a noise, so a low-confidence result is thrown away.
    """
    stem = os.path.join(tempfile.gettempdir(), f"ameka-{uuid.uuid4().hex}")
    path = stem + ".wav"
    with open(path, "wb") as fh:
        fh.write(wav_bytes)
    if not find_bin("whisper-cli") and pywhisper_available():
        try:
            text = " ".join(_pywhisper_transcribe(path, threads, prompt).split()).strip()
        except Exception:
            text = ""
        finally:
            if os.path.exists(path):
                os.unlink(path)
        text = re.sub(r"^\[.*?\]\s*", "", text)
        return corrections.apply(text, glossary) if text else ""
    cmd = [
        find_bin("whisper-cli") or "whisper-cli", "-m", str(whisper_model()), "-f", path,
        "--no-timestamps", "--no-prints", "-t", str(threads), "-l", "en",
        "--best-of", "1", "--beam-size", "1", "--output-json-full", "-of", stem,
        "--no-speech-thold", "0.5", "--entropy-thold", "2.6",
        "--max-context", "0",          # no carry-over, which is what feeds loops
    ]
    if prompt:
        cmd += ["--prompt", prompt]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return ""
    finally:
        if os.path.exists(path):
            os.unlink(path)

    text = " ".join(out.split()).strip()
    text = re.sub(r"^\[.*?\]\s*", "", text)
    text = corrections.apply(text, glossary)

    confidence = segment_confidence(stem + ".json")
    if os.path.exists(stem + ".json"):
        os.unlink(stem + ".json")
    if confidence is not None and confidence < min_confidence:
        return ""

    if text.strip().lower() in HALLUCINATIONS:
        return ""
    if corrections.looks_hallucinated(text):
        return ""
    return text


def segment_confidence(json_path: str) -> float | None:
    """Mean probability across real word tokens, ignoring whisper's markers."""
    try:
        with open(json_path) as fh:
            data = json.load(fh)
    except Exception:
        return None
    probs = []
    for segment in data.get("transcription") or []:
        for token in segment.get("tokens") or []:
            name = (token.get("text") or "").strip()
            if not name or SPECIAL_TOKEN.match(name):
                continue
            if isinstance(token.get("p"), (int, float)):
                probs.append(float(token["p"]))
    return sum(probs) / len(probs) if probs else None


def best_say_voice() -> str:
    """Pick the least robotic voice macOS actually has installed."""
    try:
        if not host.IS_MAC:
            return []
        listing = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
    except Exception:
        return "Samantha"
    names = []
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].startswith("#") is False:
            pass
        match = re.match(r"^(.+?)\s{2,}(\w\w_\w\w)", line)
        if match and match.group(2).startswith("en"):
            names.append(match.group(1).strip())
    for wanted in ("Ava (Premium)", "Zoe (Premium)", "Evan (Premium)", "Nathan (Premium)",
                   "Ava (Enhanced)", "Tom (Enhanced)", "Allison (Enhanced)", "Samantha",
                   "Daniel", "Karen"):
        if wanted in names:
            return wanted
    return names[0] if names else "Samantha"
