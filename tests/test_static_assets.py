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
    src = _read("core.js")
    assert "location.hash" in src
    assert "sessionStorage" in src


def test_the_runtime_binds_to_the_anchor_attribute():
    assert "data-wc-anchor" in _read("core.js")


def test_the_shell_loads_the_runtime_and_leaves_a_mount_point():
    html = _read("shell.html")
    assert "/_wc/core.js" in html
    assert "{{ENTRY}}" in html and "{{TITLE}}" in html


def test_the_runtime_reconnects_a_dropped_stream():
    # There is no idle shutdown any more, but a laptop sleeping still drops
    # the connection, and a page that silently stops updating is worse than
    # one that reloads.
    assert re.search(r"onerror|reconnect", _read("core.js"))
