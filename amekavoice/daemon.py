"""The loop: session finishes -> two sentences in your ear -> your voice -> reply sent."""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
import threading
import time  # noqa: F401  (used by chat pacing)
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from . import acks, audio, brain, browser, codex_watch, commands, config as cfgmod, corrections, engines, host, learn, simple, supervise, phone, relay as relay_mod, route, selfupdate, selfwatch, sessions as session_tools, summarize, tools, transcript, understand, worth


def stt_ready(config) -> bool:
    cfg = config.section("listen")
    if cfg.get("engine") in ("auto", "whisper") and engines.whisper_available():
        return True
    return config.account("openai", cfg.get("account", "default")).usable


@dataclass
class Event:
    source: str = "claude-code"
    session_id: str = ""
    cwd: str = ""
    note: str = ""
    assistant: str = ""
    user: str = ""
    title: str = ""
    target: dict = field(default_factory=dict)
    received: float = field(default_factory=time.time)

    @property
    def project(self) -> str:
        if self.source == "browser":
            return self.cwd or "browser"
        return (self.cwd or "").rstrip("/").split("/")[-1] or "session"

    @property
    def label(self) -> str:
        """How a session is named out loud: its own title, then its project."""
        title = " ".join((self.title or "").split())
        if not title:
            return self.project
        words = title.split()
        if len(words) > 9:
            title = " ".join(words[:9])
        if title.lower() == self.project.lower():
            return self.project
        return f"{title}, in {self.project}"


class Brain:
    def __init__(self, config):
        self.config = config
        session_tools.configure(config)
        self.queue: deque[Event] = deque()
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.current: Event | None = None
        self.last_brief: dict = {}
        self.last_spoken: str = ""
        self.seen: dict[str, float] = {}
        self.running = True
        self.relay = None
        self.phone_bound: dict = {}
        self.phone_bound_name: str = ""
        self.verbosity = ""              # set by voice: short, full, everything
        self.interrupted_with: tuple = (b"", 0)
        self._mic_name = ""        # so a microphone changing hands is noticed
        self.overheard = ""        # said over her, recovered after she finished
        self.briefed_at = 0.0      # when she last asked him something out loud
        self.said_news: dict[str, list] = {}   # project -> [(time, takeaway words)]
        self.unheard: list[float] = []         # speech-length captures with no words in them
        self.said_no_window: dict[str, float] = {}
        # Simple mode: one sentence per turn, nothing sent that he did not confirm.
        self.simple_sessions: dict = {}        # project -> the event that last spoke, with its route
        self.pending = None                    # a message read back and waiting for yes
        self.simple_target = ""                # where a bare message goes, if he said "talk to X"
        self.last_line = ""
        # The session loop: judged on a thread, acted on here, where the mic lives.
        self.supervisor = supervise.Supervisor(self)
        self.loop_actions: list[tuple[dict, dict]] = []
        self._loop_thread = None
        self.said_cant_hear = 0.0
        self.last_news: dict = {}  # where the last spoken briefing came from
        self.engaged_until = 0.0   # in conversation: no need to keep saying her name
        self._looked_for_code = 0.0   # when she last checked for new code
        self.tab_names: list[str] = []      # refreshed by the watcher, read instantly
        self.speaking = False
        self._voice = threading.Lock()
        self.to_say: list[str] = []      # spoken by the listening loop, not by callers
        self.waiting_since = 0.0        # when you last gave a session something to do
        self.last_announced = ""        # the session named in the last briefing
        self.waiting_since = 0.0        # when you last gave a session something to do
        self.focus_seen: tuple = ()      # the window you were last looking at
        self.chosen_by_voice = False     # you named a session; hold it until you move
        self.bound: dict = {}          # where a spoken line goes when nothing is queued
        self.bound_name: str = ""
        self.sessions: dict[str, dict] = {}   # project -> how to reach it
        self.chat = bool(config.section("behavior").get("chat", False))
        # What the two of you have actually said. She composes an
        # instruction out of the conversation, not out of one sentence.
        self.dialogue: deque[tuple[str, str]] = deque(maxlen=16)

    # ------------------------------------------------------------- plumbing --
    def log(self, *parts) -> None:
        line = f"{datetime.now():%H:%M:%S} " + " ".join(str(p) for p in parts)
        print(line, flush=True)
        if sys.stdout.isatty():          # under launchd stdout is already the log file
            try:
                cfgmod.ensure_dirs()
                with open(cfgmod.LOG_PATH, "a") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass

    @property
    def muted(self) -> bool:
        return cfgmod.MUTE_FLAG.exists()

    def asleep_for(self) -> float:
        """Seconds since it was put to sleep."""
        try:
            return time.time() - cfgmod.MUTE_FLAG.stat().st_mtime
        except OSError:
            return 0.0

    def set_muted(self, value: bool) -> None:
        cfgmod.ensure_dirs()
        if value:
            cfgmod.MUTE_FLAG.touch()
        elif cfgmod.MUTE_FLAG.exists():
            cfgmod.MUTE_FLAG.unlink()

    def submit(self, event: Event) -> bool:
        behavior = self.config.section("behavior")
        key = event.session_id or event.cwd
        now = time.time()
        with self.lock:
            waiting = any((e.session_id or e.cwd) == key for e in self.queue)
            fresh = key and now - self.seen.get(key, 0) < float(behavior["dedupe_seconds"])
            # A session that writes a turn every few seconds used to earn a
            # spoken briefing every few seconds, which is most of what sounding
            # repetitive was. One briefing per session per window now — but if
            # the last one is still waiting to be read out, the newer turn takes
            # its place, so what she says is what the session says now rather
            # than what it said a minute ago.
            if fresh and not waiting:
                return False
            self.seen[key] = now
            self.queue = deque(
                [e for e in self.queue if (e.session_id or e.cwd) != key],
                maxlen=int(behavior["max_queue"]),
            )
            self.queue.append(event)
        self.wake.set()
        return True

    # --------------------------------------------------------------- speech --
    def speak(self, text: str) -> None:
        # One voice at a time. A briefing and an answer could speak together,
        # each with its own microphone monitor, so an interruption stopped one
        # while the other carried on and neither kept what was said.
        with self._voice:
            self._speak(text)

    # Nothing she says takes this long. At 21:29 one night a write to the output
    # device never returned, and because speech runs on the loop that owns the
    # microphone, the loop waited with it — no listening, no self-check, no
    # self-update, for twenty hours, while the status endpoint said "speaking".
    # The ceiling follows the sentence: six words should not get ninety seconds.
    VOICE_CEILING = 90.0
    VOICE_FLOOR = 20.0

    def _voiced(self, work, words: int = 0) -> dict:
        """Run one act of speech, but only wait so long for it.

        The work still runs on its own thread with this one blocked on it, so
        speaking and listening are as exclusive as they were. The difference is
        what happens to a hang. First every open stream is aborted from here —
        abort, not stop, because stop waits for a device that is not coming
        back. If the thread still will not finish it is wedged inside PortAudio
        and nothing in this process can free it, so the process ends and comes
        back: the OS reclaims the device, launchd relaunches her, a second
        later she is running. The first version only freed the loop and left
        the thread holding the microphone, and the next listen hung on it.
        """
        outcome: dict = {}

        def run():
            try:
                outcome["result"] = work()
            except BaseException as exc:                 # re-raised on the loop
                outcome["error"] = exc

        ceiling = min(self.VOICE_CEILING, max(self.VOICE_FLOOR, 12 + 0.6 * words))
        worker = threading.Thread(target=run, name="voice", daemon=True)
        worker.start()
        worker.join(ceiling)
        if not worker.is_alive():
            return outcome
        # The abort itself can hang: PortAudio's abort on a device that has
        # just changed — AirPods going in mid-sentence — deadlocked here, with
        # nothing logged, and she sat blocked at 0% CPU for as long as it
        # took somebody to notice. So the abort gets three seconds, and if it
        # does not come back she restarts without it.
        count = {}
        aborter = threading.Thread(target=lambda: count.setdefault("n", engines.abort_streams()),
                                   name="voice-abort", daemon=True)
        aborter.start()
        aborter.join(3.0)
        if aborter.is_alive():
            self.restart(f"voice hung: nothing finished in {ceiling:.0f}s and the abort hung too")
        aborted = count.get("n", 0)
        worker.join(5.0)
        outcome["hung"] = True
        if not worker.is_alive():
            self.log(f"voice hung: nothing finished in {ceiling:.0f}s, aborted "
                     f"{aborted} stream{'s' if aborted != 1 else ''} and it let go")
            return outcome
        self.restart(f"voice hung: nothing finished in {ceiling:.0f}s and would not let go "
                     f"after aborting {aborted} stream{'s' if aborted != 1 else ''}")

    def _speak(self, text: str) -> None:
        self.last_spoken = text
        self.engaged_until = max(self.engaged_until,
                                 time.time() + float(self.config.section("behavior").get("conversation_seconds", 90)))
        if self.muted:
            self.log("(muted)", text)
            return
        if not (self.chat and self.config.section("behavior").get("barge_in", True)
                and audio.HAVE_SD):
            self.speaking = True
            try:
                outcome = self._voiced(lambda: audio.speak(self.config, text),
                                       words=len(text.split()))
            finally:
                self.speaking = False
            if outcome.get("hung"):
                return
            if "error" in outcome:
                raise outcome["error"]
            self.log(f"spoke [{outcome['result']}]", text)
            return
        # Talking over it should stop it, the way it would stop a person.
        self.log("speaking:", text[:70])
        self.speaking = True
        held: dict = {}

        def with_barge_in():
            with audio.BargeIn(self.config) as watcher:
                held["watcher"] = watcher
                return audio.speak(self.config, text, should_stop=watcher.triggered)

        try:
            outcome = self._voiced(with_barge_in, words=len(text.split()))
        finally:
            self.speaking = False
        if outcome.get("hung"):
            return
        if "error" in outcome:
            raise outcome["error"]
        engine, watcher = outcome["result"], held["watcher"]
        self.log(f"barge monitor: {watcher.frames} frames, peak {watcher.peak:.5f}, "
                 f"threshold {watcher.last_threshold:.5f}"
                 + (" (room mic)" if watcher.room else ""))
        if engine == "interrupted":
            self.log("stopped: you started talking")
            self.interrupted_with = (watcher.captured, watcher.rate)
            return
        self.log(f"spoke [{engine}]", text)
        # She finished, but you may have spoken over her without clearing the
        # bar that stops her — which on a laptop microphone is most of the time,
        # because her own voice comes back through the room at nearly the level
        # yours arrives at. Deciding by loudness alone cost a real request.
        spoken_over = watcher.missed_speech()
        if not spoken_over:
            return
        said = audio.transcribe(self.config, audio.resample(spoken_over, watcher.rate))
        if not said:
            return
        if self.her_own_voice(said, text):
            self.log("that was her own voice coming back:", repr(said[:50]))
            return
        self.log("you spoke while she was talking — keeping it:", repr(said[:60]))
        self.overheard = said

    @staticmethod
    def her_own_voice(heard: str, spoken: str) -> bool:
        """Did the microphone just record Ameka?

        She knows exactly what she said, which is a far better test than how
        loud it was. If most of what came back is words she had just spoken, it
        is her leaking through the room and not you — and mistaking the two is
        how a machine ends up taking dictation from itself.
        """
        words = lambda t: re.findall(r"[a-z0-9']+", (t or "").lower())
        his, hers = words(heard), words(spoken)
        if not his:
            return True
        if not hers:
            return False
        # Counting shared words made her deafer the longer she spoke: a thirty
        # second briefing has a wide vocabulary, and his short sentences kept
        # landing inside it by coincidence. "Are you able to communicate
        # properly with the billing service" was thrown away as her own
        # voice because she had said most of those words separately, in another
        # order, about something else. Her echo comes back in order.
        #
        # But not word for word. She said "densityfielddynamics" and the room
        # gave it back as three words; "They're" came back "There are".
        # One unbroken run of words could not survive that, and she answered
        # her own sentence. So: in order, by character, with the spaces gone —
        # most of what came back has to sit inside what she said, in sequence.
        mine, theirs = "".join(hers), "".join(his)
        if len(his) <= 4:                  # "sending that", "one moment", "talking to Amiga"
            if len(theirs) < 8:
                return theirs == mine[:len(theirs)] or theirs in mine.split()
            return difflib.SequenceMatcher(None, theirs, mine[:len(theirs) + 4],
                                           autojunk=False).ratio() >= 0.8
        blocks = difflib.SequenceMatcher(None, theirs, mine, autojunk=False).get_matching_blocks()
        matched = sum(b.size for b in blocks if b.size >= 4)     # no credit for stray letters
        return matched / max(1, len(theirs)) >= 0.72

    # ----------------------------------------------------------------- loop --
    def watch_codex(self) -> None:
        """Codex turns arrive the same way Claude Code turns do."""
        def on_turn(path, tail):
            project = (tail["cwd"] or "").rstrip("/").split("/")[-1] or "codex"
            self.submit(Event(
                source="codex", session_id=f"codex:{path.name}", cwd=tail["cwd"],
                assistant=tail["assistant"], user=tail["user"],
                target={"bundle_id": self.config.section("behavior").get("codex_target", "")},
            ))
            self.log(f"codex turn in {project}")

        stop = threading.Event()
        threading.Thread(target=codex_watch.watch, args=(on_turn, stop), daemon=True).start()
        self.log(f"watching Codex sessions in {codex_watch.SESSIONS}")

    def watch_browser(self) -> None:
        """ChatGPT and Claude tabs brief you like any other session."""
        seen: dict[str, str] = {}
        # Tabs already open when the watcher starts hold old answers; those are
        # backlog and stay quiet. A tab opened afterwards is live, so its very
        # first answer is news and gets read out.
        try:
            backlog = {t["id"] for t in browser.ai_tabs()} if browser.cdp_up() else set()
        except Exception:
            backlog = set()

        def loop():
            while self.running:
                time.sleep(float(self.config.section("behavior").get("browser_poll", 4.0)))
                if not browser.cdp_up():
                    continue
                seen_names = []
                for tab in browser.ai_tabs():
                    name = browser.project_name(tab)
                    # Addressable by name straight away: "switch to Yang Mills"
                    self.sessions[name.lower()] = {"cdp_tab_id": tab["id"], "label": name}
                    try:
                        data = browser.read_tab(tab)
                    except Exception:
                        seen_names.append(name)
                        continue
                    seen_names.append(browser.project_name({**tab, "full_title": data.get("title", "")}))
                    body = (data.get("text") or "").strip()
                    if not body or data.get("busy"):
                        continue
                    key = tab["id"]
                    fingerprint = f"{data.get('id')}:{len(body)}"
                    if seen.get(key) == fingerprint:
                        continue
                    first_sighting = key not in seen
                    seen[key] = fingerprint
                    if first_sighting and key in backlog:
                        continue                      # an answer from before we were watching
                    self.submit(Event(
                        source="browser", session_id=f"tab:{key}", cwd=name,
                        assistant=body,
                        target={"cdp_tab_id": key, "label": name},
                    ))

        threading.Thread(target=loop, daemon=True).start()
        self.log("watching ChatGPT and Claude browser tabs"
                 if browser.cdp_up() else
                 "browser watch armed (Chrome needs its debug port; say 'connect the browser')")

    def run(self) -> None:
        if self.config.section("phone").get("enabled"):
            try:
                self.relay = relay_mod.Relay(self.config, self.from_phone, self.log)
                self.relay.start()
            except Exception as exc:
                self.log("relay failed to start:", repr(exc))
        if self.config.section("behavior").get("watch_browser", True) and tools.chrome()["present"]:
            try:
                self.watch_browser()
            except Exception as exc:
                self.log("browser watch failed:", repr(exc))
        if self.config.section("behavior").get("watch_codex", True) and tools.codex()["present"]:
            try:
                self.watch_codex()
            except Exception as exc:
                self.log("codex watch failed:", repr(exc))
        # Hold the trees of the apps she works in, from the start and for as
        # long as she runs. Without this every click lands on "no tree".
        trusted = host.accessibility_trusted()
        self.log(f"accessibility: {'granted' if trusted else 'NOT GRANTED — she can see but not act'}")
        if not trusted:
            self.log("  System Settings > Privacy & Security > Accessibility > turn on Ameka")
        for app in ("Claude", "ChatGPT"):
            session_tools.hold_tree(app, self.log)
        if self.chat:
            target = self.bound or self.default_target()
            self.log(f"Ameka can see: {tools.summary()}.")
            self.log("Chat mode on. Talk any time — she answers, and sends to "
                     f"{route.describe(target)} only once you have settled on something.")
            greeting = self.config.section("behavior").get("greeting", "")
            if greeting:
                self.speak(greeting)
        else:
            self.log("Ameka Voice listening for sessions.")
        while self.running:
            if not self.chat:
                self.wake.wait(timeout=1.0)
                self.wake.clear()
            drained = False
            while True:
                with self.lock:
                    event = self.queue.pop() if self.queue else None   # newest first
                if event is None:
                    break
                drained = True
                try:
                    self.handle(event)
                except Exception as exc:                                # keep the daemon alive
                    self.log("error:", repr(exc))
            while self.to_say:
                # Anything asked for from elsewhere is spoken here, on the loop
                # that owns the microphone. Speaking from another thread closed
                # the microphone while this one was reading it, and it never
                # came back.
                self._speak(self.to_say.pop(0))
            if self.chat and not drained:
                try:
                    for app in ("Claude", "ChatGPT"):
                        session_tools.hold_tree(app)      # restarted if it died
                    self.take_new_code()
                    self.refresh_brain()
                    self.grow_brain()
                    self.run_session_loop()
                    self.nudge_if_slow()
                    self.check_myself()
                    self.chat_turn()
                except Exception as exc:
                    self.log("error:", repr(exc))

    # ------------------------------------------------------------ chat mode --
    def take_new_code(self) -> None:
        """Install my own new code and stop, so I come back running it.

        Every fix waited on somebody remembering to deploy. More than once an
        afternoon of edits sat in the repository looking deployed while the
        running copy never changed, and the time went on working out why a fix
        had not worked when it had simply never arrived.
        """
        # Not mid-sentence, and nothing waiting to be said. Waiting for an
        # empty event queue as well sounded careful and meant no update could
        # ever land: one session wrote a turn every few seconds, so the queue
        # was never empty, and fixes sat installed-but-not-running for half an
        # hour. A queued briefing is a status line that will be regenerated;
        # it is not worth blocking every repair behind.
        if self.speaking or self.to_say:
            return
        source = selfupdate.source_dir(self.config)
        if source is None:
            self.take_new_release()
            return
        if time.time() - self._looked_for_code < 15:
            return
        self._looked_for_code = time.time()
        waiting = selfupdate.changed(source)
        if not waiting:
            return
        if not selfupdate.sound(source):
            self.log("new code is mid-edit or does not parse — leaving it")
            return
        done = selfupdate.install(waiting)
        if not done:
            return
        self.restart("took new code: " + ", ".join(done))

    def run_session_loop(self) -> None:
        """Every ten minutes, every session: judged on a thread (her brain
        takes seconds per session), acted on here, one at a time."""
        # First, anything the last pass decided.
        while self.loop_actions:
            st, verdict = self.loop_actions.pop(0)
            try:
                self.supervisor.act(st, verdict)
            except Exception as exc:
                self.log("loop action failed:", repr(exc))
        if not self.supervisor.due():
            return
        if self._loop_thread and self._loop_thread.is_alive():
            return

        def run():
            try:
                self.supervisor.run(queue=self.loop_actions)
            except Exception as exc:
                self.log("session loop failed:", repr(exc))

        self._loop_thread = threading.Thread(target=run, name="session-loop", daemon=True)
        self._loop_thread.start()

    def spoken_name(self, project: str) -> str:
        return simple.spoken(self, project)

    def app_reachable(self, app: str) -> bool:
        """Can she reach this app at all? Says so out loud when she cannot,
        once every ten minutes — silence here looks exactly like her being
        broken, and the fix is one thing he can do in a second."""
        if session_tools.has_window(app):
            return True
        self.log(f"the {app} app has no window open — nothing to type into")
        if time.time() - self.said_no_window.get(app, 0) > 600 and not self.muted:
            self.said_no_window[app] = time.time()
            self.speak(f"The {app} app has no window open, so I cannot reach your sessions. "
                       f"Open its window and I will carry on.")
        return False

    def type_into_thread(self, st: dict, text: str, why: str = "", quiet: bool = False) -> bool:
        """A Codex thread in the ChatGPT app: click it in the sidebar, then
        type into the page's own box."""
        title = st.get("title") or ""
        if not self.app_reachable("ChatGPT"):
            return False
        if not session_tools.focus_row("ChatGPT", title, log=self.log):
            self.log(f"{why}: could not bring {title!r} up in ChatGPT — not typed")
            return False
        time.sleep(0.5)
        ok, how = session_tools.type_into_front("ChatGPT", text, log=self.log)
        self.log(f"{why}: typed {text[:60]!r} -> {title} via {how}" if ok
                 else f"{why}: not typed into {title} ({how})")
        return ok

    def type_into_session(self, st: dict, text: str, why: str = "", quiet: bool = False) -> bool:
        """Into that exact session, proven on screen first, or not at all."""
        live = st.get("live") or {}
        if not self.app_reachable("Claude"):
            return False
        title = session_tools.session_title(st.get("cwd", ""), st.get("session_id", ""))
        shown = session_tools.focus_in_app(title or st.get("title", ""), live.get("name", ""),
                                           st.get("project", ""), log=self.log)
        if shown:
            time.sleep(0.4)
            shown = session_tools.on_screen(session_tools.last_words(st.get("cwd", ""), st.get("session_id", "")), title)
            if not shown:
                self.log(f"{why}: clicked {title or st.get('project')!r} but the session on screen is not it — not typed")
        if not shown:
            self.log(f"{why}: could not bring {st.get('project')} on screen — not typed")
            if not quiet:
                self.speak(f"{self.spoken_name(st.get('project', ''))} needs: {text}. I could not get it on screen.")
            return False
        ok, how = route.deliver({"bundle_id": "com.anthropic.claudefordesktop"}, text)
        self.log(f"{why}: typed {text!r} -> {st.get('project')} via {how}" if ok else f"{why}: not typed ({how})")
        if ok and not quiet:
            self.speak(f"Told {self.spoken_name(st.get('project', ''))}: {text}")
        return ok

    def grow_brain(self) -> None:
        """Once an hour, write down what she has learned about him. On a
        thread: it asks her brain, which takes seconds."""
        if not learn.due(self.config):
            return
        if getattr(self, "_learn_thread", None) and self._learn_thread.is_alive():
            return

        def run():
            try:
                self.log("learned:", learn.look(self.config, self.supervisor.last_states, self.log))
            except Exception as exc:
                self.log("could not add to his brain:", repr(exc))

        self._learn_thread = threading.Thread(target=run, name="learn", daemon=True)
        self._learn_thread.start()

    def refresh_brain(self) -> None:
        """Once a day, ask whether there is a better brain, on a thread of its
        own: the download is gigabytes and the loop owns the microphone."""
        if not brain.runtime_available() or not brain.due():
            return
        if getattr(self, "_brain_thread", None) and self._brain_thread.is_alive():
            return

        def run():
            try:
                self.log("brain:", brain.refresh(log=lambda m: self.log("brain:", m)))
            except Exception as exc:
                self.log("brain refresh failed:", repr(exc))

        self._brain_thread = threading.Thread(target=run, name="brain-refresh", daemon=True)
        self._brain_thread.start()

    def take_new_release(self) -> None:
        """No source folder to watch, so ask ameka.ai. The path every other Mac takes."""
        if not selfupdate.remote_due(self.config):
            return
        try:
            info = selfupdate.remote_available(self.config)
        except Exception as exc:
            selfupdate.mark_checked(error=repr(exc))
            self.log("could not ask ameka.ai for updates:", repr(exc))
            return
        if info is None:
            selfupdate.mark_checked()
            return
        self.log(f"ameka.ai has {info['version']}, this is {selfupdate.installed_version()} — updating")
        try:
            done = selfupdate.remote_install(info)
        except Exception as exc:
            selfupdate.mark_checked(error=repr(exc))
            self.log("update failed, staying on what works:", repr(exc))
            return
        selfupdate.mark_checked(installed=info["version"])
        self.restart("took " + done)

    def restart(self, why: str) -> None:
        """Become the new code. Exiting is the only way to run code that was
        imported before it was written.

        exec replaces this process with a fresh one in place: same PID, so a
        launchd that is watching stays satisfied, and the same terminal, so a
        copy started by hand in a window comes back in that window instead of
        just ending. If exec itself fails, exit and let whatever supervises her
        start her again.
        """
        self.log(why, "— restarting")
        self.running = False
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        try:
            os.execv(sys.executable, [sys.executable, "-m", "amekavoice", *sys.argv[1:]])
        except Exception as exc:
            self.log("could not exec myself, exiting instead:", repr(exc))
        raise SystemExit(0)

    def check_myself(self) -> None:
        """Read my own log and say what is wrong with me.

        He should not have to be the one who notices. Everything that goes
        wrong in here leaves a line behind, and until now nobody read any of
        it — he listened to her misbehave, worked out the fault from the
        outside, and told her, which is the slowest road from a bug to a fix
        there is.
        """
        if self.muted or not selfwatch.due(self.config):
            return
        findings, offset = selfwatch.look(cfgmod.LOG_PATH)
        selfwatch.mark_looked(offset)
        fresh = selfwatch.worth_saying(self.config, findings)
        if not fresh:
            return
        mine = str(self.config.section("behavior").get("own_project", "Ameka"))
        if session_tools.hands_off(self.config, mine):
            self.log("found faults in myself but told to stay out of", mine)
            return
        self.log("found a fault in myself:",
                 ", ".join(f"{f['key']}x{f['count']}" for f in fresh))
        if self.simple():
            self.log("(simple mode: noted, not sent)")
            return
        # A report sent into a session nobody is running is a message in an
        # empty room. She cannot write the fix herself; she can at least make
        # sure somebody is there to read what she found.
        try:
            ok, how = session_tools.ensure(mine)
            self.log("somebody to tell:", how)
            if not ok:
                return
        except Exception as exc:
            self.log("could not make sure of a session:", repr(exc))
        if (self.bound_name or "").lower().find(mine.lower()) < 0:
            if not self.switch_to(mine):
                # "I can see ameka but I cannot bring it up on screen" — and
                # then it typed the report into whatever was showing.
                self.log("could not reach", mine, "— fault report not sent")
                return
        self.deliver_text(selfwatch.write_up(fresh),
                          self.config.section("behavior"),
                          quiet=True, verbatim=True)
        worst = fresh[0]
        self.speak(f"Something is wrong with me. {worst['about']}, "
                   f"{worst['count']} times. I have sent it to {mine} with the log lines.")

    def will_hold(self, event: Event) -> bool:
        """Would this be held for coming too soon after the last one? Decided
        before the brain writes a sentence for it. Seven turns from one session in three
        minutes each cost the brain five to ten seconds, on the loop that owns
        the microphone, and six of the seven were then held — and in those
        minutes she heard nothing he said."""
        behavior = self.config.section("behavior")
        recent = self.said_news.get(event.project, [])
        if not recent:
            return False
        gap = float(behavior.get("min_gap_seconds", 180))
        if time.time() - recent[-1][0] >= gap:
            return False
        said = event.assistant or ""
        if worth.WENT_WRONG.search(said[-600:]) or worth.ASKING.search(said):
            return False                                # errors and questions always get through
        self.log(f"too soon after the last one from {event.project} — not summarising: {said[:60]!r}")
        return True

    def old_news(self, project: str, brief: dict, why: str = "") -> bool:
        """Has she already read this out? The same status, differently worded,
        every ninety seconds from an automation is one piece of news, not nine.
        And even fresh news from one source is spaced, unless it went wrong or
        the session asked him something — those always get through."""
        behavior = self.config.section("behavior")
        now = time.time()
        words = set(re.findall(r"[a-z0-9]+", (brief.get("takeaway") or "").lower()))
        recent = [(t, w) for t, w in self.said_news.get(project, [])
                  if now - t < float(behavior.get("repeat_window_minutes", 15)) * 60]
        self.said_news[project] = recent
        urgent = why in ("something went wrong", "it asked him something")
        for then, said in recent:
            overlap = len(words & said) / max(1, len(words | said))
            if overlap >= float(behavior.get("repeat_overlap", 0.45)):
                self.log(f"same news as {int((now - then) / 60)} min ago — not repeating: "
                         f"{brief.get('takeaway', '')[:60]}")
                return True
        gap = float(behavior.get("min_gap_seconds", 180))
        if recent and not urgent and now - recent[-1][0] < gap:
            self.log(f"too soon after the last one from {project} — holding: "
                     f"{brief.get('takeaway', '')[:60]}")
            return True
        recent.append((now, words))
        return False

    def simple(self) -> bool:
        return str(self.config.section("behavior").get("mode", "simple")).lower() == "simple"

    def nudge_if_slow(self) -> None:
        """A long silence after an instruction is worrying. Say it is still going."""
        behavior = self.config.section("behavior")
        if self.simple():
            return
        after = float(behavior.get("still_working_after", 45))
        if not self.waiting_since or after <= 0 or self.muted:
            return
        if time.time() - self.waiting_since < after:
            return
        self.waiting_since = time.time()          # and again later if it drags on
        self.speak(acks.still_going())

    def chat_turn(self) -> None:
        """One pass of open-mic listening while nothing is queued."""
        behavior = self.config.section("behavior")
        # Anything said over her was already heard and transcribed; it goes
        # first, before she listens for whatever came after it.
        text, self.overheard = self.overheard, ""
        if not text:
            text = self.listen_once()
        if not text:
            return

        # Asleep: keep hearing, act on nothing unless it is addressed to Ameka
        # by name. Switching the microphone off entirely would leave no way to
        # switch it back on by voice.
        if self.muted:
            minutes = float(behavior.get("sleep_timeout_minutes", 30))
            timed_out = minutes > 0 and self.asleep_for() > minutes * 60
            if timed_out:
                # Last resort: twice a mangled name has left it asleep with no
                # way back except a terminal, which is what we are avoiding.
                self.set_muted(False)
                self.log(f"woke itself after {minutes:.0f} minutes asleep")
            else:
                addressed, remainder = commands.wake_split(text)
                if not addressed:
                    self.log("asleep, ignored:", repr(text[:60]))
                    return
                self.set_muted(False)
                self.log("woken")
                self.engaged_until = time.time() + float(behavior.get("conversation_seconds", 90))
                if not remainder or commands.parse(remainder).kind in ("unmute", "none"):
                    self.speak("Listening.")
                    return
                text = remainder

        # "Ameka, proceed" while awake is just "proceed".
        addressed, remainder = commands.wake_split(text)
        if addressed and remainder:
            text = remainder

        # On a headset, everything she hears is him talking to her. On the
        # laptop microphone it is him talking to Chris, the television, and
        # her own voice a beat late — and she answered all of it, out loud,
        # into the conversation, until he said she was broken.
        #
        # So in the room she needs her name — once. The first version wanted
        # it on every sentence, and he sat there saying "try to read me" to
        # someone who had decided he was the television. A conversation is a
        # window: her name opens it, everything either of them says keeps it
        # open, and a stretch of silence closes it again. An answer to a
        # briefing she just gave is always for her.
        if addressed:
            self.engaged_until = time.time() + float(behavior.get("conversation_seconds", 90))
        # Off unless asked for. He heard the version that needed the name and
        # said, in so many words, get rid of it: he would rather she answer
        # the television now and then than have to announce her every time.
        if (not addressed and behavior.get("room_mic_needs_name", False)
                and audio.on_room_mic(self.config)):
            now = time.time()
            window = float(behavior.get("answer_window_seconds", 45))
            answering = (now - self.briefed_at < window
                         and commands.parse(text).kind not in ("none", "freeform"))
            if not answering and now >= self.engaged_until:
                self.log("room chatter, not for me:", repr(text[:60]))
                return
            self.engaged_until = now + float(behavior.get("conversation_seconds", 90))

        # He trailed off mid-thought. Wait for the rest instead of sending a fragment.
        for _ in range(3):
            if not self.INCOMPLETE.search(text.strip()):
                break
            more = self.listen_once(wait_seconds=3.5)
            if not more:
                break
            text = f"{text.rstrip(' ,.')} {more.lstrip()}"
            self.log("stitched continuation")

        # A sentence can also end cleanly and still not be finished, because he
        # paused for breath. Give a long thought a moment to carry on, and join
        # anything that opens like the rest of it. Short commands go at once.
        # A sentence with no full stop on the end was still being spoken. Give
        # that far longer to continue than something that sounds finished:
        # "That one was" was sent on its own after a second and a half.
        settled = float(behavior.get("continuation_grace_seconds", 1.4))
        unfinished_grace = float(behavior.get("unfinished_grace_seconds", 3.5))
        for _ in range(5):
            grace = (unfinished_grace if not text.rstrip().endswith((".", "!", "?"))
                     else settled)
            # A fragment needs the wait more than a sentence does, not less:
            # "my" was being sent on its own because only five words or more
            # earned a pause. An exact command is the one thing that should go
            # immediately.
            if grace <= 0 or commands.parse(text).kind != "freeform":
                break
            more = self.listen_once(wait_seconds=grace)
            if not more:
                break
            # A command is always its own instruction, however it starts.
            # What came before it is kept as conversation rather than typed in:
            # she decides what gets sent, and she has not seen this yet.
            if commands.parse(more).kind != "freeform":
                self.remember("user", text)
                text = more
                break
            # Unfinished punctuation is the strongest signal there is: a
            # recogniser ends a finished sentence with a stop, and leaves one
            # off when the speaker was still going.
            unfinished = not text.rstrip().endswith((".", "!", "?"))
            if unfinished or self.CONTINUES.match(more):
                text = f"{text.rstrip('.')} {more.lstrip()}"
                self.log("joined a pause")
                continue
            # A genuinely new sentence. The one before it becomes conversation
            # rather than going straight to a session — he builds a request
            # across several sentences and she reads all of them.
            self.remember("user", text)
            text = more
            break
        someone_else = commands.meant_for_someone_else(text)
        if someone_else:
            self.log(f"for {someone_else}, not for her:", repr(text[:50]))
            return

        intent = commands.parse(text)
        self.log("heard:", intent.kind, repr(text))

        if self.simple():
            simple.turn(self, text)
            return

        # Ask what he meant before falling back to guessing from the words.
        if understand.available(self.config):
            decision = understand.decide(
                self.config, text, self.session_names_for_intent(),
                self.short_name() or self.bound_name or "",
                dialogue=list(self.dialogue),
                barred=list(self.config.section("behavior").get("hands_off", []) or []),
                mine=str(self.config.section("behavior").get("own_project", "Ameka")),
                last_news=self.last_news)
            action = decision.get("action", "")
            if action:
                self.log(f"understood: {action}"
                         + (f" -> {decision['target']!r}" if decision.get("target") else "")
                         + (f" ({decision['why']})" if decision.get("why") else ""))
                self.remember("user", text)
                handled = self.act_on(decision, text, behavior)
                if handled:
                    return

        # Only three things are about Ameka rather than about your work: where
        # the microphone points, and whether it is awake. Typing those into a
        # session would be nonsense. Everything else is typed, so you can see
        # what landed instead of guessing.
        if intent.kind in ("switch", "mute", "unmute"):
            return self.control(intent)

        if intent.kind == "freeform":
            wanted = commands.verbosity_request(text)
            if wanted:
                self.verbosity = wanted
                if not self.is_conversation():
                    self.deliver_text(text, behavior, quiet=True)
                return self.speak({"short": "Just the headline from now on.",
                                   "full": "I will read the whole point.",
                                   "everything": "I will read you everything."}[wanted])

        asked_about_me = commands.about_ameka(text) if intent.kind == "freeform" else ""
        if asked_about_me:
            if not self.is_conversation():
                self.deliver_text(text, behavior, quiet=True)
            return self.speak(self.answer_about_me(asked_about_me))

        if intent.kind == "freeform" and commands.CONNECT_BROWSER.search(text):
            if not self.is_conversation():
                self.deliver_text(text, behavior, quiet=True)
            return self.connect_browser()

        if intent.kind == "freeform" and commands.NAME_THEM.search(text):
            if not self.is_conversation():
                self.deliver_text(text, behavior, quiet=True)
            return self.say_sessions(full=True)

        if intent.kind == "freeform" and commands.LIST_SESSIONS.search(text):
            if not self.is_conversation():
                self.deliver_text(text, behavior, quiet=True)
            return self.say_sessions()

        if intent.kind == "freeform":
            starting = commands.START_SESSION.match(text)
            if starting and len(text.split()) <= 7:
                self.deliver_text(text, behavior, quiet=True)
                return self.start_session(starting.group(1))
            # Or asked in passing, mid-sentence. Here the name has to be a real
            # project before she acts on it: half of what sounds like a request
            # to open something is just a sentence with "open" in it.
            asked = commands.START_SESSION_IN.search(text)
            if asked and session_tools.resolve_project(asked.group(1)):
                self.deliver_text(text, behavior, quiet=True)
                return self.start_session(asked.group(1))

        if intent.kind in ("status", "repeat", "more", "skip"):
            if not self.is_conversation():
                self.deliver_text(text, behavior, quiet=True)
            return self.control(intent)

        return self.deliver_text(text, behavior)

    @staticmethod
    def project_of(label: str) -> str:
        """The project out of a session title.

        "Voice control for ChatGPT and Claude sessions, in Ameka" is nine words
        of preamble before she has said anything. He called it Ameka.
        """
        name = (label or "").strip()
        if ", in " in name:
            name = name.rsplit(", in ", 1)[1]
        return name.strip(" .") or label

    def short_name(self) -> str:
        """The project, not the whole session title. "Okay, Ameka" beats
        "Okay, Voice control for ChatGPT and Claude sessions, in Ameka"."""
        name = self.bound_name or ""
        if ", in " in name:
            name = name.rsplit(", in ", 1)[1]
        return name if len(name.split()) <= 3 else ""

    def is_conversation(self) -> bool:
        """Is the microphone aimed at a chat rather than a coding session?

        Typing "what sessions are running" into a Claude Code session is
        harmless noise; typing it into a research conversation puts a question
        to the model that was never meant for it.
        """
        target = self.phone_bound or self.bound or self.default_target()
        return bool(target.get("cdp_tab_id"))

    def deliver_text(self, text: str, behavior: dict, quiet: bool = False,
                     verbatim: bool = False) -> None:
        # Send what was said. Turning "Okay" into "proceed" meant a session
        # received a word its owner never spoke, and made a mis-hearing
        # impossible to spot: you say one thing and see another arrive.
        # An instruction she composed is exempt: she wrote it to be sent, so
        # parsing it as though it were speech would only damage it.
        if verbatim:
            payload = text.strip()
            if not payload:
                return
        else:
            intent = commands.parse(text)
            payload = intent.text if intent.kind == "freeform" and intent.text else text.strip()
            if intent.kind == "freeform" and len(payload.split()) < int(behavior.get("chat_min_words", 1)):
                self.log("ignored fragment:", repr(payload))
                return

        self.follow_focus()
        target = self.bound or self.default_target()
        ok, how = route.deliver(target, payload)
        self.log(f"sent {payload!r} -> {how}" if ok else f"delivery failed: {how}")
        if not ok:
            self.speak("I could not reach that session.")
            return
        # Say something immediately, the way a person does, rather than leaving
        # a silence until the session finishes.
        if behavior.get("acknowledge", True) and not quiet:
            self.speak(acks.took_it(self.short_name()))
        else:
            audio.earcon(True)
        # A quiet send is her own doing — a fault report into the session that
        # holds her code. Nobody is waiting on that, and "still working…
        # nothing back yet…" every forty-five seconds about it, at midnight,
        # is her worrying out loud on his behalf about something he did not ask.
        if not quiet:
            self.waiting_since = time.time()
        time.sleep(float(behavior.get("chat_pause_after_send", 1.0)))

    def control(self, intent: commands.Intent) -> None:
        if intent.kind == "mute":
            self.speak("Asleep. Say Ameka to wake me.")
            self.set_muted(True)
        elif intent.kind == "unmute":
            self.set_muted(False)
            self.speak("Back.")
        elif intent.kind == "status":
            with self.lock:
                names = [e.project for e in self.queue]
            here = self.bound_name or route.describe(self.bound or self.default_target())
            if names:
                self.speak(f"You are in {here}. Waiting: {', '.join(names)}.")
            else:
                self.speak(f"You are in {here}. Nothing else waiting.")
        elif intent.kind == "switch":
            self.switch_to(intent.text)
        elif intent.kind == "repeat" and self.last_spoken:
            self.speak(self.last_spoken)
        elif intent.kind == "more" and self.current:
            self.speak(self.detail(self.current))

    def connect_browser(self) -> None:
        """Restart Chrome with its debug port so browser tabs join the loop."""
        if browser.cdp_up():
            self.speak("The browser is already connected.")
            return
        self.speak("Restarting Chrome. Your tabs come back.")
        if browser.enable_cdp():
            count = len(browser.ai_tabs())
            self.speak(f"Browser connected. {count} chat tab{'s' if count != 1 else ''} in the loop.")
            self.log(f"CDP enabled, {count} AI tabs")
        else:
            self.speak("Chrome did not come back with the port open.")

    def from_phone(self, text: str) -> str:
        """A turn spoken into an iPhone. Returns what Ameka should say back.

        The Mac stays silent throughout: you are not in the room, and speaking
        would block this loop for the seconds it takes to synthesise.
        """
        said: list[str] = []
        speak_aloud, self.speak = self.speak, lambda line: said.append(line)
        try:
            return self._phone_turn(text) or (" ".join(said) if said else "")
        finally:
            self.speak = speak_aloud

    def _phone_turn(self, text: str) -> str:
        if not text:
            return "I did not catch that."

        addressed, remainder = commands.wake_split(text)
        if addressed and remainder:
            text = remainder
        intent = commands.parse(text)
        self.log("phone:", intent.kind, repr(text))

        # The phone knows every command the microphone does.
        if intent.kind == "freeform":
            if commands.LIST_SESSIONS.search(text):
                return (self.session_names() if commands.NAME_THEM.search(text)
                        else self.session_report())
            if commands.CONNECT_BROWSER.search(text):
                self.connect_browser()
                return self.last_spoken or "Browser connected."
            starting = commands.START_SESSION.match(text)
            if starting and len(text.split()) <= 7:
                return session_tools.start(starting.group(1))[1]
        if intent.kind == "mute":
            self.set_muted(True)
            return "Asleep. Say Ameka to wake me."
        if intent.kind == "unmute":
            self.set_muted(False)
            return "Listening."

        if intent.kind == "switch":
            before = (self.bound, self.bound_name)
            self.switch_to(intent.text)
            if self.config.section("phone").get("separate_binding", True) and self.bound != before[0]:
                # The phone points itself; the microphone in the room keeps
                # pointing where it was.
                self.phone_bound, self.phone_bound_name = self.bound, self.bound_name
                self.bound, self.bound_name = before
                return f"Talking to {self.phone_bound_name}."
            return self.last_spoken or f"Talking to {self.bound_name}."
        if intent.kind == "status":
            with self.lock:
                names = [e.project for e in self.queue]
            return (f"You are in {self.bound_name or 'the front session'}. "
                    + (f"Waiting: {', '.join(names)}." if names else "Nothing else waiting."))
        if intent.kind == "repeat":
            return self.last_spoken or "Nothing to repeat."
        if intent.kind == "more" and self.current:
            return self.detail(self.current)

        payload = self.config.section("behavior")["reply_affirm"] if intent.kind == "affirm" else (
            intent.text if intent.kind == "freeform" and intent.text else text.strip())
        target = self.phone_bound or self.bound or self.default_target()
        ok, how = route.deliver(target, payload)
        self.log(f"phone sent {payload!r} -> {how}" if ok else f"phone delivery failed: {how}")
        if not ok:
            return "I could not reach that session."
        return f"Sent to {self.phone_bound_name or self.bound_name or route.describe(target)}."

    def render_speech(self, text: str) -> bytes | None:
        """Ameka's own voice as a WAV, for the phone to play."""
        if not text:
            return None
        cfg = self.config.section("voice")
        if not engines.kokoro_available():
            return None
        try:
            voice = audio.kokoro_voice(cfg, None)
            samples, rate = voice.synth(engines.respell(text, cfg.get("pronounce")))
            return phone.wav_bytes(samples, rate)
        except Exception as exc:
            self.log("speech render failed:", repr(exc))
            return None

    def answer_about_me(self, kind: str) -> str:
        """Questions about Ameka get an answer, not a shrug."""
        if kind == "hearing":
            level = getattr(audio, "_LAST_PEAK", 0.0)
            if level >= 0.08:
                return "Yes, loud and clear."
            if level > 0:
                return "Yes, though you are quiet."
            return "Yes, I can hear you."
        if kind == "alive":
            return f"Here. Listening to {self.bound_name or 'whatever is in front of you'}."
        if kind == "where":
            return f"You are in {self.bound_name or route.describe(self.bound or self.default_target())}."
        if kind == "doing":
            if self.last_brief.get("takeaway"):
                return f"Last thing: {self.last_brief['takeaway']}"
            return "Nothing since you last spoke."
        if kind == "identity":
            from .identity import spoken_summary

            return spoken_summary(tools.summary())
        if kind == "microphone":
            return f"The {audio.default_input_name() or 'default microphone'}."
        return "I am here."

    def running_sessions(self) -> dict[str, list[str]]:
        """Everything running, grouped by which tool it belongs to."""
        # Every label says both the tool and where it is running. "ChatGPT" on
        # its own is ambiguous: Codex is ChatGPT too, and so is a browser tab.
        groups: dict[str, list[str]] = {
            "Claude Code": [], "Codex": [],
            "ChatGPT in the browser": [], "Claude in the browser": [],
        }
        try:
            panes = session_tools.tmux_pane_agents()
        except Exception:
            panes = {}

        for entry in session_tools.live_sessions():
            groups["Claude Code"].append(entry["project"])
        for name, (_pane, agent) in panes.items():
            bucket = agent or "Claude Code"
            if name not in groups.get(bucket, []):
                groups.setdefault(bucket, []).append(name)
        try:
            recent = len(codex_watch.latest_files())
        except Exception:
            recent = 0
        if recent and not groups["Codex"]:
            groups["Codex"] = [f"{recent} recent"]

        try:
            tabs = browser.ai_tabs()
        except Exception:
            tabs = []
        cached = list(self.tab_names)
        for index, tab in enumerate(tabs):
            name = cached[index] if index < len(cached) else browser.project_name(tab)
            where = ("ChatGPT in the browser" if "chatgpt" in tab.get("host", "")
                     else "Claude in the browser")
            groups[where].append(name)
        return {tool: names for tool, names in groups.items() if names}

    def session_report(self) -> str:
        groups = self.running_sessions()
        if not groups:
            return "Nothing running."
        total = sum(len(names) for names in groups.values())
        parts = [f"{len(names)} in {tool}" for tool, names in groups.items()]
        try:
            answerable = len(session_tools.tmux_panes()) + len(browser.ai_tabs())
        except Exception:
            answerable = 0
        line = f"{total} running: " + ", ".join(parts) + "."
        if answerable:
            line += f" {answerable} answerable by name."
        return line + " Say name them for the list."

    def session_names(self) -> str:
        """Every name, under the tool it belongs to."""
        groups = self.running_sessions()
        if not groups:
            return "Nothing running."
        return " ".join(f"{tool}: {', '.join(names)}." for tool, names in groups.items())

    def say_sessions(self, full: bool = False) -> None:
        self.speak(self.session_names() if full else self.session_report())

    def session_names_for_intent(self) -> list[str]:
        """Short descriptions of what is running, for the model to choose from."""
        try:
            names = []
            for kind, items in self.running_sessions().items():
                for item in items:
                    names.append(f"{item} ({kind})")
            return names
        except Exception:
            return []

    def act_on(self, decision: dict, text: str, behavior) -> bool:
        """Carry out what she decided. False means fall back to patterns.

        Nothing reaches a session unless she chose to send it. She used to type
        every sentence in as well as acting on it, so thinking out loud arrived
        in Claude Code as an instruction and a half-finished thought arrived as
        a command. Talking to her is now talking to her, and sending is a
        judgement she makes out of the whole conversation.
        """
        action = decision.get("action", "")
        target = decision.get("target", "")
        say = decision.get("say", "")

        if action == "talk":
            if say:
                self.speak(say)
                self.remember("ameka", say)
            return True
        if action == "send":
            barred = target or self.bound_name or ""
            said_where = target
            if session_tools.hands_off(self.config, barred):
                self.log("hands off:", repr(barred))
                self.speak(f"I am staying out of {barred}. Nothing sent.")
                return True
            # Named a session to send it to: go there first, and only send if
            # she actually got there. Claiming to send somewhere and writing
            # somewhere else puts his work in the wrong session and tells him
            # it went to the right one, which is worse than not sending at all.
            if target:
                if not self.switch_to(target):
                    self.log("could not reach", repr(target), "— not sending")
                    self.speak(f"I could not get to {said_where}. It is not the "
                               f"session on screen, so I have not sent that "
                               f"anywhere. Open it and say it again.")
                    return True
                time.sleep(0.2)
            # Her prose, not his words. She wrote it to be sent.
            self.deliver_text(decision.get("message") or text, behavior,
                              quiet=bool(say), verbatim=True)
            if say:
                self.speak(say)
                self.remember("ameka", say)
            return True
        if action == "nothing":
            self.log("not for her:", repr(text[:50]))
            return True
        if action == "start_session":
            if session_tools.hands_off(self.config, target):
                self.log("hands off:", repr(target))
                self.speak(f"I am staying out of {target}.")
                return True
            if not target or not session_tools.resolve_project(target):
                return False                      # let the patterns have a go
            self.start_session(target)
            return True
        if action == "switch":
            if not target:
                return False
            self.control(commands.Intent("switch", text=target, heard=text))
            return True
        if action in ("list_sessions", "name_sessions"):
            self.say_sessions(full=(action == "name_sessions"))
            return True
        if action == "connect_browser":
            self.connect_browser()
            return True
        if action == "about":
            answer = self.answer_about_me(commands.about_ameka(text) or text)
            self.speak(answer)
            self.remember("ameka", answer)
            return True
        if action in ("sleep", "wake"):
            self.control(commands.Intent("mute" if action == "sleep" else "unmute",
                                         heard=text))
            return True
        if action in ("repeat", "status", "more", "skip"):
            self.control(commands.Intent(action, heard=text))
            return True
        return False

    def remember(self, who: str, what: str) -> None:
        """Keep the conversation. She composes out of more than one line, and
        the operative part of a request is often three sentences back."""
        what = (what or "").strip()
        if what:
            self.dialogue.append((who, what[:400]))

    @staticmethod
    def once(takeaway: str, ask: str) -> str:
        """The takeaway and the question, without saying either of them twice.

        A summary that ends in a question and a question field holding the same
        words is read out as "...want me to track this progress for you? track
        this progress for you?" — which is what being repetitive sounds like.
        The instruction not to ask in the takeaway belongs in the prompt and is
        there, but a prompt is a request and this is a guarantee.
        """
        takeaway, ask = (takeaway or "").strip(), (ask or "").strip()
        if not ask:
            return takeaway
        if not takeaway:
            return ask
        words = lambda t: set(re.findall(r"[a-z0-9']+", t.lower()))
        theirs = words(ask)
        if theirs and len(theirs & words(takeaway)) / len(theirs) >= 0.7:
            return takeaway            # already said, in the same words
        if takeaway.rstrip().endswith("?"):
            return takeaway            # already asked something
        return f"{takeaway} {ask}"

    def start_session(self, project: str) -> None:
        ok, message = session_tools.start(project)
        self.speak(message)
        if ok:
            self.log("started session:", message)

    @staticmethod
    def _kind_of(target: dict) -> str:
        if target.get("cdp_tab_id"):
            return "browser"
        if target.get("tmux_pane"):
            return "terminal"
        return "app"

    @staticmethod
    def _tool_filter(wanted: str) -> tuple[str, str | None]:
        """Strip a named tool off the request and return what to search."""
        rules = [
            ("browser", ("chatgpt", "chat gpt", "browser", "the browser", "tab", "web")),
            ("app", ("claude code", "claude app", "the app")),
            ("terminal", ("terminal", "tmux", "codex")),
        ]
        for kind, phrases in rules:
            for phrase in phrases:
                if wanted == phrase:
                    return "", kind
                for shape in (f" in {phrase}", f" on {phrase}", f" in the {phrase}", f"{phrase} "):
                    if shape in f"{wanted} ":
                        return wanted.replace(shape.strip(), "").strip(), kind
        return wanted, None

    def switch_to(self, name: str) -> bool:
        """Point the mic at another session by project name. Did it get there?

        Pointing herself at a session and reaching it are different things. In
        the app, typing goes to whichever session is on screen, so saying "I am
        sending that to the billing service" and then writing into whatever
        happened to be in front is not a near miss — it is putting his work in
        the wrong place while telling him otherwise.

        Anything running counts, not only what has spoken to you: a session
        started five minutes ago has no briefings behind it yet.
        """
        wanted = name.strip().lower()
        known = dict(self.sessions)

        panes = {}
        try:
            panes = session_tools.tmux_panes()
            for project, pane in panes.items():
                known.setdefault(project.lower(), {"tmux_pane": pane, "label": project})
            for entry in session_tools.live_sessions():
                project = entry["project"]
                if project.lower() in known:
                    continue
                if project in panes:
                    known[project.lower()] = {"tmux_pane": panes[project], "label": project}
                else:
                    # In the desktop app. Typing reaches whichever session is
                    # in front, so the name is kept: switching has to bring
                    # that one forward or the next thing said lands in the
                    # session he happens to be looking at instead.
                    known[project.lower()] = {
                        "bundle_id": (self.config.section("behavior").get("default_target", "")
                                      or tools.default_target()),
                        "label": project,
                        "app_session": entry["name"],
                        "title": session_tools.session_title(entry.get("cwd", ""),
                                                             entry.get("session_id", "")),
                    }
        except Exception:
            pass
        try:
            for tab in browser.ai_tabs():
                label = browser.project_name(tab)
                known.setdefault(label.lower(), {"cdp_tab_id": tab["id"], "label": label})
        except Exception:
            pass

        # Name the tool to settle it: one project name can be both a Claude
        # Code project and a ChatGPT conversation, and picking whichever was
        # found first is a coin toss.
        wanted, only = self._tool_filter(wanted)
        candidates = {name: target for name, target in known.items()
                      if only is None or self._kind_of(target) == only}
        if not candidates:
            candidates = known

        # "my billing service" and "my-billing-service" are the same
        # thing said aloud and written in a folder name.
        squash = lambda t: "".join(ch for ch in t.lower() if ch.isalnum())
        target_key = squash(wanted)

        match = None
        if not wanted and only:
            match = next(iter(candidates.items()), None)
        for rank in ("exact", "starts", "contains"):
            if match:
                break
            for project, entry in candidates.items():
                key = squash(project)
                if rank == "exact" and key == target_key:
                    match = (project, entry)
                elif rank == "starts" and target_key and key.startswith(target_key):
                    match = (project, entry)
                elif rank == "contains" and target_key and (target_key in key or key in target_key):
                    match = (project, entry)
                if match:
                    break

        if not match:
            self.sessions = known
            known_names = ", ".join(sorted(known)[:8]) or "nothing yet"
            self.speak(f"I cannot find {name}. I know: {known_names}.")
            return False

        self.sessions = known
        self.bound = match[1]
        self.bound_name = match[1].get("label") or match[0]
        self.chosen_by_voice = True       # hold this until you look elsewhere
        # A pane or a browser tab is addressed exactly and always reached. The
        # app is the one that has to be shown before anything typed lands in it.
        reached = self._kind_of(self.bound) != "app"
        if self.config.section("behavior").get("reveal_on_switch", True):
            try:
                route.reveal(self.bound)   # show me what I am talking to
            except Exception:
                pass
            # And in the app, the session itself, not merely the app.
            if self.bound.get("app_session"):
                try:
                    # The title first: it is the one name that is the session
                    # row and not the project header drawn above it.
                    shown = session_tools.focus_in_app(
                        self.bound.get("title", ""),
                        self.bound["app_session"], self.bound.get("label", ""),
                        self.project_of(self.bound.get("label", "")))
                    self.log(f"brought {self.bound['app_session']} forward"
                             if shown else
                             f"could not find {self.bound['app_session']} on screen")
                    reached = shown
                except Exception as exc:
                    self.log("focus failed:", repr(exc))
                    reached = False
        try:
            self.focus_seen = route.frontmost()
        except Exception:
            pass
        where = self.project_of(self.bound_name)
        self.speak(f"Talking to {where}." if reached else
                   f"I can see {where} but I cannot bring it up on screen.")
        return reached

    def engine_report(self) -> dict:
        voice = self.config.section("voice")
        listen = self.config.section("listen")
        return {
            "voice": "kokoro" if engines.kokoro_available() else (
                "openai" if self.config.account("openai", voice.get("account", "default")).usable
                else "macos"),
            "hearing": "whisper" if engines.whisper_available() else (
                "openai" if self.config.account("openai", listen.get("account", "default")).usable
                else "none"),
        }

    def follow_focus(self) -> None:
        """Send what you say to whatever you are actually looking at.

        Switching windows by hand should move the microphone with it, rather
        than leaving it pointed at whichever session last spoke.
        """
        if not self.config.section("behavior").get("follow_focus", True):
            return
        try:
            app, url = route.frontmost()
        except Exception:
            return
        if not app:
            return

        signature = (app, url)
        moved = bool(self.focus_seen) and signature != self.focus_seen
        self.focus_seen = signature
        if self.chosen_by_voice and not moved:
            return                        # you named this one; hold it
        if moved:
            self.chosen_by_voice = False

        if app == "Google Chrome" and url:
            for tab in browser.ai_tabs():
                if tab.get("url") == url:
                    name = browser.project_name(tab)
                    if self.bound.get("cdp_tab_id") != tab["id"]:
                        self.log(f"following you to {name}")
                    self.bound = {"cdp_tab_id": tab["id"], "label": name}
                    self.bound_name = name
                    self.sessions[name.lower()] = self.bound
                    return
            return                      # a Chrome tab that is not a conversation

        bundle = {"Claude": "com.anthropic.claudefordesktop",
                  "Terminal": "com.apple.Terminal",
                  "iTerm2": "com.googlecode.iterm2",
                  "ChatGPT": "com.openai.chat"}.get(app)
        if bundle and self.bound.get("bundle_id") != bundle:
            self.log(f"following you to {app}")
            self.bound = {"bundle_id": bundle, "app_name": app}
            self.bound_name = app

    def default_target(self) -> dict:
        """Whatever this Mac actually has, rather than an assumption."""
        bundle = self.config.section("behavior").get("default_target", "") or tools.default_target()
        return {"bundle_id": bundle} if bundle else {}

    # A clause that opens like this is the rest of the previous sentence, not a
    # new instruction: "…that will override it as well." / "is priority."
    CONTINUES = re.compile(
        r"^\s*(?:is|are|was|were|and|or|but|so|then|which|that|because|since|"
        r"though|although|as well|not|with|without|"
        r"for|to|in|on|at|of|from|by|like|about|after|before|while|when|if)\b",
        re.I,
    )

    INCOMPLETE = re.compile(
        r"\b(and|or|but|so|so that|because|which|that|the|a|an|to|of|for|with|"
        r"about|if|when|like|is|are|was|were|it'?s|i'?m|we|you|my|not just|then|"
        r"actually|really|basically|only|just|both|any|every|all|some|"
        r"in|on|at|from|into|onto|over|under|through|toward|towards)\s*[,.]?$",
        re.I,
    )

    def listen_once(self, wait_seconds: float | None = None) -> str:
        cfg = self.config.section("listen")
        if not stt_ready(self.config):
            time.sleep(1.0)
            return ""
        # Whichever microphone is here. Naming AirPods and then not wearing
        # them means the laptop microphone, not silence — she listens on
        # whatever there is, and says which when it changes under her.
        using = audio.active_input_name(self.config)
        if using != self._mic_name:
            if self._mic_name:
                self.log(f"microphone changed to {using or 'the default device'}")
            self._mic_name = using
        prefix, prefix_rate = self.interrupted_with
        self.interrupted_with = (b"", 0)
        if prefix:
            self.log(f"carried over {len(prefix) / 2 / max(prefix_rate, 1):.1f}s "
                     "from the interruption")
        pcm = audio.record_until_silence(self.config, wait_seconds=wait_seconds,
                                         prefix=prefix, prefix_rate=prefix_rate)
        if not pcm:
            return ""
        # Asleep means asleep: no blip, or it sounds like it is still taking
        # everything down.
        if cfg.get("earcon", True) and not self.muted:
            audio.earcon(False)
        speechlike, why = audio.looks_like_speech(pcm)
        if not speechlike:
            self.log("not speech:", why)
            return ""
        total = len(pcm) / 32000.0
        talking = getattr(audio, "_LAST_SPEECH_SECONDS", 0.0) or total
        text = audio.transcribe(self.config, pcm)
        # Her own voice, a beat after she stopped. The speakers keep sounding
        # and the room keeps ringing for a moment after the audio ends, so the
        # next thing she listens to opens on the tail of her own last sentence.
        # She then answers herself, and the turn he was waiting for is gone —
        # which from where he is sitting is being cut off.
        if text and self.her_own_voice(text, self.last_spoken or ""):
            self.log("that was my own tail, not him:", repr(text[:50]))
            return ""
        words = len(text.split())
        pace = words / talking if talking else 0
        # Only the speaking part counts. Measured against the whole capture,
        # the pause at the end made ordinary speech look like lost words.
        flag = " <- fewer words than that much speech suggests" if talking > 2.5 and pace < 1.0 else ""
        self.log(f"captured {total:.1f}s ({talking:.1f}s talking) -> {words} words "
                 f"({pace:.1f}/s) [{getattr(audio, '_LAST_STT', '?')}]{flag}")
        self.notice_if_deaf(words, talking)
        return text

    def notice_if_deaf(self, words: int, talking: float) -> None:
        """Say so when she can hear him and not make him out.

        Four times in five minutes she caught a sentence's worth of sound and
        two recognisers found nothing in it, while the one thing she did make
        out was the television. His AirPods were on his phone, she was on the
        laptop microphone across the room, and the game was louder than he
        was. From where he sat she was ignoring him. Silence is the wrong
        answer to that; the reason is the right one, once.
        """
        now = time.time()
        if words == 0 and talking >= 0.7:
            self.unheard = [t for t in self.unheard if now - t < 180] + [now]
        elif words > 0:
            self.unheard = []
            return
        if len(self.unheard) < 3 or now - self.said_cant_hear < 600 or self.muted:
            return
        self.said_cant_hear = now
        self.unheard = []
        if audio.on_room_mic(self.config):
            self.speak("I can hear you talking but I cannot make out the words. I am on the "
                       "laptop microphone and the room is loud. Put your AirPods in, or come closer.")
        else:
            self.speak("I can hear you talking but I cannot make out the words. "
                       "Try again, a little louder.")

    def handle(self, event: Event) -> None:
        self.current = event
        self.waiting_since = 0.0
        # Remember how to reach it, but do not repoint the microphone: what you
        # are looking at, or last asked for, outranks whatever happened to
        # finish first.
        if any(event.target.get(k) for k in ("bundle_id", "tmux_pane", "cdp_tab_id", "app_name")):
            self.sessions[event.project.lower()] = event.target
            if event.title:
                self.sessions[" ".join(event.title.lower().split())] = event.target
            if not self.bound:
                self.bound, self.bound_name = event.target, event.label
            if event.title:
                self.sessions[" ".join(event.title.lower().split())] = event.target
        # A scheduled job that has already decided not to interrupt him has
        # decided. She read eleven of these out loud — tags and all, twenty-
        # seven seconds of "heartbeat, automation id,
        # a-long-hyphenated-job-name" — and every one carried
        # <decision>DONT_NOTIFY</decision> in the very text she was reading.
        told = event.assistant or ""
        if re.search(r"DONT_?NOTIFY", told, re.I):
            self.log("the job asked not to interrupt:", repr(told[:60]))
            return
        if self.simple():
            if self.will_hold(event):
                return
            simple.brief(self, event)
            return
        if corrections.mostly_machinery(told):
            self.log("machinery, not news:", repr(told[:60]))
            return
        # If it is worth saying, say the message and not the plumbing round it.
        inside = re.search(r"<message>(.*?)</message>", told, re.S)
        if inside:
            event.assistant = inside.group(1).strip()
        brief = summarize.briefing(self.config, {
            "source": event.source, "cwd": event.cwd,
            "assistant": event.assistant, "user": event.user, "note": event.note,
        })
        self.last_brief = brief
        self.log(f"brief [{event.project}]", json.dumps(brief, ensure_ascii=False))

        behavior = self.config.section("behavior")
        why = ""
        if behavior.get("speak_when", "worthwhile") == "worthwhile":
            # It read the turn, so it is better placed than a rule to say
            # whether this is worth interrupting him for — except that it said
            # yes to the same status nine times in an hour. So the
            # rules are a veto now: a session narrating its way through the
            # work is not news, whatever the model thinks.
            rules_ok, why = worth.verdict(brief, event.assistant)
            if "say" in brief:
                if not brief["say"]:
                    self.log(f"nothing worth saying — {brief['takeaway'][:60]}")
                    return
                if not rules_ok and why == "it is narrating":
                    self.log(f"the model said yes but {why} — {brief['takeaway'][:60]}")
                    return
            elif not rules_ok:
                self.log(f"stayed quiet ({why}) — {brief['takeaway'][:60]}")
                return
        if self.old_news(event.project, brief, why):
            return

        # Name the session only when it is not the one that spoke last. Saying
        # it every time is noise: you already know where you are.
        prefix = ""
        if self.config.section("behavior").get("announce_project", True):
            with self.lock:
                waiting = len(self.queue)
            # Only when it is not where he already is. Reading the session
            # name out before every line is how she started sounding like a
            # switchboard rather than a person.
            elsewhere = event.label != (self.bound_name or "")
            changed = event.label != self.last_announced
            # Only when it is a different session from the last one. The queue
            # being busy is not a reason to say where this came from: one
            # session wrote a turn every few seconds, so the queue was never
            # empty, so she read the whole title out before every line. Fifty
            # of her spoken lines opened with "Voice control for".
            if changed and elsewhere:
                name = self.project_of(event.label)
                prefix = (f"{name}, and {waiting} more waiting. " if waiting
                          else f"{name}. ")
            self.last_announced = event.label
        length = self.verbosity or self.config.section("behavior").get("briefing", "full")
        body = brief.get(
            {"short": "takeaway", "full": "full", "everything": "everything"}.get(length, "full"))
        line = f"{prefix}{self.once(body or brief['takeaway'], brief['ask'])}"
        # The microphone is hers for every second she speaks, and she was
        # speaking for twenty-seven at a stretch, back to back.
        cap = int(self.config.section("behavior").get("briefing_words", 55))
        spoken = line.split()
        if len(spoken) > cap:
            line = " ".join(spoken[:cap]).rstrip(",;:- ") + "."
            self.log(f"briefing trimmed from {len(spoken)} words to {cap}")
        self.speak(line)
        self.briefed_at = time.time()
        self.last_news = {"tool": {"codex": "Codex, inside the ChatGPT app",
                                   "claude-code": "Claude Code"}.get(event.source, event.source),
                          "project": event.project, "when": time.time(),
                          "takeaway": brief.get("takeaway", "")}
        if self.relay is not None:
            self.relay.send(line)
        if self.chat:
            return                       # the open mic in run() takes it from here
        if self.muted or not self.config.section("behavior")["auto_listen"]:
            return
        self.converse(event, brief)

    def converse(self, event: Event, brief: dict, depth: int = 0) -> None:
        if depth > 3:
            return
        intent = self.hear()
        self.log("heard:", intent.kind, repr(intent.heard))

        if intent.kind == "affirm":
            self.send(event, self.config.section("behavior")["reply_affirm"])
        elif intent.kind == "deny":
            self.speak("Holding.")
        elif intent.kind == "repeat":
            self.speak(self.once(brief['takeaway'], brief['ask']))
            self.converse(event, brief, depth + 1)
        elif intent.kind == "more":
            self.speak(self.detail(event))
            self.converse(event, brief, depth + 1)
        elif intent.kind == "skip":
            self.speak("Skipped.")
        elif intent.kind == "mute":
            self.speak("Asleep. Say Ameka to wake me.")
            self.set_muted(True)
        elif intent.kind == "unmute":
            self.set_muted(False)
            self.speak("Back.")
        elif intent.kind == "status":
            with self.lock:
                pending = len(self.queue)
            self.speak(f"{pending} session{'s' if pending != 1 else ''} waiting behind this one.")
            self.converse(event, brief, depth + 1)
        elif intent.kind == "freeform" and intent.text:
            if self.config.section("behavior")["confirm_freeform"] and len(intent.text.split()) > 2:
                self.speak(f"Send: {intent.text}?")
                confirm = self.hear()
                if confirm.kind != "affirm":
                    self.speak("Dropped it.")
                    return
            self.send(event, intent.text)
        else:
            self.log("no response; leaving it")

    def hear(self) -> commands.Intent:
        cfg = self.config.section("listen")
        if not stt_ready(self.config):
            self.log("no speech-to-text available — install whisper (" + ("pip install pywhispercpp" if host.IS_WIN else "brew install whisper-cpp") + ") "
                     "or add a key (ameka key openai)")
            return commands.Intent("none")
        if cfg.get("earcon", True) and not self.muted:
            audio.earcon(True)
        pcm = audio.record_until_silence(self.config)
        if cfg.get("earcon", True) and not self.muted:
            audio.earcon(False)
        return commands.parse(audio.transcribe(self.config, pcm))

    def send(self, event: Event, text: str) -> None:
        ok, how = route.deliver(event.target, text)
        if ok:
            self.log(f"sent {text!r} via {how}")
            audio.earcon(True)
        else:
            self.log("delivery failed:", how)
            self.speak("I could not reach that session.")

    def detail(self, event: Event) -> str:
        body = (event.assistant or event.note or "").strip()
        if not body:
            return "Nothing else recorded."
        clean = " ".join(body.split())
        for token in ("```", "|", "#"):
            clean = clean.replace(token, " ")
        return clean[:600]
