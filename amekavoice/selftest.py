"""Exercise every path and say plainly what works.

Written after a day of finding faults by talking to it: a broken microphone, a
threshold five times too high, a voice model that rewrote its lines, a Return
key that fired before the text arrived. Every one of those is a check here now,
so the next one is found by running this rather than by being misheard.
"""
from __future__ import annotations

import time

from . import audio, browser, codex_watch, commands, config as cfgmod, engines, route, tools
from .daemon import Brain
from .local_summary import briefing

TICK, CROSS, WARN = "✓", "✗", "!"


class Report:
    def __init__(self) -> None:
        self.failed = 0
        self.warned = 0

    def ok(self, area: str, detail: str = "") -> None:
        print(f"  {TICK} {area}" + (f" — {detail}" if detail else ""))

    def warn(self, area: str, detail: str) -> None:
        self.warned += 1
        print(f"  {WARN} {area} — {detail}")

    def fail(self, area: str, detail: str) -> None:
        self.failed += 1
        print(f"  {CROSS} {area} — {detail}")


def check_hearing(config, report: Report) -> None:
    if not engines.whisper_available():
        return report.fail("hearing", "whisper is missing — run: ameka setup-local")
    model = engines.whisper_model().name.replace("ggml-", "").replace(".bin", "")
    device = audio.live_input(config.section("listen").get("input_device", ""))
    name = audio.default_input_name() or "default microphone"
    rate = audio.native_rate(device)
    # A quiet room and a dead stream have almost the same level, so liveness is
    # judged by variation instead: a dead stream repeats one value forever.
    variety = audio.stream_variety(device)
    if variety < 2:
        return report.fail("hearing", f"{name} is delivering a dead stream")
    report.ok("hearing", f"{model} on {name} at {rate} Hz")


def check_voice(config, report: Report) -> None:
    cfg = config.section("voice")
    engine = cfg.get("engine", "auto")
    acct = config.account("openai", cfg.get("account", "default"))
    if engine.startswith("openai") and not acct.usable:
        return report.warn("voice", "no API key, so the local voice is used")
    if engine.startswith("openai"):
        try:
            pcm = audio._audio_model_speech(acct, cfg, "Testing one two three.")
        except Exception as exc:
            return report.fail("voice", f"{cfg.get('audio_model')} failed: {str(exc)[:60]}")
        if not pcm:
            return report.warn("voice", "the audio model rewrote its line and was rejected")
        report.ok("voice", f"{cfg.get('audio_model')} / {cfg.get('voice')}, "
                           f"{len(pcm) / 48000:.1f}s of speech")
    elif engines.kokoro_available():
        report.ok("voice", "local neural voice")
    else:
        report.warn("voice", "falling back to the macOS voice")


def check_commands(report: Report) -> None:
    cases = {
        "proceed": "affirm", "stop": "deny", "skip": "skip", "repeat that": "repeat",
        "details": "more", "switch to billing": "switch", "go to sleep": "mute",
        "wake up": "unmute", "run the tests now": "freeform",
    }
    wrong = [f"{said!r}->{commands.parse(said).kind}"
             for said, want in cases.items() if commands.parse(said).kind != want]
    if wrong:
        return report.fail("commands", ", ".join(wrong))
    woke, _ = commands.wake_split("Vika.")
    if not woke:
        return report.fail("commands", "the wake word is not recognised when mangled")
    report.ok("commands", f"{len(cases)} intents and the wake word")


def check_speech_gate(report: Report) -> None:
    if not audio.HAVE_SD:
        return report.warn("speech gate", "no audio library")
    import numpy as np

    rng = np.random.default_rng(0)
    cough = (np.clip(rng.normal(0, 1, 8000) * np.exp(-np.linspace(0, 6, 8000)) * 0.8, -1, 1)
             * 32767).astype("int16").tobytes()
    speechlike, _ = audio.looks_like_speech(cough)
    if speechlike:
        return report.fail("speech gate", "a cough was accepted as speech")
    report.ok("speech gate", "coughs and noise rejected before they become words")


def check_briefing(report: Report) -> None:
    out = briefing("All 42 tests pass and the coverage gap is closed.\n\n"
                   "Next up is wiring the webhook.")
    spoken = f"{out['takeaway']} {out['ask']}"
    if "wire the webhook" not in out["ask"]:
        return report.fail("briefings", f"asked {out['ask']!r} instead of the offered step")
    import re as _re

    pieces = [p for p in _re.split(r"(?<=[.!?])\s+", spoken) if p]
    if any(piece[-1] not in ".!?" for piece in pieces):
        return report.warn("briefings", "a sentence ended mid-clause")
    report.ok("briefings", f"{out['ask']!r}")


def check_delivery(config, report: Report) -> None:
    notes = route.preflight()
    blocked = [n for n in notes if "Accessibility" in n]
    if blocked:
        return report.fail("delivery", "Accessibility permission is missing")
    reachable = []
    if engines.find_bin("tmux"):
        reachable.append(f"{len(__import__('amekavoice.sessions', fromlist=['x']).tmux_panes())} tmux")
    if browser.cdp_up():
        reachable.append(f"{len(browser.ai_tabs())} browser tabs")
    report.ok("delivery", "paste into the front window" +
              (", " + ", ".join(reachable) if reachable else ""))


def check_sessions(config, report: Report) -> None:
    brain = Brain(config)
    brain.log = lambda *a: None
    line = brain.session_report()
    if line.startswith("Nothing"):
        return report.warn("sessions", "nothing running to talk to")
    report.ok("sessions", line.split(".")[0])


def check_phone(config, report: Report) -> None:
    if not config.section("phone").get("enabled"):
        return report.warn("phone", "off — run: ameka phone")
    import json
    import urllib.request

    from .relay import DEFAULT_URL
    from . import phone as phone_mod

    url = config.section("phone").get("relay_url") or DEFAULT_URL
    request = urllib.request.Request(
        url, data=json.dumps({"action": "poll"}).encode(),
        headers={"content-type": "application/json",
                 "x-ameka-channel": phone_mod.token(), "x-ameka-role": "phone"},
        method="POST")
    try:
        started = time.time()
        urllib.request.urlopen(request, timeout=30).read()
        report.ok("phone", f"relay answering in {time.time() - started:.1f}s")
    except Exception as exc:
        report.fail("phone", f"relay unreachable: {str(exc)[:60]}")


def run(config) -> int:
    print(f"Ameka self-test — {tools.summary()}\n")
    report = Report()
    for check in (check_hearing, check_voice, check_delivery, check_sessions, check_phone):
        try:
            check(config, report)
        except Exception as exc:
            report.fail(check.__name__.replace("check_", ""), f"{type(exc).__name__}: {exc}")
    for check in (check_commands, check_speech_gate, check_briefing):
        try:
            check(report)
        except Exception as exc:
            report.fail(check.__name__.replace("check_", ""), f"{type(exc).__name__}: {exc}")

    print()
    if report.failed:
        print(f"{report.failed} broken, {report.warned} worth knowing about.")
    elif report.warned:
        print(f"All working. {report.warned} worth knowing about.")
    else:
        print("Everything works.")
    return 1 if report.failed else 0
