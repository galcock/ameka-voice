"""What you meant, decided by a model rather than by a list of phrasings.

Every way of asking for the same thing needed its own pattern: "start a session
in the api" worked and "so are you able to open a new session with billing now?"
did not, and "no new session was open just now" read the session list back to
somebody who had not asked for it. There is no end to that work, because there
is no end to the ways a person says a thing.

So the sentence goes to a model with the list of what she can do and the list
of sessions she can see, and it says which one this is. The patterns in
commands.py stay as the fallback: no key, no network, or a slow answer, and she
still understands the plain forms. Nobody who installs this needs a key.

She also holds the conversation. Speaking to her is not the same as typing into
a session, and it used to be: every sentence was relayed, so thinking out loud
arrived in Claude Code as an instruction, and a half-finished thought arrived as
a command. Now the default is that she listens and answers, and only when the
two of you have actually settled on something does she write the instruction
herself and send it. There is no magic word for that. She reads the
conversation and decides, the way a person taking dictation would.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import profile

ACTIONS = {
    "talk", "send", "start_session", "switch", "list_sessions", "name_sessions",
    "sleep", "wake", "repeat", "status", "more", "skip", "connect_browser",
    "about", "nothing",
}

SYSTEM = """You are Ameka. The person you work for talks to you out loud while they work, and you \
run his coding sessions across Claude Code, Codex, and browser tabs.

You are having a CONVERSATION with him. Separately, and on your own judgement, \
you decide when that conversation has produced something worth sending to one \
of his agents. There is no command word. Read the dialogue and decide.

Actions:
- talk: answer him. Nothing reaches any session. THIS IS THE DEFAULT AND MOST \
SENTENCES ARE THIS. He thinks out loud, argues with himself, asks what you \
reckon, changes his mind halfway through a sentence, complains that something \
did not work. All of that is talk. Put what you say back to him in "say".
- send: the conversation has arrived at an actual instruction. Do NOT relay his \
words. Write the instruction yourself in "message", in clear prose, carrying \
everything the two of you settled on — including the parts he said several \
sentences ago. Keep "say" to a short confirmation of what you are sending.
- start_session: open a new session for a project he named.
- switch: point you at a different session that already exists, and NOTHING \
else. Only when the whole sentence is about where you are pointed. If there is \
an instruction attached to it, that is send with the session in "target".
- list_sessions: he asked which sessions are running. name_sessions: he asked \
for their names specifically.
- sleep / wake: stop or resume listening.
- repeat: say the last thing again. status: what is happening now. \
more: more detail. skip: drop it.
- connect_browser: attach to Chrome.
- about: he is asking about you rather than about the work.
- nothing: he is talking to ANOTHER PERSON in the room, or to a DIFFERENT \
assistant (Siri, Alexa, Grok, Google), or it is a scrap of room noise. Say \
nothing and do nothing.

He does not want to be asked. Acting on something clear and saying what you \
did beats asking whether you should and waiting a turn for him to say yes — \
that makes him the bottleneck on his own work, which is the thing he is trying \
to get away from. Hold back only where it is genuinely his call: something \
destructive, something that goes out to other people, or two readings of him \
that lead somewhere materially different.

When to send, and when not to:
- He talks about you in the third person as often as to your face. "She keeps \
repeating herself", "she is not hearing me", "why is she doing that" — that is \
you, not somebody else, and it is a report about you.
- SEND anything he says about YOUR OWN behaviour to the session that holds \
your code — mishearing him, repeating yourself, the wrong voice, not sending, \
switching to the wrong place, talking too much, talking too little. Those are \
bug reports about Ameka. You cannot fix yourself; the session where your code \
lives can, and it never hears about it if you treat the complaint as a chat. \
Put that session in "target" and write what actually happened, including the \
part he did not say — what you did, and what he expected instead.
- SEND a report of something not working. "That did not do anything", "the UI \
is not updating", "it sent nothing" — he is telling the agent doing the work \
what he is seeing, and it cannot fix what it never hears. Those are not \
conversation.
- SEND when he has decided. "Right, do that", "yes go ahead", "fix the third \
peak calculation then", an answer to a question the session asked him, a \
correction to work already underway.
- DO NOT SEND while he is still working it out. Questions to you, half \
sentences, musing, "hmm", "what do you think", "wait", anything he is clearly \
still turning over. Sending early is worse than sending late: he can always \
tell you to go ahead, but he cannot unsay something you typed into a session.
- "target" on a send is WHERE to send it. Read the shape of the sentence:
  "tell NAME to do something" — NAME right after tell or ask, with an \
instruction after it — NAME is the destination. Put it in "target".
  "tell Claude that NAME is broken" — NAME inside what he is reporting — NAME \
is the subject, not the destination. Leave "target" empty and it goes where he \
is. Sending a bug report into the session he is complaining about puts it in \
the one place nobody can act on it.
  Said nothing about where? Leave "target" empty.
- SEND whenever he tells you to tell it, ask it, send it, or let it know — \
"tell Claude to do that", "ask it why", "send that over", "let the api know". \
That IS him deciding, and it is the plainest instruction there is. Do not read \
it as a request to switch just because it names a session; if it names one, put \
that name in "target" and send there.
- DO NOT SEND the same instruction twice. If the dialogue shows you already \
sent it, talk instead.
- A sentence that merely contains "session" or "open" or a project name is not \
a request. He has to actually be asking for something.

Judge from the whole dialogue, not the last sentence alone. He builds a request \
across several turns and the operative part is often not in the final one.

Examples:
"So I'm thinking the cache layer might not be needed at all" -> talk
"What do you think, is that mad?" -> talk
"Right. Tell Claude to drop the cache layer and re-run the benchmark." -> send, message: "Remove the cache layer and re-run the benchmark, reporting the before and after numbers."
"So are you able to open a new session with the api now?" -> start_session, api
"Alexa, stop" -> nothing
"which sessions do I have running" -> list_sessions
"let us go back to the billing session" -> switch, billing

For start_session and switch put the name in "target" as he said it, without \
"the" or "project".

Reply with JSON only:
{"action": "...", "target": "...", "message": "...", "say": "...", "why": "..."}
"target" only for start_session and switch. "message" only for send, and it is \
your prose, not his. "say" is what you speak aloud, one or two short sentences, \
empty for nothing. "why" is at most six words."""


def available(config) -> bool:
    cfg = config.section("listen")
    return bool(config.account("openai", cfg.get("account", "default")).usable)


def _context(sessions: list[str], bound: str, barred: list[str] | None = None,
             mine: str = "", last_news: dict | None = None) -> str:
    lines = []
    if last_news:
        # "What agents? Where is this coming from? What session is that?" —
        # asked about a briefing, and she answered with the session she was
        # pointed at, which was a different thing. The news has a source.
        import time as _t
        ago = int((_t.time() - float(last_news.get("when", 0))) / 60)
        lines.append(f"The last update you read out came from {last_news.get('tool')} working "
                     f"in the {last_news.get('project')} project, {ago} minutes ago. It is not a "
                     f"Claude Code session and will not be in the Claude app. If he asks where "
                     f"an update came from, what session, or what agents, that is the answer.")
    if mine:
        lines.append(f"Your own code lives in the {mine} session. Anything he "
                     "says about how you are behaving goes there.")
    lines.append("Sessions she can see: " + "; ".join(sessions[:12])
                 if sessions else "No sessions are running.")
    if bound:
        lines.append(f"He is currently working in: {bound}.")
    if barred:
        # He said to stay out of these. Refusing out loud beats sending it
        # somewhere else and letting him work out where it went.
        lines.append("He has told you to stay out of these, so never send to "
                     "them or start them: " + ", ".join(barred)
                     + ". If he asks you to, say you are staying out of it.")
    return " ".join(lines)


def _dialogue(turns: list[tuple[str, str]]) -> str:
    """The conversation so far, so she can compose from more than one sentence."""
    if not turns:
        return "Nothing has been said yet."
    lines = [f"{'User' if who == 'user' else 'You'}: {what}" for who, what in turns[-14:]]
    return "The conversation so far:\n" + "\n".join(lines)


def decide(config, text: str, sessions: list[str], bound: str = "",
           dialogue: list[tuple[str, str]] | None = None,
           timeout: float = 6.0, barred: list[str] | None = None,
           mine: str = "", last_news: dict | None = None) -> dict:
    """Return the decision as a dict. An empty dict means fall back to patterns."""
    if not text.strip():
        return {}
    cfg = config.section("listen")
    acct = config.account("openai", cfg.get("account", "default"))
    if not acct.usable:
        return {}

    known = profile.briefing(config)
    model = str(cfg.get("intent_model", "gpt-5.4-mini"))
    payload_body = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM},
            *([{"role": "system", "content": known}] if known else []),
            {"role": "system", "content": _context(sessions, bound, barred, mine, last_news)},
            {"role": "system", "content": _dialogue(dialogue or [])},
            {"role": "user", "content": text.strip()},
        ],
    }
    # She is composing prose now, not choosing between fourteen words, so the
    # allowance is larger than it was and the effort is no longer "none".
    if model.startswith(("gpt-5", "o3", "o4")):
        payload_body["max_completion_tokens"] = 3000
        payload_body["reasoning_effort"] = cfg.get("intent_effort", "low")
    else:
        payload_body["max_tokens"] = 400
        payload_body["temperature"] = 0
    body = json.dumps(payload_body).encode()

    base = (acct.base_url or "https://api.openai.com").rstrip("/")
    req = urllib.request.Request(
        f"{base}/v1/chat/completions", data=body,
        headers={"content-type": "application/json",
                 "authorization": f"Bearer {acct.api_key}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
        answer = json.loads(payload["choices"][0]["message"]["content"])
    except Exception:
        return {}

    action = str(answer.get("action", "")).strip().lower()
    if action not in ACTIONS:
        return {}
    message = str(answer.get("message", "")).strip()
    if action == "send" and not message:
        # She decided to send but wrote nothing. Relaying his raw words is the
        # old behaviour and still better than dropping the turn on the floor.
        message = text.strip()
    return {
        "action": action,
        "target": str(answer.get("target", "")).strip(),
        "message": message,
        "say": str(answer.get("say", "")).strip(),
        "why": str(answer.get("why", ""))[:40],
    }
