from __future__ import annotations

from pathlib import Path

import pytest

from webcompanion import anchors


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "\n".join(f"line {i}" for i in range(1, 51)) + "\n"
    )
    return tmp_path


def test_an_anchor_resolves_to_the_current_file_contents(repo):
    out = anchors.resolve_anchor(
        {"file": "src/app.py", "line": 10, "snippet": "line 10"}, repo)
    assert out["status"] == "ok"
    assert any(l["text"] == "line 10" and l["role"] == "anchor"
               for l in out["lines"])


def test_resolution_happens_per_request_not_at_push_time(repo):
    # This is the entire reason the module exists: a client edits its repo
    # while a session is open, so an anchor snapshotted once would go stale
    # within a turn. Resolve, edit the file on disk, resolve again — the two
    # results must differ, proving each call re-reads the file rather than
    # caching what it saw the first time.
    a = {"file": "src/app.py", "line": 10, "snippet": "line 10"}
    first = anchors.resolve_anchor(a, repo)
    (repo / "src" / "app.py").write_text(
        "\n".join(f"CHANGED {i}" for i in range(1, 51)) + "\n")
    # Old snippet text is gone now, so re-resolving the *same* anchor dict
    # must report the drift caused by the edit, not the first call's result.
    second = anchors.resolve_anchor(a, repo)
    assert first["status"] == "ok"
    assert second["status"] == "stale"
    assert first != second


def test_a_moved_snippet_is_found_within_the_drift_radius(repo):
    (repo / "src" / "app.py").write_text(
        "\n".join(["preamble"] * 5 + [f"line {i}" for i in range(1, 51)]) + "\n")
    out = anchors.resolve_anchor(
        {"file": "src/app.py", "line": 10, "snippet": "line 10"}, repo)
    assert out["status"] == "moved"
    # authored line is preserved; actual_line carries the new position.
    assert out["line"] == 10
    assert out["actual_line"] == 15


def test_a_snippet_that_vanished_is_a_marker_not_an_exception(repo):
    out = anchors.resolve_anchor(
        {"file": "src/app.py", "line": 10, "snippet": "not in this file"}, repo)
    assert out["status"] == "stale"
    assert "message" in out
    assert "lines" not in out


@pytest.mark.parametrize("bad_file", ["../../.ssh/id_rsa", "/etc/passwd"])
def test_an_anchor_may_not_escape_the_root(repo, bad_file):
    # Anchors are model-authored and the read-only share link makes this
    # reachable by anyone holding it.
    out = anchors.resolve_anchor(
        {"file": bad_file, "line": 1, "snippet": "x"}, repo)
    assert out["status"] == "refused"
    assert "lines" not in out


def test_a_symlink_pointing_outside_the_root_is_refused(repo, tmp_path):
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("password")
    (repo / "link.txt").symlink_to(secret)
    out = anchors.resolve_anchor(
        {"file": "link.txt", "line": 1, "snippet": "password"}, repo)
    assert out["status"] == "refused"
    # Would this fail if the check were removed? Prove the file really is
    # reachable through the symlink (so containment is the only thing
    # stopping us), and that the refusal message never leaks the resolved
    # absolute path of the secret file.
    assert secret.read_text() == "password"
    assert str(secret.resolve()) not in out["message"]
    assert "lines" not in out


def test_a_file_over_the_byte_cap_is_missing_not_read(repo):
    big = repo / "big.json"
    big.write_text("x" * (anchors.MAX_BYTES + 1))
    out = anchors.resolve_anchor(
        {"file": "big.json", "line": 1, "snippet": "x"}, repo)
    assert out["status"] == "missing"
    assert "lines" not in out


def test_a_very_long_line_is_truncated(repo):
    long_line = "a" * (anchors.MAX_LINE_CHARS + 500)
    (repo / "min.js").write_text(long_line)
    out = anchors.resolve_anchor(
        {"file": "min.js", "line": 1, "snippet": long_line}, repo)
    lengths = [len(l["text"]) for l in out["lines"]]
    assert max(lengths) <= anchors.MAX_LINE_CHARS + len(" … [line truncated]")
    assert any(l["text"].endswith("[line truncated]") for l in out["lines"])


def test_the_window_is_capped(repo):
    out = anchors.resolve_anchor(
        {"file": "src/app.py", "line": 1, "end_line": 50, "snippet": "line 1"}, repo)
    assert len(out["lines"]) <= anchors.MAX_WINDOW + 2 * anchors.CONTEXT_LINES
    assert out.get("truncated", 0) > 0


def test_resolve_all_caps_the_number_of_anchors(repo):
    body = {"code": [{"file": "src/app.py", "line": i, "snippet": f"line {i}"}
                     for i in range(1, 8)]}
    assert len(anchors.resolve_all(body, repo)) == anchors.MAX_ANCHORS


def test_resolve_all_on_a_body_with_no_code_is_empty(repo):
    assert anchors.resolve_all({"text": "hello"}, repo) == []


def test_resolve_all_on_a_body_where_code_is_not_a_list_is_empty(repo):
    assert anchors.resolve_all({"code": "not-a-list"}, repo) == []


@pytest.mark.parametrize("bad,expect", [
    ({}, "must be"),
    ({"file": "", "line": 1, "snippet": "x"}, "non-empty"),
    ({"file": "/abs", "line": 1, "snippet": "x"}, "relative"),
    ({"file": "a", "line": 0, "snippet": "x"}, "positive"),
    ({"file": "a", "line": True, "snippet": "x"}, "positive"),
    ({"file": "a", "line": 2, "end_line": 1, "snippet": "x"}, "precede"),
    ({"file": "a", "line": 1, "snippet": "  "}, "non-empty"),
])
def test_anchor_problem_names_the_problem(bad, expect):
    problem = anchors.anchor_problem(bad)
    assert problem and expect in problem
