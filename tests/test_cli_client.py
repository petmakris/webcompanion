from __future__ import annotations

import json

import pytest

from webcompanion.commands import end, push, unfinish, update


def test_push_creates_a_session_and_prints_the_url(wired, tmp_path, capsys):
    doc = tmp_path / "doc.json"
    doc.write_text(json.dumps({"items": {"b-1": {"text": "hello"}}}))
    rc = push.run(["--kind", "annotate", "--cwd", str(tmp_path),
                   "--title", "My Plan", "--items", str(doc)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "/s/" in out


def test_push_prints_shell_evaluable_output(wired, tmp_path, capsys):
    # The reference docs used to parse curl output with hand-rolled
    # `python3 -c 'import json...'` one-liners. This is what removes them.
    doc = tmp_path / "doc.json"
    doc.write_text(json.dumps({"items": {}}))
    push.run(["--kind", "annotate", "--cwd", str(tmp_path), "--title", "T",
              "--items", str(doc), "--eval"])
    out = capsys.readouterr().out
    assert "WC_SID=" in out and "WC_URL=" in out and "WC_SLUG=" in out


def test_update_replaces_one_item(wired, tmp_path, capsys):
    doc = tmp_path / "doc.json"
    doc.write_text(json.dumps({"items": {"b-1": {"text": "before"}}}))
    push.run(["--kind", "annotate", "--cwd", str(tmp_path), "--title", "T",
              "--items", str(doc), "--eval"])
    sid = [l for l in capsys.readouterr().out.splitlines() if l.startswith("WC_SID=")][0][7:]
    body = tmp_path / "one.json"
    body.write_text(json.dumps({"text": "after"}))
    assert update.run(["--sid", sid, "--anchor", "b-1", "--body", str(body)]) == 0


def test_end_finishes_the_session(wired, tmp_path, capsys):
    doc = tmp_path / "doc.json"
    doc.write_text(json.dumps({"items": {}}))
    push.run(["--kind", "annotate", "--cwd", str(tmp_path), "--title", "T",
              "--items", str(doc), "--eval"])
    sid = [l for l in capsys.readouterr().out.splitlines() if l.startswith("WC_SID=")][0][7:]
    assert end.run(["--sid", sid]) == 0


def test_unfinish_reopens_a_finished_session(wired, tmp_path, capsys):
    doc = tmp_path / "doc.json"
    doc.write_text(json.dumps({"items": {}}))
    push.run(["--kind", "annotate", "--cwd", str(tmp_path), "--title", "T",
              "--items", str(doc), "--eval"])
    sid = [l for l in capsys.readouterr().out.splitlines() if l.startswith("WC_SID=")][0][7:]
    assert end.run(["--sid", sid]) == 0
    assert unfinish.run(["--sid", sid]) == 0


def test_a_payload_over_the_limit_is_refused_before_it_is_sent(wired, tmp_path, capsys):
    doc = tmp_path / "doc.json"
    doc.write_text(json.dumps({"items": {"b-1": {"t": "x" * (6 * 1024 * 1024)}}}))
    rc = push.run(["--kind", "annotate", "--cwd", str(tmp_path), "--title", "T",
                   "--items", str(doc)])
    assert rc != 0
    assert "too large" in capsys.readouterr().err


def test_no_config_never_opens_a_socket_at_all(tmp_path, monkeypatch, capsys):
    """The regression this exists for: `config.load()` falls back to the
    default host and port for a missing file, which is right for the daemon's
    own boot and wrong for a client. An unconfigured client therefore aimed
    every request at whatever was listening on the default port and -- since
    the daemon trusts any loopback caller as its owner -- did not merely read
    it, it created sessions on it. This suite's own "no daemon" tests wrote 19
    real sessions into the real daemon on this machine before it was noticed.

    Asserting the message is not enough: a client that connects, fails for
    some other reason, and prints the same text would pass. So this asserts
    the stronger property the fix actually provides -- no socket is opened.
    """
    import webcompanion.client as clientmod
    from webcompanion import config as cfgmod

    monkeypatch.setattr(cfgmod, "config_path", lambda: tmp_path / "nope.json")

    def explode(*a, **k):
        raise AssertionError(
            "an unconfigured client opened a socket; it must refuse first")

    monkeypatch.setattr(clientmod.urllib.request, "urlopen", explode)

    doc = tmp_path / "doc.json"
    doc.write_text(json.dumps({"items": {}}))
    rc = push.run(["--kind", "annotate", "--cwd", str(tmp_path), "--title", "T",
                   "--items", str(doc)])

    assert rc != 0, "a push with no configuration must fail, not go looking"
    assert "not installed" in capsys.readouterr().err


def test_a_missing_daemon_prints_the_install_command_and_never_starts_one(
        tmp_path, monkeypatch, capsys):
    from webcompanion import config as cfgmod
    p = tmp_path / "config.json"
    monkeypatch.setattr(cfgmod, "config_path", lambda: p)
    doc = tmp_path / "doc.json"
    doc.write_text(json.dumps({"items": {}}))
    rc = push.run(["--kind", "annotate", "--cwd", str(tmp_path), "--title", "T",
                   "--items", str(doc)])
    err = capsys.readouterr().err
    assert rc != 0
    assert "webcompanion install-service" in err


def test_a_contract_mismatch_names_which_side_is_old(wired, tmp_path, monkeypatch, capsys):
    import webcompanion.client as clientmod
    monkeypatch.setattr(clientmod, "CONTRACT", 99)
    doc = tmp_path / "doc.json"
    doc.write_text(json.dumps({"items": {}}))
    rc = push.run(["--kind", "annotate", "--cwd", str(tmp_path), "--title", "T",
                   "--items", str(doc)])
    assert rc != 0
    assert "daemon" in capsys.readouterr().err


def test_push_eval_output_includes_the_state_dir(wired, tmp_path, capsys):
    doc = tmp_path / "doc.json"
    doc.write_text(json.dumps({"items": {}}))
    push.run(["--kind", "show-diff", "--cwd", str(tmp_path), "--title", "T",
              "--items", str(doc), "--eval"])
    out = capsys.readouterr().out
    lines = {l.split("=", 1)[0]: l.split("=", 1)[1] for l in out.splitlines() if "=" in l}
    assert "WC_STATE_DIR" in lines
    from pathlib import Path
    assert Path(lines["WC_STATE_DIR"].strip("'\"")).is_dir()
