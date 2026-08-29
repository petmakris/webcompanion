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
    rc = doctor.run([])
    out = capsys.readouterr().out.lower()
    assert rc != 0
    assert "respawn loop" in out, (
        "must name the loop specifically -- 'recent restarts: 0' alone "
        "satisfies a bare 'restart' substring check for any count")


def test_doctor_reports_unknown_restart_count_rather_than_a_fabricated_zero(
        monkeypatch, capsys):
    """macOS's launchctl has no NRestarts equivalent -- printing 0 when the
    count is genuinely unknown reads as evidence of health to an operator
    debugging a respawn loop."""
    monkeypatch.setattr(doctor, "_recent_restart_count", lambda: None)
    doctor.run([])
    out = capsys.readouterr().out.lower()
    assert "unknown" in out
    assert "recent restarts: 0" not in out


def test_doctor_reports_the_resolved_service_python_separately_from_its_own(
        monkeypatch, capsys):
    """doctor's own interpreter (sys.executable) is not what launchd would
    exec -- launchd resolves `/usr/bin/env python3` under a minimal PATH,
    which can be a different, older python3."""
    from pathlib import Path
    monkeypatch.setattr(doctor, "_resolved_service_python",
                        lambda: (Path("/usr/bin/python3"), (3, 9, 6)))
    doctor.run([])
    out = capsys.readouterr().out.lower()
    assert "/usr/bin/python3" in out
    assert "3.9.6" in out


def test_doctor_fails_when_the_resolved_service_python_is_too_old(wired, monkeypatch, capsys):
    # `wired` makes every other check pass (real config, real healthy daemon)
    # so a failure here can only come from the version-floor check itself.
    from pathlib import Path
    monkeypatch.setattr(doctor, "_resolved_service_python",
                        lambda: (Path("/usr/bin/python3"), (3, 8, 10)))
    rc = doctor.run([])
    out = capsys.readouterr().out.lower()
    assert rc != 0
    assert "3.8.10" in out
    assert "below" in out


def test_doctor_fails_when_no_service_python_can_be_resolved(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "_resolved_service_python", lambda: None)
    rc = doctor.run([])
    out = capsys.readouterr().out.lower()
    assert rc != 0
    assert "not found" in out


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
