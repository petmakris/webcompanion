from __future__ import annotations

import re
from pathlib import Path

import webcompanion

README = Path(__file__).resolve().parents[1] / "README.md"


def test_the_readme_states_the_platform_limit():
    # threads.py imports fcntl. The claude-annotate README says macOS/Linux;
    # this package's own README must say it too, for anyone who finds it on
    # PyPI without that context.
    text = README.read_text()
    assert "macOS" in text and "Linux" in text
    assert "Windows" in text


def test_the_readme_states_the_dependency_promise():
    assert "standard library" in README.read_text().lower()


def test_the_readme_documents_the_three_failure_messages():
    text = README.read_text()
    assert "webcompanion install-service" in text
    assert "webcompanion status" in text
    assert "426" in text


# ── the route table and the server must agree, in both directions ────────
#
# What this replaces, and why it mattered: the previous version reduced each
# documented route to its LAST PATH SEGMENT and asked whether that substring
# appeared anywhere in server.py's source. `/s/{sid}/threads/{anchor}`
# passed because the word "threads" is on an import line. It therefore could
# not, structurally, notice that contract 1 documented Threads, returned
# `threads: {anchor: version}` from /poll and defined thread-changed frames
# while no thread route existed at all -- which is exactly what shipped.
#
# The rewrite compares SETS of (method, path), derived from the dispatch
# itself, and fails in both directions: a documented route the server lacks,
# and a served route the document lacks.

DOC = Path(__file__).resolve().parents[1] / "docs" / "contract.md"
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def _canonical(path: str) -> str:
    """Collapse the two vocabularies for a path variable onto one.

    The doc writes `{sid}`, `<anchor>`, `<relpath>`; the server writes
    regex groups. Neither spelling is a difference worth failing on -- a
    missing or extra ROUTE is.
    """
    path = re.sub(r"\{[^}]+\}|<[^>]+>", "*", path)
    return path.split("?")[0].rstrip("/") or "/"


def _documented_routes() -> set:
    """The routes named in the doc's quick-reference block."""
    doc = DOC.read_text()
    block = re.search(r"```\n((?:(?:%s)[^\n]*\n)+)```" % "|".join(METHODS), doc)
    assert block, "the contract doc's quick-reference route block is gone"
    out = set()
    for line in block.group(1).splitlines():
        method, path = line.split(None, 1)
        out.add((method, _canonical(path.strip())))
    return out


def _table_routes() -> set:
    """The routes in the doc's descriptive table -- a second listing of the
    same thing, which can drift from the first."""
    out = set()
    for method, path in re.findall(
            r"^\|\s*(%s)\s*\|\s*`([^`]+)`" % "|".join(METHODS),
            DOC.read_text(), re.M):
        out.add((method, _canonical(path)))
    return out


def _method_body(source: str, method: str) -> str:
    start = source.index(f"def do_{method}(self):")
    rest = source[start:]
    end = rest.find("\n        def ", 1)
    return rest if end == -1 else rest[:end]


def _served_routes() -> set:
    """The routes the dispatch actually reaches, read out of its source.

    Derived from the dispatch rather than from a hand-kept list next to it,
    because a hand-kept list is one more thing that can be right while the
    code is wrong -- which is the failure this whole test exists to catch.
    """
    import inspect

    import webcompanion.server as srv

    source = inspect.getsource(srv)
    patterns = dict(re.findall(r'^(_SID_\w+_RE) = re\.compile\(r"(.+?)"\)',
                               source, re.M))
    out = set()
    for method in METHODS:
        body = _method_body(source, method)
        for literal in re.findall(r'path == "([^"]+)"', body):
            out.add((method, _canonical(literal)))
        for name in re.findall(r"(_SID_\w+_RE)\.match\(path\)", body):
            pattern = patterns[name].strip("^$")
            template = re.sub(r"\(\[\^/\]\+\)|\(\.\+\)", "*", pattern)
            out.add((method, _canonical(template)))
    return out


def test_every_documented_route_is_actually_served():
    missing = _documented_routes() - _served_routes()
    assert not missing, (
        "the contract doc promises routes the server does not serve: "
        + ", ".join(f"{m} {p}" for m, p in sorted(missing)))


def test_every_served_route_is_documented():
    """The direction that was never checked. A route a client cannot
    discover from the contract is a route only this repository knows about,
    and contract 1 is what four separately-updated artifacts implement."""
    undocumented = _served_routes() - _documented_routes()
    assert not undocumented, (
        "the server serves routes the contract doc does not list: "
        + ", ".join(f"{m} {p}" for m, p in sorted(undocumented)))


def test_the_docs_two_route_listings_agree():
    """The quick reference and the descriptive table are two hand-kept lists
    of one thing."""
    assert _documented_routes() == _table_routes()


def test_the_contract_number_matches_the_package():
    doc = (Path(__file__).resolve().parents[1] / "docs" / "contract.md").read_text()
    assert f"contract {webcompanion.CONTRACT}" in doc
