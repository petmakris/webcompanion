from __future__ import annotations

from webcompanion.commands import status


def test_status_reports_running_and_the_session_count(wired, capsys):
    assert status.run([]) == 0
    out = capsys.readouterr().out
    assert "running" in out.lower()
    assert "sessions: 0" in out.lower()


def test_status_reports_not_running_when_the_daemon_is_unreachable(tmp_path, monkeypatch, capsys):
    from webcompanion import config as cfgmod
    p = tmp_path / "config.json"
    cfgmod.write(cfgmod.Config(port=1, token="t", bind="127.0.0.1"), p)
    monkeypatch.setattr(cfgmod, "config_path", lambda: p)

    rc = status.run([])

    assert rc != 0
    assert "not running" in capsys.readouterr().out.lower()
