# Ameka

**The first voice-controlled AI that runs every agent on your machine — and keeps them running while you walk away.**

Ameka is a local layer over the AI agents you already use: Claude Code, Codex in the
ChatGPT app, and any AI conversation open in any browser. It has its own model, running
on your Mac. No API key. Nothing leaves the machine.

```bash
curl -fsSL https://ameka.ai/install.sh | bash
```

Windows, in PowerShell:

```powershell
irm https://ameka.ai/install.ps1 | iex
```

---

## Smart loops

Most assistants wait to be asked. Ameka puts every session on a **smart loop**: its own
timer, on which Ameka reads where that session actually is, decides what it needs, and
*does something about it*.

| The session is… | What the loop does |
|---|---|
| working | checks in, asking for one plain sentence at its next stopping point |
| finished | types in the next step it named for itself |
| waiting on you | answers if the answer is obvious and safe; otherwise you get the question and it is told to carry on meanwhile |
| stuck | tells it so, and asks what it needs |
| about to do something destructive or outward-facing | **nothing** — one sentence to you, and you decide |

Every session starts on a ten-minute loop. Each has its own switch, its own timer, and a
live countdown in the menu bar. One sentence closes each pass: *"Smart loop: check-in with
billing, api; the deploy needs you."*

The guardrails are code, not prompt. A regex — delete, force-push, deploy, pay, email —
turns any proposed action into a question for you. Nothing is typed into a session that is
mid-turn, on your hands-off list, or that Ameka has not first brought on screen and
**verified** is the session it meant.

## Your brain

Ameka reads a file of who you are and what you are working toward before every loop, so it
knows which sessions matter and which way to lean on "what's next".

```bash
ameka you edit             # write it
ameka you set ~/notes.md   # or point at one you already keep
```

It also *adds* to it — what it learns about your work from the sessions it watches goes in
a section marked as its own. Everything you wrote is never touched.

## Talking to it

There is no command word. Say what you mean.

| You say | It does |
|---|---|
| *"proceed"*, *"do it"*, *"ship it"* | sends your go-ahead to the session that just spoke |
| *"stop"*, *"hold on"* | sends nothing |
| *"tell billing to re-run it with the new cutoff"* | reads it back with where it is going, sends on your yes |
| *"talking to the api"* | points at another session and brings it up on screen |
| *"open a session for billing"* | starts one in the Claude Code app |
| *"what's going on?"* | one sentence, from what it knows right now |

Anything it is not sure of is read back before it is sent. Nothing is typed into a session
without you saying yes.

## No keys, nothing leaving the machine

| | |
|---|---|
| **Thinking** | llama.cpp with a 4-bit chat model in `~/.ameka/models`, chosen as the most-used model in its class that fits your machine, re-checked daily |
| **Hearing** | whisper.cpp |
| **Speaking** | Kokoro, a local neural voice |

All three install themselves. An OpenAI or Anthropic key is optional and off by default —
a key that happens to be present should never quietly cost you money.

## Requirements

macOS 13+ or Windows 10+, and Python 3.11+. macOS asks once for **Microphone** and once for
**Accessibility**; the sheets say Ameka, and say what each is for.

## Staying current

Ameka updates itself. Every installed copy asks `ameka.ai` twice an hour, verifies the
published hash, checks the code parses and is whole, and restarts on the new version — about
a second. You install it once.

## Commands

```
ameka doctor        check every engine, device and permission
ameka status        what it can see and what is on loop
ameka you           your brain: who you are, what you want
ameka sessions      every session it can reach
ameka mute on|off   silence it, bring it back
```

## Privacy

The daemon binds to `127.0.0.1`. No audio is stored, by us or anyone. There is no server on
our end to send anything to. What Ameka reads from your sessions stays on your machine.

---

Built with [Claude Code](https://claude.com/claude-code) · [ameka.ai](https://ameka.ai)
