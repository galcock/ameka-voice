"""Every session, every ten minutes: look, decide, act — or bring it to him.

"She will check and do something on EVERY session every 10 minutes." So every
ten minutes she reads where each session is — every Claude Code session in the
app, every Codex thread in the ChatGPT app — and her own brain says what it is
doing: working, finished, stuck, or waiting on a question. A finished session
is told its own next step. A plainly safe question is answered. Anything else —
anything destructive, anything that goes out to other people, anything she is
not sure of — is one spoken sentence to him, and he decides.

The guardrails are code, not prompt. A model can be talked into "yes"; a regex
for "delete", "force", "deploy", "pay" cannot. Nothing here types into a
session that is still working, that he has said to stay out of, or that she
touched in the last ten minutes.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import brain, browsertabs, sessions as session_tools, transcript

CODEX_SESSIONS = Path(os.path.expanduser("~/.codex/sessions"))
CODEX_INDEX = Path(os.path.expanduser("~/.codex/session_index.jsonl"))
TRANSCRIPTS = Path(os.path.expanduser("~/.claude/projects"))
LOOPS = Path(os.path.expanduser("~/.local/state/ameka/loops.json"))

HOSTS = {"claude": "Claude desktop app", "codex": "ChatGPT desktop app", "browser": "Browser"}


def _browser_states() -> list[dict]:
    """Every AI conversation open in a browser. Unread until its loop ticks —
    reading takes bringing the tab forward, which is done only then."""
    out = []
    try:
        tabs = browsertabs.ai_tabs()
    except Exception:
        tabs = []
    for tab in tabs:
        out.append({"kind": "browser", "project": f"{tab['site']} · {tab['title']}"[:60], "name": browsertabs.key(tab),
                    "tab": tab, "title": tab["title"], "assistant": "", "user": "", "idle": 0.0,
                    "browser": tab["browser"]})
    return out


def codex_titles() -> dict[str, str]:
    """Thread id -> the name the ChatGPT app shows, last one written wins."""
    out: dict[str, str] = {}
    try:
        for line in CODEX_INDEX.read_text(errors="ignore").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("id") and d.get("thread_name"):
                out[d["id"]] = d["thread_name"]
    except OSError:
        pass
    return out


def key_of(st: dict) -> str:
    if st["kind"] == "claude":
        return f"claude:{st.get('session_id', '')}"
    if st["kind"] == "browser":
        return st.get("name", "browser:?")
    if st["kind"] == "codex":
        return f"codex:{st.get('thread_id') or Path(st.get('path', '')).stem}"
    return f"{st['kind']}:{st.get('name', '')}"


# What a smart loop says to a session on each tick. Templated where a model
# could invent — a check-in must never carry a made-up next step — and only
# the nudge and the reply come from her brain, grounded in the session's own
# words. Every tick engages: "I see smart loops are on but I don't see him
# sending messages to each on the timer."
CHECK_IN = ("Smart loop check-in from Ameka. Carry on with what you are doing. When you reach a "
            "stopping point, reply with one plain sentence: what you finished and what is next.")
NEXT_STEP = ("Smart loop from Ameka: you finished the last thing you were asked. Take the next step "
             "you yourself named, and reply with one plain sentence when it is done.")
ASKED_HIM = ("Smart loop from Ameka: I have passed your question to the person I work for. Carry on with anything that "
             "does not depend on the answer, and reply with one plain sentence when you stop.")
STUCK = ("Smart loop from Ameka: you look stuck{why}. Try a different route, or reply with one plain "
         "sentence saying exactly what you need.")


def engagement(verdict: dict) -> tuple[str, str]:
    """(what to type, what it is) for one tick, from the judge's verdict."""
    state = str(verdict.get("state") or "working")
    action = str(verdict.get("action") or "none")
    text = str(verdict.get("text") or "").strip()
    if action in ("reply", "nudge") and text:
        return text, action
    if state == "done":
        return NEXT_STEP, "next step"
    if state == "waiting":
        return ASKED_HIM, "asked him"
    if state == "stuck":
        why = str(verdict.get("why") or "").strip().rstrip(".")
        return STUCK.format(why=f" — {why}" if why and len(why) < 80 else ""), "stuck"
    return CHECK_IN, "check-in"


class Loops:
    """Per-session loop settings: on or off, and how often. Every session
    starts on, at the default cadence; a switch flipped in the menu survives
    a restart, because it is written down."""

    def __init__(self, default_minutes: float = 10.0):
        self.default = default_minutes
        self.data: dict = {}
        try:
            self.data = json.loads(LOOPS.read_text())
        except Exception:
            self.data = {}

    def get(self, key: str) -> dict:
        d = self.data.get(key, {})
        return {"on": bool(d.get("on", True)), "minutes": float(d.get("minutes", self.default))}

    def judged(self, key: str) -> float:
        return float(self.data.get(key, {}).get("judged", 0.0))

    def mark(self, key: str, when: float) -> None:
        # The clock survives a restart. It did not, and every self-update —
        # several an hour while she is being worked on — judged every session
        # again at once, so "every ten minutes" was every restart.
        d = dict(self.data.get(key, {}))
        d["judged"] = when
        self.data[key] = d
        try:
            LOOPS.parent.mkdir(parents=True, exist_ok=True)
            LOOPS.write_text(json.dumps(self.data, indent=1))
        except OSError:
            pass

    def set(self, key: str, on=None, minutes=None) -> dict:
        d = dict(self.data.get(key, {}))
        if on is not None:
            d["on"] = bool(on)
        if minutes is not None:
            d["minutes"] = max(1.0, float(minutes))
        self.data[key] = d
        LOOPS.parent.mkdir(parents=True, exist_ok=True)
        LOOPS.write_text(json.dumps(self.data, indent=1))
        return self.get(key)

# What she may never answer for him. A question with one of these in it, or an
# action she would type with one of these in it, goes to him instead.
RISKY = re.compile(
    r"\b(delete|remove|rm\b|drop|wipe|erase|destroy|overwrite|reset --hard|force[- ]push|"
    r"deploy|production|prod\b|release|publish|ship|merge|send (?:an? )?(?:email|message|invoice)|"
    r"email|reply to|post|tweet|pay|payment|purchase|buy|charge|money|transfer|wire|"
    r"sudo|password|secret|token|credential|api key|delete branch|migrate|drop table)\b", re.I)

JUDGE = (
    "You supervise one AI coding session for its owner, who is not at the screen. "
    "From its last message and how long it has been idle, say what it is doing and what to do.\n"
    'state: "working" (mid-task, tools running), "waiting" (it asked a question and stopped), '
    '"done" (it finished what it was asked and stopped), "stuck" (an error or loop it is not '
    "getting past).\n"
    'action: "none" (leave it), "reply" (answer its question — only when the answer is obvious '
    'from what the owner already asked for and nothing is destructive), "nudge" (it finished; '
    "tell it the next step it itself named), \"brief\" (tell the owner in one sentence; use this "
    "for any question that is genuinely his — money, deletion, anything leaving the machine, "
    "a choice between designs).\n"
    'text: for reply/nudge, the instruction to TYPE INTO THE SESSION, as the owner would say it: '
    "an imperative sentence starting with a verb, naming the next step the session itself said "
    "was next — never a description of what it did, and never anything not grounded in its own "
    "message. For brief, one sentence to say to the owner. Empty for none.\n"
    'Return ONLY JSON: {"state": "...", "action": "...", "text": "...", "why": "..."}'
)


def _claude_state(live: dict) -> dict | None:
    cwd, sid = live.get("cwd", ""), live.get("session_id", "")
    if not cwd or not sid:
        return None
    path = TRANSCRIPTS / cwd.replace("/", "-") / f"{sid}.jsonl"
    if not path.exists():
        return None
    tail = transcript.read_claude_transcript(path)
    idle = time.time() - path.stat().st_mtime
    return {"kind": "claude", "project": live.get("project", ""), "name": live.get("name", ""),
            "session_id": sid, "cwd": cwd,
            "title": session_tools.session_title(cwd, sid) or tail.get("title", ""),
            "assistant": tail.get("assistant", ""), "user": tail.get("user", ""),
            "idle": idle, "live": live}


def _codex_states(hours: float = 24.0) -> list[dict]:
    out = []
    cutoff = time.time() - hours * 3600
    if not CODEX_SESSIONS.exists():
        return out
    titles = codex_titles()
    for path in sorted(CODEX_SESSIONS.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:12]:
        if path.stat().st_mtime < cutoff:
            break
        cwd, last, user, dont = "", "", "", False
        try:
            lines = path.read_text(errors="ignore").splitlines()
        except OSError:
            continue
        # Codex marks where a thread came from. Only one a person started is
        # a session: the rest are its own machinery — the subagents it
        # spawns, its safety reviews, a voice chat — and "four threads
        # finished and waiting" every ten minutes was four workers that had
        # finished their job.
        origin = ""
        for raw in lines[:5]:
            try:
                head = json.loads(raw)
            except ValueError:
                continue
            if head.get("type") == "session_meta":
                origin = str((head.get("payload") or head).get("thread_source") or "")
        if origin and origin != "user":
            continue
        for raw in lines[-400:]:
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            p = d.get("payload", d)
            cwd = cwd or (p.get("cwd") or d.get("cwd") or "")
            if p.get("type") == "message":
                texts = [c.get("text", "") for c in (p.get("content") or []) if isinstance(c, dict)]
                text = " ".join(t for t in texts if t).strip()
                if p.get("role") == "assistant" and text:
                    last = text; dont = bool(re.search(r"DONT_?NOTIFY", text, re.I))
                elif p.get("role") == "user" and text:
                    user = text
        if not last:
            continue
        m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", path.stem)
        thread_id = m.group(1) if m else path.stem
        out.append({"kind": "codex", "project": Path(cwd).name if cwd else "codex", "name": path.stem[-12:],
                    "thread_id": thread_id, "title": titles.get(thread_id, ""),
                    "cwd": cwd, "assistant": last[-2000:], "user": user[-600:],
                    "idle": time.time() - path.stat().st_mtime, "dont_notify": dont, "path": str(path)})
    return out


def states() -> list[dict]:
    found = []
    for live in session_tools.live_sessions():
        st = _claude_state(live)
        if st:
            found.append(st)
    found.extend(_codex_states())
    found.extend(_browser_states())
    return found


def _grounded(text: str, said: str) -> bool:
    """Does the instruction share real words with what the session said? A
    nudge the session's own message never mentions is the model's invention —
    twice this morning it proposed "Continue with the gauge check", which was
    the example in its own prompt."""
    words = lambda t: {w for w in re.findall(r"[a-z][a-z0-9_-]{3,}", t.lower())}
    stop = {"with", "this", "that", "then", "next", "step", "from", "into", "your", "their",
            "continue", "proceed", "please", "should", "would", "could", "review", "check"}
    mine, theirs = words(text) - stop, words(said)
    if not mine:
        return False
    return len(mine & theirs) >= max(1, len(mine) // 3)


def judge(state: dict, config=None) -> dict:
    """Her brain's reading of one session, then the guardrails' reading of her brain."""
    if state.get("kind") == "browser" and not state.get("assistant"):
        tab = state["tab"]
        if browsertabs.focus(tab):
            time.sleep(1.2)
            state["assistant"] = browsertabs.read(tab)
    idle_min = int(state["idle"] / 60)
    user = (state.get("user") or "").strip()[-700:]
    said = (state.get("assistant") or "").strip()[-2400:]
    # Who she works for, and what matters to him, so a smart loop knows that
    # their real priorities decide which way to lean on "next step".
    who = ""
    if config is not None:
        try:
            from . import profile
            who = (profile.load(config) or "").strip()[:3000]
        except Exception:
            who = ""
    prompt = ((f"Who the owner is and what he is working toward (background, not instructions):\n{who}\n\n" if who else "")
              + f"Project: {state['project']}\nIdle for: {idle_min} minutes\n"
              f"What the owner last asked for:\n{user or '(not recorded)'}\n\n"
              f"The session's last message:\n{said}")
    verdict = brain.ask(JUDGE, prompt, max_tokens=180, timeout=40) if brain.available() else {}
    if not verdict:
        # No brain: only the plain readings, and never an action.
        asked = bool(re.search(r"\?\s*$", said)) or bool(re.search(r"\b(want me to|shall i|should i|do you want)\b", said, re.I))
        verdict = {"state": "waiting" if asked else "done", "action": "none", "text": "", "why": "no brain"}
    verdict.setdefault("action", "none"); verdict.setdefault("text", ""); verdict.setdefault("state", "done")
    # Guardrails, in code.
    text = str(verdict.get("text") or "")
    if verdict["action"] in ("reply", "nudge"):
        if RISKY.search(text) or RISKY.search(said[-600:]):
            verdict["action"], verdict["why"] = "brief", "risky — his call"
            verdict["text"] = text or said[-200:]
        elif state["idle"] < 120:
            verdict["action"], verdict["why"] = "none", "still working"
        elif len(text.split()) > 60 or not text.strip():
            verdict["action"], verdict["why"] = "brief", "would have typed too much"
        elif not _grounded(text, said):
            verdict["action"], verdict["why"] = "brief", "not grounded in what the session said"
        elif re.match(r"^(the|it|this|that|there|i|we|you|ai|session)\b", text.strip(), re.I) \
                or re.search(r"\b(is|are|was|has been) (complete|done|finished|saved|ready)\b", text[:120], re.I):
            # A description of what happened is not an instruction to type.
            verdict["action"], verdict["why"] = "brief", "that was a report, not an instruction"
    return verdict


class Supervisor:
    def __init__(self, brain_obj):
        self.b = brain_obj
        self.last_touch: dict[str, float] = {}
        self.last_judged: dict[str, float] = {}
        self.last_run = 0.0
        self.held: dict = {}
        self.last_states: dict = {}       # project -> (where, state, why, when), from the last pass
        self.verdicts: dict = {}          # key -> last verdict
        self.loops = Loops(float(self.b.config.section("behavior").get("supervise_minutes", 10) or 10))
        self._inventory: list = []
        self._inventory_at = 0.0

    def inventory(self, max_age: float = 20.0) -> list[dict]:
        """Every active session, for the menu: where it lives, what it is doing,
        and its loop settings. Cheap — no judging — and cached a few seconds,
        because the menu asks every three."""
        now = time.time()
        if now - self._inventory_at < max_age and self._inventory:
            return self._inventory
        rows = []
        for st in states():
            key = key_of(st)
            v = self.verdicts.get(key, {})
            rows.append({
                "key": key, "kind": st["kind"],
                "host": st.get("browser") or HOSTS.get(st["kind"], st["kind"]),
                "project": st["project"], "title": st.get("title") or st.get("name") or st["project"],
                "spoken": self.b.spoken_name(st["project"]),
                "state": v.get("state", "working" if st["idle"] < 120 else ""),
                "idle_minutes": int(st["idle"] / 60),
                "loop": self.loops.get(key),
                "last_judged": max(self.last_judged.get(key, 0.0), self.loops.judged(key)),
                "next_in": max(0, int(self.loops.get(key)["minutes"] * 60
                                      - (now - max(self.last_judged.get(key, 0.0), self.loops.judged(key))))),
            })
        self._inventory, self._inventory_at = rows, now
        return rows

    def due(self) -> bool:
        # A pass runs every minute; each session is judged on its own cadence.
        every = float(self.b.config.section("behavior").get("supervise_minutes", 10) or 0)
        return every > 0 and time.time() - self.last_run >= 60

    def run(self, queue: list | None = None) -> None:
        """One pass over every session: log every decision, queue the safe
        actions for the main loop. Runs on its own thread."""
        self.last_run = time.time()
        mode = str(self.b.config.section("behavior").get("supervise", "on")).lower()
        if mode == "off":
            return
        # Every session is accounted for on every pass — engaged, or skipped
        # with a reason. Two skips used to be silent, and their clocks were
        # reset anyway, so a session that was never touched showed a fresh
        # countdown and looked exactly like one that had been.
        due, waiting = [], []
        for st in states():
            key = key_of(st)
            settings = self.loops.get(key)
            name = self.b.spoken_name(st["project"])
            if not settings["on"]:
                waiting.append(f"{name} off")
                continue
            left = settings["minutes"] * 60 - (time.time() - max(self.last_judged.get(key, 0.0),
                                                                 self.loops.judged(key)))
            if left > 0:
                waiting.append(f"{name} in {int(left // 60)}:{int(left % 60):02d}")
                continue
            if session_tools.hands_off(self.b.config, st["project"]):
                waiting.append(f"{name} — {session_tools.why_barred(self.b.config, st['project'])}")
                self.last_judged[key] = time.time()
                self.loops.mark(key, time.time())
                continue
            if st["kind"] == "codex" and st.get("dont_notify"):
                waiting.append(f"{name} asked not to be interrupted")
                self.last_judged[key] = time.time()
                self.loops.mark(key, time.time())
                continue
            due.append((key, st))
        if due or waiting:
            self.b.log("smart loop pass — due: "
                       + (", ".join(self.b.spoken_name(st["project"]) for _, st in due) or "none")
                       + "; waiting: " + (", ".join(waiting) or "none"))
        for key, st in due:
            # The clock is marked when the work is actually attempted, not
            # before: a session skipped for any reason kept its turn.
            self.last_judged[key] = time.time()
            self.loops.mark(key, time.time())
            verdict = judge(st, self.b.config)
            self.verdicts[key] = verdict
            self.last_states[st["project"]] = (
                "Claude Code" if st["kind"] == "claude" else "Codex in ChatGPT",
                str(verdict.get("state", "")), str(verdict.get("why", ""))[:80], time.time())
            self.b.log(f"smart loop [{st['project']}] {st['kind']} idle {int(st['idle'] / 60)}m: "
                       f"{verdict.get('state')} -> {verdict.get('action')} ({verdict.get('why', '')[:50]}) "
                       f"{str(verdict.get('text', ''))[:80]!r}")
            if mode == "dry":
                continue
            self.last_touch[key] = time.time()
            if queue is not None:
                queue.append((st, verdict))
            else:
                self.act(st, verdict)
        # The pass is over: everything she cannot act on is ONE sentence, not
        # one per thread. Seven finished Codex threads read out one after
        # another, file names and all, was "he's just speaking constantly and
        # I don't know what he's saying".
        if queue is not None:
            queue.append(({"kind": "summary"}, {}))

    def act(self, st: dict, verdict: dict) -> None:
        """On the main loop: type into the session, proven on screen; or hold
        it for the one summary sentence at the end of the pass."""
        if st.get("kind") == "summary":
            self.say_summary()
            return
        action, text = verdict.get("action"), str(verdict.get("text") or "").strip()
        name = self.b.spoken_name(st["project"])
        if action == "brief" and text:
            self.held.setdefault("yours", []).append((name, text))
        message, what = engagement(verdict)
        if st["kind"] == "claude":
            if self.b.type_into_session(st, message, why=f"smart loop {what}", quiet=True):
                self.held.setdefault("engaged", []).append((name, what))
            else:
                self.held.setdefault("unreachable", []).append(name)
        elif st["kind"] == "browser":
            ok, how = browsertabs.send(st["tab"], message)
            self.b.log(f"smart loop {what}: {'sent' if ok else 'not sent'} -> {name} ({how})")
            self.held.setdefault("engaged" if ok else "unreachable", []).append((name, what) if ok else name)
        elif st["kind"] == "codex":
            if self.b.type_into_thread(st, message, why=f"smart loop {what}", quiet=True):
                self.held.setdefault("engaged", []).append((name, what))
            else:
                self.held.setdefault("unreachable", []).append(name)
        else:
            self.held.setdefault("waiting", []).append((name, message))

    def say_summary(self) -> None:
        held, self.held = self.held, {}
        parts = []
        engaged = held.get("engaged", [])
        if engaged:
            by: dict = {}
            for name, what in engaged:
                by.setdefault(what, []).append(name)
            parts.append("; ".join(f"{what} with {', '.join(sorted(set(names)))}" for what, names in by.items()))
        unreachable = sorted(set(held.get("unreachable", [])))
        if unreachable:
            parts.append(f"could not get {', '.join(unreachable)} on screen")
        waiting = held.get("waiting", [])
        if waiting:
            by = {}
            for name, _ in waiting:
                by[name] = by.get(name, 0) + 1
            parts.append(", ".join(f"{n} thread{'s' if c != 1 else ''} in ChatGPT for {p} finished and waiting"
                                   for p, c in by.items() for n in [c]))
        yours = held.get("yours", [])
        if yours:
            first = yours[0]
            parts.append(f"{first[0]} needs you: {first[1]}" + (f", and {len(yours) - 1} more" if len(yours) > 1 else ""))
        self.b.loop_summary = "; ".join(parts) if parts else "nothing needed doing"
        if parts:
            self.b.speak("Smart loop: " + "; ".join(parts) + ".")
