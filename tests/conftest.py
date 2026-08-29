from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from webcompanion import paths
from webcompanion.config import Config, mint_token
from webcompanion.server import Daemon

# Resolved at import time, BEFORE any test redirects HOME.
_REAL_PLAYWRIGHT_CACHE = next(
    (p for p in (Path.home() / "Library" / "Caches" / "ms-playwright",
                 Path.home() / ".cache" / "ms-playwright")
     if p.is_dir()), None)


@pytest.fixture(autouse=True)
def _never_the_real_state_root(request, tmp_path, monkeypatch):
    """No test may touch anything under the developer's real home.

    Earned the hard way while writing tests/test_integration.py: a test that
    called `migrate.run(["--apply"])` with only `config_path` patched still
    built its `Registry` from `paths.state_root()`, and moved its fixture
    into the developer's real workspace root. `migrate` MOVES data that
    cannot be recreated, and the five legacy roots it reads
    (`~/.claude/annotate`, ...) are real directories on a developer's
    machine -- so the blast radius of one unpatched call is a user's actual
    sessions, not a stray temp file.

    SOURCE AND DESTINATION, TOGETHER. Redirecting `paths.state_root()` alone
    protects only where data is written TO. `migrate._default_old_roots()`
    reads `Path.home()/".claude"/<skill>`, so a guard that stops at the
    destination still lets one `migrate.run(["--apply"])` MOVE a developer's
    real workspaces out of their home -- the move succeeds, it just lands
    somewhere else. So this fixture redirects both ends:

    - `HOME`, which is what `Path.home()` and `os.path.expanduser("~")`
      read, so anything computing a path from the home directory that is
      not named below lands in `tmp_path` too instead of escaping.
    - Every function that spells a real path out, patched BY NAME so it
      breaks loudly if it is ever renamed rather than silently reverting to
      the real path: `paths.state_root`, `cfgmod.config_path`,
      `migrate._default_old_roots`, and the three service paths
      `default_plist_path`, `default_unit_path`, `default_zipapp_path`.

    tests/test_migrate.py asserts this holds, by running the real
    `migrate.run(["--apply"])` under it and checking the real `~/.claude`
    -- found through `pwd`, not $HOME -- is untouched afterwards.

    A test that genuinely means to assert the real default path opts out
    with `@pytest.mark.real_state_root`, which is loud enough to be noticed
    in review. Everything else is redirected whether it asked or not.
    """
    if request.node.get_closest_marker("real_state_root"):
        return

    from webcompanion import config as cfgmod
    from webcompanion.commands import install_service as svc
    from webcompanion.commands import migrate

    # NOT created on disk: a test whose subject IS `tmp_path` (see
    # tests/test_threads.py) counts what is in there, and an empty directory
    # this fixture minted would be a stranger in that listing. `Path.home()`
    # does not require the directory to exist, and anything that genuinely
    # writes under it creates it on the way.
    fake_home = tmp_path / "_home"
    monkeypatch.setenv("HOME", str(fake_home))
    # Playwright resolves its downloaded-browser cache from HOME, so the one
    # test that drives a real browser would look for chromium inside the
    # fake home and find nothing there. Point it back at the real cache: it
    # is a read-only download cache, not user data, and a browser download
    # is not what this guard exists to contain.
    if _REAL_PLAYWRIGHT_CACHE is not None:
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH",
                           str(_REAL_PLAYWRIGHT_CACHE))

    monkeypatch.setattr(paths, "state_root", lambda: tmp_path / "_state_root")
    monkeypatch.setattr(
        cfgmod, "config_path",
        lambda: tmp_path / "_state_root" / "config.json")
    monkeypatch.setattr(
        migrate, "_default_old_roots",
        lambda: [fake_home / ".claude" / name for name in migrate._OLD_SKILLS])
    monkeypatch.setattr(
        svc, "default_plist_path",
        lambda label=svc.DEFAULT_LABEL: tmp_path / "_agents" / f"{label}.plist")
    monkeypatch.setattr(
        svc, "default_unit_path",
        lambda name=svc.DEFAULT_SERVICE_NAME: tmp_path / "_agents" / name)
    monkeypatch.setattr(
        svc, "default_zipapp_path",
        lambda: tmp_path / "_share" / "webcompanion.pyz")


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
