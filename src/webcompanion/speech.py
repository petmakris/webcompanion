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
import http.client
import json
import os
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
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
        e.close()
        raise SpeechError(502, f"Azure refused the token request (HTTP {e.code})") from None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
        raise SpeechError(502, f"Azure did not answer ({e.__class__.__name__})") from None
    if not token:
        raise SpeechError(502, "Azure returned an empty token")
    return {"token": token, "region": cfg.region, "expires_in": TOKEN_TTL_REPORTED}


MAX_SELECTION = 4000
MAX_CONTEXT = 4000
MAX_GLOSSARY = 200
SCRIPT_TIMEOUT_S = 45
CACHE_MAX_AGE_S = 30 * 86400
# Part of every cache key: changing the prompt or the schema must not serve
# scripts written under the old one. Bump it whenever either changes.
PROMPT_VERSION = 1

SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {"pieces": {"type": "array", "items": {
        "type": "object",
        "properties": {"say": {"type": "string"}, "src": {"type": "string"}},
        "required": ["say", "src"], "additionalProperties": False}}},
    "required": ["pieces"], "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You turn a passage a reader selected into a short spoken explanation. A
text-to-speech voice will read your words aloud while the reader looks at the
page.

Rules:
- Explain what the passage means and why it matters. Do not recite it.
- Use only what the selection, the surrounding context and the glossary say.
  Never guess what a term, acronym, name or code means. If nothing given to
  you defines an abbreviation, say it letter by letter (EDR becomes "E-D-R")
  and do not explain it.
- Expand an abbreviation the glossary or the context defines, the first time
  it is spoken.
- Say codes, numbers and symbols the way a person would: "06a/07a" becomes
  "zero-six-A and zero-seven-A", "21 Aug" becomes "August twenty-first", "&"
  becomes "and".
- Leave out citations that are not content, such as "transcript lines 29-34".
- Split the explanation into pieces of one or two sentences, in the order of
  the selection. Each piece's "src" is the exact substring of the selection
  that the piece explains, copied character for character.
- Plain spoken English. No markdown, lists, parentheses or emoji.
"""


def _glossary_lines(glossary: list) -> list[str]:
    lines = []
    for g in glossary:
        if not isinstance(g, dict) or not isinstance(g.get("term"), str) or not g["term"]:
            continue
        rest = " ".join(str(g.get(k) or "").strip() for k in ("definition", "role")).strip()
        lines.append(f"- {g['term']}: {rest}".rstrip(": ").rstrip())
    return lines


def build_prompt(selection: str, context: str, glossary: list, page_title: str) -> str:
    gl = "\n".join(_glossary_lines(glossary)) or "(none)"
    return (f"Page title: {page_title or '(none)'}\n\n"
            f"Glossary:\n{gl}\n\n"
            f"Surrounding context:\n{context or '(none)'}\n\n"
            f"Selection to explain:\n{selection}\n")


def cache_key(selection: str, context: str, glossary: list, page_title: str) -> str:
    raw = json.dumps([PROMPT_VERSION, selection, context, glossary, page_title],
                     sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _clean_pieces(raw, selection: str) -> list[dict]:
    """Keep every piece with something to say. A `src` that is not an exact
    substring of the selection is blanked rather than dropped: the model
    paraphrases, and losing a sentence of the explanation is worse than that
    sentence not lighting the page."""
    out = []
    for p in raw if isinstance(raw, list) else []:
        if not isinstance(p, dict):
            continue
        say, src = p.get("say"), p.get("src")
        if not isinstance(say, str) or not say.strip():
            continue
        ok = isinstance(src, str) and src != "" and src in selection
        out.append({"say": say.strip(), "src": src if ok else ""})
    return out


def _sweep_cache(cache_dir: Path) -> None:
    cutoff = time.time() - CACHE_MAX_AGE_S
    for f in cache_dir.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


# One lock per cache key, so identical concurrent requests run claude once:
# the second waits, then finds the first one's cache entry. An entry is
# removed when its last user leaves, so the dict does not grow.
_KEY_LOCKS: dict = {}
_KEY_LOCKS_GUARD = threading.Lock()


@contextmanager
def _key_lock(key: str):
    with _KEY_LOCKS_GUARD:
        entry = _KEY_LOCKS.setdefault(key, [threading.Lock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _KEY_LOCKS_GUARD:
            entry[1] -= 1
            if entry[1] == 0:
                _KEY_LOCKS.pop(key, None)


def write_script(cfg: SpeechConfig, cache_dir: Path, selection, context="",
                 glossary=None, page_title="") -> dict:
    if not isinstance(selection, str) or not selection.strip():
        raise SpeechError(400, "selection is required")
    if len(selection) > MAX_SELECTION:
        raise SpeechError(413, f"selection is over {MAX_SELECTION} characters")
    if glossary is None:
        glossary = []
    if not isinstance(glossary, list):
        raise SpeechError(400, "glossary must be a list")
    context = context[:MAX_CONTEXT] if isinstance(context, str) else ""
    page_title = page_title if isinstance(page_title, str) else ""
    glossary = glossary[:MAX_GLOSSARY]

    cache_dir = Path(cache_dir)
    key = cache_key(selection, context, glossary, page_title)
    with _key_lock(key):
        return _write_locked(cfg, cache_dir, cache_dir / f"{key}.json", selection,
                             context, glossary, page_title)


def _write_locked(cfg, cache_dir, hit, selection, context, glossary, page_title) -> dict:
    try:
        cached = json.loads(hit.read_text())["pieces"]
        if isinstance(cached, list) and cached:
            return {"pieces": cached, "cached": True}
    except (OSError, ValueError, KeyError, TypeError):
        pass

    claude = resolve_claude(cfg)
    if claude is None:
        raise SpeechError(503, "claude was not found; set CLAUDE_BIN in "
                               "~/.claude/webcompanion/speech.env")
    argv = [claude, "-p", "--model", "opus", "--tools", "",
            "--no-session-persistence", "--setting-sources", "",
            "--output-format", "json", "--json-schema", json.dumps(SCRIPT_SCHEMA),
            "--system-prompt", SYSTEM_PROMPT,
            build_prompt(selection, context, glossary, page_title)]
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True,
                                start_new_session=True)
    except OSError as e:
        raise SpeechError(503, f"could not run claude ({e.__class__.__name__})") from None
    try:
        stdout, stderr = proc.communicate(timeout=SCRIPT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        # claude may have children; kill the whole process group, not just it.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise SpeechError(504, f"writing the explanation took longer than "
                               f"{SCRIPT_TIMEOUT_S} s") from None
    except BaseException:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise

    try:
        envelope = json.loads(stdout)
    except ValueError:
        envelope = None
    if proc.returncode != 0 or not isinstance(envelope, dict) or envelope.get("is_error"):
        if isinstance(envelope, dict) and isinstance(envelope.get("result"), str):
            detail = envelope["result"]
        else:
            detail = (stderr or stdout or "").strip() or f"exit {proc.returncode}"
        raise SpeechError(502, f"claude could not write the explanation: {detail[:200]}")

    structured = envelope.get("structured_output")
    pieces = _clean_pieces(structured.get("pieces") if isinstance(structured, dict) else None,
                           selection)
    if not pieces:
        raise SpeechError(502, "claude returned no explanation")
    cache_dir.mkdir(parents=True, exist_ok=True)
    write_text_atomic(hit, json.dumps({"pieces": pieces}, ensure_ascii=False))
    _sweep_cache(cache_dir)
    return {"pieces": pieces, "cached": False}
