from __future__ import annotations

import os
import urllib.parse
import urllib.request

from webcompanion.config import Config, mint_token
from webcompanion.server import Daemon

from conftest import raw_call


def _session(call, cwd):
    return call("POST", "/api/sessions", {"kind": "stage", "cwd": str(cwd), "title": "T"})[1]


def _project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "deck" / "assets").mkdir(parents=True)
    (proj / "deck" / "index.html").write_text("<p>deck</p>")
    (proj / "deck" / "assets" / "logo.svg").write_text("<svg/>")
    return proj


def test_a_mount_inside_the_project_serves_its_files(tmp_path, call):
    proj = _project(tmp_path)
    s = _session(call, proj)
    status, body = call("POST", f"/s/{s['sid']}/api/mounts",
                        {"name": "deck-1", "root": str(proj / "deck")})
    assert status == 200
    assert body == {"name": "deck-1", "url": "mounts/deck-1/"}
    assert call("GET", f"/s/{s['sid']}/mounts/deck-1/index.html") == (200, "<p>deck</p>")
    assert call("GET", f"/s/{s['sid']}/mounts/deck-1/assets/logo.svg")[0] == 200


def test_a_mount_is_never_cached(tmp_path, call, daemon):
    proj = _project(tmp_path)
    s = _session(call, proj)
    call("POST", f"/s/{s['sid']}/api/mounts", {"name": "deck-1", "root": str(proj / "deck")})
    with urllib.request.urlopen(daemon.url + f"/s/{s['sid']}/mounts/deck-1/index.html") as r:
        assert r.headers["Cache-Control"] == "no-store"
        assert r.headers["Content-Type"].startswith("text/html")


def test_a_mount_serves_a_file_with_a_space_in_its_name(tmp_path, call):
    proj = _project(tmp_path)
    (proj / "deck" / "My Deck.html").write_text("spaced")
    s = _session(call, proj)
    call("POST", f"/s/{s['sid']}/api/mounts", {"name": "deck-1", "root": str(proj / "deck")})
    path = urllib.parse.quote("My Deck.html")
    assert call("GET", f"/s/{s['sid']}/mounts/deck-1/{path}") == (200, "spaced")


def test_a_root_outside_the_project_is_refused(tmp_path, call):
    proj = _project(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    s = _session(call, proj)
    status, _ = call("POST", f"/s/{s['sid']}/api/mounts", {"name": "x", "root": str(elsewhere)})
    assert status == 403


def test_a_bad_name_or_root_is_refused(tmp_path, call):
    proj = _project(tmp_path)
    s = _session(call, proj)
    assert call("POST", f"/s/{s['sid']}/api/mounts", {"name": "Bad Name", "root": str(proj)})[0] == 400
    assert call("POST", f"/s/{s['sid']}/api/mounts", {"name": "ok", "root": str(proj / "nope")})[0] == 400
    assert call("POST", f"/s/{s['sid']}/api/mounts", {"name": "ok"})[0] == 400


def test_a_symlink_out_of_the_mount_is_refused(tmp_path, call):
    proj = _project(tmp_path)
    (proj / "secret.txt").write_text("password")
    os.symlink(proj / "secret.txt", proj / "deck" / "link.txt")
    s = _session(call, proj)
    call("POST", f"/s/{s['sid']}/api/mounts", {"name": "deck-1", "root": str(proj / "deck")})
    assert call("GET", f"/s/{s['sid']}/mounts/deck-1/link.txt")[0] == 403
    assert call("GET", f"/s/{s['sid']}/mounts/deck-1/../secret.txt")[0] in (403, 404)


def test_an_unknown_mount_or_file_is_404(tmp_path, call):
    proj = _project(tmp_path)
    s = _session(call, proj)
    assert call("GET", f"/s/{s['sid']}/mounts/nothing/index.html")[0] == 404
    call("POST", f"/s/{s['sid']}/api/mounts", {"name": "deck-1", "root": str(proj / "deck")})
    assert call("GET", f"/s/{s['sid']}/mounts/deck-1/missing.html")[0] == 404


def test_a_mount_survives_a_daemon_restart(tmp_path):
    cfg = Config(port=0, token=mint_token(), bind="127.0.0.1", workspace_root=tmp_path / "ws")
    state_root = tmp_path / "state"
    proj = _project(tmp_path)
    d1 = Daemon(cfg, state_root=state_root)
    d1.start()
    try:
        _, s = raw_call(d1, "POST", "/api/sessions", {"kind": "stage", "cwd": str(proj), "title": "T"})
        assert raw_call(d1, "POST", f"/s/{s['sid']}/api/mounts",
                        {"name": "deck-1", "root": str(proj / "deck")})[0] == 200
    finally:
        d1.stop()
    d2 = Daemon(cfg, state_root=state_root)
    d2.start()
    try:
        assert raw_call(d2, "GET", f"/s/{s['sid']}/mounts/deck-1/index.html") == (200, "<p>deck</p>")
    finally:
        d2.stop()
