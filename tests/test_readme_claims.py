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


def test_every_route_in_the_contract_doc_exists_in_the_server():
    import webcompanion.server as srv
    import inspect
    source = inspect.getsource(srv)
    doc = (Path(__file__).resolve().parents[1] / "docs" / "contract.md").read_text()
    routes = set(re.findall(r"^\s*(?:GET|PUT|POST|PATCH|DELETE)\s+(/\S+)", doc, re.M))
    assert routes, "the contract doc lists no routes"
    for route in routes:
        stem = route.split("<")[0].rstrip("/").split("?")[0]
        assert stem.strip("/").split("/")[-1] in source or stem in source, route


def test_the_contract_number_matches_the_package():
    doc = (Path(__file__).resolve().parents[1] / "docs" / "contract.md").read_text()
    assert f"contract {webcompanion.CONTRACT}" in doc
