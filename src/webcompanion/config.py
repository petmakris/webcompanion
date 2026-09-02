"""The daemon's only configuration source.

Every setting lives in one 0600 JSON file at a fixed path. Nothing here reads
os.environ, and that is deliberate: the five per-skill servers this package
replaces were launched by a Claude session and inherited its shell
environment, so WEBCOMPANION_BIND and friends worked. A launchd or systemd
service has a fixed environment those variables never reach, and a setting
that quietly stops applying is worse than one that never existed.

The file also carries the write token, which is why it is owner-only. The
token must survive restarts: an always-on service that remints it on every
start invalidates the IntelliJ plugin's credential mid-session.
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PORT = 3080
DEFAULT_BIND = "127.0.0.1"

# Unlike `retention_days` (disabled by default -- see cleanup.py's module
# docstring), auto-expiry is a safety net for skills that never call
# `webcompanion end`, per docs/2026-09-02-session-lifecycle-design.md
# Decision 1 -- it ships ON. 12 hours was picked against real data: of 33
# genuinely-live sessions observed on the running daemon, 32 were 1-3 days
# old and none were under 6 hours old, so 12 hours catches every observed
# abandoned session while leaving more than a full same-day working session's
# worth of headroom before a live review could be marked finished by mistake.
DEFAULT_IDLE_EXPIRY_HOURS = 12


@dataclass
class Config:
    port: int = DEFAULT_PORT
    token: str = ""
    bind: str = DEFAULT_BIND
    retention_days: int | None = None
    idle_expiry_hours: int | None = DEFAULT_IDLE_EXPIRY_HOURS
    workspace_root: Path | None = None


def config_path() -> Path:
    return Path(os.path.expanduser("~/.claude/webcompanion/config.json"))


def mint_token() -> str:
    return secrets.token_urlsafe(32)


def load(path: Path | None = None) -> Config:
    """The configuration, or defaults. Never raises."""
    path = Path(path) if path is not None else config_path()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return Config()
    if not isinstance(raw, dict):
        return Config()

    def _int(key: str, default):
        v = raw.get(key, default)
        return v if isinstance(v, int) and not isinstance(v, bool) else default

    def _str(key: str, default):
        v = raw.get(key, default)
        return v if isinstance(v, str) else default

    def _int_or_none(key: str, default):
        """Like `_int`, but an explicit JSON `null` means "disabled",
        distinct from the key being absent (which means "use `default`").

        `_int` alone cannot make that distinction -- `raw.get(key, default)`
        already returns `None` for an explicit `null`, same as for a missing
        key, so both collapse onto `default`. That is harmless for
        `retention_days`, whose default already IS `None`. It is not
        harmless here: `idle_expiry_hours` defaults to a real number, so
        collapsing an explicit `null` into that default would leave no way
        to turn the safety net off from the config file at all.
        """
        if key not in raw:
            return default
        v = raw[key]
        if v is None:
            return None
        return v if isinstance(v, int) and not isinstance(v, bool) else default

    # A relative workspace_root would resolve against the daemon's cwd, which
    # is not the directory anyone was thinking of. Scattering workspaces
    # silently is the exact failure this package exists to end, so a relative
    # value is dropped in favour of the default.
    ws_raw = raw.get("workspace_root")
    ws: Path | None = None
    if isinstance(ws_raw, str) and ws_raw.strip():
        candidate = Path(ws_raw).expanduser()
        if candidate.is_absolute():
            ws = candidate

    return Config(
        port=_int("port", DEFAULT_PORT),
        token=_str("token", ""),
        bind=_str("bind", DEFAULT_BIND),
        retention_days=_int("retention_days", None) if raw.get("retention_days") is not None else None,
        idle_expiry_hours=_int_or_none("idle_expiry_hours", DEFAULT_IDLE_EXPIRY_HOURS),
        workspace_root=ws,
    )


def write(cfg: Config, path: Path | None = None) -> None:
    """Write the config 0600, atomically.

    The mode is set on the temp file BEFORE the rename, so the token is never
    briefly world-readable at the destination path.
    """
    path = Path(path) if path is not None else config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "port": cfg.port,
        "token": cfg.token,
        "bind": cfg.bind,
        "retention_days": cfg.retention_days,
        "idle_expiry_hours": cfg.idle_expiry_hours,
        "workspace_root": str(cfg.workspace_root) if cfg.workspace_root else None,
    }
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="config.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, indent=2))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
