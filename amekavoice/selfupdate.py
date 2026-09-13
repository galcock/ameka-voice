"""She picks up her own new code without being restarted.

Every fix today waited on somebody remembering to run a deploy script. Between
writing a fix and her actually having it there was a manual step, and more than
once an afternoon of edits sat in the repository looking deployed while the
running copy never changed.

So she watches where her code is written, and when it changes she installs it
and stops. Whatever supervises her starts her again a second later, running the
new code. Nothing about this is clever: the care is all in refusing to install
something half-written or something that does not parse.
"""
from __future__ import annotations

import ast
import os
import shutil
import time
from pathlib import Path

INSTALLED = Path(os.path.expanduser("~/.ameka/app/amekavoice"))
# Long enough that a file being written is finished being written. An editor
# saving a large file is not atomic, and installing half of one is worse than
# waiting three seconds.
SETTLED = 3.0


def source_dir(config) -> Path | None:
    named = str(config.section("behavior").get("source_dir", "")).strip()
    if not named:
        return None
    found = Path(os.path.expanduser(named))
    return found if found.is_dir() else None


def _mirrored(source: Path) -> list[tuple[Path, Path]]:
    """(source file, installed file) for everything the local watcher carries.

    The Python package, and the native helpers beside it: ameka-ax grew a
    click mode and the running copy kept the old binary, because only .py
    files were being watched — the daemon called the new mode and got the
    usage line back.
    """
    pairs = [(f, INSTALLED / f.name) for f in sorted(source.glob("*.py"))]
    native_src, native_dst = source.parent / "native", INSTALLED.parent / "native"
    if native_src.is_dir():
        for f in sorted(native_src.iterdir()):
            if f.is_file() and not f.name.startswith("."):
                pairs.append((f, native_dst / f.name))
    return pairs


def changed(source: Path) -> list[Path]:
    """Source files that differ from what is installed, and have settled."""
    if not INSTALLED.is_dir():
        return []
    now = time.time()
    out = []
    for file, target in _mirrored(source):
        try:
            stat = file.stat()
        except OSError:
            continue
        if now - stat.st_mtime < SETTLED:      # still being written
            continue
        try:
            if target.exists() and target.stat().st_mtime >= stat.st_mtime \
                    and target.read_bytes() == file.read_bytes():
                continue
        except OSError:
            pass
        try:
            if target.exists() and target.read_bytes() == file.read_bytes():
                continue
        except OSError:
            pass
        out.append(file)
    return out


# What the daemon must still be able to do. An edit that removes a method
# leaves a file that parses perfectly and a program that cannot start: a
# careless replacement took out the main loop along with the method it was
# aimed at, and the only warning was her restarting every five seconds.
ESSENTIAL = ("run", "handle", "chat_turn", "act_on", "speak", "listen_once")


def sound(source: Path) -> bool:
    """Does every file parse, and is the daemon still whole?"""
    for file in source.glob("*.py"):
        try:
            tree = ast.parse(file.read_text())
        except (SyntaxError, OSError):
            return False
        if file.name != "daemon.py":
            continue
        brain = next((n for n in ast.walk(tree)
                      if isinstance(n, ast.ClassDef) and n.name == "Brain"), None)
        if brain is None:
            return False
        have = {n.name for n in brain.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        if not set(ESSENTIAL) <= have:
            return False
    return True


MENU_APP = Path("/Applications/Ameka.app/Contents/MacOS/Ameka")


def install(files: list[Path]) -> list[str]:
    done = []
    for file in files:
        target = (INSTALLED.parent / "native" / file.name
                  if file.parent.name == "native" else INSTALLED / file.name)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target)
            done.append(file.name)
        except OSError:
            pass
        # The menu bar app is a copy in /Applications, started by its own
        # login agent; a new build of it has to go there too and be relaunched,
        # or the menu keeps showing what last year's binary knew.
        if file.name == "ameka-menu" and MENU_APP.exists():
            try:
                shutil.copy2(file, MENU_APP)
                import subprocess
                subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(MENU_APP.parents[2])],
                               capture_output=True, timeout=60)
                subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/ai.ameka.menu"],
                               capture_output=True, timeout=20)
                done.append("Ameka.app (relaunched)")
            except Exception:
                pass
    return done


# ------------------------------------------------------------------ remote --
# The Mac this was written on has the repository, so it watches the folder.
# Every other Mac has nothing to watch: whatever version was installed is the
# version it has for ever, and a fix made here reaches it only if its owner
# remembers to run the install command again. He will not. So a copy with no
# source folder asks ameka.ai instead, and a push here becomes an update there
# within the hour. Same care as the local path — nothing installs that does
# not parse or that would leave the daemon unable to start — plus a hash check
# against what the site says it published.
#
# What this trusts is the TLS connection to ameka.ai, exactly as the install
# command does. The hash proves the download is the file the site meant to
# serve; it does not prove who put it there. Signing is the next step.

LATEST_URL = "https://ameka.ai/downloads/latest.json"
APP = INSTALLED.parent                    # ~/.ameka/app — the whole install
VENV_PIP = Path(os.path.expanduser("~/.ameka/venv/bin/pip"))
STATE = Path(os.path.expanduser("~/.local/state/ameka/update.json"))


def _version_tuple(text: str) -> tuple:
    out = []
    for part in str(text).strip().split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def installed_version() -> str:
    try:
        from . import __version__
        return __version__
    except Exception:
        return "0"


def remote_due(config) -> bool:
    """Time to ask the site again? Never when there is a source folder to watch."""
    behavior = config.section("behavior")
    if not behavior.get("auto_update", True) or source_dir(config) is not None:
        return False
    every = float(behavior.get("update_minutes", 30) or 0)
    if every <= 0:
        return False
    try:
        import json
        last = float(json.loads(STATE.read_text()).get("checked", 0))
    except Exception:
        last = 0.0
    return time.time() - last >= every * 60


def mark_checked(**extra) -> None:
    import json
    STATE.parent.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(STATE.read_text())
    except Exception:
        state = {}
    state.update(checked=time.time(), **extra)
    STATE.write_text(json.dumps(state))


def remote_available(config) -> dict | None:
    """What the site is offering, if it is newer than what is running."""
    import json
    import urllib.request
    url = str(config.section("behavior").get("update_url", "") or LATEST_URL)
    req = urllib.request.Request(url, headers={"User-Agent": f"ameka-voice/{installed_version()}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        info = json.loads(resp.read().decode("utf-8"))
    version = str(info.get("version", ""))
    if not version or not info.get("url") or not info.get("sha256"):
        return None
    if _version_tuple(version) <= _version_tuple(installed_version()):
        return None
    return info


def remote_install(info: dict) -> str:
    """Download, verify, check, and swap in. Returns what was done, or raises."""
    import hashlib
    import tarfile
    import tempfile
    import urllib.request

    work = Path(tempfile.mkdtemp(prefix="ameka-update-"))
    try:
        archive = work / "release.tar.gz"
        with urllib.request.urlopen(info["url"], timeout=120) as resp, open(archive, "wb") as fh:
            shutil.copyfileobj(resp, fh)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != str(info["sha256"]).lower():
            raise RuntimeError(f"hash mismatch: got {digest[:12]}…, site says {str(info['sha256'])[:12]}…")

        staged = work / "staged"
        staged.mkdir()
        with tarfile.open(archive) as tar:
            members = tar.getmembers()
            for m in members:                 # nothing escapes the staging dir
                target = (staged / m.name).resolve()
                if not str(target).startswith(str(staged.resolve())):
                    raise RuntimeError(f"archive tried to write outside itself: {m.name}")
            tar.extractall(staged)
        tops = [p for p in staged.iterdir() if p.is_dir()]
        if len(tops) != 1:
            raise RuntimeError("archive does not hold exactly one release folder")
        release = tops[0]
        package = release / "amekavoice"
        if not (package / "daemon.py").exists():
            raise RuntimeError("archive holds no daemon")
        if not sound(package):
            raise RuntimeError("new code does not parse, or the daemon is not whole")
        shipped = ""
        for line in (package / "__init__.py").read_text().splitlines():
            if line.startswith("__version__"):
                shipped = line.split("=", 1)[1].strip().strip('"\'')
        if shipped != str(info["version"]):
            raise RuntimeError(f"archive says {shipped or '?'}, site says {info['version']}")

        # A dependency added upstream would otherwise be an ImportError at the
        # next start. Best effort, bounded, and never the reason an update fails.
        deps = _dependencies(release / "pyproject.toml")
        if deps and VENV_PIP.exists():
            import subprocess
            try:
                subprocess.run([str(VENV_PIP), "install", "--quiet", *deps],
                               check=False, timeout=300,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass

        # Swap each top-level piece in whole. The running process has already
        # imported what it needs, so replacing files under it is safe; only a
        # restart makes the new ones count.
        APP.mkdir(parents=True, exist_ok=True)
        for item in release.iterdir():
            dest = APP / item.name
            if dest.is_dir() and not dest.is_symlink():
                shutil.rmtree(dest, ignore_errors=True)
            elif dest.exists() or dest.is_symlink():
                dest.unlink()
            shutil.move(str(item), str(dest))
        return f"version {info['version']} from ameka.ai"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _dependencies(pyproject: Path) -> list[str]:
    try:
        text = pyproject.read_text()
    except OSError:
        return []
    start = text.find("dependencies")
    if start < 0:
        return []
    open_, close = text.find("[", start), text.find("]", start)
    if open_ < 0 or close < 0:
        return []
    return [p.strip().strip('"\'') for p in text[open_ + 1:close].split(",") if p.strip()]
