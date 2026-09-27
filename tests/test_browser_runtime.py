"""Drives static/core.js in a real browser against a live daemon.

Everything else in this suite tests core.js from the Python side: its
source contains the right strings, the routes that serve it return the
right bytes. None of that proves the click -> composer -> submit -> refetch
cycle actually works in a browser, or that the stream's opening snapshot
really does arrive marked `initial`. This does, at the cost of a browser.

playwright is a dev-only extra (see pyproject.toml's
[project.optional-dependencies]); the runtime dependency list stays [] and
CI's guard on it is unaffected. Run with:

    pip install -e '.[dev]' && playwright install chromium
    pytest tests/test_browser_runtime.py -q

The default `pytest -q` run (CI's `pip install pytest` only) skips this file
cleanly via importorskip.
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright  # noqa: E402

from webcompanion.config import Config, mint_token  # noqa: E402
from webcompanion.server import Daemon  # noqa: E402

# A minimal inline renderer: marks two commentable regions under the
# runtime's mount point and hands every onDelta straight to a JS-side array
# the test can inspect. It renders nothing either -- that is not its job,
# it is a stand-in for one of the five real clients.
BUNDLE_JS = """
window.__deltas = [];
document.addEventListener('DOMContentLoaded', () => {
  const main = document.querySelector('[data-wc-root]');
  for (const anchor of ['a-1', 'a-2']) {
    const el = document.createElement('div');
    el.id = anchor;
    el.setAttribute('data-wc-anchor', anchor);
    el.textContent = anchor;
    main.appendChild(el);
  }
  window.WebCompanion.init({ onDelta: (d) => { window.__deltas.push(d); } });
});
"""


@pytest.fixture
def live_daemon(tmp_path):
    cfg = Config(port=0, token=mint_token(), bind="127.0.0.1",
                 workspace_root=tmp_path / "ws")
    d = Daemon(cfg, state_root=tmp_path / "state")
    d.start()
    try:
        yield d
    finally:
        d.stop()


def _call(daemon, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(daemon.url + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as r:
        raw = r.read().decode()
        return r.status, (json.loads(raw) if raw.strip().startswith("{") else raw)


def test_the_runtime_works_end_to_end_in_a_real_browser(live_daemon, tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "app.js").write_text(BUNDLE_JS)

    _, s = _call(live_daemon, "POST", "/api/sessions",
                 {"kind": "annotate", "cwd": str(tmp_path), "title": "T"})
    sid = s["sid"]
    assert _call(live_daemon, "POST", f"/s/{sid}/api/assets",
                 {"static_root": str(bundle), "entry": "app.js"})[0] == 200

    dirs = live_daemon.registry.lookup(live_daemon.registry.resolve(sid))
    events_dir = Path(dirs["events_dir"])

    # Two pre-existing items, so the stream's opening snapshot has two
    # anchors to echo before the browser ever gets a chance to open.
    assert _call(live_daemon, "PUT", f"/s/{sid}/items/a-1", {"t": "one"})[0] == 200
    assert _call(live_daemon, "PUT", f"/s/{sid}/items/a-2", {"t": "two"})[0] == 200

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(f"{live_daemon.url}/s/{sid}/")

            page.wait_for_function("() => !!window.WebCompanion")
            assert page.evaluate("() => typeof window.WebCompanion.init") == "function"

            # Loopback grants write access with no token at all.
            page.wait_for_function("() => window.WebCompanion.writable === true")

            # Important 1: the opening snapshot must reach the client, but
            # marked `initial` so a client that already has this content
            # (it just fetched it) can tell it apart from a real change and
            # skip re-rendering. Two pre-existing anchors -> exactly two
            # initial deltas, zero non-initial ones, before any real edit.
            page.wait_for_function("() => window.__deltas.length >= 2", timeout=5000)
            deltas = page.evaluate("() => window.__deltas")
            initial = [d for d in deltas if d.get("initial")]
            non_initial = [d for d in deltas if not d.get("initial")]
            assert len(initial) == 2, f"expected 2 initial deltas, got {deltas}"
            assert not non_initial, (
                f"a client filtering initial deltas would still have "
                f"re-rendered on: {non_initial}")

            # click -> composer bound to the clicked anchor.
            page.click("#a-1")
            page.wait_for_selector(".wc-composer")
            composer = page.query_selector(".wc-composer")
            assert composer.get_attribute("data-wc-composer") == "a-1"

            # submit -> the event queue picks up the right anchor and text.
            page.fill(".wc-composer-text", "nice catch")
            page.click(".wc-composer-actions >> text=Comment")
            page.wait_for_selector(".wc-composer", state="detached", timeout=5000)

            queued = []
            for _ in range(50):
                queued = list(events_dir.glob("*.json"))
                if queued:
                    break
                page.wait_for_timeout(100)
            assert queued, "submit never queued an event on disk"
            payload = json.loads(queued[0].read_text())
            assert payload["anchor"] == "a-1"
            assert payload["text"] == "nice catch"

            # A server-side PUT (standing in for the watcher applying the
            # comment) must update only the edited element's version and
            # notify the browser of exactly that anchor -- not its sibling.
            before = len(page.evaluate("() => window.__deltas"))
            assert _call(live_daemon, "PUT", f"/s/{sid}/items/a-1",
                         {"t": "one-edited"})[0] == 200
            page.wait_for_function(
                f"() => window.__deltas.length > {before}", timeout=5000)
            latest = page.evaluate("() => window.__deltas[window.__deltas.length - 1]")
            assert latest["anchor"] == "a-1"
            assert latest["initial"] is False

            _, v1 = _call(live_daemon, "GET", f"/s/{sid}/items/a-1")
            _, v2 = _call(live_daemon, "GET", f"/s/{sid}/items/a-2")
            assert v1["version"] == 2, "the edited item should have moved to v2"
            assert v2["version"] == 1, "the untouched sibling must stay at v1"
        finally:
            browser.close()


def _session_with_bundle(daemon, tmp_path):
    """A session whose renderer is the stand-in bundle, with one item in it."""
    bundle = tmp_path / "bundle"
    bundle.mkdir(exist_ok=True)
    (bundle / "app.js").write_text(BUNDLE_JS)
    _, s = _call(daemon, "POST", "/api/sessions",
                 {"kind": "annotate", "cwd": str(tmp_path), "title": "T"})
    sid = s["sid"]
    assert _call(daemon, "POST", f"/s/{sid}/api/assets",
                 {"static_root": str(bundle), "entry": "app.js"})[0] == 200
    assert _call(daemon, "PUT", f"/s/{sid}/items/a-1", {"t": "one"})[0] == 200
    return sid


def test_a_write_probe_that_never_answers_does_not_latch_the_page_read_only(
        live_daemon, tmp_path):
    """The probe is one request, and it used to get exactly one attempt.

    Every open page holds one immortal SSE stream, and a browser allows six
    connections per origin. Six open documents and the seventh request on
    that origin queues behind streams that never end -- measured against a
    real daemon: with six EventSources open /api/whoami stalls indefinitely,
    and answers in 2ms the moment one closes. A dropped connection (the
    daemon restarting, a laptop waking) fails the same probe a different
    way.

    Either way the old code decided `writable = false`, painted the body
    read-only and never asked again. The document still rendered, so the
    page looked fine -- it had just silently dropped every control on it,
    with no way back but a reload at a luckier moment.

    The stall is injected rather than staged with six real streams: a
    saturated pool also starves the page's own assets, so the failure could
    not be set up without stopping the page from loading at all.
    """
    from playwright.sync_api import TimeoutError as PWTimeout

    sid = _session_with_bundle(live_daemon, tmp_path)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        try:
            stalled = {"on": True}

            def probe(route):
                # Returning without fulfilling leaves the request hanging,
                # which is what a starved connection pool does to it.
                if not stalled["on"]:
                    route.continue_()

            page.route("**/api/whoami", probe)

            page.goto(f"{live_daemon.url}/s/{sid}/")
            page.wait_for_function("() => !!window.WebCompanion")

            # Precondition: with no answer there is no verdict to act on.
            with pytest.raises(PWTimeout):
                page.wait_for_function(
                    "() => window.WebCompanion.writable === true", timeout=3000)

            # A slot frees up. Nothing else about the page changes -- no
            # reload, no navigation, no user action.
            stalled["on"] = False

            page.wait_for_function(
                "() => window.WebCompanion.writable === true", timeout=20000)
            assert page.evaluate(
                "() => document.body.classList.contains('read-only')") is False, \
                "the page recovered write access but still renders as read-only"
        finally:
            browser.close()


def test_a_refused_probe_still_paints_the_page_read_only(live_daemon, tmp_path):
    """The other half of the contract, so retrying cannot swallow a real
    refusal: a probe the daemon ANSWERS with "not writable" is a verdict,
    not a glitch. It is painted, and it stands."""
    sid = _session_with_bundle(live_daemon, tmp_path)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        try:
            page.route("**/api/whoami", lambda route: route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({"writable": False})))

            page.goto(f"{live_daemon.url}/s/{sid}/")
            page.wait_for_function(
                "() => document.body.classList.contains('read-only')",
                timeout=10000)
            page.wait_for_timeout(2000)
            assert page.evaluate("() => window.WebCompanion.writable") is False
            assert page.evaluate(
                "() => document.body.classList.contains('read-only')") is True, \
                "a refusal was retried away"
        finally:
            browser.close()


def test_an_ack_reaches_the_page_as_an_event_acked_delta(live_daemon, tmp_path):
    """The ack is the only signal for an answer that changed nothing (a round
    of pure `keep` marks). The handler once called `parse(ev)`, a helper that
    does not exist, so it threw on every ack and the page never heard."""
    sid = _session_with_bundle(live_daemon, tmp_path)
    dirs = live_daemon.registry.lookup(live_daemon.registry.resolve(sid))
    consumed = Path(dirs["consumed_dir"])

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.goto(f"{live_daemon.url}/s/{sid}/")
            page.wait_for_function("() => window.__deltas.length >= 1", timeout=5000)
            consumed.mkdir(parents=True, exist_ok=True)
            (consumed / "evt-123.ack").write_text("")
            # Wake the stream loop now instead of waiting out its idle poll.
            assert _call(live_daemon, "PUT", f"/s/{sid}/items/a-2", {"t": "x"})[0] == 200
            page.wait_for_function(
                "() => window.__deltas.some(d => d.kind === 'event-acked')",
                timeout=8000)
            acked = page.evaluate(
                "() => window.__deltas.filter(d => d.kind === 'event-acked')")
            assert acked == [{"kind": "event-acked", "anchor": None, "version": 0,
                              "initial": False, "event_id": "evt-123"}]
            assert not errors, errors
        finally:
            browser.close()
