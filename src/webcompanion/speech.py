"""Speech for every skill's page: Azure tokens and spoken explanation scripts.

The page never holds a key. Through the /api/speech routes it asks this
module for a ten-minute Azure token, which the browser's Speech SDK uses
directly, and for a script that explains a selection in speakable pieces.

Stdlib only, like the rest of the daemon (pyproject.toml asserts
`dependencies = []`). Azure's Python SDK is a native package, which is why
synthesis and recognition run in the browser and only the token is made
here. The script is written by `claude -p` on the user's own subscription,
so no second model key exists anywhere.

Config lives in `<state_root>/speech.env` and is re-read on every request,
so editing it needs no restart. The key it holds must never reach a
response, an error message or a log line.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from webcompanion.atomic import write_text_atomic

ENV_FILE = "speech.env"
CACHE_DIR = "speech-cache"


class SpeechError(Exception):
    """A failure the route reports as `status` with `message` as its body."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class SpeechConfig:
    key: str | None
    region: str | None
    claude_bin: str | None


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines. `#` comments, blank lines, an `export ` prefix and
    one pair of surrounding quotes are allowed; anything else is skipped."""
    out: dict[str, str] = {}
    try:
        text = Path(path).read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[len("export "):].strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k:
            out[k] = v
    return out


def load(state_root: Path) -> SpeechConfig:
    env = read_env_file(Path(state_root) / ENV_FILE)
    return SpeechConfig(key=env.get("AZURE_SPEECH_KEY") or None,
                        region=env.get("AZURE_SPEECH_REGION") or None,
                        claude_bin=env.get("CLAUDE_BIN") or None)


def resolve_claude(cfg: SpeechConfig) -> str | None:
    """CLAUDE_BIN, then ~/.local/bin/claude, then PATH. launchd's PATH does
    not include ~/.local/bin, which is where the installer puts claude."""
    for c in (cfg.claude_bin,
              str(Path.home() / ".local" / "bin" / "claude"),
              shutil.which("claude")):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def status(cfg: SpeechConfig) -> dict:
    configured = bool(cfg.key and cfg.region)
    claude = resolve_claude(cfg) is not None
    out: dict = {"configured": configured, "claude": claude}
    if cfg.region:
        out["region"] = cfg.region
    if not configured:
        out["reason"] = ("AZURE_SPEECH_KEY and AZURE_SPEECH_REGION are not both "
                         "set in ~/.claude/webcompanion/speech.env")
    elif not claude:
        out["reason"] = ("claude was not found; set CLAUDE_BIN in "
                         "~/.claude/webcompanion/speech.env")
    return out


STS_URL = "https://{region}.api.cognitive.microsoft.com/sts/v1.0/issueToken"
# Azure's tokens live ten minutes. Reporting nine makes the page refresh a
# minute early instead of finding out from a failed synthesis.
TOKEN_TTL_REPORTED = 540


def mint_token(cfg: SpeechConfig) -> dict:
    if not (cfg.key and cfg.region):
        raise SpeechError(503, "speech is not configured: set AZURE_SPEECH_KEY and "
                               "AZURE_SPEECH_REGION in ~/.claude/webcompanion/speech.env")
    req = urllib.request.Request(STS_URL.format(region=cfg.region), data=b"",
                                 method="POST")
    req.add_header("Ocp-Apim-Subscription-Key", cfg.key)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            token = r.read().decode("ascii", "replace").strip()
    except urllib.error.HTTPError as e:
        raise SpeechError(502, f"Azure refused the token request (HTTP {e.code})") from None
    except (urllib.error.URLError, OSError) as e:
        raise SpeechError(502, f"Azure did not answer ({e.__class__.__name__})") from None
    if not token:
        raise SpeechError(502, "Azure returned an empty token")
    return {"token": token, "region": cfg.region, "expires_in": TOKEN_TTL_REPORTED}
