from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from webcompanion.config import Config, mint_token
from webcompanion.server import Daemon


@pytest.fixture
def daemon(tmp_path):
    cfg = Config(port=0, token=mint_token(), bind="127.0.0.1",
                 workspace_root=tmp_path / "ws")
    d = Daemon(cfg, state_root=tmp_path / "state")
    d.start()
    try:
        yield d
    finally:
        d.stop()


def raw_call(daemon, method, path, body=None, headers=None):
    """Make one HTTP call against a given (already-started) Daemon.

    Factored out of the `call` fixture so a test that needs a SECOND daemon
    instance against the same state root (e.g. a restart test) can reuse the
    same request logic instead of duplicating it.
    """
    url = daemon.url + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read().decode()
            parsed = json.loads(raw) if raw.strip().startswith(("{", "[")) else raw
            return r.status, parsed
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        return e.code, raw


@pytest.fixture
def call(daemon):
    def _call(method, path, body=None, headers=None, expect=None):
        return raw_call(daemon, method, path, body=body, headers=headers)
    return _call


@pytest.fixture
def wired(daemon, tmp_path, monkeypatch):
    """Point the CLI at the test daemon, not the real config file."""
    from webcompanion import config as cfgmod
    p = tmp_path / "config.json"
    cfgmod.write(cfgmod.Config(port=int(daemon.url.rsplit(":", 1)[1]),
                               token=daemon.cfg.token), p)
    monkeypatch.setattr(cfgmod, "config_path", lambda: p)
    return daemon
