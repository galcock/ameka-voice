"""Simple mode. One sentence per finished turn, and nothing typed anywhere he did not say.

He asked for this in so many words: "just have it read and summarize the latest
session result in one sentence, no other functions. I'll decide if I want it
to do anything, and make sure it can speak with any session correctly."

So there is no intent model in this mode. Nothing decides on his behalf whether
he was chatting, instructing, or filing a bug. A finished turn becomes one
spoken sentence with the project's name in front of it. Whatever he says back
is either a control word or a message — and a message is read back to him with
where it is going, and typed only after he says yes and only once she has that
session on screen and has checked. If she cannot get there, she says so and
types nothing.

    "billing. The nightly reconcile finished with two rows unmatched."
    "Tell billing to re-run it with the new cutoff."
    "To billing: re-run it with the new cutoff. Send it?"
    "Yes."
    "Sent to billing."
"""
from __future__ import annotations

import re
import time

from . import audio, brain, commands, local_summary, route, sessions as session_tools, summarize

ONE_SENTENCE = (
    "You turn the tail of an AI coding session into ONE spoken sentence for the person "
    "who runs it, who is not looking at the screen. Up to 25 words. What happened and "
    "what it means, in plain English: no jargon, no file paths, no code, no tool names. "
    "Say it the way a colleague would across a desk. Lead with the thing itself — the "
    "result, the number, the file, the error — as the subject of the sentence; never with "
    "\"The system\", \"The session\", \"The AI\" or \"It\". No word like now, currently or "
    "successfully. Use only what is in the session's message: nothing from these "
    "instructions, nothing invented. "
    "It states; it never asks and never offers to do anything. "
    "Do not name the project — that is said before it. "
    'Return ONLY JSON: {"takeaway": "..."}'
)
# What the model still opens with sometimes. Not cut — "Prevents duplicate
# alerts" is a fragment with its subject torn off, which he hates more than
# the filler — but sent back once for a rewrite with a real subject.
FILLER = re.compile(r"^(?:the (?:system|session|ai|agent|model|assistant|code|team|update|work)|it|this)\b", re.I)
REWRITE = ('Your sentence began with "{opener}". Rewrite it so it leads with the fact itself and has a '
           'real subject — the tests, the proof, the file, the number — not "the system" or "it". '
           'Same meaning, one sentence, up to 25 words. Return ONLY JSON: {{"takeaway": "..."}}')

# "tell billing to …", "ask ameka why …", "send to the api: …", "billing, …"
VERB = re.compile(r"^(?:please\s+)?(?:tell|ask|send(?:\s+(?:this|that|it))?\s+to|say\s+to|for|to)\s+", re.I)
JOIN = re.compile(r"^(?:[,:]\s*)?(?:to|that)\s+", re.I)
NOT_NAMES = {"me", "it", "him", "her", "them", "us", "you", "this", "that", "the", "a", "an"}
FRAGMENT = re.compile(r"(?:--|—|…|\.\.\.|\b(?:so|and|but|because|then|um|uh)\s*[,.]?)\s*$", re.I)
QUESTION = re.compile(r"^(?:what|where|why|how|when|which|who|is|are|was|were|can|could|do|does|did|"
                      r"will|would|should|have|has|any|anything)\b|.*\?\s*$", re.I)

ANSWER = (
    "You are Ameka. The person you work for is talking to you out loud; you watch their coding sessions and read them "
    "what they do. Answer his question in ONE short spoken sentence, in plain words, using the "
    "facts below — name the sessions, say what each is doing, give the numbers and the times. "
    "Speak to him as 'you'; never say 'the owner' or 'the user'. Never say you are ready to "
    "assist, never offer to do anything, never ask him anything. If the facts do not cover it, "
    'say what you do know instead. Return ONLY JSON: {"say": "..."}'
)


FOR_ME = (
    "You are Ameka, a voice assistant. The owner's laptop microphone hears everything in the "
    "room: him talking to you, him talking to other people, the television. Decide whether this "
    "sentence is the owner giving YOU an instruction or a message for one of his coding "
    "sessions. A television advert, a news broadcast, one side of someone else's conversation, "
    'or small talk is not. Return ONLY JSON: {"for_me": true/false}'
)


def for_me(b, text: str) -> bool:
    """On the laptop microphone, is this even for her? A television advert
    for fast, powerful relief was read back as "To billing: … Send it?"."""
    if not audio.on_room_mic(b.config) or not brain.available():
        return True
    got = brain.ask(FOR_ME, f"The sentence: {text}", max_tokens=20, timeout=8)
    if not got or "for_me" not in got:
        return True
    if not got["for_me"]:
        b.log("not for her, says her brain:", repr(text[:60]))
    return bool(got["for_me"])


def _answer(b, question: str) -> None:
    """One sentence back, from what she knows right now. No action, ever."""
    facts = []
    if b.last_news:
        ago = int((time.time() - b.last_news["when"]) / 60)
        facts.append(f"The last update, {ago} minutes ago, was from {spoken(b, b.last_news['project'])}: "
                     f"{b.last_news.get('takeaway', '')}")
    # What each session is doing, as her last pass over them saw it.
    seen = getattr(getattr(b, "supervisor", None), "last_states", {}) or {}
    if seen:
        facts.append("What each session is doing: " + "; ".join(
            f"{spoken(b, proj)} ({where}) is {state}{(' — ' + why) if why else ''}"
            for proj, (where, state, why, _) in seen.items()))
    else:
        names = [spoken(b, k) for k in targets(b)]
        facts.append("Open sessions: " + (", ".join(names) or "none"))
    if getattr(b, "loop_summary", ""):
        facts.append("Her last smart-loop pass over the sessions: " + b.loop_summary)
    facts.append(f"Time now: {time.strftime('%H:%M')}")
    got = brain.ask(ANSWER, "Facts:\n" + "\n".join(facts) + f"\n\nHis question: {question}", max_tokens=80, timeout=20)
    line = (got.get("say") or "").strip() if got else ""
    if not line:
        line = facts[0] if b.last_news else "Nothing has come in yet."
    b.speak(line)
# Spoken shorthand for a folder — "api" for "billing-api", say — comes
# from [aliases] in the config. Nobody's project names are compiled in.
ALIASES: dict[str, str] = {}


def sentence(config, event) -> str:
    """One sentence, from the model if there is one, else from the local extractor."""
    said = (event.assistant or event.note or "").strip()
    if not said:
        return ""
    cfg = config.section("summarize")
    out = {}
    try:
        out = summarize.briefing(config, {
            "source": event.source, "cwd": event.cwd,
            "assistant": said, "user": event.user, "note": event.note,
        }, system=ONE_SENTENCE)
    except Exception:
        out = {}
    text = (out.get("takeaway") or "").strip()
    if not text:
        text = (local_summary.local_briefing(said).get("takeaway") or "").strip()
    # One sentence means one: the first full stop ends it.
    first = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0] if text else ""
    m = FILLER.match(first)
    if m and brain.available():
        again = brain.ask(ONE_SENTENCE, REWRITE.format(opener=m.group(0)) + "\n\nSentence: " + first,
                          max_tokens=80, timeout=15)
        better = re.split(r"(?<=[.!?])\s+", (again.get("takeaway") or "").strip(), maxsplit=1)[0] if again else ""
        if better and len(better.split()) >= 4 and not FILLER.match(better):
            first = better
    words = first.split()
    if len(words) > int(cfg.get("simple_max_words", 30)):
        first = " ".join(words[:int(cfg.get("simple_max_words", 30))]).rstrip(",;:- ") + "."
    return first


class Pending:
    def __init__(self, name: str, message: str):
        self.name, self.message, self.at = name, message, time.time()


def split_target(text: str, known: list[str], aliases: dict | None = None) -> tuple[str, str]:
    """(session name, message). The name is empty when he did not say where.

    After "tell", "ask", "send to", "for": up to four words are tried as a
    name, longest first, and the first that is a session she can see wins.
    Without a verb, a name counts only with a comma or colon straight after it
    — "Ameka, fix the echo" — so "tell me more" is not a message to a session
    called "me", and a sentence that merely mentions a project is not sent to it.
    """
    t = text.strip()
    m = VERB.match(t)
    body = t[m.end():] if m else t
    words = body.split()
    for n in range(min(4, len(words)), 0, -1):
        candidate = " ".join(words[:n])
        name = resolve(candidate.strip(" ,.:"), known, aliases)
        if not name:
            continue
        after = body[len(candidate):]
        # Without a verb the name has to be marked: "Ameka, …" or "Ameka: …".
        if not m and not (candidate.rstrip().endswith((",", ":")) or after.lstrip().startswith((",", ":"))):
            continue
        rest = JOIN.sub("", after.strip()).strip(" ,.:")
        return name, rest
    return "", t


def resolve(spoken: str, known: list[str], aliases: dict | None = None) -> str:
    """A spoken name to a session she can see, or nothing. Never a guess from
    two letters: "me" is not Ameka."""
    squash = lambda t: "".join(ch for ch in t.lower() if ch.isalnum())
    table = dict(ALIASES)
    table.update({k.lower(): v for k, v in (aliases or {}).items()})
    spoken = table.get(spoken.lower().strip(), spoken)
    want = squash(spoken)
    if len(want) < 3 or spoken.lower().strip() in NOT_NAMES:
        return ""
    by = {squash(n): n for n in known}
    if want in by:
        return by[want]
    if len(want) >= 4:
        for key, name in by.items():
            if key.startswith(want):
                return name
    import difflib
    close = difflib.get_close_matches(want, list(by), n=1, cutoff=0.8)
    return by[close[0]] if close else ""


def targets(brain) -> dict:
    """Every session she can send to: the ones that have spoken, and every
    Claude Code session that is open, whether or not it has said anything yet."""
    out = dict(brain.simple_sessions)
    for live in session_tools.live_sessions():
        project = live.get("project") or ""
        if project and project not in out and live.get("cwd"):
            from .daemon import Event
            out[project] = Event(source="claude-code", session_id=live.get("session_id", ""),
                                 cwd=live["cwd"],
                                 target={"bundle_id": "com.anthropic.claudefordesktop"})
    return out


def spoken(brain, name: str) -> str:
    """How a project is said aloud: an alias if there is one, else the
    folder name with the dashes taken out."""
    aliases = brain.config.section("aliases") if "aliases" in brain.config.raw else {}
    table = dict(ALIASES); table.update({k.lower(): v for k, v in aliases.items()})
    for said, folder in table.items():
        if folder.lower() == name.lower() and len(said) <= 5:
            return said.upper()
    return brain.project_of(name).replace("-", " ").replace("_", " ")


def brief(brain, event) -> None:
    """A finished turn becomes one sentence, said once."""
    line = sentence(brain.config, event)
    if not line:
        brain.log("simple: nothing to say for", event.project)
        return
    if brain.old_news(event.project, {"takeaway": line}, ""):
        return
    brain.current = event
    brain.simple_sessions[event.project] = event
    brain.last_news = {"tool": event.source, "project": event.project,
                       "when": time.time(), "takeaway": line}
    said = f"{spoken(brain, event.project)}. {line}"
    brain.last_line = said
    brain.speak(said)
    brain.briefed_at = time.time()
    if brain.relay is not None:
        brain.relay.send(said)


def turn(brain, text: str) -> None:
    """What he said, after his name and sleep/wake have been dealt with."""
    intent = commands.parse(text)
    kind = intent.kind
    all_targets = targets(brain)
    known = list(all_targets)
    aliases = brain.config.section("aliases") if "aliases" in brain.config.raw else {}
    say_names = lambda: ", ".join(spoken(brain, k) for k in known) or "nothing yet"
    if kind == "repeat":
        brain.speak(brain.last_line or "Nothing yet.")
    elif kind == "status":
        if brain.last_news:
            ago = int((time.time() - brain.last_news["when"]) / 60)
            brain.speak(f"Last from {spoken(brain, brain.last_news['project'])}, "
                        f"{ago} minute{'s' if ago != 1 else ''} ago. I can send to: {say_names()}.")
        else:
            brain.speak("Nothing yet.")
    elif kind == "more":
        brain.speak(brain.detail(brain.current) if brain.current else "Nothing yet.")
    elif kind == "deny":
        if brain.pending:
            brain.pending = None
            brain.speak("Dropped.")
    elif kind == "affirm":
        if brain.pending:
            deliver(brain, brain.pending.name, brain.pending.message)
            brain.pending = None
        elif brain.current:
            # The one thing that does not need reading back: the go-ahead
            # word, to the session that just spoke.
            word = str(brain.config.section("behavior").get("reply_affirm", "proceed"))
            deliver(brain, brain.current.project, word)
    elif kind == "switch":
        name = resolve(intent.text, known, aliases)
        if name:
            brain.simple_target = name
            brain.speak(f"Okay, {spoken(brain, name)}.")
        else:
            brain.speak(f"I cannot see a session called {intent.text}. I can send to: {say_names()}.")
    elif kind in ("skip", "none"):
        return
    else:                                              # freeform
        raw = (intent.text or text).strip()
        # He started a sentence and stopped: "Okay, so--". That is not a message.
        if FRAGMENT.search(raw) or len(raw.split()) < 3 and not VERB.match(raw):
            return
        # A question to her is answered, from what she knows, in one sentence —
        # and never sent anywhere. That is the conversation he asked for, with
        # nothing decided on his behalf.
        if not VERB.match(raw) and QUESTION.match(raw) and not split_target(raw, known, aliases)[0]:
            _answer(brain, raw)
            return
        name, message = split_target(raw, known, aliases)
        if not name and not for_me(brain, raw):
            return
        if not name and VERB.match((intent.text or text).strip()):
            # "tell X …" where X is nothing she can see: say so, do not guess.
            words = VERB.sub("", (intent.text or text).strip()).split()
            first = " ".join(w for w in words[:2] if w.lower() not in NOT_NAMES and w.lower() != "to") or (words[0] if words else "that")
            brain.speak(f"I cannot see a session called {first}. I can send to: {say_names()}.")
            return
        if not name:
            name = brain.simple_target or (brain.current.project if brain.current else "")
        if not name:
            brain.speak("I do not know where to send that. Say which session.")
            return
        if not message:
            return
        if session_tools.hands_off(brain.config, name):
            brain.speak(f"I am staying out of {spoken(brain, name)}. Not sent.")
            return
        brain.pending = Pending(name, message)
        brain.speak(f"To {spoken(brain, name)}: {message}. Send it?")


def deliver(brain, name: str, message: str) -> None:
    """Type into that session, or say plainly that she could not."""
    event = targets(brain).get(name)
    if event is None:
        brain.speak(f"I cannot see {spoken(brain, name)} any more. Not sent.")
        return
    target = dict(event.target)
    if target.get("bundle_id") and not target.get("tmux_pane"):
        # The app types into whichever session is on screen. Get that session
        # on screen first, and prove it, or type nothing.
        title = session_tools.session_title(event.cwd, event.session_id)
        live = [s for s in session_tools.live_sessions() if s.get("session_id") == event.session_id]
        registry = live[0]["name"] if live else ""
        shown = session_tools.focus_in_app(title, registry, event.project)
        if shown:
            time.sleep(0.4)
            shown = session_tools.on_screen(session_tools.last_words(event.cwd, event.session_id), title)
        brain.log(f"simple: {'brought' if shown else 'could not bring'} {event.project} forward "
                  f"(title {title!r}){'' if shown else ' — or it is not the session on screen'}")
        if not shown:
            brain.speak(f"I cannot get {spoken(brain, name)} on screen. Not sent.")
            return
        time.sleep(0.3)
    ok, how = route.deliver(target, message)
    brain.log(f"simple: sent {message!r} -> {name} via {how}" if ok else f"simple: not sent to {name}: {how}")
    if ok:
        audio.earcon(True)
        brain.speak(f"Sent to {spoken(brain, name)}.")
    else:
        brain.speak(f"I could not reach {spoken(brain, name)}. Not sent.")
