"""Put the names back.

Speech recognisers have never heard of Claude, Codex or Ameka, so they reach for
the nearest everyday word: Claude becomes cloud, Codex becomes codecs, Ameka
becomes America. Two defences — the decoder is told the vocabulary up front, and
whatever still slips through is corrected here.
"""
from __future__ import annotations

import re

# Given to whisper as an initial prompt. The decoder is biased toward words it
# has just seen, which is the cheapest fix and the one that keeps punctuation.
GLOSSARY_TERMS = [
    # the agents and their makers
    "Claude", "Claude Code", "Anthropic", "Opus", "Sonnet", "Haiku",
    "ChatGPT", "OpenAI", "Codex", "GPT", "Gemini", "Copilot", "Cursor",
    "Ameka", "MCP", "LLM", "agent", "subagent", "session", "prompt", "token",
    # the work
    "repo", "commit", "branch", "merge", "rebase", "pull request", "deploy",
    "staging", "production", "tmux", "Vercel", "GitHub", "npm", "Python",
    # Your own project names come from [glossary] in the config — what you hear
    # it get wrong on the left, what you meant on the right — and are added to
    # this list at runtime, so the recogniser is told to expect them.
]
COMMAND_TERMS = [
    "proceed", "continue", "run it", "do it", "stop", "repeat", "details",
    "skip", "mute", "unmute", "status", "switch to", "connect the browser",
    "what sessions are running",
]


def vocabulary(extra: list[str] | None = None) -> str:
    """The initial prompt handed to whisper. A decoder is biased toward words
    it has just read, so naming them up front is the cheapest correction."""
    terms = GLOSSARY_TERMS + list(extra or [])
    return ", ".join(terms) + ". Commands: " + ", ".join(COMMAND_TERMS) + "."


VOCABULARY = vocabulary()

# Words that prove "cloud" really did mean servers.
CLOUD_NOUNS = (
    r"computing|storage|provider|providers|native|hosting|host|infra|infrastructure|"
    r"service|services|server|servers|front|flare|run|function|functions|platform|"
    r"region|regions|bucket|buckets|instance|instances|vm|vms|cost|costs|bill|spend|"
    r"migration|deployment|deploy|first|based|only|architecture|stack"
)
NOT_CLOUD_TECH = rf"(?!\s+(?:{CLOUD_NOUNS})\b)"

# (pattern, replacement). Ordered: longer phrases first so they win.
RULES: list[tuple[re.Pattern, str]] = [
    # Claude Code, however it comes out
    (re.compile(r"\b(?:cloud|clod|clog|clock|claud|clawed|chloe|clyde|cloudy|glaude|clode|cloth|client|clientz|clyde|closed|cold|called|clode)\s+code\b", re.I), "Claude Code"),
    (re.compile(r"\bcode\s+(?:cloud|clod|claud)\b", re.I), "Claude Code"),
    # Claude on its own, but only where a product is clearly meant
    # "move to cloud storage" is about servers, not Anthropic.
    (re.compile(r"\b(?:switch|talk|go|jump|move)\s+to\s+(?:the\s+)?(?:cloud|clod|clog|claud|clawed)\b"
                + NOT_CLOUD_TECH, re.I),
     lambda m: m.group(0).rsplit(" ", 1)[0] + " Claude"),
    (re.compile(r"\b(?:cloud|clod|clog|claud|clawed)\s+(?:session|sessions|tab|tabs|window|"
                r"windows|chat|chats|app|conversation|conversations|browser)\b", re.I),
     lambda m: "Claude " + m.group(0).split(None, 1)[1]),
    (re.compile(r"\b(?:in|on|from|to|for|or|and|versus|vs|between|with|about|use|using|open)\s+"
                r"(?:cloud|clod|clog|claud|clawed|clode)\b" + NOT_CLOUD_TECH, re.I),
     lambda m: m.group(0).rsplit(" ", 1)[0] + " Claude"),
    # ChatGPT
    (re.compile(r"\bchat\s*(?:gpt|g\.?p\.?t\.?|jpt|gbt|gpd)\b", re.I), "ChatGPT"),
    (re.compile(r"\b(?:fortchett|forchette|fortune|chat)\s*(?:tpt|tbt|gpt)\b", re.I), "ChatGPT"),
    (re.compile(r"\bchet\s*gpt\b", re.I), "ChatGPT"),
    # Codex
    (re.compile(r"\b(?:codecs|codec|co-?dex|kodak|codeine)\b", re.I), "Codex"),
    # Ameka
    (re.compile(r"\b(?:amica|amika|america|a\s*meek\s*(?:a|of)|ameca|amaka|omega\s*ka)\b", re.I), "Ameka"),
    # the rest of the vocabulary
    (re.compile(r"\b(?:tea\s*mux|t\s*mux|team\s*ux)\b", re.I), "tmux"),
    (re.compile(r"\b(?:anthropic|anthropoc|antropic)\b", re.I), "Anthropic"),
    # Vercel. "verse cell" and "versatile" both reached a session as themselves.
    (re.compile(r"\b(?:verse\s*cell|versatile|vercell|ver\s*sell|virsel|vursel|"
                r"her\s*cell|first\s*cell)\b", re.I), "Vercel"),
    # Project names of your own that keep coming back wrong go in [glossary].
    # Nothing here is anyone's particular vocabulary.
]


def _custom_rules(glossary: dict) -> list[tuple[re.Pattern, str]]:
    """Turn {"my project": "MyProject"} from the config into match rules."""
    rules = []
    for heard, meant in (glossary or {}).items():
        heard = str(heard).strip()
        if not heard:
            continue
        rules.append((re.compile(r"\b" + re.escape(heard) + r"\b", re.I), str(meant)))
    return sorted(rules, key=lambda r: -len(r[0].pattern))


SOUND_NOTE = re.compile(r"[\(\[][^)\]]{0,40}[\)\]]")


def strip_sound_notes(text: str) -> str:
    """whisper narrates what it cannot transcribe: (bell ringing), [laughs]."""
    return re.sub(r"\s{2,}", " ", SOUND_NOTE.sub(" ", text or "")).strip(" ,.;:")


MACHINERY = re.compile(
    r"<[^>]{1,80}>"                       # <heartbeat>, </automation_id>
    r"|\b[a-z0-9]+(?:[-_][a-z0-9]+){2,}\b"   # a-long-hyphenated-automation-id
    r"|\[\[?[A-Z_]{3,}\]\]?", re.I)


def strip_machinery(text: str) -> str:
    """Take the plumbing out of something meant to be said out loud.

    A scheduled job writes markers into its own transcript, and she read one
    aloud: "heartbeat, automation id, a-long-hyphenated-job-name".
    That is a machine talking to itself, and he is wearing headphones.
    """
    out = MACHINERY.sub(" ", text or "")
    return re.sub(r"\s{2,}", " ", out).strip(" .,;:-")


def mostly_machinery(text: str) -> bool:
    """Was there anything in it for a person at all?"""
    words = len((text or "").split())
    return words > 0 and len(strip_machinery(text).split()) < max(3, words * 0.4)


def looks_hallucinated(text: str) -> bool:
    """whisper drops into a loop when the audio is unclear, repeating a phrase
    until it runs out of room: "a little bit, a little bit, a little bit". Any
    transcript doing that is guesswork and should not reach a session."""
    words = re.findall(r"[a-z']+", (text or "").lower())
    if len(words) < 6:
        return False
    for size in (1, 2, 3, 4, 5):
        i, runs = size, 1
        while i + size <= len(words):
            if words[i:i + size] == words[i - size:i]:
                runs += 1
                if runs >= 3:
                    return True
                i += size                 # keep counting the same repeat
            else:
                runs = 1
                i += 1                    # a loop can start anywhere
    # a handful of distinct words padding a long transcript is the same failure
    if len(words) >= 12 and len(set(words)) / len(words) < 0.4:
        return True
    return False


PROMPT_PIECES = {t.lower() for t in GLOSSARY_TERMS + COMMAND_TERMS}


def strip_prompt_spill(text: str) -> str:
    """Whisper reading its own hint sheet back to you.

    The glossary goes in as an initial prompt so it spells Ameka and Codex
    correctly. Given a capture with little in it, whisper will transcribe that
    list instead of your voice, and it arrives looking exactly like an
    instruction. Nobody says "proceed, continue, run it, do it, stop, repeat"
    out loud, so a run of them at the end is the prompt, not the speaker.
    """
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 3:
        return text
    # Sometimes it reads the whole sheet, ending on something that is not a
    # term at all — a stray "###", or the last command with a full stop. Then
    # nothing is trimmed from the end and all of it arrives. If most of the
    # commas separate our own vocabulary, none of it was said out loud.
    ours = sum(1 for p in parts if p.lower().strip(".!?#: ") in PROMPT_PIECES)
    if len(parts) >= 5 and ours / len(parts) >= 0.6:
        return ""
    dropped = 0
    while len(parts) > 1 and parts[-1].lower().strip(".!?") in PROMPT_PIECES:
        parts.pop()
        dropped += 1
    if dropped < 3:
        return text
    # The list usually starts mid-sentence, glued to your last real word.
    words = parts[-1].split()
    while words and words[-1].lower().strip(".!?") in PROMPT_PIECES:
        words.pop()
    parts[-1] = " ".join(words)
    return ", ".join(p for p in parts if p).strip(" ,")


def apply(text: str, glossary: dict | None = None) -> str:
    """Correct product names in a transcript, leaving everything else alone.

    The config's own glossary runs first, so anyone can override or extend this
    without touching the code:

        [glossary]
        "what you hear" = "what you meant"
    """
    if not text:
        return text
    text = strip_sound_notes(text)
    text = strip_prompt_spill(text)
    for pattern, replacement in _custom_rules(glossary):
        text = pattern.sub(replacement, text)
    for pattern, replacement in RULES:
        text = pattern.sub(replacement, text)
    return text
