from __future__ import annotations

# Two tests from the brief are deliberately absent here:
#   test_the_runtime_is_served_by_the_daemon
#   test_the_shell_page_loads_the_runtime_and_the_registered_entry
# Both need static files (core.js, shell.html) that Task 13 creates. Writing
# them now, or creating placeholder static files to pass them, would be
# testing a file this task does not own.


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
