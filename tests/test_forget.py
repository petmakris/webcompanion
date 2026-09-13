"""`forget` is the only route that destroys a user's data on request, so what it
refuses matters as much as what it removes."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from webcompanion import paths
from webcompanion.config import Config, mint_token
from webcompanion.registry import Registry
from webcompanion.server import Daemon, _is_terminal


def _daemon(tmp_path: Path) -> Daemon:
    cfg = Config(port=0, token=mint_token(), bind="127.0.0.1",
                 workspace_root=tmp_path / "ws")
    d = Daemon(cfg, state_root=tmp_path / "state")
    d.start()
    return d


def _call(daemon: Daemon, method: str, path: str, body=None):
    req = urllib.request.Request(
        daemon.url + path, method=method,
        data=json.dumps(body).encode() if body is not None else None)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _session(daemon: Daemon, kind: str = "deck", cwd: str = "/tmp/proj") -> str:
    status, body = _call(daemon, "POST", "/api/sessions", {"kind": kind, "cwd": cwd})
    assert status == 201, body
    return body["sid"]


def _workspace(daemon: Daemon, sid: str) -> Path:
    return Path(paths.base_of(daemon.registry.lookup(sid)))


# ── what it removes ─────────────────────────────────────────────────────

def test_forget_removes_the_row_and_the_whole_workspace(tmp_path):
    """`end` and `cancel` only write a marker, so before this a machine kept
    every session it had ever served and the only way to drop one was to stop
    the daemon and edit the registry by hand."""
    d = _daemon(tmp_path)
    try:
        sid = _session(d)
        _call(d, "PUT", f"/s/{sid}/items/note", {"text": "something"})
        base = _workspace(d, sid)
        assert base.is_dir()
        _call(d, "POST", f"/s/{sid}/api/finish")

        status, body = _call(d, "DELETE", f"/s/{sid}/")

        assert status == 200 and body["ok"] is True and body["sid"] == sid
        assert not base.exists()
        assert d.registry.lookup(sid) is None
        assert _call(d, "GET", f"/s/{sid}/items")[0] == 404
    finally:
        d.stop()


def test_the_removal_survives_into_the_registry_file(tmp_path):
    """Unregistering in memory alone would come back on the next restart — and
    the stray sweep would then delete the directory of whatever row was lost."""
    d = _daemon(tmp_path)
    try:
        sid = _session(d)
        _call(d, "POST", f"/s/{sid}/api/finish")
        _call(d, "DELETE", f"/s/{sid}/")
        on_disk = json.loads((tmp_path / "state" / "sessions.json").read_text())
    finally:
        d.stop()
    assert sid not in on_disk


def test_a_slug_names_a_session_as_well_as_a_sid(tmp_path):
    """Typing a sid by hand is what makes a delete verb unusable in practice."""
    d = _daemon(tmp_path)
    try:
        status, body = _call(d, "POST", "/api/sessions",
                             {"kind": "deck", "cwd": "/tmp/proj", "title": "AI Friday"})
        sid, slug = body["sid"], body["slug"]
        _call(d, "POST", f"/s/{sid}/api/finish")

        assert _call(d, "DELETE", f"/s/{slug}/")[0] == 200
        assert d.registry.lookup(sid) is None
    finally:
        d.stop()


# ── what it refuses ─────────────────────────────────────────────────────

def test_a_live_session_is_refused_and_nothing_is_deleted(tmp_path):
    """The one deletion nobody means to make is of something still running."""
    d = _daemon(tmp_path)
    try:
        sid = _session(d)
        base = _workspace(d, sid)

        status, body = _call(d, "DELETE", f"/s/{sid}/")

        assert status == 409
        assert "still live" in body
        assert base.is_dir()
        assert d.registry.lookup(sid) is not None
    finally:
        d.stop()


def test_force_deletes_a_live_session(tmp_path):
    """Refusing outright would leave a wedged session undeletable, which is the
    state this verb exists to end."""
    d = _daemon(tmp_path)
    try:
        sid = _session(d)
        base = _workspace(d, sid)

        assert _call(d, "DELETE", f"/s/{sid}/?force=1")[0] == 200
        assert not base.exists()
    finally:
        d.stop()


def test_an_unknown_session_is_a_404_not_a_deletion(tmp_path):
    d = _daemon(tmp_path)
    try:
        assert _call(d, "DELETE", "/s/260101-000000-nope/")[0] == 404
    finally:
        d.stop()


def test_a_row_pointing_outside_its_kinds_root_is_refused(tmp_path):
    """A registry row is data, and a corrupt or migrated one can name any path on
    the machine. Deleting what it points at would make one bad row into an
    arbitrary `rm -rf`, so the only path this removes is the one the kind's own
    root would have built for that sid."""
    state_root = tmp_path / "state"
    cfg = Config(port=0, token=mint_token(), bind="127.0.0.1",
                 workspace_root=tmp_path / "ws")
    elsewhere = tmp_path / "not-a-workspace"
    for sub in ("state", "items", "threads", "assets"):
        (elsewhere / sub).mkdir(parents=True)
    precious = elsewhere / "items" / "keep.json"
    precious.write_text("{}")

    reg = Registry(state_root)
    reg.rehydrate()
    sid = "260101-000000-abcdefabcdefabcd"
    reg.create("deck", sid, {
        "state_dir": str(elsewhere / "state"), "items_dir": str(elsewhere / "items"),
        "threads_dir": str(elsewhere / "threads"), "assets_dir": str(elsewhere / "assets"),
        "events_dir": str(elsewhere / "state"), "consumed_dir": str(elsewhere / "state"),
    }, {"title": "planted"}, "/tmp/proj")
    reg.persist()

    d = Daemon(cfg, state_root=state_root)
    d.start()
    try:
        status, body = _call(d, "DELETE", f"/s/{sid}/?force=1")
        assert status == 409
        assert "would have put it" in body
        assert precious.exists()
    finally:
        d.stop()


# ── the streams a deletion leaves behind ────────────────────────────────

def test_a_workspace_that_is_gone_reads_as_terminal(tmp_path):
    """A stream open on a forgotten session polls its state directory once a
    second. Without this it would poll a path that no longer exists for as long
    as the tab stayed open and never reach `session-ended`."""
    missing = tmp_path / "state" / "gone"
    assert _is_terminal(missing) is True

    live = tmp_path / "state" / "here"
    live.mkdir(parents=True)
    assert _is_terminal(live) is False
