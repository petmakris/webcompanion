from __future__ import annotations

import json
import threading

from webcompanion import CONTRACT, __version__
from webcompanion import cleanup
from webcompanion.config import Config, mint_token
from webcompanion.gate import CONTRACT_HEADER
from webcompanion.server import Daemon

from conftest import raw_call


def test_health_reports_contract_and_version(call):
    status, body = call("GET", "/health")
    assert status == 200
    assert body["contract"] == CONTRACT
    assert body["version"] == __version__
    assert "webcompanion" in body["banner"]


def test_create_returns_a_sid_slug_and_url(call):
    status, body = call("POST", "/api/sessions",
                        {"kind": "annotate", "cwd": "/proj", "title": "My Plan"})
    assert status == 201
    assert body["slug"] == "my-plan"
    assert body["sid"]
    assert body["url"].endswith("/s/" + body["sid"] + "/")


def test_create_without_a_kind_is_rejected(call):
    # One daemon holds every kind; a session that cannot say which it is makes
    # discovery ambiguous for both IntelliJ clients.
    status, _ = call("POST", "/api/sessions", {"cwd": "/proj", "title": "X"})
    assert status == 400


def test_create_with_an_unusable_kind_is_rejected(call):
    for kind in ["../escape", "A B", ""]:
        status, _ = call("POST", "/api/sessions",
                         {"kind": kind, "cwd": "/proj", "title": "X"})
        assert status == 400, kind


def test_discovery_filters_by_kind(call):
    call("POST", "/api/sessions", {"kind": "walkthrough", "cwd": "/p", "title": "W"})
    call("POST", "/api/sessions", {"kind": "annotate", "cwd": "/p", "title": "A"})
    _, rows = call("GET", "/api/sessions?cwd=/p&kind=walkthrough")
    assert [r["kind"] for r in rows] == ["walkthrough"]


def test_discovery_rows_carry_the_kind(call):
    call("POST", "/api/sessions", {"kind": "deck", "cwd": "/p", "title": "D"})
    _, rows = call("GET", "/api/sessions?cwd=/p")
    assert rows and all("kind" in r for r in rows)


def test_discovery_without_a_cwd_is_a_400(call):
    status, _ = call("GET", "/api/sessions")
    assert status == 400


def test_two_kinds_may_share_a_slug(call):
    _, a = call("POST", "/api/sessions", {"kind": "annotate", "cwd": "/p", "title": "Plan"})
    _, d = call("POST", "/api/sessions", {"kind": "deck", "cwd": "/p", "title": "Plan"})
    assert a["slug"] == d["slug"] == "plan"


def test_finish_marks_the_session_and_is_visible_to_poll(call):
    _, s = call("POST", "/api/sessions", {"kind": "annotate", "cwd": "/p", "title": "T"})
    assert call("POST", f"/s/{s['sid']}/api/finish")[0] == 200
    _, poll = call("GET", f"/s/{s['sid']}/poll")
    assert poll["finished"] is True


def test_cancel_marks_the_session(call):
    _, s = call("POST", "/api/sessions", {"kind": "annotate", "cwd": "/p", "title": "T"})
    assert call("POST", f"/s/{s['sid']}/api/cancel")[0] == 200
    _, poll = call("GET", f"/s/{s['sid']}/poll")
    assert poll["cancelled"] is True


def test_finished_survives_a_daemon_restart(tmp_path):
    # This is the test whose absence let a Critical through: finished state
    # was kept in a server-side dict, so an upgrade-triggered restart of the
    # always-on daemon resurrected every finished session, and Task 15's
    # watcher process (which polls state_dir for a `finished` file, not this
    # process's memory) would never have seen it end.
    cfg = Config(port=0, token=mint_token(), bind="127.0.0.1",
                 workspace_root=tmp_path / "ws")
    state_root = tmp_path / "state"

    d1 = Daemon(cfg, state_root=state_root)
    d1.start()
    try:
        _, s = raw_call(d1, "POST", "/api/sessions",
                        {"kind": "annotate", "cwd": "/p", "title": "T"})
        assert raw_call(d1, "POST", f"/s/{s['sid']}/api/finish")[0] == 200
        _, poll = raw_call(d1, "GET", f"/s/{s['sid']}/poll")
        assert poll["finished"] is True
    finally:
        d1.stop()

    d2 = Daemon(cfg, state_root=state_root)
    d2.start()
    try:
        _, poll = raw_call(d2, "GET", f"/s/{s['sid']}/poll")
        assert poll["finished"] is True
    finally:
        d2.stop()


def test_the_daemon_boots_and_serves_even_when_the_startup_sweep_raises(tmp_path, monkeypatch):
    """A cleanup failure (a PermissionError on a workspace directory, say)
    must never keep the socket from binding. Daemon.start() calls
    registry.rehydrate() -> cleanup.sweep() -> registry.persist() before
    binding; if cleanup.sweep() were left unguarded, an exception here would
    mean the daemon never starts -- under launchd's KeepAlive that is a
    silent respawn loop with every client seeing connection refused and
    nothing saying why. A daemon that skips a sweep must still boot.
    """
    def _boom(cfg, registry, **kw):
        raise PermissionError("simulated: cleanup sweep failed")

    monkeypatch.setattr(cleanup, "sweep", _boom)

    cfg = Config(port=0, token=mint_token(), bind="127.0.0.1",
                workspace_root=tmp_path / "ws")
    d = Daemon(cfg, state_root=tmp_path / "state")
    d.start()  # must not raise
    try:
        status, body = raw_call(d, "GET", "/health")
        assert status == 200
        assert body["contract"] == CONTRACT
    finally:
        d.stop()


def test_a_swallowed_startup_sweep_failure_leaves_a_durable_marker(tmp_path, monkeypatch):
    """A stderr traceback alone means nobody learns cleanup stopped running
    -- `doctor` needs something durable under state_root to report."""
    def _boom(cfg, registry, **kw):
        raise PermissionError("simulated: cleanup sweep failed")

    monkeypatch.setattr(cleanup, "sweep", _boom)

    state_root = tmp_path / "state"
    cfg = Config(port=0, token=mint_token(), bind="127.0.0.1",
                workspace_root=tmp_path / "ws")
    d = Daemon(cfg, state_root=state_root)
    d.start()
    try:
        marker_path = state_root / "startup_sweep_failed.json"
        assert marker_path.is_file()
        marker = json.loads(marker_path.read_text())
        assert "PermissionError" in marker["error"]
        assert marker["when"] > 0
    finally:
        d.stop()


def test_supersede_ends_the_older_session_of_the_same_kind_and_cwd(call):
    _, older = call("POST", "/api/sessions", {"kind": "annotate", "cwd": "/p", "title": "Old"})
    _, newer = call("POST", "/api/sessions",
                    {"kind": "annotate", "cwd": "/p", "title": "New", "supersede": True})
    _, poll_older = call("GET", f"/s/{older['sid']}/poll")
    _, poll_newer = call("GET", f"/s/{newer['sid']}/poll")
    assert poll_older["finished"] is True
    assert poll_newer["finished"] is False


def test_supersede_leaves_other_kinds_and_cwds_alone(call):
    # Supersede replaces a per-skill setting where one client (annotate) ends
    # its older sessions and another (deck) deliberately does not. Scoping it
    # too broadly would silently end a user's unrelated work.
    _, other_kind = call("POST", "/api/sessions", {"kind": "deck", "cwd": "/p", "title": "D"})
    _, other_cwd = call("POST", "/api/sessions", {"kind": "annotate", "cwd": "/q", "title": "Q"})
    call("POST", "/api/sessions",
        {"kind": "annotate", "cwd": "/p", "title": "New", "supersede": True})
    _, poll_kind = call("GET", f"/s/{other_kind['sid']}/poll")
    _, poll_cwd = call("GET", f"/s/{other_cwd['sid']}/poll")
    assert poll_kind["finished"] is False
    assert poll_cwd["finished"] is False


def test_an_unknown_session_is_a_404(call):
    assert call("GET", "/s/does-not-exist/poll")[0] == 404


def test_a_mismatched_contract_is_426_and_names_the_old_side(call):
    status, body = call("GET", "/health", headers={CONTRACT_HEADER: "99"})
    assert status == 426
    assert "daemon" in body


def test_a_matching_contract_passes(call):
    assert call("GET", "/health", headers={CONTRACT_HEADER: str(CONTRACT)})[0] == 200


def test_scope_all_lists_every_kind(call):
    call("POST", "/api/sessions", {"kind": "annotate", "cwd": "/a", "title": "A"})
    call("POST", "/api/sessions", {"kind": "deck", "cwd": "/b", "title": "B"})
    _, rows = call("GET", "/api/sessions?scope=all")
    assert {r["kind"] for r in rows} == {"annotate", "deck"}


def test_whoami_reports_writable_on_loopback(call):
    _, body = call("GET", "/api/whoami")
    assert body["writable"] is True


def test_concurrent_creates_get_distinct_slugs(call):
    out = []
    def create():
        out.append(call("POST", "/api/sessions",
                        {"kind": "annotate", "cwd": "/p", "title": "Same"})[1]["slug"])
    ts = [threading.Thread(target=create) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(set(out)) == 6, f"slug collision under concurrency: {out}"


def test_the_server_is_threaded(daemon):
    from http.server import ThreadingHTTPServer
    assert isinstance(daemon._httpd, ThreadingHTTPServer)
    assert daemon._httpd.daemon_threads is True


def test_there_is_no_idle_shutdown(daemon):
    # Under a KeepAlive supervisor an idle shutdown becomes a daily restart
    # that drops every open SSE stream, and the browser runtime has no
    # reconnect logic. This checks the actual absence of the mechanism, not
    # a docstring's wording about it.
    import inspect
    import webcompanion.server as srv
    source = inspect.getsource(srv)
    assert "shutdown_after" not in source
    assert "threading.Timer" not in source
    assert "idle_timeout" not in source
    assert "idle-timeout" not in source


# ── an ambiguous slug is 409 with the candidates, never 404 ──────────────

def test_a_slug_used_by_three_kinds_answers_409_naming_them(call, tmp_path):
    """Real in the user's own data: `montblanc` exists in three kinds.

    Slugs are unique WITHIN a kind by design, so this is not a collision to
    prevent -- it is one to answer. `resolve` returns None for it, which
    surfaced as `404 no such session`: a status that reads as "it is gone"
    about something that exists three times over.
    """
    for kind in ("annotate", "deck", "dataflow"):
        call("POST", "/api/sessions",
             {"kind": kind, "cwd": str(tmp_path), "slug": "montblanc"})

    status, body = call("GET", "/s/montblanc/items")
    assert status == 409
    assert "annotate" in body and "deck" in body and "dataflow" in body
    assert "?kind=" in body


def test_the_kind_query_disambiguates_a_shared_slug(call, tmp_path):
    ids = {}
    for kind in ("annotate", "deck"):
        _, created = call("POST", "/api/sessions",
                          {"kind": kind, "cwd": str(tmp_path), "slug": "montblanc"})
        ids[kind] = created["sid"]
    call("PUT", f"/s/{ids['deck']}/items/only-in-deck", {"text": "x"})

    status, body = call("GET", "/s/montblanc/items?kind=deck")
    assert status == 200
    assert set(body) == {"only-in-deck"}

    assert call("GET", "/s/montblanc/items?kind=annotate")[1] == {}


def test_an_unambiguous_slug_still_resolves_without_a_kind(call, tmp_path):
    """The negative control: 409 must not become the answer for every
    slug."""
    call("POST", "/api/sessions",
         {"kind": "annotate", "cwd": str(tmp_path), "slug": "solo"})
    assert call("GET", "/s/solo/items")[0] == 200


def test_a_slug_nobody_uses_is_still_404(call):
    status, body = call("GET", "/s/never-existed/items")
    assert status == 404
    assert "no such session" in body


# ── a session row with no cwd ────────────────────────────────────────────

def test_an_item_in_a_session_with_no_cwd_is_400_not_a_500(daemon, call, tmp_path):
    """`dirs["_cwd"]` raised KeyError inside the handler for a malformed
    row -- a 500 with a traceback for what is bad input. And an EMPTY _cwd
    was worse than an error: Path("").resolve() is the daemon's own cwd
    ("/" under launchd), and is_relative_to("/") is true for every path on
    the machine, so anchor containment silently passed for anything the
    daemon could read."""
    _, created = call("POST", "/api/sessions",
                      {"kind": "annotate", "cwd": str(tmp_path)})
    sid = created["sid"]
    call("PUT", f"/s/{sid}/items/a", {"text": "x"})
    daemon.registry.lookup(sid)["_cwd"] = ""

    status, body = call("GET", f"/s/{sid}/items/a")
    assert status == 400
    assert "workspace root" in body


def test_an_empty_root_refuses_every_code_anchor(tmp_path):
    """The containment check itself, below the route: an empty root must
    resolve nothing, not everything."""
    from webcompanion import anchors

    body = {"code": [{"file": "etc/passwd", "line": 1, "snippet": "root:"}]}
    for root in ("", "   ", None):
        resolved = anchors.resolve_all(body, root)
        assert resolved and resolved[0]["status"] == "refused"
        assert "no workspace root" in resolved[0]["message"]
