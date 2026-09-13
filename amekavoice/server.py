"""Localhost intake. Hooks POST here; nothing is exposed off the machine."""
from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import audio, phone, route
from .daemon import Brain, Event
from .transcript import read_claude_transcript


def build_event(payload: dict) -> Event:
    event = Event(
        source=payload.get("source") or "claude-code",
        session_id=payload.get("session_id") or "",
        cwd=payload.get("cwd") or "",
        note=payload.get("text") or payload.get("note") or "",
        target={
            "tmux_pane": payload.get("tmux_pane") or "",
            "bundle_id": payload.get("bundle_id") or "",
            "app_name": payload.get("app_name") or "",
        },
    )
    # Text can come straight in, not only out of a transcript file on disk.
    # Without this nothing can post an event with what was actually said —
    # which made the notify path impossible to exercise without waiting for a
    # real session to do the thing you were trying to test.
    event.assistant = payload.get("assistant") or ""
    event.user = payload.get("user") or ""
    event.title = payload.get("title") or ""
    path = payload.get("transcript_path")
    if path:
        tail = read_claude_transcript(path)
        event.assistant = tail["assistant"]
        event.user = tail["user"]
        event.title = tail.get("title", "")
    return event


def make_handler(brain: Brain):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def handle_one_request(self):
            # The menu polls every three seconds with a short timeout, so a
            # client hanging up mid-request is routine. socketserver prints a
            # traceback for it, and a traceback in her log is a crash to her
            # self-watch.
            try:
                super().handle_one_request()
            except (ConnectionResetError, BrokenPipeError):
                self.close_connection = True

        def _reply(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            try:
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                # The menu asks every three seconds with a short timeout and
                # sometimes gives up first. That is not a fault worth a
                # traceback — and a traceback here counted as a crash.
                pass

        def _authorised(self) -> bool:
            supplied = self.headers.get("X-Ameka-Token") or ""
            return bool(supplied) and secrets.compare_digest(supplied, phone.token())

        def _local(self) -> bool:
            if not (self.client_address[0] or "").startswith(("127.", "::1")):
                return False
            # A tunnel connects to the loopback too, so a request that arrived
            # from the internet would otherwise look like it came from this Mac.
            proxied = ("cf-ray", "cf-connecting-ip", "x-forwarded-for",
                       "x-forwarded-host", "x-real-ip", "forwarded")
            return not any(self.headers.get(h) for h in proxied)

        def _allowed(self) -> bool:
            """Anything off this Mac must carry the token, always. The hook and
            the menu bar talk over the loopback and do not need one."""
            return self._local() or self._authorised()

        def _send_bytes(self, code: int, kind: str, payload: bytes) -> None:
            self.send_response(code)
            self.send_header("content-type", kind)
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            path = self.path.rstrip("/")
            if not self._allowed():
                return self._reply(401, {"ok": False, "error": "token required"})
            if path == "/phone":
                with brain.lock:
                    waiting = [e.project for e in brain.queue]
                return self._reply(200, {
                    "ok": True, "muted": brain.muted, "bound": brain.bound_name,
                    "last": brain.last_brief, "waiting": waiting,
                })
            if path == "/health":
                with brain.lock:
                    depth = len(brain.queue)
                return self._reply(200, {"ok": True, "queued": depth, "muted": brain.muted,
                                         "speaking": brain.speaking})
            if path == "/state":
                with brain.lock:
                    waiting = [{"project": e.project, "source": e.source} for e in brain.queue]
                return self._reply(200, {
                    "ok": True,
                    "muted": brain.muted,
                    "chat": brain.chat,
                    "bound": brain.bound_name or route.describe(brain.bound or brain.default_target()),
                    "sessions": sorted(brain.sessions),
                    "waiting": waiting,
                    "last": {"takeaway": brain.last_brief.get("takeaway", ""),
                             "ask": brain.last_brief.get("ask", ""),
                             "project": brain.current.project if brain.current else ""},
                    "engines": brain.engine_report(),
                    "active": _inventory(brain),
                })
            if path == "/sessions":
                return self._reply(200, {"ok": True, "active": _inventory(brain)})
            self._reply(404, {"ok": False})

        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            if length > 1_000_000:
                return self._reply(413, {"ok": False})
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._reply(400, {"ok": False, "error": "bad json"})

            path = self.path.rstrip("/")
            if not self._allowed():
                return self._reply(401, {"ok": False, "error": "token required"})

            if path.startswith("/phone/"):
                text = (payload.get("text") or "").strip()
                if path == "/phone/reply":
                    spoken = brain.from_phone(text)
                    return self._reply(200, {"ok": True, "speech": spoken})
                if path == "/phone/speak":
                    data = brain.render_speech(text)
                    if data is None:
                        return self._reply(503, {"ok": False, "error": "no voice available"})
                    return self._send_bytes(200, "audio/wav", data)
                return self._reply(404, {"ok": False})

            if path == "/event":
                accepted = brain.submit(build_event(payload))
                return self._reply(200, {"ok": True, "accepted": accepted})
            if path == "/bind":
                brain.switch_to(payload.get("project", ""))
                return self._reply(200, {"ok": True, "bound": brain.bound_name})
            if path == "/heard":
                # Exactly as if the microphone had heard it: same understanding,
                # same dispatch. Testing the voice path should not require a
                # voice, and a bug found by talking takes twenty times as long.
                text = (payload.get("text") or "").strip()
                if text:
                    brain.overheard = text
                return self._reply(200, {"ok": True, "queued": bool(text)})
            if path == "/loop":
                # The menu's switches: {key, on, minutes}, or {all: true, ...}.
                sup = getattr(brain, "supervisor", None)
                if sup is None:
                    return self._reply(503, {"ok": False, "error": "no loop"})
                if payload.get("all"):
                    for row in sup.inventory(max_age=0):
                        sup.loops.set(row["key"], on=payload.get("on"), minutes=payload.get("minutes"))
                    sup._inventory_at = 0.0
                    return self._reply(200, {"ok": True})
                key = payload.get("key", "")
                if not key:
                    return self._reply(400, {"ok": False, "error": "key"})
                got = sup.loops.set(key, on=payload.get("on"), minutes=payload.get("minutes"))
                sup._inventory_at = 0.0
                brain.log(f"loop settings for {key}: {'on' if got['on'] else 'off'}, every {got['minutes']:.0f} min")
                return self._reply(200, {"ok": True, "loop": got})
            if path == "/say":
                text = payload.get("text", "")
                if text:
                    brain.to_say.append(text)      # spoken by the listening loop
                return self._reply(200, {"ok": True, "queued": bool(text)})
            if path == "/mute":
                brain.set_muted(bool(payload.get("muted", True)))
                return self._reply(200, {"ok": True, "muted": brain.muted})
            self._reply(404, {"ok": False})

        def log_message(self, *args):        # keep stdout for our own log lines
            return

    return Handler


def _inventory(brain) -> list:
    sup = getattr(brain, "supervisor", None)
    if sup is None:
        return []
    try:
        return sup.inventory()
    except Exception as exc:
        brain.log("inventory failed:", repr(exc))
        return []


def serve(brain: Brain) -> ThreadingHTTPServer:
    cfg = brain.config.section("server")
    host = cfg["host"]
    if brain.config.section("phone").get("enabled"):
        host = "0.0.0.0"                      # reachable from your phone, token-gated
    httpd = ThreadingHTTPServer((host, int(cfg["port"])), make_handler(brain))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    if host == "0.0.0.0":
        brain.log(f"intake on http://{phone.lan_address()}:{cfg['port']} (phone enabled)")
    else:
        brain.log(f"intake on http://{cfg['host']}:{cfg['port']}")
    return httpd
