"""Map a spoken phrase to an intent. Deliberately dumb, deliberately fast."""
from __future__ import annotations

import re
from dataclasses import dataclass

AFFIRM = {
    "proceed", "continue", "go", "go ahead", "go for it", "do it", "run it", "run",
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "affirmative", "confirm",
    "confirmed", "approve", "approved", "send it", "ship it", "carry on",
    "keep going", "keep gooing", "make it so", "green light", "fine", "correct",
    "sounds good", "lets go", "let's go", "hit it", "please do", "absolutely",
}
DENY = {
    "no", "nope", "nah", "stop", "hold", "hold on", "hold off", "wait", "cancel",
    "abort", "negative", "dont", "don't", "do not", "not yet", "leave it", "pause",
}
REPEAT = {"repeat", "again", "say again", "say that again", "what", "what was that",
          "pardon", "come again", "sorry"}
MORE = {"details", "more", "more detail", "more details", "explain", "expand",
        "tell me more", "why", "go deeper", "context"}
SKIP = {"skip", "next", "move on", "next one", "skip it"}
MUTE: set[str] = set()      # handled by SLEEP_RE below
UNMUTE: set[str] = set()    # handled by WAKE_RE below

# Sleep and wake are said a hundred different ways, so match the shape of the
# request rather than keeping a list of exact phrases.
# While asleep Ameka still hears, but only acts on something addressed to it by
# name. That is the only way "stop listening" can be undone by voice.
# A recogniser mangles a short isolated name badly: "Ameka" has come back as
# Vika, Amica, America and Mecca. Being generous costs a stray "Listening.";
# being strict leaves no way back from sleep, which is far worse.
# Unmistakable: nobody says these by accident, so they wake it anywhere.
NAME_STRONG = {"ameka", "amika", "ameeka", "amica", "ameca", "amaka", "emeka",
               "meeka", "vika", "veeka", "umeka", "omeka", "ahmeka", "amekah"}
# Real English words that a recogniser reaches for. Only count when the whole
# utterance is little more than the name itself.
NAME_WEAK = {"america", "americo", "mecca", "mica", "mika", "amiga", "annika"}
NAME_MIN_RATIO = 0.66
NAME_ONSET = set("aeioumv")          # "game" scores close to "ameka" otherwise
CLEAN_WORD = re.compile(r"[a-z']+")


def _sounds_like_name(word: str, short_utterance: bool) -> bool:
    from difflib import SequenceMatcher

    word = word.lower().strip("'")
    if len(word) < 4 or len(word) > 9:
        return False
    if word in NAME_STRONG:
        return True
    if word in NAME_WEAK:
        return True
    if word[0] not in NAME_ONSET or not short_utterance:
        return False
    return any(SequenceMatcher(None, word, form).ratio() >= NAME_MIN_RATIO
               for form in ("ameka", "amika", "ameeka"))


# Someone else's name at the front means the sentence was not for Ameka.
OTHER_ASSISTANT = re.compile(
    r"^\s*(?:hey\s+|ok(?:ay)?\s+|hi\s+)?"
    r"(alexa|echo|google|siri|grok|gemini|bixby|cortana|computer)\b",
    re.I,
)


def meant_for_someone_else(text: str) -> str:
    """The assistant being addressed, if it is not Ameka.

    "Hey Siri, Ameka" is the exception: Siri is being used to reach her.
    """
    match = OTHER_ASSISTANT.match(text or "")
    if not match:
        return ""
    rest = (text or "")[match.end():]
    addressed, _remainder = wake_split(rest)
    return "" if addressed else match.group(1).lower()


# Words that turn her name into a noun: the project, not the person.
NAME_IS_A_THING = {"session", "sessions", "project", "projects", "repo", "repos",
                   "folder", "directory", "code", "app", "one", "window", "tab"}
GREETINGS = {"hey", "hi", "ok", "okay", "yo", "hello", "so", "and", "um", "uh"}


def _position_of(raw: str, word: str) -> int:
    return raw.lower().find(word)


def wake_split(text: str) -> tuple[bool, str]:
    """(was it addressed to Ameka, what was said after the name).

    The name is looked for anywhere, not only at the front: "I said Ameka but
    it did not wake up" is plainly addressed to it. An explicit wake phrase
    counts on its own, because being stuck asleep with no way back is a far
    worse failure than waking when nobody asked.
    """
    raw = text or ""
    words = CLEAN_WORD.findall(raw.lower())
    short = len(words) <= 3
    for index, word in enumerate(words):
        if not _sounds_like_name(word, short):
            continue
        # Ameka is also a project on this Mac. "Open the Ameka session" was
        # being read as the name plus an instruction, so everything before it
        # was thrown away and ten words arrived as "session".
        if index + 1 < len(words) and words[index + 1] in NAME_IS_A_THING:
            continue
        # Being addressed means the name comes first, or after nothing but a
        # greeting, or is most of what was said. In the middle of a sentence it
        # is a word like any other.
        if not (short or index == 0
                or (index == 1 and words[0] in GREETINGS)
                or re.search(r"[.,;!?]\s*$", raw[:_position_of(raw, word)])):
            continue
        position = _position_of(raw, word)
        tail = raw[position + len(word):] if position >= 0 else ""
        return True, tail.strip(" ,.:;!?-")

    # "are you there", "wake up", "start listening" — no name needed.
    if WAKE_RE.search(raw.lower()):
        return True, ""
    return False, raw


SLEEP_RE = re.compile(
    r"\b(?:"
    r"(?:stop|quit|cease|pause|halt|hold|end|stop\s+the)\s+"
    r"(?:listening|recording|hearing|talking|listing)"
    r"|(?:do\s*n[o']?t|dont|don't|no\s+more)\s+"
    r"(?:listen|listening|record|recording|hear|hearing)"
    r"|(?:turn|switch|shut|power)\s+(?:off|down)\s+(?:the\s+)?"
    r"(?:mic|microphone|mike|listening|audio|voice|ears)"
    r"|(?:mic|microphone|mike|audio|voice)\s+off"
    r"|go\s+(?:to\s+)?(?:sleep|quiet|silent|dark)"
    r"|(?:go|going)\s+to\s+sleep"
    r"|(?:be\s+)?(?:quiet|silent)"
    r"|(?:leave|leaving)\s+me\s+alone"
    r"|ignore\s+me"
    r"|stand\s+down"
    r"|shut\s+(?:up|it)"
    r"|hold\s+(?:my\s+calls|on\s+a|off)"
    r"|take\s+a\s+(?:break|breather|minute|second)"
    r"|give\s+me\s+a\s+(?:minute|moment|second|sec)"
    r"|(?:not|nothing)\s+(?:right\s+)?now"
    r"|sleep\s*(?:mode|now)?"
    r"|mute(?:\s+yourself|\s+the\s+mic)?"
    r"|shush|hush|silence"
    r")\b",
    re.I,
)
WAKE_RE = re.compile(
    r"\b(?:"
    r"(?:start|resume|begin|carry\s+on|keep)\s+(?:listening|recording|hearing)"
    r"|(?:turn|switch|power)\s+(?:on|back\s+on)\s+(?:the\s+)?"
    r"(?:mic|microphone|mike|listening|audio|voice)"
    r"|(?:mic|microphone|mike|audio|voice)\s+(?:back\s+)?on"
    r"|wake\s*(?:up|back\s+up)?"
    r"|unmute|un-?mute"
    r"|(?:i'?m|im)\s+back"
    r"|(?:you|are\s+you)\s+there"
    r"|come\s+back"
    r"|listen\s*(?:up|again|to\s+me)?"
    r"|resume"
    r")\b",
    re.I,
)

STATUS = {"status", "queue", "whats pending", "what's pending", "what's in the queue",
          "anything else", "what else"}

FREEFORM_RE = re.compile(
    r"^\s*(?:tell\s+(?:it|him|her|them)|say|send|type|reply|respond|answer|ask\s+it)"
    r"\b[\s,:]+(?:to\s+)?",
    re.I,
)
VOCAB_ORDER = None  # populated below

INTENTS = "affirm deny repeat more skip mute unmute status switch freeform none".split()


@dataclass
class Intent:
    kind: str
    text: str = ""
    heard: str = ""


def normalize(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[^\w\s']", " ", text)
    return re.sub(r"\s+", " ", text).strip()


FILLER = {"uh", "um", "er", "please", "thanks", "thank", "you", "just"}

# Speech recognisers turn room noise into short grammatical-looking scraps
# ("be people", "the one", "and so"). None of it should reach a session.
NOISE_WORDS = {
    "a", "an", "the", "be", "been", "being", "is", "are", "was", "were", "am",
    "people", "person", "thing", "things", "one", "two", "some", "any", "all",
    "and", "or", "but", "so", "then", "of", "to", "in", "on", "at", "for",
    "with", "it", "its", "this", "that", "these", "those", "there", "here",
    "he", "she", "they", "them", "we", "us", "i", "me", "my", "your", "well",
    "its", "it's", "that's", "thats", "there's", "theres", "im", "i'm",
    "like", "know", "yeah", "hmm", "mm", "mhm", "ah", "oh", "eh", "huh",
    "uh", "um", "er", "okay", "right", "now", "very", "really", "actually",
    "please", "thanks", "thank", "yes", "no", "just", "get", "got", "go",
}


def is_noise(text: str) -> bool:
    """True when nothing was said that could be meant for a session.

    Only ever consulted for freeform speech — an exact command like "yes" or
    "proceed" is matched before this runs.
    """
    words = normalize(text).split()
    if not words:
        return True
    if len(words) > 6:
        return False                      # long enough to be a real sentence
    return all(word in NOISE_WORDS for word in words)


def _strip_filler(phrase: str) -> str:
    return " ".join(w for w in phrase.split() if w not in FILLER)


def _exact(phrase: str, vocab: set[str]) -> bool:
    return phrase in vocab or _strip_filler(phrase) in vocab


def _fuzzy(phrase: str, vocab: set[str]) -> bool:
    """A short utterance that is nothing BUT a command, once padding is removed.

    "yeah run it" is a yes. "run the tests now" is an instruction, not a yes, so
    every remaining word has to belong to the command vocabulary.
    """
    words = phrase.split()
    if not words or len(words) > 4:
        return False
    core = [w for w in words if w not in FILLER and w not in NOISE_WORDS]
    return bool(core) and all(word in vocab for word in core)


SWITCH_RE = re.compile(
    # "switch back to", "take me back to", "go over to" — the word between the
    # verb and "to" was enough to make this fall through and be typed into
    # someone's research conversation instead of moving the microphone.
    r"^\s*(?:let'?s\s+|can you\s+|could you\s+|please\s+|now\s+|okay,?\s+|ok,?\s+)?"
    r"(?:switch|go|change|move|jump|flip|hop|head|take me|bring me|put me|"
    r"send me|return)\s*(?:back|over|straight|right)?\s*(?:to|into|onto)\s+(.+?)[.?!]?\s*$|"
    r"^\s*(?:let'?s\s+|please\s+)?(?:talk|speak)\s+(?:back\s+)?to\s+(.+?)[.?!]?\s*$|"
    r"^\s*(?:back|return)\s+to\s+(.+?)[.?!]?\s*$",
    re.I,
)

START_SESSION = re.compile(
    r"^\s*(?:start|open|launch|spin up|fire up)\s+(?:a\s+)?(?:new\s+)?"
    r"(?:session|one)?\s*"
    # "under" is how you say it out loud as often as "in", and leaving it out
    # made the project name "under billing", which matches nothing.
    r"(?:in|on|for|with|under|using|inside|within|at|called|named|over in)?\s*"
    r"(?:the\s+)?(.+?)\s*(?:project|repo|folder)?[.?!]?\s*$",
    re.I,
)
# Said in the middle of a sentence, which is how anybody actually says it:
# "no need for that, so are you able to open a new session with Algo 8 now?"
# A preposition before the name is required here — without one, "no new session
# was open just now" reads as a request to open a project called "just now".
# Whether it really is a request is settled by whether the name is a project.
START_SESSION_IN = re.compile(
    r"\b(?:start|open|launch|spin up|fire up|boot up|kick off)\s+"
    r"(?:a|an|another|one|the)?\s*(?:new\s+)?"
    r"(?:session|window|instance|claude|claude code|codex)?\s*"
    r"(?:in|on|for|with|using|under|inside|within|at|called|named|over in)\s+"
    r"(?:the\s+)?([A-Za-z0-9][\w .'-]{1,40}?)"
    r"\s*(?:[.,!?]|$)",
    re.I,
)
NAME_THEM = re.compile(
    r"\b(name them|list them|what are they|read them|the names|name the sessions|"
    r"which ones|what are their names)\b", re.I)
# Asking has to look like asking. This used to match "sessions" near "open",
# which fired on "no new session was open just now" and read the whole list
# back to somebody who had not asked for it. The character classes exclude
# sentence endings so a request cannot be assembled out of two sentences.
LIST_SESSIONS = re.compile(
    r"\b(?:list|show|read|name)\b[^.?!]{0,24}\bsessions?\b"
    r"|\b(?:what|which|how many)\b[^.?!]{0,24}\bsessions?\b"
    r"|\bsessions?\b[^.?!]{0,16}\b(?:do i have|are (?:there|running|open|live))\b"
    r"|\bare\s+(?:there\s+)?any\b[^.?!]{0,20}\bsessions?\b",
    re.I,
)

# Questions put to Ameka rather than to your work. Answering "can you hear me?"
# by silently typing it into a session is the least useful thing it could do.
ABOUT_AMEKA = [
    (re.compile(r"\b(can|do) you (hear|understand) me\b|\bam i (coming through|audible)\b|"
                r"\bare you (hearing|getting) (me|this)\b", re.I), "hearing"),
    (re.compile(r"\bare you (there|awake|listening|on|alive|working|running)\b|"
                r"\byou there\b|\bstill there\b", re.I), "alive"),
    (re.compile(r"\b(which|what) session (am i|are we|is this)\b|\bwhere am i\b|"
                r"\bwho am i talking to\b|\bwhere is this going\b", re.I), "where"),
    (re.compile(r"\bwhat(?:'s| is| are) (you|ameka) (doing|up to)\b|\bwhat did you (just )?do\b",
                re.I), "doing"),
    (re.compile(r"\b(who|what) are you\b|\bwhat do you do\b|\bwhat is ameka\b|"
                r"\bwhat can you do\b|\bwhat are you in charge of\b", re.I), "identity"),
    (re.compile(r"\bwhat (microphone|mic) (are you (using|on)|is this)\b|"
                r"\bwhich (microphone|mic)\b", re.I), "microphone"),
]


VERBOSITY = [
    (re.compile(r"\b(read|tell) me everything\b|\bthe whole thing\b|\bread it all\b|"
                r"\bmore detail all the time\b|\bfull(er)? (answer|response)s?\b", re.I), "everything"),
    (re.compile(r"\bjust the (headline|takeaway|gist|summary)\b|\bkeep it short\b|"
                r"\bshorter\b|\bless detail\b|\bbe brief\b", re.I), "short"),
    (re.compile(r"\bmore detail\b|\bread the whole point\b|\blonger\b|"
                r"\bdon'?t make me read\b|\bstop making me read\b", re.I), "full"),
]


def verbosity_request(text: str) -> str:
    for pattern, level in VERBOSITY:
        if pattern.search(text or ""):
            return level
    return ""


def about_ameka(text: str) -> str:
    for pattern, kind in ABOUT_AMEKA:
        if pattern.search(text or ""):
            return kind
    return ""


CONNECT_BROWSER = re.compile(
    r"\b(connect|hook up|wire up|enable|turn on)\b.{0,12}\b(browser|chrome|chatgpt|tabs?)\b",
    re.I,
)

ORDER = ((MUTE, "mute"), (UNMUTE, "unmute"), (REPEAT, "repeat"), (MORE, "more"),
         (SKIP, "skip"), (STATUS, "status"), (DENY, "deny"), (AFFIRM, "affirm"))


def parse(text: str) -> Intent:
    """Exact command first, then 'tell it ...' dictation, then a loose match."""
    phrase = normalize(text)
    if not phrase:
        return Intent("none", heard=text or "")

    # Sleep and wake outrank everything: they must work mid-sentence and in
    # whatever words come out.
    if len(phrase.split()) <= 7:
        # Sleep first: "don't listen to me" contains "listen to me".
        if SLEEP_RE.search(phrase):
            return Intent("mute", heard=text)
        if WAKE_RE.search(phrase):
            return Intent("unmute", heard=text)

    for vocab, kind in ORDER:
        if _exact(phrase, vocab):
            return Intent(kind, heard=text)

    switch = SWITCH_RE.match(text.strip())
    if switch:
        name = next((g for g in switch.groups() if g), "").strip()
        if name:
            return Intent("switch", text=name, heard=text)

    stripped = FREEFORM_RE.sub("", text.strip())
    if stripped != text.strip() and stripped.strip(" ,."):
        return Intent("freeform", text=stripped.strip(" ,."), heard=text)

    for vocab, kind in ORDER:
        if _fuzzy(phrase, vocab):
            return Intent(kind, heard=text)

    return Intent("freeform", text=text.strip(), heard=text)
