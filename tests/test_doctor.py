from __future__ import annotations

import json

from webcompanion.commands import doctor


def test_doctor_reports_a_missing_service(tmp_path, monkeypatch, capsys):
    from webcompanion import config as cfgmod
    monkeypatch.setattr(cfgmod, "config_path", lambda: tmp_path / "absent.json")
    assert doctor.run([]) != 0
    assert "install-service" in capsys.readouterr().out


def test_doctor_detects_a_dangling_interpreter(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "_service_interpreter",
                        lambda: tmp_path / "gone" / "python3")
    doctor.run([])
    out = capsys.readouterr().out
    assert "interpreter" in out.lower()


def test_doctor_detects_a_respawn_loop(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "_recent_restart_count", lambda: 40)
    doctor.run([])
    assert "restart" in capsys.readouterr().out.lower()


def test_doctor_reports_a_healthy_daemon(wired, capsys):
    assert doctor.run([]) == 0
    assert "ok" in capsys.readouterr().out.lower()


def test_doctor_names_the_port_holder_when_the_port_is_taken(monkeypatch, capsys):
    # "port 3080 held by node (pid 4821)" is the single most useful line the
    # old launcher printed, and a fixed port makes collisions more likely.
    monkeypatch.setattr(doctor, "_port_holder", lambda port: ("node", 4821))
    monkeypatch.setattr(doctor, "_health", lambda: None)
    doctor.run([])
    out = capsys.readouterr().out
    assert "node" in out and "4821" in out


def test_doctor_reports_a_swallowed_startup_sweep_failure(tmp_path, monkeypatch, capsys):
    """Task 14 made the daemon swallow a cleanup-sweep exception so a bad
    workspace directory cannot stop it booting, but that used to leave only
    a stderr traceback -- nobody learned cleanup stopped running. The daemon
    now drops a durable marker under state_root; doctor must surface it."""
    from webcompanion import paths as pathsmod
    monkeypatch.setattr(pathsmod, "state_root", lambda: tmp_path)
    (tmp_path / "startup_sweep_failed.json").write_text(json.dumps({
        "when": 1234567890.0,
        "error": "PermissionError: simulated: cleanup sweep failed",
    }))
    doctor.run([])
    out = capsys.readouterr().out.lower()
    assert "cleanup" in out
    assert "permissionerror" in out
