"""Store an API key in the config file without it touching your shell history."""
from __future__ import annotations

import getpass
import json
import re
import urllib.error
import urllib.request

from . import config as cfgmod

PROVIDERS = {
    "openai": ("https://api.openai.com/v1/models", lambda k: {"authorization": f"Bearer {k}"}),
    "anthropic": ("https://api.anthropic.com/v1/models",
                  lambda k: {"x-api-key": k, "anthropic-version": "2023-06-01"}),
}


def verify(provider: str, key: str) -> tuple[bool, str]:
    url, headers = PROVIDERS[provider]
    req = urllib.request.Request(url, headers=headers(key))
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        models = [m.get("id", "") for m in data.get("data", [])]
        return True, f"{len(models)} models visible"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code} — {'bad key' if exc.code in (401, 403) else exc.reason}"
    except Exception as exc:
        return False, str(exc)[:120]


def write_key(provider: str, name: str, key: str) -> None:
    """Set api_key inside [accounts.<provider>.<name>], leaving the rest alone."""
    cfgmod.ensure_dirs()
    path = cfgmod.CONFIG_PATH
    text = path.read_text() if path.exists() else ""
    header = f"[accounts.{provider}.{name}]"
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')

    if header in text:
        start = text.index(header)
        end = text.find("\n[", start + 1)
        end = len(text) if end == -1 else end
        block = text[start:end]
        if re.search(r"^\s*api_key\s*=", block, re.M):
            block = re.sub(r'^\s*api_key\s*=.*$', f'api_key = "{escaped}"', block, count=1, flags=re.M)
        else:
            block = block.rstrip() + f'\napi_key = "{escaped}"\n'
        # a stale api_key_env would still win nothing, but drop it to avoid confusion
        block = re.sub(r'^\s*api_key_env\s*=.*$\n?', "", block, flags=re.M)
        text = text[:start] + block + text[end:]
    else:
        text = text.rstrip() + f'\n\n{header}\napi_key = "{escaped}"\n'

    path.write_text(text)
    path.chmod(0o600)


def prompt_and_store(provider: str, name: str, check: bool = True) -> int:
    label = "OpenAI" if provider == "openai" else "Anthropic"
    print(f"Paste your {label} API key. It is not echoed, not logged, and not kept in")
    print(f"your shell history — it goes straight into {cfgmod.CONFIG_PATH} (chmod 600).")
    key = getpass.getpass(f"{label} key: ").strip()
    if not key:
        print("Nothing entered.")
        return 1
    if provider == "openai" and not key.startswith("sk-"):
        print("That does not look like an OpenAI key (they start with 'sk-').")
        return 1

    if check:
        ok, detail = verify(provider, key)
        print(f"  {'verified' if ok else 'REJECTED'}: {detail}")
        if not ok:
            return 1

    write_key(provider, name, key)
    print(f"  saved to {cfgmod.CONFIG_PATH} as [accounts.{provider}.{name}]")
    return 0
