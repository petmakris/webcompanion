from __future__ import annotations

import threading

from webcompanion import CONTRACT, __version__
from webcompanion.gate import CONTRACT_HEADER


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
