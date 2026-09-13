"""Compress a session's last turn into exactly two spoken sentences.

Sentence 1: the single most important takeaway.
Sentence 2: a yes/no question offering the obvious next action.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request

from .local_summary import briefing as local_briefing

from . import brain, profile

SYSTEM = (
    "You turn the tail of an AI coding session into a two-sentence spoken briefing "
    "for a busy founder wearing AirPods, away from his desk.\n"
    "Rules:\n"
    "1. The takeaway: what happened and what it means, in one or two full "
    "sentences, up to 40 words. Plain English, no jargon, no file paths, no "
    "code. Say enough that he does not have to look at the screen to follow "
    "it — a bare headline leaves him guessing, which is worse than a sentence "
    "more. Subject and verb in every sentence. It states; it NEVER asks. Put "
    "no question in it at all: the question is spoken straight after it, and "
    "asking in both is how she ends up saying the same thing twice in one "
    "breath.\n"
    "2. Sentence two: a direct yes/no question offering the single obvious next action, "
    "phrased 'Want me to ...?' and under 15 words.\n"
    "3. Never mention tools, tokens, or that you are summarising.\n"
    "4. If the session asked the user a question, sentence one states the question's "
    "substance and sentence two offers the most likely answer as an action.\n"
    "5. Say it the way a colleague would say it across a desk. No formula, no "
    "announcing the project, no 'the session has'. Just tell him the thing.\n"
    "6. \"say\": whether this is worth interrupting him for at all. False for "
    "acknowledgements, for work he just asked for going as expected, for "
    "confusion the tooling caused rather than the work, and for anything he "
    "already knows because he just said it. He is wearing AirPods and trying to "
    "think; every line you speak costs him something. When there is nothing "
    "worth saying, say nothing.\n"
    "Say nothing — \"say\": false — for a session narrating its way through the "
    "work: what it is about to do, what it is looking at, what it has decided "
    "to try next, progress that changes nothing for him. \"Let me check the "
    "gauge sector next\" is a machine thinking out loud, and reading it to him "
    "takes his place in his own work away to tell him nothing.\n"
    "Say something — \"say\": true — for a result, a decision he has to make, "
    "something that went wrong, or a question the session asked him. Those are "
    "the four. If it is not one of them, it can wait until he looks.\n"
    'Return ONLY JSON: {"say": true/false, "takeaway": "...", "ask": "...", '
    '"action": "short verb phrase"}'
)


def _post_json(url: str, headers: dict, payload: dict, timeout: float = 25.0) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _extract_json(text: str) -> dict:
    text = text.strip()
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


def _anthropic(cfg, acct, prompt: str, system: str = "") -> dict:
    base = (acct.base_url or "https://api.anthropic.com").rstrip("/")
    body = {
        "model": cfg["model"],
        "max_tokens": 300,
        "system": (system or SYSTEM),
        "messages": [{"role": "user", "content": prompt}],
    }
    data = _post_json(
        f"{base}/v1/messages",
        {
            "content-type": "application/json",
            "x-api-key": acct.api_key,
            "anthropic-version": "2023-06-01",
        },
        body,
    )
    text = "".join(b.get("text", "") for b in data.get("content", []))
    return _extract_json(text)


def _openai(cfg, acct, prompt: str, known: str = "", system: str = "") -> dict:
    base = (acct.base_url or "https://api.openai.com").rstrip("/")
    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system or SYSTEM},
            # What he is actually working towards, so a briefing can tell a
            # milestone from a detail rather than reading out whatever the
            # session said last.
            *([{"role": "system", "content": known}] if known else []),
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
    }
    data = _post_json(
        f"{base}/v1/chat/completions",
        {"content-type": "application/json", "authorization": f"Bearer {acct.api_key}"},
        body,
    )
    return _extract_json(data["choices"][0]["message"]["content"])


def _claude_cli(cfg, prompt: str, system: str = "") -> dict:
    """Use the Claude Code CLI you already have. Runs on your subscription.

    AMEKA_NO_HOOK stops the headless run from firing our own Stop hook, which
    would brief you about the briefing, forever.
    """
    if not shutil.which("claude"):
        return {}
    env = dict(os.environ, AMEKA_NO_HOOK="1")
    cmd = ["claude", "-p", f"{(system or SYSTEM)}\n\n{prompt}"]
    if cfg.get("cli_model"):
        cmd += ["--model", cfg["cli_model"]]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    except Exception:
        return {}
    return _extract_json(out.stdout or "")


def _fallback(assistant: str) -> dict:
    """No API key, or the call failed. Speak the first real sentence verbatim."""
    clean = re.sub(r"```.*?```", " ", assistant or "", flags=re.S)
    clean = re.sub(r"[`*#>_\[\]]", "", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    first = re.split(r"(?<=[.!?])\s+", clean)[0] if clean else "A session finished."
    return {
        "takeaway": first[:220] or "A session finished.",
        "ask": "Want me to proceed?",
        "action": "proceed",
    }


def briefing(config, event: dict, system: str = "") -> dict:
    """event: {source, cwd, assistant, user, note}. Returns takeaway/ask/action.

    `system` replaces the two-sentence prompt: simple mode wants one sentence
    and no question, and says so itself."""
    cfg = config.section("summarize")
    assistant = (event.get("assistant") or event.get("note") or "").strip()
    if not assistant:
        return _fallback("")

    prompt = (
        f"Project folder: {event.get('cwd') or 'unknown'}\n"
        f"Source: {event.get('source') or 'claude-code'}\n\n"
        f"What I last asked for:\n{(event.get('user') or '(nothing recorded)')[:2000]}\n\n"
        f"What the session just said:\n{assistant[: cfg['max_input_chars']]}"
    )

    provider = cfg.get("provider", "auto")
    if provider in ("auto", "brain"):
        # Her own brain, in the process, no key. The cloud is not tried on
        # "auto" any more: he wants the whole app to run without an API key,
        # and a key that is merely present should not quietly cost money.
        if brain.available():
            asked = brain.ask(system or SYSTEM, prompt, max_tokens=200)
            if asked.get("takeaway"):
                asked.setdefault("ask", "")
                asked.setdefault("action", "proceed")
                asked.setdefault("say", True)
                return asked
        provider = "local"

    if provider == "local":
        return local_briefing(assistant)
    if provider == "claude-cli":
        return _claude_cli(cfg, prompt, system) or local_briefing(assistant)

    acct = config.account(provider, cfg.get("account", "default"))
    if not acct.usable:
        return local_briefing(assistant)

    try:
        out = (_anthropic(cfg, acct, prompt, system) if provider == "anthropic"
               else _openai(cfg, acct, prompt, profile.briefing(config) if not system else "", system))
    except (urllib.error.URLError, KeyError, TimeoutError, OSError):
        return local_briefing(assistant)

    if not out.get("takeaway"):
        return local_briefing(assistant)
    out.setdefault("ask", "Want me to proceed?")
    out.setdefault("action", "proceed")
    return out
