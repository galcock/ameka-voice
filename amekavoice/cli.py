"""ameka — command line entry point."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__, audio, commands, config as cfgmod, engines, keys, onboard, phone, route, selftest, sessions, setup_local, tools, host
from .daemon import Brain, Event
from .server import build_event, serve

HOOK_SRC = Path(__file__).resolve().parent.parent / "hooks" / "ameka_hook.py"
HOOK_DST = Path(os.path.expanduser("~/.claude/hooks/ameka_hook.py"))
SETTINGS = Path(os.path.expanduser("~/.claude/settings.json"))
PLIST = Path(os.path.expanduser("~/Library/LaunchAgents/ai.ameka.voice.plist"))


def _post(config, path: str, payload: dict, timeout: float = 3.0) -> dict:
    cfg = config.section("server")
    url = f"http://{cfg['host']}:{cfg['port']}{path}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# --------------------------------------------------------------- commands --
def cmd_serve(args, config) -> int:
    cfgmod.ensure_dirs()
    brain = Brain(config)
    if getattr(args, "chat", False):
        brain.chat = True
    if getattr(args, "target", ""):
        brain.bound = {"bundle_id": args.target}
    serve(brain)
    for note in route.preflight():
        brain.log("warn:", note)
    try:
        brain.run()
    except KeyboardInterrupt:
        brain.log("stopped")
    return 0


def cmd_say(args, config) -> int:
    engine = audio.speak(config, " ".join(args.text))
    print(f"[{engine}]")
    return 0


def cmd_listen(args, config) -> int:
    print("Speak after the tone…")
    audio.earcon(True)
    pcm = audio.record_until_silence(config)
    audio.earcon(False)
    if not pcm:
        print("Heard nothing.")
        return 1
    text = audio.transcribe(config, pcm)
    intent = commands.parse(text)
    print(f"heard   : {text!r}\nintent  : {intent.kind}"
          + (f"\npayload : {intent.text!r}" if intent.text else ""))
    return 0


def cmd_notify(args, config) -> int:
    payload = {
        "source": args.source,
        "text": args.text,
        "cwd": args.cwd or os.getcwd(),
        "session_id": args.session_id or "",
        "transcript_path": args.transcript or "",
        "bundle_id": os.environ.get("__CFBundleIdentifier", ""),
        "tmux_pane": os.environ.get("TMUX_PANE", ""),
        "app_name": os.environ.get("TERM_PROGRAM", ""),
    }
    try:
        print(json.dumps(_post(config, "/event", payload)))
    except urllib.error.URLError:
        print("ameka is not running — start it with `ameka serve`", file=sys.stderr)
        return 1
    return 0


def cmd_mute(args, config) -> int:
    try:
        print(json.dumps(_post(config, "/mute", {"muted": args.state == "on"})))
    except urllib.error.URLError:
        cfgmod.ensure_dirs()
        if args.state == "on":
            cfgmod.MUTE_FLAG.touch()
        elif cfgmod.MUTE_FLAG.exists():
            cfgmod.MUTE_FLAG.unlink()
        print(json.dumps({"ok": True, "muted": args.state == "on", "offline": True}))
    return 0


def cmd_status(args, config) -> int:
    cfg = config.section("server")
    try:
        with urllib.request.urlopen(
            f"http://{cfg['host']}:{cfg['port']}/health", timeout=2
        ) as resp:
            print(resp.read().decode())
    except urllib.error.URLError:
        print(json.dumps({"ok": False, "error": "not running"}))
        return 1
    return 0


def cmd_test(args, config) -> int:
    """Fake a finished session end to end, without touching a real one."""
    cfgmod.ensure_dirs()
    brain = Brain(config)
    event = Event(
        source="test", cwd=os.getcwd(), session_id="test",
        note=args.text or (
            "I rebuilt the checkout flow and every test passes. The staging deploy "
            "is queued but not promoted to production yet."
        ),
        target={"bundle_id": os.environ.get("__CFBundleIdentifier", ""),
                "tmux_pane": os.environ.get("TMUX_PANE", ""),
                "app_name": os.environ.get("TERM_PROGRAM", "")},
    )
    brain.handle(event)
    return 0


def cmd_doctor(args, config) -> int:
    ok = True
    print(f"Ameka Voice {__version__}\n")

    print("config")
    print(f"  file            : {cfgmod.CONFIG_PATH} "
          f"{'(found)' if cfgmod.CONFIG_PATH.exists() else '(missing — using defaults)'}")
    for provider in ("openai", "anthropic"):
        names = list(config.accounts(provider)) or ["default"]
        for name in names:
            acct = config.account(provider, name)
            if acct.usable:
                mark = "key set"
            elif acct.api_key:
                mark = "PLACEHOLDER KEY — edit the config file"
            else:
                mark = "no key"
            if not acct.usable and provider == "openai" and not engines.kokoro_available():
                ok = False
            print(f"  {provider:<10} {name:<12}: {mark}"
                  + (f"  base_url={acct.base_url}" if acct.base_url else ""))

    print("\nlocal engines (no key needed)")
    kok, whi = engines.kokoro_available(), engines.whisper_available()
    print(f"  voice (Kokoro)  : {'ready' if kok else 'missing — run: ameka setup-local'}")
    model = engines.whisper_model().name.replace("ggml-", "").replace(".bin", "")
    print(f"  hearing (whisper): {model if whi else 'missing — run: ameka setup-local'}")
    if not kok:
        print(f"  macOS fallback  : {engines.best_say_voice()}")

    print("\naudio")
    print(f"  sounddevice     : {'yes' if audio.HAVE_SD else 'NO (pip install sounddevice numpy)'}")
    if not audio.HAVE_SD:
        ok = False
    for line in audio.device_report()[:14]:
        print(line)
    voice_cfg, listen_cfg = config.section("voice"), config.section("listen")
    for label, want, kind in (("output", voice_cfg["output_device"], "output"),
                              ("input", listen_cfg["input_device"], "input")):
        if want:
            idx = audio.find_device(want, kind)
            print(f"  {label} match    : {want!r} -> "
                  + (f"device {idx}" if idx is not None else "NOT FOUND (using system default)"))

    print("\ndelivery")
    from . import brain as brain_mod
    print(f"  brain           : {brain_mod.describe()}")
    print(f"  tmux            : {'yes' if shutil.which('tmux') else 'no (optional)'}")
    notes = route.preflight()
    for note in notes:
        if "Accessibility" in note:
            ok = False
        print(f"  ! {note}")
    if not notes:
        print("  accessibility   : granted")

    print("\ntools on this Mac")
    for tool in tools.detect():
        print(f"  {tool['name']:<16}: {tool['detail']}")

    print("\nhooks")
    print(f"  hook script     : {HOOK_DST} {'(installed)' if HOOK_DST.exists() else '(not installed)'}")
    if SETTINGS.exists():
        try:
            data = json.loads(SETTINGS.read_text())
            wired = [k for k, v in (data.get("hooks") or {}).items() if "ameka" in json.dumps(v)]
            print(f"  claude settings : {', '.join(wired) if wired else 'not wired (run: ameka install)'}")
        except json.JSONDecodeError:
            print("  claude settings : unreadable")
    elif not tools.claude_code()["present"]:
        print("  claude settings : not needed — Claude Code is not on this Mac")
    if not ok:
        print("\nFix it with:  ameka setup-local   (free, on-device)"
              "\n         or:  ameka key openai    (uses your API credit)")
    print(f"\n{'READY' if ok else 'NOT READY — fix the items above'}")
    return 0 if ok else 1


def cmd_selftest(args, config) -> int:
    return selftest.run(config)


def cmd_setup(args, config) -> int:
    return onboard.run(connect_browser=args.browser)


def cmd_phone(args, config) -> int:
    """Turn on phone access and print how to set the iPhone up."""
    cfgmod.ensure_dirs()
    if getattr(args, "new_key", False):
        phone.new_token()
        print("new channel key issued — rebuild the Shortcut with the one below\n")
    path = cfgmod.CONFIG_PATH
    text = path.read_text() if path.exists() else ""
    if "[phone]" not in text:
        text = text.rstrip() + "\n\n[phone]\nenabled = true\n"
        path.write_text(text)
        path.chmod(0o600)
        print("phone access enabled in", path)
    elif "enabled = false" in text:
        path.write_text(text.replace("enabled = false", "enabled = true"))
        print("phone access enabled in", path)
    print("restart with:  launchctl kickstart -k gui/$(id -u)/ai.ameka.voice\n")
    print(phone.shortcut_recipe(config))
    return 0


def cmd_sessions(args, config) -> int:
    print("Sessions Ameka can see:")
    for line in sessions.report():
        print(line)
    return 0


def cmd_start(args, config) -> int:
    ok, message = sessions.start(" ".join(args.project))
    print(message)
    return 0 if ok else 1


def cmd_setup_local(args, config) -> int:
    return setup_local.run(skip_brew=args.no_brew)


def cmd_key(args, config) -> int:
    return keys.prompt_and_store(args.provider, args.account, check=not args.no_verify)


def cmd_you(args, config) -> int:
    """The user's brain: who they are, what they are working toward. Read by
    every smart loop and every answer she gives. `ameka you` shows where it is
    and what it says; `ameka you edit` opens it; `ameka you set <path>` points
    at a file that already exists — an Obsidian note, a CLAUDE.md."""
    from . import profile
    import subprocess
    if getattr(args, "path", None):
        target = Path(os.path.expanduser(args.path))
        if not target.is_file():
            print(f"no such file: {target}")
            return 1
        cfgmod.set_value("behavior", "profile_path", str(target))
        print(f"your brain -> {target}")
        return 0
    found = profile.path_for(config)
    if getattr(args, "edit", False) or found is None:
        target = found or Path(os.path.expanduser("~/.config/ameka/profile.md"))
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(profile.TEMPLATE)
            print(f"created {target}")
        subprocess.run(["open", "-t", str(target)] if host.IS_MAC else ["notepad", str(target)])
        return 0
    text = profile.load(config) or ""
    print(f"your brain: {found}  ({len(text)} characters)\n")
    print(text[:1200] + ("\n…" if len(text) > 1200 else ""))
    return 0


def cmd_keepalive(args, config) -> int:
    """Start the daemon if nothing is answering. What Windows runs every five
    minutes instead of launchd's KeepAlive — and must never start a second one."""
    port = int(config.section("server").get("port", 8765))
    if host.already_running(port):
        return 0
    import subprocess
    quiet = Path(sys.executable).with_name("pythonw.exe")
    python = str(quiet if quiet.exists() else sys.executable)
    flags = (0x00000008 | 0x00000200) if host.IS_WIN else 0
    subprocess.Popen([python, "-m", "amekavoice", "chat"], creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("started Ameka")
    return 0


def cmd_install(args, config) -> int:
    """Wire the Claude Code hooks and (optionally) the login agent."""
    cfgmod.ensure_dirs()
    if not tools.claude_code()["present"]:
        print(f"Claude Code is not on this {host.NAME} machine, so there are no hooks to wire.")
        print(f"Ameka will use what you do have: {tools.summary()}.")
        return 0
    HOOK_DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HOOK_SRC, HOOK_DST)
    HOOK_DST.chmod(0o755)
    print(f"hook  -> {HOOK_DST}")

    settings = {}
    if SETTINGS.exists():
        try:
            settings = json.loads(SETTINGS.read_text())
        except json.JSONDecodeError:
            print(f"! {SETTINGS} is not valid JSON — not touching it", file=sys.stderr)
            return 1
        shutil.copyfile(SETTINGS, SETTINGS.with_suffix(".json.ameka-backup"))

    hooks = settings.setdefault("hooks", {})
    entry = {"hooks": [{"type": "command",
                        "command": f'"{sys.executable}" "{HOOK_DST}"',
                        "timeout": 5}]}
    for event in ("Stop", "Notification"):
        existing = [m for m in hooks.get(event, []) if "ameka" not in json.dumps(m)]
        hooks[event] = existing + [entry]
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"wired -> {SETTINGS} (Stop, Notification)")

    if not cfgmod.CONFIG_PATH.exists():
        sample = Path(__file__).resolve().parent.parent / "config.example.toml"
        if sample.exists():
            shutil.copyfile(sample, cfgmod.CONFIG_PATH)
            print(f"config-> {cfgmod.CONFIG_PATH} (add your API keys)")

    if args.login_agent and host.IS_WIN:
        mode = "serve" if getattr(args, "no_chat", False) else "chat"
        # pythonw: no console window sitting on the taskbar for a background voice.
        quiet = Path(sys.executable).with_name("pythonw.exe")
        python = str(quiet if quiet.exists() else sys.executable)
        # At logon she starts; every five minutes `keepalive` starts her again
        # only if nothing is answering on her port.
        ok, how = host.install_startup([python, "-m", "amekavoice", mode],
                                       keepalive=[python, "-m", "amekavoice", "keepalive"])
        print(f"startup -> {how}")
        if not ok:
            print("  (she will not start at login until that is fixed, but she starts now)")
        # Start her regardless. The first Windows install "did not appear to do
        # anything" — if the scheduled task had failed, nothing would have.
        cmd_keepalive(args, config)
        import time
        for _ in range(20):
            time.sleep(0.5)
            if host.already_running(int(config.section("server").get("port", 8765))):
                print("running.")
                break
        else:
            print("! she did not come up. Run `ameka chat` in this window to see why, "
                  f"and look at {cfgmod.LOG_PATH}")
    elif args.login_agent:
        mode = "serve" if getattr(args, "no_chat", False) else "chat"
        # launchd hands a process a bare PATH, which loses Homebrew — and with it
        # whisper-cli, which is how Ameka hears you.
        launch_path = ":".join(dict.fromkeys(
            ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin",
             "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
            + [d for d in os.environ.get("PATH", "").split(":") if d]
        ))
        # Run from the app bundle, so every permission sheet says Ameka.
        from . import appbundle
        runner = appbundle.ready(__version__) or Path(sys.executable)
        extra = appbundle.env() if runner != Path(sys.executable) else {}
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.ameka.voice</string>
  <key>ProgramArguments</key>
  <array><string>{runner}</string><string>-m</string><string>amekavoice</string><string>{mode}</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>{cfgmod.LOG_PATH}</string>
  <key>StandardErrorPath</key><string>{cfgmod.LOG_PATH}</string>
  <key>WorkingDirectory</key><string>{Path(__file__).resolve().parent.parent}</string>
  <key>EnvironmentVariables</key><dict>
    <key>PYTHONPATH</key><string>{extra.get("PYTHONPATH", Path(__file__).resolve().parent.parent)}</string>
    <key>PATH</key><string>{launch_path}</string>
  </dict>
</dict></plist>
""")
        os.system(f"launchctl unload {PLIST} 2>/dev/null; launchctl load {PLIST}")
        print(f"agent -> {PLIST} (starts at login)")

    print("\nNext:  ameka doctor")
    return 0


# ------------------------------------------------------------------- parse --
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ameka", description="Run your coding sessions by voice.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the voice daemon")
    p.add_argument("--chat", action="store_true",
                   help="open mic: talk any time, not only after a briefing")
    p.add_argument("--target", default="", help="bundle id to type into (default: the Claude app)")

    p = sub.add_parser("chat", help="serve with the mic open — talk to your session")
    p.add_argument("--target", default="")
    sub.add_parser("doctor", help="check keys, audio, permissions, hooks")
    sub.add_parser("status", help="is the daemon up, and what is queued")

    p = sub.add_parser("say", help="speak a line in the Ameka voice")
    p.add_argument("text", nargs="+")

    sub.add_parser("listen", help="record one command and show what was understood")

    p = sub.add_parser("test", help="simulate a finished session end to end")
    p.add_argument("--text", default="")

    p = sub.add_parser("notify", help="push a briefing from any tool")
    p.add_argument("--text", default="")
    p.add_argument("--source", default="cli")
    p.add_argument("--cwd", default="")
    p.add_argument("--session-id", default="")
    p.add_argument("--transcript", default="")

    p = sub.add_parser("mute", help="silence or unsilence")
    p.add_argument("state", choices=["on", "off"])

    p = sub.add_parser("setup", help="first run: ask for permissions and find your sessions")
    p.add_argument("--browser", action="store_true", help="also connect Chrome (it restarts)")

    sub.add_parser("selftest", help="exercise every path and report what works")
    sub.add_parser("keepalive", help="start the daemon if it is not running (Windows scheduled task)")
    p = sub.add_parser("you", help="your brain: who you are and what you want, read by every smart loop")
    p.add_argument("edit", nargs="?", help="'edit' to open it, or 'set' with a path")
    p.add_argument("path", nargs="?", help="with 'set': a file that already holds your bio and goals")

    p = sub.add_parser("phone", help="use Ameka from your iPhone, anywhere")
    p.add_argument("--new-key", action="store_true", help="issue a fresh channel key")

    sub.add_parser("sessions", help="list every session Ameka can reach")

    p = sub.add_parser("start", help="open a Claude Code session for a project, in tmux")
    p.add_argument("project", nargs="+")

    p = sub.add_parser("setup-local", help="install the free on-device voice + hearing engines")
    p.add_argument("--no-brew", action="store_true", help="do not run brew install")

    p = sub.add_parser("key", help="store an API key safely (prompts, never echoed)")
    p.add_argument("provider", choices=["openai", "anthropic"])
    p.add_argument("--account", default="personal", help="account name (default: personal)")
    p.add_argument("--no-verify", action="store_true", help="skip the live check")

    p = sub.add_parser("install", help="install the Claude Code hooks")
    p.add_argument("--login-agent", action="store_true",
                   help="run it at login and restart it if it ever dies")
    p.add_argument("--no-chat", action="store_true",
                   help="login agent listens only after a briefing, not always")

    args = parser.parse_args(argv)
    config = cfgmod.load()
    if args.cmd == "chat":
        args.chat = True
    handlers = {
        "serve": cmd_serve, "chat": cmd_serve, "doctor": cmd_doctor, "status": cmd_status, "say": cmd_say,
        "keepalive": cmd_keepalive, "you": cmd_you,
        "listen": cmd_listen, "test": cmd_test, "notify": cmd_notify, "mute": cmd_mute,
        "key": cmd_key, "install": cmd_install, "setup-local": cmd_setup_local,
        "sessions": cmd_sessions, "start": cmd_start, "phone": cmd_phone, "setup": cmd_setup, "selftest": cmd_selftest,
    }
    return handlers[args.cmd](args, config)
