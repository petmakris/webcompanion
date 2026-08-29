from __future__ import annotations

import re
from importlib.resources import as_file, files


def _read(name: str) -> str:
    with as_file(files("webcompanion").joinpath("static", name)) as p:
        return p.read_text()


def test_the_runtime_is_packaged_and_reachable_through_importlib():
    # Path(__file__).parent survives a wheel but not the zipapp the service
    # runs from; as_file works for both.
    assert "WebCompanion" in _read("core.js")


def test_the_runtime_stays_small():
    # It is the interaction, not a renderer. annotate's 480KB bundle is a
    # renderer and belongs to annotate.
    size = len(_read("core.js").encode())
    assert size < 20_000, f"core.js is {size} bytes; a renderer has leaked in"


def test_the_runtime_renders_nothing():
    src = _read("core.js")
    for forbidden in ["markdown", "markdownit", "hljs", "highlight"]:
        assert forbidden not in src, f"{forbidden} is a rendering concern"


def test_the_runtime_sends_the_contract_header():
    assert "X-WebCompanion-Contract" in _read("core.js")


def test_the_runtime_reads_the_token_from_the_fragment_not_the_query():
    """The "not the query" half was never tested: a runtime that read
    location.search would have passed on the two asserts below alone.

    The fragment is what browsers never send to the server and never write
    to a log or a Referer, which is the entire reason the owner URL is safe
    to paste. Reading it from the query string would put the write token in
    the daemon's own request line.
    """
    src = _read("core.js")
    assert "sessionStorage" in src

    # The line that actually EXTRACTS the token, not merely a mention of
    # `location.hash` somewhere in the file. core.js legitimately names
    # location.search elsewhere -- it preserves the query while stripping
    # the fragment from the address bar -- so the assertion has to land on
    # the extraction itself.
    extract = [ln for ln in src.splitlines() if "k=([^&]+)" in ln]
    assert len(extract) == 1, (
        "the k= token pattern is gone or duplicated; "
        "the owner URL no longer works as documented")
    assert "location.hash" in extract[0]
    assert "location.search" not in extract[0], (
        "the token is being read from the query string, not the fragment -- "
        "browsers send the query to the server and write it to logs")


def test_the_runtime_binds_to_the_anchor_attribute():
    assert "data-wc-anchor" in _read("core.js")


def test_the_shell_loads_the_runtime_and_leaves_a_mount_point():
    html = _read("shell.html")
    assert "/_wc/core.js" in html
    assert "{{ENTRY}}" in html and "{{TITLE}}" in html


def test_the_runtime_reconnects_a_dropped_stream():
    """There is no idle shutdown any more, but a laptop sleeping still drops
    the connection, and a page that silently stops updating is worse than
    one that reloads.

    This used to grep for `onerror|reconnect` -- both of which appear in
    comments, so deleting the entire reconnect implementation left it green.
    It now asserts the four things the mechanism is MADE of: an error
    handler that closes the stream, a fallback to polling, a timer that
    retries the stream, and a guard so a session that has ended does not
    retry forever.
    """
    src = _read("core.js")
    body = src[src.index("es.onerror"):]
    assert "closeStream()" in body
    assert "startPolling()" in body
    assert "scheduleReconnect()" in body

    sched = src[src.index("function scheduleReconnect"):]
    sched = sched[:sched.index("\n  }")]
    assert "setTimeout" in sched and "startStream()" in sched
    assert "ended" in sched, "a reconnect loop that ignores `ended` never stops"


def test_the_poll_fallback_ends_on_a_cancelled_session_too():
    """The SSE path ends on session-ended, which the daemon emits for
    finished AND cancelled. The poll fallback checked only `finished`, so a
    cancelled session polled once a second forever -- on exactly the
    transport a client uses when its connection is already struggling."""
    src = _read("core.js")
    poll = src[src.index("async function pollOnce"):]
    poll = poll[:poll.index("function startPolling")]
    assert "data.cancelled" in poll


def test_the_runtime_does_not_claim_the_token_is_reminted():
    """It is not. It lives in the daemon's config file and survives every
    restart on purpose -- reminting would invalidate the IDE plugin's saved
    credential mid-session, which is the package's central invariant."""
    assert "reminted on every restart" not in _read("core.js")
