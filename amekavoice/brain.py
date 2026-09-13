"""Her own brain, embedded, and kept current.

He asked for three things in one breath: a proper brain of her own, inside the
app rather than a service beside it, always the newest — and no API keys
anywhere. This is that, done the way her hearing and voice are already done:
llama.cpp in the process, a model file in ~/.ameka/models beside whisper and
Kokoro, installed by the installer, nothing leaving the machine.

Which model is a question with a checkable answer, not a memory: the most
pulled 7-14B chat model from a trusted publisher on Hugging Face that fits this
machine's RAM and disk, at 4-bit. Today that is Qwen3.5-9B. Once a day she asks
again, and if the answer has changed she downloads the new one, loads it, makes
it write one briefing from a fixed transcript, and only then swaps it in — the
old file stays for a day in case the new one is worse.

What this brain is for is her own loop: the one-sentence briefing, what he
meant, where a thing goes. It is not the brain that reads a screen and works an
app — that stays Claude in the desktop app, which is his subscription and not
a key, and is the best there is at it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

MODELS = Path(os.path.expanduser("~/.ameka/models"))
HEADROOM = 8 * 1024 ** 3      # what the disk keeps, whatever the brain wants
BRAIN = MODELS / "brain.gguf"
PREVIOUS = MODELS / "brain.previous.gguf"
DOWNLOAD = MODELS / "brain.download"
MANIFEST = MODELS / "brain.json"

TRUSTED = {"qwen", "meta-llama", "google", "mistralai", "microsoft",
           "unsloth", "bartowski", "ggml-org", "lmstudio-community"}
# Not a general chat model, or not a chat model at all.
EXCLUDE = ("vl", "vision", "audio", "coder", "math", "embed", "rerank", "guard", "omni",
           "tts", "base", "pretrain", "distill", "r1", "reason", "think", "flux", "mtp",
           "abliterat", "uncensor", "roleplay", "rp-", "nsfw")
SIZES = ("7b", "8b", "9b", "12b", "14b")
QUANTS = ("Q4_K_M", "Q4_K_S", "IQ4_XS", "Q4_0")      # best first, all ~4-bit

# The check that a downloaded model is a working brain and not a large file.
PROBE_SYSTEM = ('You turn the tail of an AI coding session into ONE spoken sentence. '
                'Up to 25 words. Plain English. Return ONLY JSON: {"takeaway": "..."}')
PROBE_USER = ("What the session just said:\nI ran the full test suite after the boundary-term "
              "fix. All 41 tests pass, and the two that were failing on the moving boundary now "
              "agree with the geometry check. Want me to open the PR?")

_LOCK = threading.Lock()
_MODEL = None
_LOADED_PATH = ""
_LOADED_MTIME = 0.0


# ---------------------------------------------------------------- machine --
def budget_bytes() -> int:
    """The largest model file this machine should carry: a third of RAM for
    the weights (the rest is context, and everything else he is running), and
    never into the last few gigabytes of disk."""
    try:
        import subprocess
        ram = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip())
    except Exception:
        ram = 16 * 1024 ** 3
    try:
        free = shutil.disk_usage(MODELS if MODELS.exists() else Path.home()).free
    except Exception:
        free = 20 * 1024 ** 3
    # Replacing the brain frees the old one, so its size counts as room.
    for old in (BRAIN, PREVIOUS):
        if old.exists():
            free += old.stat().st_size
    # And the machine keeps eight gigabytes. The first download took this Mac
    # from eleven free to none — macOS, her own log, and everything he was
    # running stopped being able to write — and a brain is not worth that.
    return int(min(ram * 0.35, free - HEADROOM))


def runtime_available() -> bool:
    try:
        import llama_cpp  # noqa: F401
        return True
    except Exception:
        return False


def available() -> bool:
    return BRAIN.exists() and runtime_available()


def manifest() -> dict:
    try:
        return json.loads(MANIFEST.read_text())
    except Exception:
        return {}


def _write_manifest(**fields) -> None:
    data = manifest()
    data.update(fields)
    MODELS.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(data, indent=2))


# --------------------------------------------------------------- choosing --
def _api(url: str, timeout: float = 20.0):
    req = urllib.request.Request(url, headers={"User-Agent": "ameka-voice"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def candidates() -> list[dict]:
    """Chat models in the 7-14B band from publishers worth trusting, most
    pulled first. Pulls are the closest thing to a running vote on what is
    good this month."""
    seen: dict[str, dict] = {}
    for term in ("8B GGUF", "9B GGUF", "7B GGUF", "12B GGUF", "14B GGUF"):
        q = urllib.parse.urlencode({"search": term, "sort": "downloads", "direction": "-1", "limit": 60})
        try:
            rows = _api(f"https://huggingface.co/api/models?{q}")
        except Exception:
            continue
        for m in rows:
            mid = m.get("modelId", ""); low = mid.lower()
            author = low.split("/")[0]
            if author not in TRUSTED or "gguf" not in low:
                continue
            if any(x in low for x in EXCLUDE) or not any(s in low for s in SIZES):
                continue
            seen[mid] = {"repo": mid, "downloads": int(m.get("downloads", 0) or 0),
                         "updated": (m.get("lastModified") or "")[:10]}
    return sorted(seen.values(), key=lambda r: r["downloads"], reverse=True)


def pick() -> dict | None:
    """The model to run: the most pulled candidate that has a 4-bit file
    within this machine's budget."""
    limit = budget_bytes()
    for cand in candidates()[:12]:
        try:
            info = _api(f"https://huggingface.co/api/models/{cand['repo']}?blobs=true")
        except Exception:
            continue
        files = {s["rfilename"]: int(s.get("size") or 0) for s in info.get("siblings", [])
                 if s["rfilename"].endswith(".gguf") and "/" not in s["rfilename"]}
        for quant in QUANTS:
            for name, size in files.items():
                if quant in name and 0 < size <= limit:
                    return {"repo": cand["repo"], "file": name, "size": size,
                            "downloads": cand["downloads"], "updated": cand["updated"]}
    return None


# ------------------------------------------------------------- refreshing --
def due(hours: float = 24.0) -> bool:
    return time.time() - float(manifest().get("checked", 0)) >= hours * 3600


def _download(repo: str, name: str, size: int, log) -> bool:
    """Resumable, so a flaky evening does not start 5 GB over."""
    url = f"https://huggingface.co/{repo}/resolve/main/{urllib.parse.quote(name)}"
    MODELS.mkdir(parents=True, exist_ok=True)
    have = DOWNLOAD.stat().st_size if DOWNLOAD.exists() else 0
    if have > size:
        DOWNLOAD.unlink(); have = 0
    for attempt in range(8):
        if have >= size:
            break
        req = urllib.request.Request(url, headers={"User-Agent": "ameka-voice", "Range": f"bytes={have}-"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp, open(DOWNLOAD, "ab") as fh:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk); have += len(chunk)
        except Exception as exc:
            log(f"brain download paused at {have // (1 << 20)} MB ({exc!r}); resuming")
            time.sleep(min(60, 5 * (attempt + 1)))
    return DOWNLOAD.exists() and DOWNLOAD.stat().st_size == size


def probe(path: Path) -> tuple[bool, str]:
    """Load it and make it write one briefing. A brain that cannot do this is
    not a brain, whatever its download count."""
    try:
        from llama_cpp import Llama
        model = Llama(model_path=str(path), n_ctx=4096, n_gpu_layers=-1, verbose=False)
        started = time.time()
        out = _complete(model, PROBE_SYSTEM, PROBE_USER, max_tokens=80)
        took = time.time() - started
        del model
        text = (_json(out).get("takeaway") or "").strip()
        if not text or len(text.split()) > 45:
            return False, f"probe produced {out[:80]!r}"
        return True, f"probe ok in {took:.1f}s: {text[:60]!r}"
    except Exception as exc:
        return False, f"probe failed: {exc!r}"


def refresh(log=print, force: bool = False) -> str:
    """Ask what the newest good brain is; swap to it if it passes. Returns a
    one-line account for the log."""
    if not force and not due():
        return "checked recently"
    chosen = pick()
    if chosen is None:
        _write_manifest(checked=time.time())
        return "could not reach the model registry; keeping what I have"
    have = manifest()
    if BRAIN.exists() and have.get("repo") == chosen["repo"] and have.get("file") == chosen["file"]:
        _write_manifest(checked=time.time())
        return f"brain is current: {chosen['repo']} {chosen['file']}"
    log(f"newer brain: {chosen['repo']} {chosen['file']} ({chosen['size'] / 1e9:.1f} GB, "
        f"{chosen['downloads']:,} pulls) — downloading")
    # Room for the download: the rollback copy goes first, then it is a question
    # of whether the disk can hold two brains at once.
    if PREVIOUS.exists() and shutil.disk_usage(MODELS).free < chosen["size"] + HEADROOM:
        PREVIOUS.unlink()
    if shutil.disk_usage(MODELS).free < chosen["size"] + HEADROOM:
        _write_manifest(checked=time.time())
        return (f"not enough disk to download {chosen['file']} beside the current brain "
                f"({shutil.disk_usage(MODELS).free / 1e9:.1f} GB free); keeping what I have")
    if not _download(chosen["repo"], chosen["file"], chosen["size"], log):
        _write_manifest(checked=time.time())
        return "download did not complete; keeping what I have"
    ok, why = probe(DOWNLOAD)
    if not ok:
        DOWNLOAD.unlink(missing_ok=True)
        _write_manifest(checked=time.time(), rejected=chosen["repo"], rejected_why=why)
        return f"rejected {chosen['repo']}: {why}"
    with _LOCK:
        if PREVIOUS.exists():
            PREVIOUS.unlink()
        if BRAIN.exists():
            BRAIN.rename(PREVIOUS)
        DOWNLOAD.rename(BRAIN)
        _write_manifest(checked=time.time(), installed=time.time(), **chosen)
    return f"brain updated to {chosen['repo']} {chosen['file']} — {why}"


# ---------------------------------------------------------------- asking --
def _load():
    global _MODEL, _LOADED_PATH, _LOADED_MTIME
    from llama_cpp import Llama
    mtime = BRAIN.stat().st_mtime
    if _MODEL is None or _LOADED_PATH != str(BRAIN) or _LOADED_MTIME != mtime:
        _MODEL = Llama(model_path=str(BRAIN), n_ctx=8192, n_gpu_layers=-1, verbose=False)
        _LOADED_PATH, _LOADED_MTIME = str(BRAIN), mtime
    return _MODEL


def _complete(model, system: str, user: str, max_tokens: int = 160) -> str:
    # Qwen-family models think out loud by default; "/no_think" is the soft
    # switch, and anything that still arrives in think tags is stripped.
    out = model.create_chat_completion(
        messages=[{"role": "system", "content": system + " /no_think"},
                  {"role": "user", "content": user}],
        temperature=0.2, max_tokens=max_tokens)
    text = out["choices"][0]["message"]["content"] or ""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def _json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def ask(system: str, user: str, max_tokens: int = 160, timeout: float = 25.0, log=print) -> dict:
    """One question, one JSON answer. Empty when she has no brain or it took
    too long — the caller falls back to the rule-based path, as before.

    An empty answer used to be silent, and an empty answer reads downstream as
    "no brain": when the model stopped decoding at lunchtime, her judgement
    became templates and her briefings the rule-based extractor, with nothing
    in the log to say so. Every way of coming back empty now says which.
    """
    if not available():
        return {}
    result: dict = {}

    def run():
        global _MODEL
        try:
            with _LOCK:
                try:
                    result["out"] = _complete(_load(), system, user, max_tokens)
                except Exception as first:
                    # A model that will not decode is reloaded once. A slow
                    # answer beats a day of silent empty ones.
                    result["reloaded"] = repr(first)
                    _MODEL = None
                    result["out"] = _complete(_load(), system, user, max_tokens)
        except Exception as exc:
            result["error"] = repr(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    if result.get("reloaded"):
        log(f"brain would not decode ({result['reloaded'][:60]}) — reloaded it")
    if worker.is_alive():
        log(f"brain did not answer within {timeout:.0f}s")
        return {}
    if "out" not in result:
        log(f"brain failed: {str(result.get('error', 'no answer'))[:90]}")
        return {}
    parsed = _json(result["out"])
    if not parsed:
        log(f"brain answered but not in JSON: {result['out'][:80]!r}")
    return parsed


def describe() -> str:
    m = manifest()
    if not available():
        return "no embedded brain" + ("" if runtime_available() else " (llama.cpp runtime missing)")
    when = datetime.fromtimestamp(float(m.get("installed", 0))).strftime("%Y-%m-%d") if m.get("installed") else "?"
    return f"{m.get('repo', '?')} {m.get('file', '')} (installed {when})"
