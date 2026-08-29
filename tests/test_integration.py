"""Failures that live BETWEEN modules, where per-module tests cannot see them.

Every other test file in this package is named after one module, and that is
exactly why the two worst bugs in 1.0.0 were invisible: `test_migrate.py`
never constructed a `Daemon`, `test_cleanup.py` never imported `migrate`, and
nothing wrote a corrupt `sessions.json` and then booted. Each module passed
its own tests; the pair destroyed data.

The rule for this file: every test here must drive at least two modules that
have no import relationship, in the order a real user hits them.
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from webcompanion import paths
from webcompanion.commands import migrate, watch
from webcompanion.config import Config, mint_token
from webcompanion.registry import Registry
from webcompanion.server import Daemon

LEGACY_SID = "250101-120000-aaaabbbbccccdddd"


def _legacy_root(tmp_path: Path, kind: str = "annotate") -> Path:
    """One session in the on-disk shape the five per-skill servers wrote."""
    old_root = tmp_path / "old" / kind
    base = old_root / "sessions" / LEGACY_SID
    (base / "state").mkdir(parents=True)
    (base / "response").mkdir(parents=True)
    (base / "response" / "blocks.json").write_text(
        json.dumps({"blocks": [{"id": "b1", "text": "an irreplaceable note"}]}))
    (old_root / "sessions.json").write_text(json.dumps({LEGACY_SID: {
        "state_dir": str(base / "state"),
        "response_dir": str(base / "response"),
        "_cwd": str(tmp_path),
    }}))
    (old_root / "sessions_meta.json").write_text(json.dumps({
        LEGACY_SID: {"slug": "montblanc", "title": "Mont Blanc"}}))
    return old_root


def _post_session(daemon: Daemon, cwd: Path, kind: str = "deck") -> str:
    req = urllib.request.Request(
        daemon.url + "/api/sessions", method="POST",
        data=json.dumps({"kind": kind, "cwd": str(cwd)}).encode())
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())["sid"]


# ── C1: migrate writes rows the running daemon must not clobber ──────────
def test_a_running_daemons_persist_does_not_erase_what_migrate_registered(tmp_path):
    """The README's own order destroyed every migrated workspace.

    `install-service` starts the daemon, THEN the user runs `migrate
    --apply`. Two processes own `sessions.json`. The daemon's next
    `persist()` used to rewrite it wholesale from its own memory -- which
    never contained migrate's 30 rows -- and the next startup sweep then
    reclaimed 30 unregistered sid-shaped directories as strays.
    """
    state_root = tmp_path / "state"
    cfg = Config(port=0, token=mint_token(), bind="127.0.0.1",
                 workspace_root=tmp_path / "ws")
    old_root = _legacy_root(tmp_path)

    daemon = Daemon(cfg, state_root=state_root)
    daemon.start()
    try:
        # A second process: migrate's own Registry on the same state root.
        migrating = Registry(state_root)
        migrating.rehydrate()
        migrate.apply(migrate.plan([old_root]), cfg, migrating)
        assert LEGACY_SID in json.loads((state_root / "sessions.json").read_text())

        # Anything at all that makes the daemon persist.
        _post_session(daemon, tmp_path)

        rows = json.loads((state_root / "sessions.json").read_text())
        assert LEGACY_SID in rows, "the daemon erased the migrated row"
        assert len(rows) == 2
    finally:
        daemon.stop()

    # And the next boot must not reclaim it as a stray.
    again = Daemon(cfg, state_root=state_root)
    again.start()
    try:
        assert (cfg.workspace_root / "annotate" / LEGACY_SID).is_dir()
    finally:
        again.stop()


def test_persist_does_not_resurrect_a_row_whose_workspace_was_deleted(tmp_path):
    """The merge is a union filtered by what exists on disk, not a blind
    union. `expire`/`prune_dead_rows` unregister a row BECAUSE its workspace
    is gone; re-adding it from the file would undo every cleanup pass."""
    state_root = tmp_path / "state"
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(state_root)
    sid = "250101-120000-1111222233334444"
    dirs = paths.make_session_dirs(cfg, "annotate", sid)
    reg.create("annotate", sid, dirs, {"title": "t"}, str(tmp_path))
    reg.persist()

    import shutil
    shutil.rmtree(paths.base_of(dirs))
    reg.unregister(sid)
    reg.persist()

    assert sid not in json.loads((state_root / "sessions.json").read_text())


def test_migrate_apply_refuses_while_the_daemon_is_answering(tmp_path, monkeypatch):
    """The other half of C1: even with a merging persist, running the two
    writers concurrently is not a supported shape. `migrate --apply` refuses
    while `/health` answers and says what to do instead."""
    cfg = Config(port=0, token=mint_token(), bind="127.0.0.1",
                 workspace_root=tmp_path / "ws")
    daemon = Daemon(cfg, state_root=tmp_path / "state")
    daemon.start()
    try:
        from webcompanion import config as cfgmod
        cfg_file = tmp_path / "config.json"
        cfgmod.write(cfgmod.Config(port=int(daemon.url.rsplit(":", 1)[1]),
                                   token=daemon.cfg.token), cfg_file)
        monkeypatch.setattr(cfgmod, "config_path", lambda: cfg_file)
        monkeypatch.setattr(migrate, "_default_old_roots",
                            lambda: [_legacy_root(tmp_path)])
        assert migrate.run(["--apply"]) == 1
        # ...and it moved nothing.
        assert (tmp_path / "old" / "annotate" / "sessions" / LEGACY_SID).is_dir()
    finally:
        daemon.stop()


def test_migrate_into_still_rehearses_while_the_daemon_is_answering(tmp_path, monkeypatch):
    """A rehearsal copies and registers into a throwaway root, so it never
    races the daemon's registry. Refusing it too would leave a user with no
    way to rehearse without stopping their service."""
    cfg = Config(port=0, token=mint_token(), bind="127.0.0.1",
                 workspace_root=tmp_path / "ws")
    daemon = Daemon(cfg, state_root=tmp_path / "state")
    daemon.start()
    try:
        from webcompanion import config as cfgmod
        cfg_file = tmp_path / "config.json"
        cfgmod.write(cfgmod.Config(port=int(daemon.url.rsplit(":", 1)[1]),
                                   token=daemon.cfg.token), cfg_file)
        monkeypatch.setattr(cfgmod, "config_path", lambda: cfg_file)
        monkeypatch.setattr(migrate, "_default_old_roots",
                            lambda: [_legacy_root(tmp_path)])
        assert migrate.run(["--into", str(tmp_path / "rehearsal")]) == 0
        assert (tmp_path / "rehearsal" / "annotate" / LEGACY_SID).is_dir()
        assert (tmp_path / "old" / "annotate" / "sessions" / LEGACY_SID).is_dir()
    finally:
        daemon.stop()


# ── C2: an unreadable registry must not authorise deletion ───────────────
def test_a_corrupt_sessions_file_does_not_let_the_boot_sweep_delete_workspaces(tmp_path):
    """`rehydrate` used to swallow the JSONDecodeError and return, so an
    empty registry was indistinguishable from "nothing is live" -- and the
    stray sweep deletes exactly what no row points at."""
    state_root = tmp_path / "state"
    cfg = Config(port=0, token=mint_token(), bind="127.0.0.1",
                 workspace_root=tmp_path / "ws")

    daemon = Daemon(cfg, state_root=state_root)
    daemon.start()
    try:
        for _ in range(3):
            _post_session(daemon, tmp_path, kind="annotate")
    finally:
        daemon.stop()
    before = sorted(p.name for p in (cfg.workspace_root / "annotate").iterdir())
    assert len(before) == 3

    (state_root / "sessions.json").write_text('{"250101-120000-aaaa')

    again = Daemon(cfg, state_root=state_root)
    again.start()
    try:
        after = sorted(p.name for p in (cfg.workspace_root / "annotate").iterdir())
        assert after == before
        marker = json.loads((state_root / "startup_sweep_failed.json").read_text())
        assert "could not be parsed" in marker["refused"]
    finally:
        again.stop()
    # The unreadable file itself is preserved, not clobbered by the new one.
    assert list(state_root.glob("sessions.json.corrupt-*"))


def test_an_empty_registry_facing_a_populated_workspace_root_refuses_the_sweep(tmp_path):
    """The second untrusted shape: the file is absent or empty but the
    workspace root is full. Deleting every workspace is never a legitimate
    outcome of an ordinary boot."""
    cfg = Config(port=0, token=mint_token(), bind="127.0.0.1",
                 workspace_root=tmp_path / "ws")
    orphan = paths.kind_root(cfg, "annotate") / "250101-120000-9999888877776666"
    (orphan / "items").mkdir(parents=True)

    daemon = Daemon(cfg, state_root=tmp_path / "state")
    daemon.start()
    try:
        assert orphan.is_dir()
        marker = json.loads(
            (tmp_path / "state" / "startup_sweep_failed.json").read_text())
        assert "registry is empty" in marker["refused"]
    finally:
        daemon.stop()


# ── I2: watch must resolve a sid, never create one ───────────────────────
def test_watch_against_an_unknown_sid_fails_instead_of_creating_a_workspace(
        tmp_path, capsys, monkeypatch):
    """`watch --sid <typo>` used to mkdir a six-directory tree and then
    block forever printing nothing. The same call also recreated `state_dir`
    for a workspace that had just been reaped, so the documented
    WEBCOMPANION_CANCELLED-on-reap could never fire."""
    cfg = Config(port=0, token=mint_token(), bind="127.0.0.1",
                 workspace_root=tmp_path / "ws")
    daemon = Daemon(cfg, state_root=tmp_path / "state")
    daemon.start()
    try:
        from webcompanion import config as cfgmod
        cfg_file = tmp_path / "config.json"
        cfgmod.write(cfgmod.Config(port=int(daemon.url.rsplit(":", 1)[1]),
                                   token=daemon.cfg.token,
                                   workspace_root=cfg.workspace_root), cfg_file)
        monkeypatch.setattr(cfgmod, "config_path", lambda: cfg_file)
        monkeypatch.setattr(paths, "state_root", lambda: tmp_path / "state")

        typo = "250101-120000-deadbeefdeadbeef"
        rc = watch.run(["--kind", "annotate", "--sid", typo])
        assert rc == 1
        assert "no such session" in capsys.readouterr().err
        assert not (cfg.workspace_root / "annotate" / typo).exists()
    finally:
        daemon.stop()


def test_watch_resolves_a_real_session_without_touching_the_filesystem(tmp_path, monkeypatch):
    """The positive half: a sid that IS registered resolves to the very
    directories the daemon created, read out of the registry."""
    cfg = Config(port=0, token=mint_token(), bind="127.0.0.1",
                 workspace_root=tmp_path / "ws")
    daemon = Daemon(cfg, state_root=tmp_path / "state")
    daemon.start()
    try:
        sid = _post_session(daemon, tmp_path, kind="annotate")
        monkeypatch.setattr(paths, "state_root", lambda: tmp_path / "state")
        dirs = watch.resolve_session_dirs("annotate", sid)
        assert Path(dirs["state_dir"]) == cfg.workspace_root / "annotate" / sid / "state"
    finally:
        daemon.stop()


@pytest.mark.parametrize("kind", ["annotate", "deck"])
def test_a_reaped_workspace_ends_the_watch_instead_of_being_recreated(
        tmp_path, kind, monkeypatch):
    """Resolution reads the registry; it does not mkdir. So a workspace that
    was reaped between resolution and the loop leaves `state_dir` absent,
    which is what makes WEBCOMPANION_CANCELLED reachable at all."""
    import io
    import shutil

    cfg = Config(workspace_root=tmp_path / "ws")
    sid = "250101-120000-5555444433332222"
    dirs = paths.make_session_dirs(cfg, kind, sid)
    shutil.rmtree(paths.base_of(dirs))

    out = io.StringIO()
    rc = watch.watch_loop(kind, sid, dirs["state_dir"], dirs["events_dir"],
                          dirs["consumed_dir"], out=out, poll_seconds=0.01)
    assert rc == 0
    assert "WEBCOMPANION_CANCELLED" in out.getvalue()
