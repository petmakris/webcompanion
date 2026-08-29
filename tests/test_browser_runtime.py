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
