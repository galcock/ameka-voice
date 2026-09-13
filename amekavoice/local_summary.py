"""Two-sentence briefing with no model at all.

Claude Code and Codex both end a turn with a short plain-English wrap-up, and
frequently with a direct question. That is exactly the material we want, so this
extracts it instead of paying a model to rewrite it.
"""
from __future__ import annotations

import re

FENCE = re.compile(r"```.*?```", re.S)
INLINE = re.compile(r"`([^`]*)`")
LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
HEADING = re.compile(r"^#{1,6}\s*", re.M)
BULLET = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+", re.M)
TABLE = re.compile(r"^\s*\|.*\|\s*$", re.M)
BOLD = re.compile(r"\*\*([^*]+)\*\*")
ITALIC = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
PATHY = re.compile(r"[/\\~]?[\w.-]+/[\w./-]+")
SENTENCE = re.compile(r"(?<=[.!?])\s+")
WORD = re.compile(r"[A-Za-z0-9']+")   # "All 42 tests pass" is four words

# Sentences that are scaffolding, not the point.
SKIP_STARTS = (
    "here'", "here is", "here are", "let me", "i'll ", "i will ", "first,", "now ",
    "next,", "one moment", "working on", "checking", "looking at", "running ",
    "say ", "just say", "then you", "and then", "so ", "two things", "one thing",
)
# Sentences addressed at the listener about the tool itself, not about the work.
META = re.compile(
    r"\b(you'?ll hear|you will hear|say something|let me know|talk back|"
    r"when i finish|when i stop|lands here|goes to this session|read it (out|aloud))\b",
    re.I,
)
OPENER = re.compile(r"^(?:and|but|so|then|also|plus|meanwhile|anyway)[,\s]+", re.I)
# Ways a session offers to do the next thing. The point is to ask about THAT,
# not to fall back on a limp "want me to proceed?".
OFFERS = [
    re.compile(r"\bsay the word and (?:i'?ll|i will|we'?ll)\s+(.{4,90}?)\s*(?=[,;.!?]|$)", re.I),
    re.compile(r"\band (?:i'?ll|i will|we'?ll)\s+(.{4,90}?)\s*(?=[,;.!?]|$)", re.I),
    re.compile(r"\b(?:next|then|after that|now|first)\s+(?:i'?ll|i will|we'?ll)\s+(.{4,90}?)\s*(?=[,;.!?]|$)", re.I),
    re.compile(r"(?:^|[.!?]\s+)(?:i'?ll|i will|we'?ll)\s+(.{4,90}?)\s*(?=[,;.!?]|$)", re.I),
    re.compile(r"\bwhat(?:'s| is) left is\s+(?:to\s+)?(.{4,90}?)\s*(?=[,;.!?]|$)", re.I),
    re.compile(r"\bthat leaves\s+(.{4,90}?)\s*(?=[,;.!?]|$)", re.I),
    re.compile(r"\btell me to\s+(.{4,90}?)\s+and\b", re.I),
    re.compile(r"\bnext (?:is|up is|step is|job is|thing is)\s+(?:to\s+)?(.{4,90}?)\s*(?=[,;.!?]|$)", re.I),
    re.compile(r"\bi'?d\s+((?:go|start|do|run|fix|take|look|check|try|use)\b.{3,64}?)\s*(?=[,;.!?]|$)", re.I),
    re.compile(r"^(?:i can|i could|i'?ll|i will|we can|we could|ready to|happy to)\s+(.{4,90}?)\s*[.!?]?$", re.I),
    re.compile(r"\b(?:still|left|yet) to do:?\s+(.{4,90}?)\s*(?=[,;.!?]|$)", re.I),
    re.compile(r"\b(?:still|yet) to\s+(.{4,90}?)\s*(?=[,;.!?]|$)", re.I),
    re.compile(r"\bremains? (?:is )?to\s+(.{4,90}?)\s*(?=[,;.!?]|$)", re.I),
    re.compile(r"\bwaiting (?:on|for) (?:you to\s+)?(.{4,90}?)\s*(?=[,;.!?]|$)", re.I),
]
# An offer has to be an action, so it has to start with a verb. Without this
# the patterns happily suggest "want me to got it?" or a conversation's title.
ACTION_VERB = re.compile(
    r"^(?:run|start|stop|open|close|connect|wire|hook|build|make|write|add|remove|delete|"
    r"fix|patch|ship|deploy|release|publish|push|pull|commit|merge|rebase|revert|"
    r"check|test|verify|review|read|look|search|find|try|install|update|upgrade|"
    r"set|setup|configure|enable|disable|switch|move|rename|clean|tidy|refactor|"
    r"send|reply|answer|ask|tell|call|draft|do|go|carry|keep|continue|proceed|"
    r"finish|complete|handle|sort|wrap|take|put|get|bring|turn|kill|restart|"
    r"generate|create|apply|land|merge|split|copy|back|record|log|watch|approve|"
    r"confirm|promote|rerun|retry|rebuild|redeploy|archive|export|import|migrate)\b",
    re.I,
)
TRAILING_JUNK = re.compile(r"\s+(?:but|so|because|if|when|which)\b.*$", re.I)


VERBS = (
    "run|start|stop|open|close|connect|wire|hook|build|make|write|add|remove|delete|"
    "fix|patch|ship|deploy|release|publish|push|pull|commit|merge|rebase|revert|"
    "check|test|verify|review|read|look|search|find|try|install|update|upgrade|"
    "set|setup|configure|enable|disable|switch|move|rename|clean|tidy|refactor|"
    "send|reply|answer|ask|tell|call|draft|do|go|carry|keep|continue|proceed|"
    "finish|complete|handle|sort|wrap|take|put|get|bring|turn|kill|restart|"
    "generate|create|apply|land|split|copy|record|log|watch|approve|confirm|"
    "promote|rerun|retry|rebuild|redeploy|archive|export|import|migrate|benchmark"
)
def _gerund_map() -> dict[str, str]:
    """wiring -> wire, running -> run, shipping -> ship."""
    table = {}
    for verb in VERBS.split("|"):
        table[verb + "ing"] = verb                       # check -> checking
        if verb.endswith("e"):
            table[verb[:-1] + "ing"] = verb              # wire -> wiring
        if len(verb) > 2 and verb[-1] not in "aeiouwxy" and verb[-2] in "aeiou" and verb[-3] not in "aeiou":
            table[verb + verb[-1] + "ing"] = verb        # ship -> shipping
    return table


GERUNDS = _gerund_map()
GERUND = re.compile(r"^(\w+ing)\b", re.I)


def _tidy_action(text: str) -> str:
    action = text.strip().rstrip(".,;:")
    # Only cut a trailing clause off something long enough to still make sense:
    # "run Tier 1 and 2" must survive, "fix it so the build passes" need not.
    cut = TRAILING_JUNK.search(action)
    if cut and len(action[: cut.start()].split()) >= 4:
        action = action[: cut.start()]
    action = re.sub(r"^(?:to|just|now|also|then|do:)\s*", "", action, flags=re.I)
    # "wiring the webhook" is the same offer as "wire the webhook".
    gerund = GERUND.match(action)
    if gerund:
        base = GERUNDS.get(gerund.group(1).lower())
        if base:
            action = base + action[gerund.end():]
    words = action.split()
    if len(words) > 12:
        action = " ".join(words[:12])
    return action.strip()


def build_ask(paragraphs, all_sentences) -> tuple[str, str, str]:
    """Find the specific next action this message is offering.

    Returns the question, a short action name, and the sentence it came from so
    the takeaway can avoid repeating it back.
    """
    for sentence in reversed(all_sentences):
        candidate = sentence.strip()
        if not candidate.endswith("?"):
            continue
        # A question only reads as one if it survived whole.
        shortened = _trim(candidate, 22)
        if not shortened.endswith("?"):
            shortened = candidate
        if len(shortened.split()) <= 26:
            return shortened, "answer", sentence

    tail = " ".join(_sentences(paragraphs[-1])) if paragraphs else ""
    haystacks = [tail, " ".join(all_sentences[-6:])]

    for text in haystacks:
        for pattern in OFFERS:
            for match in pattern.finditer(text):
                action = _tidy_action(match.group(1))
                if action and ACTION_VERB.match(action):
                    source = next((x for x in all_sentences if match.group(1)[:24] in x), "")
                    return f"Want me to {action}?", action, source
    return "", "", ""


def _clean(text: str) -> str:
    text = FENCE.sub(" ", text or "")
    text = TABLE.sub(" ", text)
    text = LINK.sub(r"\1", text)
    text = INLINE.sub(r"\1", text)
    text = BOLD.sub(r"\1", text)
    text = ITALIC.sub(r"\1", text)
    text = HEADING.sub("", text)
    text = BULLET.sub("", text)
    text = re.sub(r"[│─┌┐└┘├┤┬┴┼✓✗✔✘•▶►→←—]+", " ", text)
    return text


def _is_prose(sentence: str) -> bool:
    words = WORD.findall(sentence)
    if len(words) < 4:
        return False
    if PATHY.search(sentence) and len(words) < 10:
        return False
    symbols = sum(1 for c in sentence if not (c.isalnum() or c.isspace() or c in ".,'\"?!-()"))
    return symbols <= max(3, len(sentence) // 18)


def _sentences(block: str) -> list[str]:
    out = []
    for raw in SENTENCE.split(" ".join(block.split())):
        s = raw.strip()
        if s and _is_prose(s):
            out.append(s)
    return out


CLAUSE_END = re.compile(r"[,;:—–]| \u2014 | - ")


def _trim(sentence: str, limit: int = 40) -> str:
    """Shorten without ever leaving a fragment.

    Cutting on a word count produces things like "since a public door to
    something." — which is what a person hears as being cut off mid-thought.
    A cut is only allowed at a clause boundary, and if there is no usable one
    the whole sentence is spoken instead. Long beats broken.
    """
    sentence = sentence.strip()
    words = sentence.split()
    if len(words) <= limit:
        return sentence

    best = ""
    for match in CLAUSE_END.finditer(sentence):
        head = sentence[: match.start()].strip()
        count = len(head.split())
        if count > limit:
            break
        if count >= max(6, limit // 3):
            best = head
    if best:
        return best.rstrip(",;:—–- ") + ("" if best[-1] in ".!?" else ".")
    return sentence          # nothing clean to cut on; say the whole thing


FIXED_ALREADY = re.compile(
    r"\b(?:fixed|resolved|sorted|corrected|now works?|working now|no longer|"
    r"all correct|all pass(?:es|ed)?|is (?:live|green|clean|right))\b", re.I)
QUESTION_TO_YOU = re.compile(
    r"\b(?:which|whether|would you|do you want|your call|up to you|pick|choose|decide)\b", re.I)
TROUBLE_WORDS = re.compile(
    r"\b(fail(?:s|ed|ing|ure)?|error|broke|broken|crash|cannot|can't|blocked|stuck|"
    r"wrong|missing|refus|denied|conflict|regression|timed out)\b", re.I)
FINISHED_WORDS = re.compile(
    r"\b(built|added|fixed|shipped|deployed|pushed|committed|wrote|written|done|"
    r"finished|works?|working|passes|passed|landed|merged|installed|connected|live)\b", re.I)
UNFINISHED = re.compile(
    r"\b(?:but|however|though)\b[^.!?]{4,70}?\b(?:not|isn'?t|hasn'?t|still|yet|remains?)\b"
    r"[^.!?]{0,60}|\b(?:not yet|still to|left to|remaining)\b[^.!?]{4,60}", re.I)


# Words that name nothing in particular, so naming them helps nobody.
STOP_SUBJECTS = {
    "it", "that", "this", "them", "they", "there", "here", "one", "ones",
    "thing", "things", "something", "everything", "anything", "nothing",
    "way", "ways", "time", "times", "bit", "part", "parts", "point", "rest",
    "same", "other", "others", "next", "last", "first", "whole", "only",
    "steps", "step", "case", "cases", "kind", "sort", "lot", "end", "start",
    "moment", "reason", "idea", "problem", "question", "answer", "result",
}
# "the relay", "the staging deploy" — a thing with a name.
# Only a name worth saying out loud. "session" and "deploy" are words, not
# things: naming them produced "want me to fix session?".
PROPER_NAMES = [
    "Claude Code", "ChatGPT", "Claude", "Codex", "Anthropic", "OpenAI", "Ameka",
    "Vercel", "GitHub", "Postgres", "Stripe", "Docker", "Kubernetes", "Terraform",
    "TypeScript", "Python", "Rust", "Swift", "React", "Next.js", "Tailwind",
]


def subject(text: str) -> str:
    """What th

    Only two kinds count: a product or project by name, and a plain "the X"
    noun phrase. A capitalised word is not a name — most of them are just the
    start of a sentence, which is how "want me to fix Every?" happened.
    """
    for term in PROPER_NAMES:
        if re.search(r"\b" + re.escape(term) + r"\b", text or "", re.I):
            return term

    # Anything else risks a severed phrase — "the wrong", "the dark" — which is
    # worse than a plain question. A name or nothing.
    return ""


def fallback_ask(sentences: list[str], takeaway_hint: str = "") -> str:
    """No explicit offer, so ask something that fits what was actually said.

    The tail of a message is often just its summary, so the whole thing is read.
    Trouble outranks unfinished work, which outranks finished work.
    """
    body = takeaway_hint or " ".join(sentences)
    tail = " ".join(sentences[-3:])

    # Trouble described in the body of a message is usually a bug being
    # explained after it was fixed. Only trouble still standing at the end is
    # trouble now.
    subject = subject_of(tail) or subject_of(body)
    about = f" on {subject}" if subject else ""

    resolved = FIXED_ALREADY.search(tail) or FINISHED_WORDS.search(tail)
    if TROUBLE_WORDS.search(tail) and not resolved:
        return f"Want me to fix {subject}?" if subject else "Want me to fix it?"
    if UNFINISHED.search(tail) or UNFINISHED.search(body):
        return f"Want me to finish {subject}?" if subject else "Want me to finish that?"
    if QUESTION_TO_YOU.search(tail):
        return "What would you like to do?"
    if FINISHED_WORDS.search(body):
        return f"Want me to keep going{about}?"
    return f"Anything to change{about}?" if subject else "Anything you want changed?"


def closing(paragraphs, all_sentences, ask_source: str, limit: int = 70) -> str:
    """The whole point it was making, not one sentence out of it.

    Two sentences was the original brief, but in use it means reading the
    screen for the rest, which is the thing this exists to avoid.
    """
    for paragraph in reversed(paragraphs):
        picked, count = [], 0
        for sentence in _sentences(paragraph):
            if sentence.endswith("?") or sentence == ask_source:
                continue
            words = len(sentence.split())
            if count + words > limit and picked:
                break
            picked.append(sentence)
            count += words
        if picked and count >= 8:
            return " ".join(picked)
    return ""


def briefing(assistant: str) -> dict:
    cleaned = _clean(assistant)
    paragraphs = [p for p in re.split(r"\n\s*\n", cleaned) if _sentences(p)]
    if not paragraphs:
        flat = " ".join(cleaned.split())
        return {"takeaway": _trim(flat) or "A session finished.",
                "ask": "Anything you want changed?", "action": "proceed"}

    all_sentences = [s for p in paragraphs for s in _sentences(p)]

    ask, action, ask_source = build_ask(paragraphs, all_sentences)
    action = action or "proceed"
    if not ask:
        ask = fallback_ask(all_sentences, takeaway_hint=cleaned)

    # The takeaway: a wrap-up paragraph leads with its point, so read the last
    # paragraph forwards rather than the whole message backwards.
    def usable(sentence: str) -> bool:
        if sentence.endswith("?") or sentence == ask or sentence == ask_source:
            return False
        if META.search(sentence):
            return False
        if sentence.lower().startswith(SKIP_STARTS):
            return False
        return len(WORD.findall(sentence)) >= 4

    takeaway = ""
    for paragraph in reversed(paragraphs):
        for sentence in _sentences(paragraph):
            if usable(sentence):
                takeaway = sentence
                break
        if takeaway:
            break
    if not takeaway:
        # Never read the question back as though it were the news.
        for sentence in all_sentences:
            if not sentence.endswith("?") and sentence != ask_source:
                takeaway = sentence
                break
    if not takeaway:
        for sentence in all_sentences:
            if not sentence.endswith("?"):
                takeaway = sentence
                break
    takeaway = OPENER.sub("", takeaway).strip()
    takeaway = takeaway[:1].upper() + takeaway[1:] if takeaway else "A session finished."
    full = closing(paragraphs, all_sentences, ask_source)

    everything = " ".join(s for s in all_sentences if not s.endswith("?"))
    return {"takeaway": _trim(takeaway), "full": full or _trim(takeaway),
            "everything": everything or takeaway, "ask": ask, "action": action}
