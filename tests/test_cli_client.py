from __future__ import annotations

import json

import pytest

from webcompanion.commands import end, push, update


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


def test_a_payload_over_the_limit_is_refused_before_it_is_sent(wired, tmp_path, capsys):
    doc = tmp_path / "doc.json"
    doc.write_text(json.dumps({"items": {"b-1": {"t": "x" * (6 * 1024 * 1024)}}}))
    rc = push.run(["--kind", "annotate", "--cwd", str(tmp_path), "--title", "T",
                   "--items", str(doc)])
    assert rc != 0
    assert "too large" in capsys.readouterr().err


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
