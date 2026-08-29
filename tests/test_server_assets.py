from __future__ import annotations

from webcompanion.config import Config, mint_token
from webcompanion.server import Daemon

from conftest import raw_call

def test_the_runtime_is_served_by_the_daemon(call):
    status, body = call("GET", "/_wc/core.js")
    assert status == 200
    assert "WebCompanion" in body


def test_the_shell_page_loads_the_runtime_and_the_registered_entry(tmp_path, call):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "app.js").write_text("console.log('renderer')")
    s = call("POST", "/api/sessions",
             {"kind": "annotate", "cwd": "/p", "title": "T"})[1]
    assert call("POST", f"/s/{s['sid']}/api/assets",
                {"static_root": str(bundle), "entry": "app.js"})[0] == 200
    status, html = call("GET", f"/s/{s['sid']}/")
    assert status == 200
    assert "/_wc/core.js" in html
    assert "assets/app.js" in html


def test_a_client_registers_its_own_renderer_and_it_is_served(tmp_path, call):
    bundle = tmp_path / "bundle"
    (bundle / "sub").mkdir(parents=True)
    (bundle / "app.js").write_text("console.log('renderer')")
    (bundle / "sub" / "style.css").write_text("body{}")
    s = call("POST", "/api/sessions",
             {"kind": "annotate", "cwd": "/p", "title": "T"})[1]
    assert call("POST", f"/s/{s['sid']}/api/assets",
                {"static_root": str(bundle)})[0] == 200
    assert call("GET", f"/s/{s['sid']}/assets/app.js")[1] == "console.log('renderer')"
    assert call("GET", f"/s/{s['sid']}/assets/sub/style.css")[0] == 200


def test_assets_cannot_escape_the_registered_root(tmp_path, call):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (tmp_path / "secret.txt").write_text("password")
    s = call("POST", "/api/sessions",
             {"kind": "annotate", "cwd": "/p", "title": "T"})[1]
    call("POST", f"/s/{s['sid']}/api/assets", {"static_root": str(bundle)})
    assert call("GET", f"/s/{s['sid']}/assets/../secret.txt")[0] in (403, 404)


def test_a_session_with_no_registered_renderer_still_serves_a_shell(call):
    s = call("POST", "/api/sessions",
             {"kind": "annotate", "cwd": "/p", "title": "T"})[1]
    status, html = call("GET", f"/s/{s['sid']}/")
    assert status == 200
    assert "/_wc/core.js" in html


def test_a_registered_renderer_survives_a_daemon_restart(tmp_path):
    # The exact analogue of test_finished_survives_a_daemon_restart in
    # tests/test_server_sessions.py: registration was kept in a server-side
    # dict, so an upgrade-triggered restart of the always-on daemon made
    # every open browser tab 404 its own stylesheet and script, with no
    # visible cause. Verified by exact reproduction before the fix:
    #   register assets:      200
    #   serve before restart: (200, "console.log(1)")
    #   serve AFTER restart : (404, "no renderer registered for this session")
    cfg = Config(port=0, token=mint_token(), bind="127.0.0.1",
                 workspace_root=tmp_path / "ws")
    state_root = tmp_path / "state"
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "app.js").write_text("console.log(1)")

    d1 = Daemon(cfg, state_root=state_root)
    d1.start()
    try:
        _, s = raw_call(d1, "POST", "/api/sessions",
                        {"kind": "annotate", "cwd": "/p", "title": "T"})
        assert raw_call(d1, "POST", f"/s/{s['sid']}/api/assets",
                        {"static_root": str(bundle)})[0] == 200
        assert raw_call(d1, "GET", f"/s/{s['sid']}/assets/app.js") == (200, "console.log(1)")
    finally:
        d1.stop()

    d2 = Daemon(cfg, state_root=state_root)
    d2.start()
    try:
        assert raw_call(d2, "GET", f"/s/{s['sid']}/assets/app.js") == (200, "console.log(1)")
    finally:
        d2.stop()
