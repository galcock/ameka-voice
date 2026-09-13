"""Thin audio layer: OpenAI TTS out, mic + VAD in. Degrades to macOS built-ins."""
from __future__ import annotations

import io
import json
import re
import math
import os
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
import wave

from datetime import datetime

from . import engines
from .identity import delivery

TTS_RATE = 24000      # OpenAI pcm output is 24 kHz mono s16le
MIC_RATE = 16000
FLOOR_MIN = 0.006      # absolute floor: quiet rooms sit near 0.0002
FRAME_MS = 30

try:
    import numpy as np
    import sounddevice as sd
    HAVE_SD = True
except Exception:  # pragma: no cover - optional dependency
    np = None
    sd = None
    HAVE_SD = False


# ----------------------------------------------------------------- devices --
def find_device(substring: str, kind: str) -> int | None:
    if not HAVE_SD or not substring:
        return None
    want = substring.lower()
    for idx, dev in enumerate(sd.query_devices()):
        chans = dev["max_output_channels"] if kind == "output" else dev["max_input_channels"]
        if chans > 0 and want in dev["name"].lower():
            return idx
    return None


ROOM_MICS = ("macbook", "built-in", "imac", "mac mini", "mac studio",
             "studio display", "display audio")


def active_input_name(config) -> str:
    """The microphone actually in use, named or fallen back to."""
    if not HAVE_SD:
        return ""
    want = str(config.section("listen").get("input_device", "")).strip()
    if want:
        idx = find_device(want, "input")
        if idx is not None:
            try:
                return str(sd.query_devices(idx)["name"])
            except Exception:
                return want
    return default_input_name()


def on_room_mic(config) -> bool:
    """True when the microphone is in the room rather than on your head.

    A headset hears you and almost nothing else. A laptop microphone a foot
    from the laptop speakers hears Ameka herself at about the level a quiet
    remark arrives at, and she stops mid-sentence for her own voice.
    """
    name = active_input_name(config).lower()
    return not name or any(k in name for k in ROOM_MICS)


def device_report() -> list[str]:
    if not HAVE_SD:
        return ["sounddevice not installed"]
    out = []
    try:
        default_in, default_out = sd.default.device
    except Exception:
        default_in = default_out = None
    for idx, dev in enumerate(sd.query_devices()):
        tags = []
        if dev["max_input_channels"]:
            tags.append("in")
        if dev["max_output_channels"]:
            tags.append("out")
        mark = " *" if idx in (default_in, default_out) else ""
        out.append(f"  [{idx}] {dev['name']} ({'/'.join(tags)}){mark}")
    return out


# --------------------------------------------------------------------- TTS --
def _audio_model_speech(acct, cfg, text: str) -> bytes | None:
    """The Advanced Voice Mode family: speech straight out of the audio model.

    gpt-4o-mini-tts reads text aloud. These models generate speech natively,
    which is where the intonation and timing come from — a question actually
    sounds like one.
    """
    import base64

    base = (acct.base_url or "https://api.openai.com").rstrip("/")
    body = {
        "model": cfg.get("audio_model", "gpt-audio"),
        "modalities": ["text", "audio"],
        "audio": {"voice": cfg.get("voice", "marin"), "format": "pcm16"},
        # The audio model is a conversationalist, not a reader. Put the script
        # in the system slot and it reads it; put it in the user slot and it
        # answers it — it replied "sounds like everything is in good shape,
        # push it to production" instead of reading the line out.
        "messages": [
            {"role": "system", "content":
                "You are a speech synthesiser. Read the text below aloud, word for "
                "word. Begin with its first word. Never acknowledge, never preface, "
                "never add a word of your own. "
                + delivery(cfg.get("instructions", "") or "") + " TEXT: " + text},
            {"role": "user", "content": "Read it."},
        ],
    }
    request = urllib.request.Request(
        f"{base}/v1/chat/completions", data=json.dumps(body).encode(),
        headers={"content-type": "application/json",
                 "authorization": f"Bearer {acct.api_key}"}, method="POST")
    # A sentence comes back in a second or two. Waiting a minute for it is what
    # the ceiling then interrupts, so this gives up early and falls to Kokoro.
    with urllib.request.urlopen(request, timeout=12) as response:
        payload = json.loads(response.read().decode())
    encoded = ((payload.get("choices") or [{}])[0].get("message") or {}).get("audio") or {}
    data = encoded.get("data")
    if not data:
        return None
    # If it answered instead of reading, throw it away rather than say something
    # Ameka never wrote.
    said = (encoded.get("transcript") or "").strip().lower()
    wanted = text.strip().lower()
    if said and not _reads_faithfully(said, wanted):
        return None
    return base64.b64decode(data)


def _reads_faithfully(said: str, wanted: str) -> bool:
    """It must start where the line starts.

    Checking overlap alone let "Understood. I'll start now." through as a
    preface, because every word of the real line was still in there afterwards.
    """
    trim = lambda t: re.sub(r"[^a-z0-9 ]", " ", t).split()
    said_words, want_words = trim(said), trim(wanted)
    if not want_words:
        return True
    opening = want_words[: min(4, len(want_words))]
    if said_words[: len(opening)] != opening:
        return False
    overlap = len(set(said_words) & set(want_words)) / len(set(want_words))
    length_ok = 0.7 <= (len(said_words) / len(want_words)) <= 1.4
    return overlap >= 0.8 and length_ok


def _tts_request(acct, cfg, text: str, fmt: str):
    base = (acct.base_url or "https://api.openai.com").rstrip("/")
    body = {
        "model": cfg["model"],
        "voice": cfg["voice"],
        "input": text,
        "response_format": fmt,
        "speed": float(cfg.get("speed", 1.0)),
    }
    if "tts" in cfg["model"] and cfg["model"] != "tts-1":
        body["instructions"] = delivery(cfg.get("instructions", "") or "")
    req = urllib.request.Request(
        f"{base}/v1/audio/speech",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "authorization": f"Bearer {acct.api_key}"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=12)


def say_system(text: str, voice: str = "Daniel") -> None:
    # The voice the OS ships with: `say` on a Mac, SAPI on Windows.
    host.speak_system(text, voice if host.IS_MAC else "")


_KOKORO = None
_KOKORO_KEY = None


def kokoro_voice(cfg, device):
    """One loaded model for the life of the process — reloading costs a second."""
    global _KOKORO, _KOKORO_KEY
    key = (cfg.get("kokoro_voice", "am_michael"), float(cfg.get("speed", 1.0)), device)
    if _KOKORO is None or _KOKORO_KEY != key:
        _KOKORO = engines.KokoroVoice(voice=key[0], speed=key[1], device=device)
        _KOKORO_KEY = key
    return _KOKORO


def speak(config, text: str, should_stop=None) -> str:
    """Speak text. Returns the engine actually used, or "interrupted"."""
    cfg = config.section("voice")
    device = find_device(cfg.get("output_device", ""), "output")
    engine = cfg.get("engine", "auto")

    # Local neural voice first: natural, instant, free.
    if engine in ("auto", "kokoro") and HAVE_SD and engines.kokoro_available():
        try:
            stopped = kokoro_voice(cfg, device).speak(
                text, cfg.get("pronounce"), should_stop=should_stop)
            return "interrupted" if stopped else "kokoro"
        except Exception:
            pass

    acct = config.account("openai", cfg.get("account", "default"))
    if engine == "say" or not acct.usable or not cloud_ok():
        return _fallback_voice(config, cfg, text, device)

    if engine in ("auto", "openai-audio") and HAVE_SD:
        try:
            pcm = _audio_model_speech(acct, cfg, engines.respell(text, cfg.get("pronounce")))
            if pcm:
                samples = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
                stream = sd.OutputStream(samplerate=24000, channels=1,
                                         dtype="float32", device=device)
                interrupted = False
                with engines.active(stream), stream:       # registered through the close
                    block = 2400                      # a tenth of a second
                    for start in range(0, samples.size, block):
                        if should_stop is not None and should_stop():
                            interrupted = True
                            break
                        stream.write(samples[start:start + block])
                    if interrupted:
                        try:
                            stream.abort()
                        except Exception:
                            pass
                return "interrupted" if interrupted else "openai-audio"
        except Exception as exc:
            cloud_failed(repr(exc))
            return _fallback_voice(config, cfg, text, device)

    if HAVE_SD:
        try:
            resp = _tts_request(acct, cfg, engines.respell(text, cfg.get("pronounce")), "pcm")
            stream = sd.RawOutputStream(
                samplerate=TTS_RATE, channels=1, dtype="int16", device=device, blocksize=0
            )
            interrupted = False
            with engines.active(stream), stream, resp:
                buf = b""
                while True:
                    if should_stop is not None and should_stop():
                        interrupted = True
                        break
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    if len(buf) % 2:                 # keep frames whole
                        stream.write(buf[:-1])
                        buf = buf[-1:]
                    else:
                        stream.write(buf)
                        buf = b""
                if interrupted:
                    try:
                        stream.abort()
                    except Exception:
                        pass
                else:
                    sd.sleep(120)
            return "interrupted" if interrupted else "openai-tts"
        except Exception as exc:
            cloud_failed(repr(exc))
            return _fallback_voice(config, cfg, text, device)

    if not host.IS_MAC:                          # afplay is a Mac tool
        return _fallback_voice(config, cfg, text, device)
    try:
        with _tts_request(acct, cfg, engines.respell(text, cfg.get("pronounce")), "mp3") as resp:
            path = os.path.join(tempfile.gettempdir(), f"ameka-{uuid.uuid4().hex}.mp3")
            with open(path, "wb") as fh:
                fh.write(resp.read())
        subprocess.run(["afplay", path], check=False, timeout=90)
        os.unlink(path)
        return "openai-tts/afplay"
    except Exception:
        # Out of credit, offline, rate limited — whatever it was, the neural
        # voice on this Mac beats the 1990s one, so fall to that first.
        return _fallback_voice(config, cfg, text, device)


def _fallback_voice(config, cfg, text: str, device) -> str:
    """Best voice still available: the local neural one, then macOS."""
    if HAVE_SD and engines.kokoro_available():
        try:
            kokoro_voice(cfg, device).speak(text, cfg.get("pronounce"))
            return "kokoro"
        except Exception:
            pass
    say_system(engines.respell(text, cfg.get("pronounce")),
               cfg.get("fallback_say_voice") or engines.best_say_voice())
    return "macos-say"


def earcon(rising: bool = True) -> None:
    """Short blip so you know the mic just opened (or closed)."""
    if not HAVE_SD:
        return
    try:
        rate, dur = 24000, 0.09
        t = np.linspace(0, dur, int(rate * dur), endpoint=False)
        f0, f1 = (660, 990) if rising else (990, 660)
        freq = np.linspace(f0, f1, t.size)
        env = np.minimum(1.0, np.minimum(t / 0.012, (dur - t) / 0.03))
        tone = (0.18 * env * np.sin(2 * math.pi * freq * t)).astype("float32")
        sd.play(tone, rate, blocking=True)
    except Exception:
        pass


# --------------------------------------------------------------- listening --
_STREAM = None
_STREAM_KEY = None
_SILENT_RUNS = 0          # consecutive captures that were digital silence
_LAST_INPUT_NAME = None   # so a Bluetooth handoff is noticed
_STREAM_OPENED = 0.0      # a Bluetooth stream can go stale without going quiet
_STREAM_RATE = MIC_RATE   # the microphone's own rate, converted afterwards
_LAST_PEAK = 0.0          # how loud the last captured utterance was
_LAST_STT = ""            # which recogniser produced the last transcript

# When a cloud call stalls or fails, nothing else asks the cloud for a minute.
# The voice hung on a flaky connection for forty seconds, the ceiling fired,
# she restarted and the sentence was lost — and the next one did the same,
# because every path tried the cloud first and waited its full timeout before
# falling to the voice and the ears she has on the Mac. Now the first failure
# is remembered and the rest go local at once, until the minute is up.
_CLOUD_DOWN_UNTIL = 0.0
CLOUD_BACKOFF = 60.0


def cloud_ok() -> bool:
    return time.time() >= _CLOUD_DOWN_UNTIL


def cloud_failed(why: str = "") -> None:
    global _CLOUD_DOWN_UNTIL
    if cloud_ok():
        print(f"{datetime.now():%H:%M:%S} cloud unreachable ({why[:60]}) — local voice and ears "
              f"for the next {int(CLOUD_BACKOFF)}s", flush=True)
    _CLOUD_DOWN_UNTIL = time.time() + CLOUD_BACKOFF
_HERS_UNTIL = 0.0         # audio buffered before this is Ameka's own voice
_LAST_SPEECH_SECONDS = 0.0  # of that capture, how much was speech
_DEAD_DEVICE = None       # a microphone that is listed but delivers nothing
_WORKING_DEVICE = None    # what we moved to instead


def default_input_name() -> str:
    """Name of the device macOS is currently routing input from."""
    if not HAVE_SD:
        return ""
    try:
        return str(sd.query_devices(kind="input")["name"])
    except Exception:
        return ""


class BargeIn:
    """Watch for the user talking over Ameka, and say when to stop.

    The bar is deliberately high. Ameka's own voice leaks back through the
    headphones, so the floor is measured while it is already speaking and a real
    interruption has to be several times louder than that leakage — otherwise it
    would talk itself into silence.
    """

    def __init__(self, config):
        cfg = config.section("listen")
        self.device = find_device(cfg.get("input_device", ""), "input")
        self.over = float(cfg.get("barge_over_leakage", 3.0))
        self.floor_min = float(cfg.get("barge_floor", 0.02))
        self.needed = max(3, int(float(cfg.get("barge_seconds", 0.25)) / 0.03))
        # On a headset the only thing that clears the bar is you. On the laptop
        # microphone her own voice clears it too — measured at 0.0055 against a
        # bar of 0.0040 — so she spent an afternoon interrupting herself. In the
        # room the bar goes up and has to be held longer: you speak up to cut in.
        self.room = on_room_mic(config)
        if self.room:
            self.over = float(cfg.get("barge_over_room", 6.0))
            self.floor_min = float(cfg.get("barge_floor_room", 0.03))
            self.needed = max(6, int(float(cfg.get("barge_seconds_room", 0.3)) / 0.03))
        self.settle = float(cfg.get("barge_settle_seconds", 1.2))
        # However loud the room gets, speech never has to clear more than this.
        self.ceiling = float(cfg.get("barge_ceiling", 0.05))
        self._stop = threading.Event()
        self._hit = threading.Event()
        self._thread = None
        self.captured = b""        # what you had already said when it stopped
        self.rate = MIC_RATE
        self.peak = 0.0
        self.last_threshold = 0.0
        self.frames = 0
        self._own = None           # the live stream, handed on rather than closed
        self.everything: list[bytes] = []   # all of it, for the echo check

    def triggered(self) -> bool:
        return self._hit.is_set()

    def missed_speech(self) -> bytes:
        """What the room said while she was talking, if it was loud enough to be
        anything at all.

        This used to return nothing, because almost everything a microphone
        hears while she is speaking is her: she read a list of commands aloud,
        recorded herself, and sent it back as an instruction. But throwing it
        all away loses real sentences — spoken over her at a volume that does
        not clear the interrupt bar, which on a laptop microphone is most of
        them.

        So it comes back, and the caller settles who said it by transcribing it
        and comparing against the words she just spoke. She knows exactly what
        she said, which is a far better test than how loud it was.
        """
        heard = b"".join(self.everything)
        if not heard:
            return b""
        try:
            loudest = float(np.abs(np.frombuffer(heard, dtype="int16")).max()) / 32768.0
        except Exception:
            return b""
        return heard if loudest > 0.012 else b""

    def _run(self) -> None:
        # A stream of its own. Two threads reading one PortAudio stream is
        # undefined, and it took the whole daemon down with it.
        own = None
        try:
            rate = native_rate(self.device)
            own = sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                                 blocksize=0, device=self.device)
            engines._ACTIVE.append(own)          # so a hang elsewhere can free the mic
            own.start()
            stream = own
            frame = int(rate * 0.03)
            self.rate = rate
            floor, run, seen = None, 0, 0
            recent: list[bytes] = []
            kept: list[bytes] = []
            cap = int(30 / 0.03)          # a sentence, not a filibuster
            settle = int(self.settle / 0.03)
            while not self._stop.is_set():
                block, _over = stream.read(frame)
                raw = bytes(block)
                level = _rms(raw)
                seen += 1
                self.frames = seen
                if len(self.everything) < cap:
                    self.everything.append(raw)
                if self._hit.is_set():
                    # Still your sentence. It used to stop recording the instant
                    # it noticed you, which threw away every word said between
                    # "she noticed" and "the microphone came back" — most of a
                    # second, and the first thing you actually wanted to say.
                    if len(kept) < cap:
                        kept.append(raw)
                        self.captured = b"".join(kept)
                    continue
                recent.append(raw)
                if len(recent) > 24:          # keep about 0.7s of history
                    recent.pop(0)

                # A noise floor must follow the quiet, not the loud. The first
                # version rose with whatever it heard, so talking louder raised
                # the bar you were trying to clear: measured at peak 0.186
                # against a threshold of 0.191. It falls fast and rises slowly.
                if floor is None:
                    floor = level
                elif level < floor:
                    floor = floor * 0.85 + level * 0.15
                else:
                    floor = floor * 0.995 + level * 0.005
                if seen <= settle:            # playback has not ramped up yet
                    continue
                threshold = min(max(floor * self.over, self.floor_min), self.ceiling)
                self.peak = max(getattr(self, "peak", 0.0), level)
                self.last_threshold = threshold
                run = run + 1 if level > threshold else 0
                if run >= self.needed:
                    # What you had already said when it noticed. The loop above
                    # carries on from here until the microphone is handed back.
                    kept = list(recent)
                    self.captured = b"".join(kept)
                    self._hit.set()
        except Exception:
            return
        finally:
            self._own = own

    def __enter__(self):
        # Hand the microphone over. A second input stream opened alongside the
        # main one gets a fraction of the signal on AirPods: your voice measured
        # 0.026 on a stream of its own and 0.0012 alongside another, which is
        # why interrupting never worked. Nothing else is reading it while she
        # speaks, and it hands the same stream straight back afterwards.
        close_input()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                # Wedged in a read. Two threads on one stream is what took the
                # whole daemon down, so it does not get shared — start over.
                self._own = None
                reset_audio()
                return False
        stream, self._own = self._own, None
        if stream is None:
            # It never got a stream of its own, so put the audio system back on
            # its feet: reopening a half-built one leaves the device returning
            # nothing, which looks exactly like a crash from the outside.
            reset_audio()
            return False
        # Otherwise pass the running stream to whoever listens next. Opening a
        # fresh one on a Bluetooth headset costs the better part of a second and
        # records nothing while it happens, and that second is the one holding
        # the start of whatever you interrupted her to say.
        adopt_input(stream, self.device, self.rate)
        return False


def probe_input(device, seconds: float = 0.25) -> float:
    """Loudest sample this device gives us. Zero means it is not delivering."""
    try:
        rate = native_rate(device)
        with sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                            device=device) as stream:
            time.sleep(0.06)
            frames = []
            for _ in range(max(1, int(seconds / 0.03))):
                block, _over = stream.read(int(rate * 0.03))
                frames.append(np.frombuffer(bytes(block), dtype="int16"))
        samples = np.concatenate(frames).astype("float32") / 32768.0
        return float(np.abs(samples).max())
    except Exception:
        return 0.0


def stream_variety(device, seconds: float = 0.4) -> int:
    """How many distinct sample values a microphone produces.

    A live microphone in a silent room still dithers across several values; a
    dead stream repeats one. Level alone cannot tell them apart — a dead AirPod
    read higher than a live room did.
    """
    if not HAVE_SD:
        return 0
    try:
        rate = native_rate(device)
        with sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                            device=device) as stream:
            time.sleep(0.08)
            frames = []
            for _ in range(max(1, int(seconds / 0.03))):
                block, _over = stream.read(int(rate * 0.03))
                frames.append(np.frombuffer(bytes(block), dtype="int16"))
        return int(np.unique(np.concatenate(frames)).size)
    except Exception:
        return 0


def live_input(preferred: str = "") -> int | None:
    """A microphone that is actually delivering audio.

    AirPods stay listed as the input long after they hand their microphone to a
    phone: connected, chosen, and silent. Rather than trust the label, every
    candidate is measured and a dead one is passed over.
    """
    if not HAVE_SD:
        return None
    candidates: list[int | None] = []
    if preferred:
        match = find_device(preferred, "input")
        if match is not None:
            candidates.append(match)
    candidates.append(None)                       # the system default
    try:
        for index, info in enumerate(sd.query_devices()):
            if info["max_input_channels"] > 0 and index not in candidates:
                candidates.append(index)
    except Exception:
        pass

    # A quiet room and a dead microphone look almost identical for a quarter of
    # a second, so the bar is only "not digital zero", and the device you asked
    # for is tried first and kept unless it is truly delivering nothing.
    best, best_level = None, 0.0
    for candidate in candidates:
        level = probe_input(candidate)
        if level > 2.5e-4:
            return candidate
        if level > best_level:
            best, best_level = candidate, level
    return best


def reset_audio() -> None:
    """Re-enumerate devices. PortAudio caches the device list when it starts,
    so an AirPod that leaves for a phone and comes back is invisible until the
    host API is torn down and brought back up."""
    global _SILENT_RUNS
    close_input()
    _SILENT_RUNS = 0
    try:
        sd._terminate()
        sd._initialize()
        print(f"{datetime.now():%H:%M:%S} audio reset — listening on "
              f"{default_input_name() or 'the default device'}", flush=True)
    except Exception:
        pass


def native_rate(device) -> int:
    """What this microphone actually runs at.

    AirPods hand back digital silence when asked for 16 kHz and real audio at
    their own 24 kHz: the resampler in the middle produces nothing and reports
    no error. So take whatever the device offers and convert afterwards.
    """
    try:
        info = sd.query_devices(device if device is not None else sd.default.device[0])
        rate = int(info.get("default_samplerate") or MIC_RATE)
        return rate if 8000 <= rate <= 96000 else MIC_RATE
    except Exception:
        return MIC_RATE


def _input_stream(device):
    """One long-lived mic stream, at the microphone's own sample rate."""
    global _STREAM, _STREAM_KEY, _STREAM_RATE, _STREAM_OPENED
    if _STREAM is not None and _STREAM_KEY == device:
        return _STREAM
    close_input()
    rate = native_rate(device)
    try:
        _STREAM = sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                                 blocksize=0, device=device)
        _STREAM.start()
    except Exception:
        rate = MIC_RATE
        _STREAM = sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                                 blocksize=0, device=device)
        _STREAM.start()
    _STREAM_KEY = device
    _STREAM_RATE = rate
    _STREAM_OPENED = time.time()
    return _STREAM


def resample(pcm: bytes, source: int, target: int = MIC_RATE) -> bytes:
    """Down to 16 kHz, which is all whisper wants — without aliasing.

    Dropping samples to go from 48 kHz to 16 kHz folds everything above 8 kHz
    back down on top of the speech. It is not audible as distortion so much as
    a recogniser that quietly loses words: six words came back from seven
    seconds of talking. Averaging over the decimation window first is a crude
    low-pass, but it removes the fold-down that was doing the damage.
    """
    if source == target or not pcm:
        return pcm
    samples = np.frombuffer(pcm, dtype="int16").astype("float32")
    if samples.size < 4:
        return pcm

    ratio = source / target
    width = max(1, int(round(ratio)))
    if width > 1:
        kernel = np.ones(width, dtype="float32") / width
        samples = np.convolve(samples, kernel, mode="same")

    count = int(samples.size / ratio)
    if count < 2:
        return pcm
    resampled = np.interp(
        np.linspace(0, samples.size - 1, count, dtype="float64"),
        np.arange(samples.size, dtype="float64"),
        samples,
    )
    return np.clip(resampled, -32768, 32767).astype("int16").tobytes()


def adopt_input(stream, device, rate: int) -> None:
    """Take over an already-running stream as the one everything reads from.

    Two streams on one microphone is the bug that made interrupting useless, so
    only one is ever open. This is how it changes hands without closing.
    """
    global _STREAM, _STREAM_KEY, _STREAM_RATE, _STREAM_OPENED, _HERS_UNTIL
    if _STREAM is not stream:
        close_input()
    _STREAM, _STREAM_KEY, _STREAM_RATE = stream, device, rate
    _STREAM_OPENED = time.time()
    # Her voice does not stop when the audio does. On speakers it is still
    # leaving the room for a moment afterwards, and that moment belongs to her,
    # not to him.
    _HERS_UNTIL = _STREAM_OPENED + 0.45


def close_input() -> None:
    global _STREAM, _STREAM_KEY
    if _STREAM is not None:
        try:
            _STREAM.stop()
            _STREAM.close()
        except Exception:
            pass
    _STREAM, _STREAM_KEY = None, None


def _rms(block) -> float:
    mono = np.frombuffer(block, dtype="int16").astype("float32") / 32768.0
    return float(np.sqrt(np.mean(mono ** 2)) + 1e-9)


def lift(pcm: bytes, target: float = 0.30, most: float = 14.0) -> bytes:
    """Bring a quiet capture up to something a recogniser can work with.

    His voice arrives at the laptop microphone around a hundredth of full
    scale, a tenth of what a headset gives. Recognisers are poor at that: three
    seconds of perfectly clear speech came back as no words at all, from two
    different recognisers, because there was barely a signal to work on.

    Scaling it up is not cleverness, it is turning the volume up on something
    already recorded. The ceiling matters — without one, a silent room is
    amplified into a roar and every rustle becomes a candidate for speech.
    """
    if not pcm:
        return pcm
    try:
        samples = np.frombuffer(pcm, dtype="int16").astype("float32")
        peak = float(np.abs(samples).max()) / 32768.0
        if peak <= 1e-4 or peak >= target:
            return pcm
        gain = min(target / peak, most)
        lifted = np.clip(samples * gain, -32768, 32767).astype("int16")
        return lifted.tobytes()
    except Exception:
        return pcm


def record_until_silence(config, wait_seconds: float | None = None,
                        prefix: bytes = b"", prefix_rate: int = 0) -> bytes:
    """Wait for you to start talking, then capture until you stop.

    Returns raw 16 kHz mono PCM, or b"" if nothing was said inside the window.
    """
    cfg = config.section("listen")
    if not HAVE_SD:
        return _record_ffmpeg(float(cfg["window_seconds"]))

    global _SILENT_RUNS, _LAST_INPUT_NAME

    # A Bluetooth handoff to a phone and back leaves the open stream alive but
    # mute, so watch the device macOS is actually using.
    current_input = default_input_name()
    if _LAST_INPUT_NAME is not None and current_input != _LAST_INPUT_NAME:
        reset_audio()
    _LAST_INPUT_NAME = current_input

    # Stale from the last capture otherwise. The interrupted path used to
    # return without touching it, so a two-second capture was reported as
    # holding four seconds of speech, and the check for whether she had lost
    # words fired on arithmetic rather than on anything that happened.
    globals()["_LAST_SPEECH_SECONDS"] = 0.0
    device = find_device(cfg.get("input_device", ""), "input")
    if _DEAD_DEVICE is not None and device == _DEAD_DEVICE:
        device = _WORKING_DEVICE
    per_second = 1000 / FRAME_MS
    # A microphone across the desk hears the room, and the room never stops.
    # Settings that suit a headset let a capture run for seventy-eight seconds
    # to catch half a second of speech, and he waits a minute for an answer. She
    # works out which microphone she is on and listens accordingly, rather than
    # being retuned by hand every time the AirPods wander off to his phone.
    room = on_room_mic(config)
    window = float(cfg["window_seconds"])
    if room:
        window = min(window, float(cfg.get("room_window_seconds", 25)))
    # Nobody says one sentence for a minute. Whatever the settings say.
    max_frames = int(min(window, 45.0) * per_second)
    # One rule: you have stopped when you have been quiet this long. Three
    # thresholds that varied with how much you had already said were impossible
    # to predict and cut people off mid-sentence.
    silence_frames = int(float(cfg["silence_seconds"]) * per_second)
    short_utterance = 0
    min_speech = max(2, int(float(cfg["min_speech_seconds"]) * per_second))
    onset_frames = max(1, int(float(cfg.get("onset_seconds", 0.045)) * 1000 / FRAME_MS))
    # Waiting for you to start and capturing what you say are different jobs.
    # Tying both to the 90-second utterance cap meant a dead stream took minutes
    # to notice, because one silent wait lasted a minute and a half.
    idle_wait = float(cfg.get("idle_wait_seconds", 12))
    wait_frames = int((wait_seconds if wait_seconds is not None else idle_wait) * per_second)
    # Everything before speech is detected is only kept if it is still in this
    # buffer. At 240ms the first word or two of a sentence was being lost, which
    # reads as a thought starting in the middle. Two thirds of a second costs
    # nothing and covers the delay through a Bluetooth headset as well.
    preroll_len = int(float(cfg.get("preroll_seconds", 0.66)) * 1000 / FRAME_MS)

    try:
        stream = _input_stream(device)
    except Exception:
        return _record_ffmpeg(float(cfg["window_seconds"]))
    rate = _STREAM_RATE
    frame = int(rate * FRAME_MS / 1000)

    # What is waiting in this buffer is her voice up to the moment she stopped,
    # and yours after it. Dropping the lot was throwing away the first words of
    # every sentence you began while she was still transcribing the last one —
    # two or three seconds where the microphone is unattended but not deaf.
    #
    # So only her half goes. Everything recorded since she finished is yours.
    carried = b""
    if not prefix:
        yours = min(max(0.0, time.time() - _HERS_UNTIL), 6.0)
        keep_bytes = int(yours * rate) * 2
        try:
            held = []
            stale = stream.read_available
            while stale >= frame:
                block, _over = stream.read(min(stale, frame * 16))
                held.append(bytes(block))
                stale = stream.read_available
            if keep_bytes:
                carried = b"".join(held)[-keep_bytes:]
        except Exception:
            carried = b""

    floor = None
    preroll: list[bytes] = []
    voice = 0.0
    waited = 0
    carry_frames = int(1.5 * 1000 / FRAME_MS)
    loud_run = 0
    seen_peak = 0.0

    # Already talking, and we have the start of it: skip waiting and keep it.
    if prefix:
        if prefix_rate and prefix_rate != rate:
            prefix = resample(prefix, prefix_rate, rate)
        collected = [prefix]
        # Interrupting her means you are part way through a sentence, so use the
        # patient end-of-speech rule rather than the one meant for one-word
        # commands.
        speech = max(min_speech, short_utterance + 1)
        quiet = 0
        threshold = FLOOR_MIN
        for _ in range(max_frames):
            try:
                block, _over = stream.read(frame)
            except Exception:
                break
            raw = bytes(block)
            collected.append(raw)
            if _rms(raw) > threshold:
                speech += 1
                quiet = 0
            else:
                quiet += 1
                if quiet >= silence_frames:
                    break
        captured = b"".join(collected)
        seconds = len(captured) / 2 / max(rate, 1)
        globals()["_LAST_SPEECH_SECONDS"] = min(
            speech * FRAME_MS / 1000.0, seconds)
        try:
            globals()["_LAST_PEAK"] = float(
                np.abs(np.frombuffer(captured, dtype="int16")).max()) / 32768.0
        except Exception:
            pass
        return resample(lift(captured), rate)

    # ---- phase one: has he started talking? --------------------------------
    for _ in range(wait_frames):
        try:
            block, _over = stream.read(frame)
        except Exception:
            reset_audio()
            return b""
        raw = bytes(block)
        waited += 1
        level = _rms(raw)
        seen_peak = max(seen_peak, level)
        floor = level if floor is None else (floor * 0.92 + level * 0.08)
        threshold = max(floor * 3.5, FLOOR_MIN)

        preroll.append(raw)
        if len(preroll) > preroll_len:
            preroll.pop(0)

        if level > threshold:
            loud_run += 1
            voice = max(voice, level)     # how loud he is, not just that he is
        else:
            loud_run = 0
        if loud_run >= onset_frames:
            break
    else:
        # Real rooms are never perfectly silent. A run of pure digital zero
        # means the stream is dead, not that nobody spoke.
        if seen_peak < 2e-4:
            _SILENT_RUNS += 1
            if _SILENT_RUNS >= 2:
                globals()["_DEAD_DEVICE"] = device
                reset_audio()
                replacement = live_input(cfg.get("input_device", ""))
                if replacement != device:
                    globals()["_WORKING_DEVICE"] = replacement
                    try:
                        name = sd.query_devices(replacement)["name"] if replacement is not None \
                            else default_input_name()
                    except Exception:
                        name = "another microphone"
                    print(f"{datetime.now():%H:%M:%S} that microphone went quiet — "
                          f"listening on {name}", flush=True)
        else:
            _SILENT_RUNS = 0
            # A Bluetooth stream can keep delivering room noise while no longer
            # carrying the voice, which no silence check will ever catch. So an
            # idle stream is simply retired: reopening costs a third of a second
            # and removes the whole class of fault.
            max_age = float(cfg.get("stream_max_idle_seconds", 90))
            if max_age > 0 and _STREAM_OPENED and time.time() - _STREAM_OPENED > max_age:
                close_input()
        return b""                                     # nobody said anything

    # ---- phase two: capture until he stops ---------------------------------
    # What was carried over belongs to this sentence only if the sentence began
    # straight away. Left any longer it is just an old room, not your opening.
    collected = ([carried] if carried and waited <= carry_frames else []) + list(preroll)
    speech = loud_run
    quiet = 0
    # How far above the room a sound has to be before it counts as still
    # talking. Set from a multiple of the room alone it is a guess about how
    # loud he is, and the guess was wrong in both directions on the same day:
    # too low and the room kept a sentence open for seventy-eight seconds, too
    # high and he was cut off after a fifth of a second, mid-word, seven
    # captures running.
    #
    # It is not a guess now. Phase one measured him, so the bar sits between
    # the room and his actual voice: comfortably above the room, well under the
    # quiet parts of his own speech.
    room_floor = (floor or 0.002)
    over = float(cfg.get("room_speech_over", 5.0)) if room else 3.0
    threshold = max(room_floor * (2.2 if room else 1.8), FLOOR_MIN * 0.7)
    if voice > threshold * 2:
        threshold = max(room_floor * 2.2, min(voice * 0.28, room_floor * over))
    voice_level = 0.0

    for _ in range(max_frames):
        try:
            block, _over = stream.read(frame)
        except Exception:
            break
        raw = bytes(block)
        collected.append(raw)
        level = _rms(raw)

        # Silence is judged against how loudly you were speaking, not against a
        # fixed number. A fixed floor never falls quiet in a noisy room: one
        # nine-word sentence was recorded for thirty-one seconds.
        bar = max(threshold, voice_level * 0.22) if voice_level else threshold
        if level > bar:
            speech += 1
            quiet = 0
            voice_level = level if not voice_level else voice_level * 0.9 + level * 0.1
        else:
            quiet += 1
            if speech >= min_speech and quiet >= silence_frames:
                break

    if speech < min_speech:
        return b""
    captured = b"".join(collected)
    try:
        globals()["_LAST_PEAK"] = float(
            np.abs(np.frombuffer(captured, dtype="int16")).max()) / 32768.0
        # How much of that was actually talking, rather than the pause at the
        # end. Reporting total length made deliberate speech look like lost
        # words and sent me hunting a fault that was not there.
        kept = len(captured) / 2 / max(rate, 1)
        globals()["_LAST_SPEECH_SECONDS"] = min(speech * FRAME_MS / 1000.0, kept)
    except Exception:
        pass
    # Measured first, lifted second: the log should say how loud he actually
    # was, and the recogniser should get something it can work with.
    return resample(lift(captured), rate)


def _record_ffmpeg(seconds: float) -> bytes:
    """Fallback capture when sounddevice is unavailable."""
    path = os.path.join(tempfile.gettempdir(), f"ameka-{uuid.uuid4().hex}.wav")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "avfoundation",
           "-i", ":0", "-t", str(seconds), "-ac", "1", "-ar", str(MIC_RATE), "-y", path]
    try:
        subprocess.run(cmd, check=True, timeout=seconds + 10)
        with wave.open(path, "rb") as wf:
            return wf.readframes(wf.getnframes())
    except Exception:
        return b""
    finally:
        if os.path.exists(path):
            os.unlink(path)


def looks_like_speech(pcm: bytes, rate: int = MIC_RATE) -> tuple[bool, str]:
    """Reject coughs, nose-blowing, throat-clearing and keyboard clatter.

    The test is pitch, not loudness. Vowels are periodic — the vocal folds
    repeat a waveform 70 to 350 times a second, which shows up as a strong
    autocorrelation peak at that lag. Coughs and blown noise carry no such
    period however loud or long they are.
    """
    if not HAVE_SD or not pcm:
        return True, ""
    audio = np.frombuffer(pcm, dtype="int16").astype("float32") / 32768.0
    if audio.size < rate * 0.14:
        return False, "too short"

    frame = int(rate * 0.03)
    usable = (audio.size // frame) * frame
    frames = audio[:usable].reshape(-1, frame)
    energy = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
    peak = float(energy.max())
    loud = energy > max(peak * 0.15, 0.006)
    if loud.sum() < 4:                                    # under ~0.12s of sound
        return False, "too brief for speech"

    lo, hi = int(rate / 350), int(rate / 70)              # 350 Hz down to 70 Hz
    lags: list[int] = []
    tested = 0
    for block in frames[loud]:
        block = block - block.mean()
        norm = float((block ** 2).sum())
        if norm < 1e-7:
            continue
        tested += 1
        corr = np.correlate(block, block, mode="full")[frame - 1:]
        window = corr[lo:hi]
        if window.size and float(window.max()) / norm > 0.32:
            lags.append(lo + int(window.argmax()))
    if not tested:
        return False, "silent"

    # Real speech through a Bluetooth headset is far less uniformly voiced than
    # synthesised speech: consonants, breaths and the codec all break the period.
    # This gate only needs to catch broadband noise, which scores near zero —
    # low rumble is caught independently by the speech-band test below.
    ratio = len(lags) / tested
    if ratio < 0.15:
        return False, f"no pitch ({ratio:.2f} voiced)"

    # Blowing your nose into a microphone is a rumble: almost all of its energy
    # sits below 500 Hz, where it can autocorrelate like a low pitch. Speech
    # always carries formants and consonants through the 300-3400 Hz band, which
    # is exactly why telephones kept it and threw the rest away.
    voice_band = audio[: usable]
    spectrum = np.abs(np.fft.rfft(voice_band * np.hanning(voice_band.size))) ** 2
    freqs = np.fft.rfftfreq(voice_band.size, 1.0 / rate)
    total = float(spectrum[(freqs >= 50) & (freqs <= 7000)].sum()) + 1e-12
    speech_band = float(spectrum[(freqs >= 300) & (freqs <= 3400)].sum())
    share = speech_band / total
    # Measured: speech lands between 0.34 and 0.74 of its energy in this band,
    # blown noise between 0.10 and 0.17. The gap is wide, so sit in the middle.
    if share < 0.25:
        return False, f"no speech band ({share:.3f}, pitch {ratio:.2f})"
    return True, ""


def pcm_to_wav(pcm: bytes, rate: int = MIC_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _clean(text: str, glossary: dict) -> str:
    """The same scrutiny whichever recogniser produced it.

    The cloud path used to return its answer untouched, so nothing spelled
    Ameka properly and nothing caught a recogniser talking to itself.
    """
    text = engines.corrections.apply((text or "").strip(), glossary)
    if not text or text.strip().lower() in engines.HALLUCINATIONS:
        return ""
    return "" if engines.corrections.looks_hallucinated(text) else text


def transcribe(config, pcm: bytes) -> str:
    if not pcm:
        return ""
    cfg = config.section("listen")
    engine = cfg.get("engine", "auto")
    wav = pcm_to_wav(pcm)

    # whisper.cpp on this Mac: no key, no network, ~0.25s.
    if engine in ("auto", "whisper") and engines.whisper_available():
        glossary = config.section("glossary") if "glossary" in config.raw else {}
        text = engines.whisper_transcribe(
            wav, threads=int(cfg.get("threads", 4)),
            prompt=cfg.get("prompt") or engines.corrections.vocabulary(list(glossary.values())),
            glossary=glossary,
        )
        if text or engine == "whisper":
            globals()["_LAST_STT"] = "whisper.cpp"
            return text

    # The local recogniser found nothing in it. If there was barely any speech
    # in there either, there is nothing for the cloud to find, and uploading
    # forty seconds of a quiet room to be told so costs money for silence.
    # A second opinion from the cloud costs a call. On the laptop microphone a
    # quarter-second over the bar is a chair or a cough, and nine of those in
    # ten minutes went out and came back empty. It has to be sentence-length
    # before it is worth asking twice; whisper still hears a short "okay".
    floor = 0.9 if on_room_mic(config) else 0.6
    if _LAST_SPEECH_SECONDS and _LAST_SPEECH_SECONDS < floor:
        return ""

    # Off unless asked for: it is a paid call, and the whole app runs without one.
    if not cfg.get("cloud_second_opinion", False):
        return ""
    acct = config.account("openai", cfg.get("account", "default"))
    if not acct.usable or not cloud_ok():
        return ""
    glossary = config.section("glossary") if "glossary" in config.raw else {}

    boundary = f"----ameka{uuid.uuid4().hex}"
    parts: list[bytes] = []

    def field(name: str, value: str) -> None:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )

    for model in (cfg["stt_model"], "whisper-1"):
        parts = []
        field("model", model)
        field("language", "en")
        # A hint sheet, not a script. This used to be the command list alone,
        # and a recogniser given thin audio reads its hint sheet back to you —
        # "proceed, continue, run it, do it, stop" arriving as an instruction.
        field("prompt", engines.corrections.vocabulary(list(glossary.values())))
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="speech.wav"\r\n'
            f"Content-Type: audio/wav\r\n\r\n".encode()
        )
        parts.append(wav)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(parts)

        base = (acct.base_url or "https://api.openai.com").rstrip("/")
        req = urllib.request.Request(
            f"{base}/v1/audio/transcriptions",
            data=body,
            headers={
                "content-type": f"multipart/form-data; boundary={boundary}",
                "authorization": f"Bearer {acct.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                globals()["_LAST_STT"] = model
                text = _clean(json.loads(resp.read().decode()).get("text"), glossary)
                # Noise comes back as a web address now and then —
                # "https://fashionecstasy.com" from a chair — and she said
                # "got it". A link nobody spoke is nothing.
                if text and " " not in text.strip() and ("://" in text or text.count(".") >= 1):
                    return ""
                return text
        except Exception as exc:
            cloud_failed(repr(exc))
            return ""
    return ""
