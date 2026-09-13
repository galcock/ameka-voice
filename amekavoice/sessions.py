"""Start and list Claude Code sessions that Ameka can address exactly.

A session running in a tmux pane can be written to by name, silently, without
stealing focus — however many are open. Sessions in the desktop app can only
receive what is typed at the front window, so this is how you get more than one
answerable session at a time.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import host
from .engines import find_bin

REGISTRY = Path(os.path.expanduser("~/.claude/sessions"))
# Where projects were on the first Mac this ran on. It stays as one place to
# look, not the only one: on any other Mac it does not exist, and a spoken
# project name answered with "I could not find a project" every single time.
PROJECT_ROOT = Path(os.path.expanduser("~/claudecode"))
_CONFIGURED_ROOT: Path | None = None


def configure(config) -> None:
    """Take the project root from config, if one is named."""
    global _CONFIGURED_ROOT
    named = str(config.section("behavior").get("project_root", "")).strip()
    _CONFIGURED_ROOT = Path(os.path.expanduser(named)) if named else None


def project_roots() -> list[Path]:
    """Every folder that holds projects, most deliberate first.

    The configured one, then the historical one, then wherever the sessions
    that are actually running live — their parent folders. Nothing to set up:
    a Mac with one Claude Code session open already says where its work is.
    """
    roots: list[Path] = []
    for root in (_CONFIGURED_ROOT, PROJECT_ROOT):
        if root is not None and root.is_dir() and root not in roots:
            roots.append(root)
    for live in live_sessions():
        cwd = live.get("cwd") or ""
        parent = Path(cwd).parent if cwd else None
        if parent and parent != parent.parent and parent.is_dir() and parent not in roots:
            roots.append(parent)
    return roots


def tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([find_bin("tmux") or "tmux", *args],
                          capture_output=True, text=True)


def live_sessions() -> list[dict]:
    """Every Claude Code session currently running, from its own registry."""
    out = []
    if not REGISTRY.exists():
        return out
    for path in REGISTRY.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not data.get("pid"):
            continue
        try:
            if not host.pid_alive(int(data["pid"])):      # still alive? asked, not signalled
                continue
        except (OSError, ValueError):
            continue
        out.append({
            "pid": data["pid"],
            "name": data.get("name") or "",
            "session_id": data.get("sessionId") or "",
            "cwd": data.get("cwd") or "",
            "project": Path(data.get("cwd") or "").name,
            "entrypoint": data.get("entrypoint") or "",
        })
    return sorted(out, key=lambda s: s["project"])


def tmux_panes() -> dict[str, str]:
    """tmux session name -> pane id."""
    return {name: pane for name, (pane, _agent) in tmux_pane_agents().items()}


def tmux_pane_agents() -> dict[str, tuple[str, str]]:
    """tmux session name -> (pane id, which agent is running in it)."""
    result = tmux("list-panes", "-a", "-F",
                  "#{session_name}\t#{pane_id}\t#{pane_current_command}")
    panes = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            command = parts[2].lower()
            agent = ("Codex" if "codex" in command else
                     "Claude Code" if "claude" in command or "node" in command else "")
            panes[parts[0]] = (parts[1], agent)
    return panes


FILLER = ("the", "a", "an", "my", "new", "project", "repo", "folder",
          "session", "under", "in", "on", "for", "with", "using", "called",
          "named", "at", "over",
          # trailing politeness: "open a session with Algo 8 now, please"
          "now", "please", "today", "again", "right", "then", "just", "ok",
          "okay", "thanks")


def hands_off(config, project: str) -> bool:
    """Is this a project she must not send to or start?

    Two ways of saying it. A focus list names what she may touch and bars
    everything else, which cannot go stale when a new project appears. A
    hands-off list names what she may not, for when most things are fair game.
    Focus wins where both are set.

    Checked in code rather than written into a prompt. A prompt is a request,
    and "stay out of this one" is not a request.
    """
    if not project:
        return False
    behavior = config.section("behavior")
    squash = lambda t: "".join(c for c in str(t).lower() if c.isalnum())
    # The label can be a whole session title — "Voice control for ChatGPT and
    # Claude sessions, in Ameka" — so either may contain the other.
    folder = resolve_project(project)
    names = [squash(project)] + ([squash(folder.name)] if folder else [])
    names = [n for n in names if n]

    def listed(entries) -> bool:
        for entry in entries:
            key = squash(entry)
            if key and any(key in n or n in key for n in names):
                return True
        return False

    focus = behavior.get("focus", []) or []
    if focus:
        return not listed(focus)
    return listed(behavior.get("hands_off", []) or [])


def why_barred(config, project: str) -> str:
    """Which of the two lists barred this, in the user's own words.

    "hands-off" was logged for a session kept out by the focus list, which is
    the opposite explanation: nobody had named it, and reading the log you
    would look for it on a list it was never on.
    """
    behavior = config.section("behavior")
    if behavior.get("focus", []):
        return "not in your focus list"
    return "on your hands-off list"


def resolve_project(name: str) -> Path | None:
    """Turn a spoken project name into a directory.

    Spoken, not spelled. A recogniser turns an invented product name into the
    nearest real word, so a name that sounds like a folder counts as that
    folder — this only ever picks between directories that already exist.
    """
    words = [w for w in re.split(r"[^a-z0-9]+", name.strip().lower()) if w]
    while words and words[0] in FILLER:
        words.pop(0)
    while words and words[-1] in FILLER:
        words.pop()
    wanted = "".join(words)
    if not wanted:
        return None
    candidate = Path(os.path.expanduser(name))
    if candidate.is_dir():
        return candidate
    folders = [p for root in project_roots() for p in root.iterdir() if p.is_dir()]
    if not folders:
        return None
    squash = lambda t: "".join(ch for ch in t.lower() if ch.isalnum())
    for folder in folders:                                   # exact first
        # Both sides squashed: "ameka-scratch" said aloud is "amekascratch",
        # and compared unsquashed it missed and fell through to "contains",
        # which handed back the Ameka folder instead.
        if squash(folder.name) == wanted:
            return folder
    holding = [f for f in folders                            # then contains
               if wanted in squash(f.name) or squash(f.name) in wanted]
    if holding:
        # Shortest wins. A short name sits inside half a dozen folder names, and
        # whichever the filesystem happened to list first is not an answer.
        return min(holding, key=lambda f: len(f.name))
    close = difflib.get_close_matches(                       # then sounds like
        wanted, [f.name.lower() for f in folders], n=1, cutoff=0.6)
    if close:
        return next(f for f in folders if f.name.lower() == close[0])
    return None


def ensure(project: str) -> tuple[bool, str]:
    """Make sure something is running for this project, without duplicating it.

    A fault she reports into a session nobody is running is a message in an
    empty room. This checks whether anything is already working on the project
    — in the app or in a pane — and only starts one when nothing is.
    """
    folder = resolve_project(project)
    if folder is None:
        return False, f"I could not find a project called {project}"
    name = folder.name.lower()
    for live in live_sessions():
        if (live.get("project") or "").lower() == name:
            return True, f"{folder.name} is already running"
    if folder.name in tmux_panes():
        return True, f"{folder.name} is already running"
    return start(project)


def start(project: str, first_message: str = "", command: str = "") -> tuple[bool, str]:
    """Open a coding session for a project in its own tmux pane.

    Whichever agent this Mac has: Claude Code if it is here, otherwise Codex.
    """
    folder = resolve_project(project)
    if folder is None:
        return False, f"I could not find a project called {project}"
    name = folder.name
    if host.IS_WIN:
        return False, f"I cannot open sessions on Windows yet. Open {name} in Claude Code and I will see it"

    # In the app he actually works in, first. A session he cannot see is not a
    # session as far as he is concerned, and tmux is invisible by design.
    # `command` is an agent binary for a tmux pane; `first_message` is what
    # gets typed into the new app session to bring it into being — the two
    # were one argument once, and a message went off as a shell command.
    if not command:
        for live in live_sessions():
            if live.get("cwd") == str(folder) and live.get("entrypoint") == "claude-desktop":
                open_app()
                return True, f"{name} is already open"
        if open_in_app(folder, first_message=first_message):
            return True, f"{name} is open"
        return False, f"I could not get a session started for {name}"

    if not find_bin("tmux"):
        return False, "tmux is not installed"
    agent = command or find_bin("claude") or find_bin("codex")
    if not agent:
        return False, "neither Claude Code nor Codex is installed"

    if name in tmux_panes():
        # Asking again for something you cannot see means show it to me.
        show(name)
        return True, f"{name} is already running"

    result = tmux("new-session", "-d", "-s", name, "-c", str(folder), "-x", "200", "-y", "50", agent)
    if result.returncode != 0:
        return False, (result.stderr or "tmux refused").strip()[:80]
    show(name)
    return True, f"{name} is running"


CLAUDE_APP = Path("/Applications/Claude.app")


@host.mac_only(False)
def open_in_app(folder: Path, first_message: str = "") -> bool:
    """Open the project as a session in the Claude Code app itself.

    A tmux session is exact and silent and completely invisible, which is no
    use at all: you start a session and expect to see one, in the window you
    already work in. The app opens the folder itself, so it appears where every
    other session of yours appears.

    A new session comes first. Opening a folder on its own repoints whichever
    session happens to be in front, so asking for one project quietly moved
    another session to it instead of starting anything.
    """
    if not CLAUDE_APP.is_dir():
        return False
    path = str(folder)
    script = f'''
tell application "Claude" to activate
delay 0.6
tell application "System Events" to tell process "Claude"
  click menu item "New Session" of menu 1 of menu bar item "File" of menu bar 1
  delay 1.0
  click menu item "Open Folder…" of menu 1 of menu bar item "File" of menu bar 1
end tell
delay 1.2
tell application "System Events"
  keystroke "g" using {{command down, shift down}}
  delay 0.5
  keystroke "{path}"
  delay 0.4
  key code 36
  delay 0.6
  key code 36
end tell
'''
    try:
        done = subprocess.run(["osascript", "-e", script],
                              capture_output=True, text=True, timeout=20)
    except Exception:
        return False
    if done.returncode != 0:
        return False
    # A session does not exist to the app until its first message is sent. The
    # menu and the dialog leave a blank session view pointed at the folder,
    # which is what "hands is open" used to mean — nothing in the sidebar,
    # nothing in the registry, nothing to type into. So she sends the first
    # message herself and waits for the app to write the session down.
    from . import route
    known = {f for f in APP_STORE.rglob("local_*.json")} if APP_STORE.is_dir() else set()
    time.sleep(0.8)
    ok, _ = route.deliver({"bundle_id": "com.anthropic.claudefordesktop"},
                          first_message or f"Ameka opened this session for {folder.name}. "
                                           f"Say in one sentence what you see in this folder.")
    if not ok:
        return False
    for _ in range(30):
        time.sleep(2)
        for f in APP_STORE.rglob("local_*.json"):
            if f in known:
                continue
            try:
                d = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if d.get("cwd") == str(folder):
                return True
    return False


# The Claude app is Electron, and Electron builds its accessibility tree only
# once something assistive asks for it — and drops it again whenever the
# renderer reloads: an account switch, a sign-in, an update. After that the
# window answers with four blank controls and no sidebar, and every switch
# reports "could not find it on screen" against a list that is not there.
# AXManualAccessibility is the switch that turns the tree back on.
WAKE_AX = (
    'tell application "System Events" to tell process "Claude"\n'
    '  try\n'
    '    set value of attribute "AXManualAccessibility" to true\n'
    '  end try\n'
    'end tell'
)


TRANSCRIPTS = Path(os.path.expanduser("~/.claude/projects"))
AMEKA_AX = Path(__file__).resolve().parent.parent / "native" / "ameka-ax"


def session_title(cwd: str, session_id: str) -> str:
    """What the sidebar calls this session, if it has been given a name.

    The registry knows the session as "ameka-1a"; the app draws "Ameka project
    catchup". The title is in the transcript, as the last custom-title line —
    and it is the one string that picks the session row out from the project
    header above it, which is a button reading just "Ameka".
    """
    if not cwd or not session_id:
        return ""
    known = app_titles().get(session_id)
    if known:
        return known
    folder = TRANSCRIPTS / cwd.replace("/", "-")
    path = folder / f"{session_id}.jsonl"
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:            # titles are set late; read the tail
            fh.seek(max(0, size - 400_000))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    title = ""
    for line in tail.splitlines():
        if '"custom-title"' in line:
            try:
                title = str(json.loads(line).get("customTitle") or "") or title
            except ValueError:
                pass
    return title.strip()


APP_STORE = Path(os.path.expanduser("~/Library/Application Support/Claude/claude-code-sessions"))


def app_titles() -> dict[str, str]:
    """The sidebar title of every session, custom or generated, keyed by the
    Claude Code session id — from the app's own store. The transcript only
    holds custom titles, and a session without one got matched to the project
    header instead of its row, which typed into whatever was on screen."""
    out: dict[str, str] = {}
    if not APP_STORE.is_dir():
        return out
    for f in APP_STORE.rglob("local_*.json"):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        sid, title = d.get("cliSessionId") or "", (d.get("title") or "").strip()
        if sid and title and not d.get("isArchived"):
            out[sid] = title
    return out


def on_screen(snippets: list[str], title: str = "") -> bool:
    """Is the session on screen the one meant? Its own last words should be in
    the transcript pane — the labels to the right of the sidebar. A click that
    landed on the project header, a scrolled transcript, or a stale window all
    fail this, and then nothing is typed."""
    if not AMEKA_AX.exists():
        return True                                    # no way to check: the old behaviour
    try:
        out = subprocess.run([str(AMEKA_AX), "text", "Claude"], capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return True
    squash = lambda t: re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()
    pane = squash(" ".join(line.split("\t", 1)[1] for line in out.splitlines()
                           if "\t" in line and line.split("\t", 1)[0].isdigit()
                           and int(line.split("\t", 1)[0]) >= 280))
    # The pane's header names the session it is showing — true mid-turn as
    # well, when the transcript's last words are off the top of the screen.
    if title and squash(title) and squash(title) in pane:
        return True
    wanted = [squash(w) for w in snippets if w and len(w.split()) >= 3]
    if not wanted:
        return True
    return any(w in pane for w in wanted)


def last_words(cwd: str, session_id: str, count: int = 10) -> list[str]:
    """Distinctive phrases from the end of the session's transcript — the last
    few hundred words of what was said, either side — so that whatever part of
    the tail the pane happens to show, one of them is in it."""
    path = TRANSCRIPTS / cwd.replace("/", "-") / f"{session_id}.jsonl"
    if not path.exists():
        return []
    try:
        lines = path.read_text(errors="ignore").splitlines()[-300:]
    except OSError:
        return []
    said: list[str] = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("isSidechain") or rec.get("isMeta"):
            continue
        content = (rec.get("message") or {}).get("content")
        texts = [content] if isinstance(content, str) else [
            c.get("text", "") for c in (content or []) if isinstance(c, dict) and c.get("type") == "text"]
        for t in texts:
            t = " ".join(t.split())
            if t and not t.startswith("<") and len(t.split()) >= 4:
                said.append(t)
    words = " ".join(said[-12:]).split()[-400:]
    if len(words) < 5:
        return [" ".join(words)] if words else []
    step = max(1, len(words) // (count + 1))
    return [" ".join(words[i:i + 5]) for i in range(0, len(words) - 4, step)][-count:]


_HOLDERS: dict[str, object] = {}


def hold_tree(app: str, log=None) -> None:
    """Keep one assistive client alive against an app, so its accessibility
    tree stays built.

    Chromium — the Claude app and the ChatGPT app both — builds its tree when
    something assistive asks and drops it when the last client goes away. Every
    other call here is a process that exits, so the next one arrived to "no
    tree" and a click that found no rows: for an afternoon every smart loop
    decided what to do and then could not reach the session to do it.
    """
    if not AMEKA_AX.exists() or not host.IS_MAC:
        return
    live = _HOLDERS.get(app)
    if live is not None and getattr(live, "poll", lambda: 0)() is None:
        return
    try:
        # Every restart of hers started another, and she restarts often while
        # she is being worked on: eleven were running, all setting the same
        # flag every five seconds, and the tree came back empty as often as
        # not. One per app, and the old ones go first.
        subprocess.run(["pkill", "-f", f"ameka-ax hold {app}"], capture_output=True, timeout=10)
        _HOLDERS[app] = subprocess.Popen([str(AMEKA_AX), "hold", app],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if log:
            log(f"holding {app}'s accessibility tree open")
    except Exception as exc:
        if log:
            log(f"could not hold {app}'s tree: {exc!r}")


def has_window(app: str) -> bool:
    """Does this app have a window at all?

    The Claude app can run with none — the process alive, the menu bar there,
    the sessions still going, and nothing on screen. Every click then reports
    "no tree", which reads like her being broken when the truth is that there
    is nowhere to type. Nothing scriptable brings the window back: not
    activate, not open -a, not File > New Session.
    """
    # Asked of the window server, not of accessibility. Accessibility reports
    # no windows whenever its tree has been torn down, and a 1512x909 window
    # plainly on screen was read as "the app has no window" — so she announced
    # that and stopped trying, which is worse than the fault it was meant to
    # explain.
    if AMEKA_AX.exists():
        try:
            out = subprocess.run([str(AMEKA_AX), "windows", app],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
            if out.isdigit():
                return int(out) > 0
        except Exception:
            pass
    return True                                    # cannot tell: carry on


def focus_row(app: str, title: str, group: str = "", log=None) -> bool:
    """Click a row by title in any app's sidebar — the Claude app, the ChatGPT
    app. Same helper, same rules: rows only, never a header."""
    if not AMEKA_AX.exists() or not title or len(title) < 4:
        return False
    args = [str(AMEKA_AX), "click", app, title] + (["--group", group] if group else [])
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=60)
    except Exception as exc:
        if log:
            log(f"helper failed on {app}: {exc!r}")
        return False
    out = done.stdout.strip()
    if out.startswith("yes"):
        return True
    if log:
        log(f"helper could not click {title!r} in {app}: {out or 'no answer'}")
    return False


def type_into_front(app: str, text: str, log=None) -> tuple[bool, str]:
    """Put the cursor in the front window's message box and send the text."""
    from . import route
    if AMEKA_AX.exists():
        try:
            got = subprocess.run([str(AMEKA_AX), "field", app],
                                 capture_output=True, text=True, timeout=25).stdout.strip()
        except Exception as exc:
            return False, f"could not find the message box ({exc!r})"
        if not got.startswith("yes"):
            return False, f"no message box in {app}"
    return route.deliver({"app_name": app}, text)


@host.mac_only(None)
def wake_accessibility() -> None:
    """Ask the Claude app to build its accessibility tree. Idempotent, cheap."""
    try:
        subprocess.run(["osascript", "-e", WAKE_AX], capture_output=True, timeout=8)
    except Exception:
        pass


@host.mac_only(False)
def focus_in_app(*names: str, log=None) -> bool:
    """Bring one session to the front of the Claude app.

    There is no deep link for an existing session and no menu item for it, so
    it clicks the session in the list the app draws down the side. Switching
    used to change only where she was pointed, which meant the next thing he
    said was typed into whichever session he happened to be looking at — not
    the one she had just told him she was talking to.

    The list shows each session's title — the sentence the app generated for
    it — and never the name in the registry, which is what this
    spent an afternoon looking for. Any name it is known by is tried, and only
    the sidebar counts: the same words appear in the middle of conversations,
    and clicking those scrolls a transcript while reporting success.
    """
    wants = [n.strip().replace("\\", "").replace('"', "")
             for n in names if n and n.strip()]
    if not wants or not CLAUDE_APP.is_dir():
        return False
    # Native first: it walks the tree in milliseconds and waits for Chromium to
    # rebuild it when it has been dropped. The script below does the same job
    # through System Events at three round trips per element, which on a
    # thousand-element sidebar is a minute — on the loop that owns the mic.
    # The first name is the session's title, as the sidebar draws it; the last
    # is its project. Without a title there is nothing safe to click — the
    # fallback names matched the project header and typed into whatever was
    # showing — so it is no click at all, and the caller says so.
    title, project = wants[0], wants[-1]
    if title == project or len(title) < 4:
        if log:
            log(f"no row to click for {project}: title {title!r} is not a session title")
        return False
    if AMEKA_AX.exists():
        try:
            done = subprocess.run([str(AMEKA_AX), "click", "Claude", title, "--group", project],
                                  capture_output=True, text=True, timeout=60)
            out = done.stdout.strip()
            if out.startswith("yes"):
                return True
            if out in ("no", "no tree", "no window") or out.startswith("no press"):
                # Say which of the several ways it failed: the row was not in
                # the sidebar, the tree was not built, there was no window.
                if log:
                    log(f"helper could not click {title!r}: {out or 'no answer'}"
                        + (f" [{done.stderr.strip()[:60]}]" if done.stderr.strip() else ""))
                return False
            if log:
                log(f"helper said {out[:60]!r} for {title!r}")
        except Exception as exc:
            if log:
                log(f"helper failed for {title!r}: {exc!r}")
    listed = ", ".join(f'"{w}"' for w in wants)
    script = f'''
with timeout of 90 seconds
  tell application "Claude" to activate
  delay 0.3
  set wanted to {{{listed}}}
  tell application "System Events" to tell process "Claude"
    try
      set value of attribute "AXManualAccessibility" to true
    end try
    set stuff to {{}}
    repeat with attempt from 1 to 3
      try
        set stuff to entire contents of window 1
      end try
      if (count of stuff) > 12 then exit repeat
      -- An empty tree: the renderer reloaded and dropped it. Wake it and wait.
      -- The window itself can disappear for a moment while it is rebuilt.
      try
        set value of attribute "AXManualAccessibility" to true
      end try
      delay 2.5
    end repeat
    if (count of stuff) < 12 then return "no tree"
    -- Through a variable: "item 1 of (size of window 1)" inline is a coercion
    -- error in System Events on this macOS, and it was failing every call.
    set sz to size of window 1
    set edge to (item 1 of sz) / 3
    repeat with pass from 1 to 2
      repeat with e in stuff
        try
          set v to (value of e) as text
          set p to position of e
          set z to size of e
          if v is not "" and (item 1 of p) < edge and (item 2 of z) > 0 then
            repeat with w in wanted
              set hit to false
              if pass is 1 then
                if v is (w as text) then set hit to true
              else
                if v contains (w as text) then set hit to true
              end if
              if hit then
                click at {{(item 1 of p) + ((item 1 of z) / 2), (item 2 of p) + ((item 2 of z) / 2)}}
                return "yes " & v
              end if
            end repeat
          end if
        end try
      end repeat
    end repeat
  end tell
  return "no"
end timeout
'''
    try:
        done = subprocess.run(["osascript", "-e", script],
                              capture_output=True, text=True, timeout=110)
    except Exception:
        return False
    return done.stdout.strip().startswith("yes")


@host.mac_only("")
def _window_fingerprint() -> str:
    """What the Claude window is showing, roughly. Empty if it cannot tell."""
    # Every static text in the window, not just the window's own children —
    # they are nested a dozen groups deep and asking for direct children
    # returns nothing at all, which made every switch report failure.
    script = ('with timeout of 60 seconds\n'
              'tell application "System Events" to tell process "Claude"\n'
              '  try\n'
              '    set value of attribute "AXManualAccessibility" to true\n'
              '  end try\n'
              '  get entire contents of window 1\n'
              'end tell\n'
              'end timeout')
    try:
        out = subprocess.run(["osascript", "-e", script],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return ""
    return hashlib.sha1(out.encode()).hexdigest() if out.strip() else ""


@host.mac_only(None)
def open_app() -> None:
    """Bring the Claude app forward, so an existing session is in front of you."""
    try:
        subprocess.run(["osascript", "-e", 'tell application "Claude" to activate'],
                       capture_output=True, timeout=8)
    except Exception:
        pass


@host.mac_only(False)
def show(name: str) -> bool:
    """Put the session on screen.

    A detached tmux session is a real session doing real work that you cannot
    see, so asking for one and being told it is running looks exactly like
    nothing happening. It opens in a window as well.
    """
    script = f'tell application "Terminal" to do script "tmux attach -t {name}"'
    try:
        done = subprocess.run(["osascript", "-e", script],
                              capture_output=True, timeout=8).returncode == 0
    except Exception:
        return False
    if done:
        subprocess.run(["osascript", "-e",
                        'tell application "Terminal" to activate'],
                       capture_output=True, timeout=8)
    return done


def report() -> list[str]:
    lines = []
    panes = tmux_panes()
    for session in live_sessions():
        where = "tmux" if session["project"] in panes else (session["entrypoint"] or "app")
        lines.append(f"  {session['project']:24} {session['name']:24} {where}")
    for name, pane in panes.items():
        if not any(s["project"] == name for s in live_sessions()):
            lines.append(f"  {name:24} {'(starting)':24} tmux {pane}")
    return lines or ["  no sessions running"]
