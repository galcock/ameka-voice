"""Talk to the phone without anything reaching into this Mac.

A phone on cellular cannot open a connection to a laptop behind a router, and
making that possible means a VPN or a tunnel — an app to install and an account
to make. So the direction is reversed: the Mac holds an outbound request open,
the phone posts to the same address, and Ameka's relay hands the message over
and forgets it. No ports, no tunnel, nothing to configure.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from . import phone

DEFAULT_URL = "https://ameka-relay.vercel.app/api/relay"
BACKOFF_MAX = 30.0


def _ran_out(exc: BaseException) -> bool:
    """Did the poll simply reach the end of its wait?

    urllib hands back a bare socket timeout sometimes and wraps it in a
    URLError other times, and only one of those is a TimeoutError.
    """
    reason = getattr(exc, "reason", None)
    return (isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError)
            or "timed out" in str(exc).lower())


class Relay:
    def __init__(self, config, on_message, log=print):
        cfg = config.section("phone")
        self.url = cfg.get("relay_url") or DEFAULT_URL
        self.channel = phone.token()
        self.on_message = on_message
        self.log = log
        self.running = True

    # ------------------------------------------------------------ transport --
    def _post(self, body: dict, timeout: float) -> dict:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode(),
            headers={
                "content-type": "application/json",
                "x-ameka-channel": self.channel,
                "x-ameka-role": "mac",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())

    def send(self, text: str, reply_to: str | None = None) -> bool:
        if not text:
            return False
        try:
            body = {"action": "send", "text": text}
            if reply_to:
                body["replyTo"] = reply_to
            return bool(self._post(body, 15).get("ok"))
        except Exception as exc:
            self.log("relay send failed:", str(exc)[:80])
            return False

    # ----------------------------------------------------------------- loop --
    def run(self) -> None:
        self.log(f"relay open for your phone ({self.url})")
        backoff = 1.0
        while self.running:
            try:
                answer = self._post({"action": "poll"}, 45)
                backoff = 1.0
            except Exception as exc:
                if _ran_out(exc):
                    # The poll is held open until your phone says something or
                    # the time runs out, so running out IS the quiet case.
                    # Calling it a failure put twenty-seven alarms in the log
                    # for a day of nothing happening, and she read them back
                    # as a fault in herself.
                    continue
                self.log("relay poll failed:", str(exc)[:80])
                time.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue

            message = (answer or {}).get("message")
            if not message:
                continue
            text = (message.get("text") or "").strip()
            if not text:
                continue
            asked = message.get("id")
            try:
                reply = self.on_message(text)
            except Exception as exc:
                self.log("relay handler failed:", repr(exc))
                reply = "Something went wrong here."
            if reply:
                self.send(reply, reply_to=asked)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()
        return thread
