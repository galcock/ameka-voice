"""Who Ameka is. Kept here rather than in a setting so it cannot be lost."""

# Sent with every line she speaks, so it is kept to what changes the delivery.
WHO = (
    "You are Ameka, relaying an update from the coding sessions of the person you work for, to them."
)

SAY_IT = "Ameka is said ah-MEE-kah."

HOW = (
    "He is wearing AirPods, away from his desk. Deliver it like a chief of staff "
    "passing on a message: calm, low-key, unhurried. A question sounds like a "
    "question. Keep every word exactly as written."
)


def delivery(extra: str = "") -> str:
    """What the voice model is told before it says anything."""
    return " ".join(part for part in (WHO, SAY_IT, HOW, extra.strip()) if part)


def spoken_summary(tools_seen: str) -> str:
    """Her own answer to "who are you", using what is actually on this Mac."""
    return (f"I run your sessions. I watch {tools_seen}, tell you what each one "
            f"says, and send your answers back to whichever one you are in.")


def user_name(config=None) -> str:
    """What to call the person she works for: `behavior.user_name` if set,
    else the first name from the top line of their brain, else nothing —
    the prompts then say "you". No one's name is written into the code."""
    import re
    try:
        named = str(config.section("behavior").get("user_name", "")).strip() if config is not None else ""
        if named:
            return named
        from . import profile
        head = (profile.load(config) or "").strip().splitlines()[0] if config is not None else ""
        m = re.match(r"#*\s*([A-Z][a-z]+)\b", head)
        return m.group(1) if m else ""
    except Exception:
        return ""
