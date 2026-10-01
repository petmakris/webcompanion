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


def _pretend_installed(monkeypatch, tmp_path):
    """A plist/unit on disk that the supervisor knows about, without going
    anywhere near the real login session."""
    service = tmp_path / "dev.webcompanion.plist"
    service.write_text("<service definition>")
    monkeypatch.setattr(doctor, "_service_file", lambda: (service, True))
    monkeypatch.setattr(doctor, "_supervisor_knows_the_job", lambda: True)
    return service


def test_doctor_reports_a_healthy_daemon(wired, tmp_path, monkeypatch, capsys):
    _pretend_installed(monkeypatch, tmp_path)
    assert doctor.run([]) == 0
    assert "ok" in capsys.readouterr().out.lower()


def test_doctor_tells_never_installed_apart_from_installed_correctly(
        wired, tmp_path, monkeypatch, capsys):
    """Both used to print the same line -- "no service installed, or
    resolved via /usr/bin/env" -- so doctor could not distinguish a machine
    that never ran install-service from a healthy one. Opposite diagnoses,
    opposite fixes."""
    monkeypatch.setattr(doctor, "_service_file",
                        lambda: (tmp_path / "absent.plist", False))
    assert doctor.run([]) == 1
    absent = capsys.readouterr().out
    assert "NOT INSTALLED" in absent
    assert "webcompanion install-service" in absent

    _pretend_installed(monkeypatch, tmp_path)
    assert doctor.run([]) == 0
    assert "knows the job" in capsys.readouterr().out


def test_doctor_reports_a_service_file_the_supervisor_never_loaded(
        wired, tmp_path, monkeypatch, capsys):
    """A plist on disk is not a running service. An install that wrote the
    file and failed to load it looks identical on disk to one that worked --
    which is exactly what the Linux install-service bug produced."""
    _pretend_installed(monkeypatch, tmp_path)
    monkeypatch.setattr(doctor, "_supervisor_knows_the_job", lambda: False)
    assert doctor.run([]) == 1
    out = capsys.readouterr().out
    assert "does NOT know the job" in out


def test_doctor_reports_an_unreadable_sessions_file(wired, tmp_path, monkeypatch, capsys):
    """The file whose loss deletes data. doctor must surface it BEFORE the
    next restart, not after."""
    from webcompanion import paths as pathsmod

    _pretend_installed(monkeypatch, tmp_path)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "sessions.json").write_text("{not json")
    monkeypatch.setattr(pathsmod, "state_root", lambda: state)

    assert doctor.run([]) == 1
    out = capsys.readouterr().out
    assert "sessions.json: UNREADABLE" in out
    assert "refuses its startup stray sweep" in out


def test_doctor_reads_the_log_file_the_installed_service_actually_writes(
        tmp_path, monkeypatch):
    """The one assertion that would have caught the mismatch this test file
    used to hide.

    `doctor.log_path()` said `webcompanion.log`; the plist `install-service`
    writes on macOS -- the primary platform -- pointed StandardErrorPath at
    `dev.webcompanion.log`. The old test wrote the log file itself, under
    the name it had chosen, so it passed against a `doctor` that was reading
    a file nothing ever wrote. So: no filename appears in this test at all.
    It parses the service definition `install-service` renders and compares
    `doctor`'s path to the path found INSIDE it.
    """
    import plistlib
    import sys

    from webcompanion import paths as pathsmod
    from webcompanion.commands import install_service as svc

    state = tmp_path / "state"
    monkeypatch.setattr(pathsmod, "state_root", lambda: state)

    pyz = tmp_path / "webcompanion.pyz"
    if sys.platform == "darwin":
        rendered = svc.render_plist(pyz=pyz, log_dir=pathsmod.state_root(),
                                    label=svc.DEFAULT_LABEL)
        written_to = plistlib.loads(rendered.encode())["StandardErrorPath"]
    else:
        rendered = svc.render_unit(pyz=pyz, log_dir=pathsmod.state_root())
        written_to = next(
            line.split("append:", 1)[1]
            for line in rendered.splitlines()
            if line.startswith("StandardError="))

    assert str(doctor.log_path()) == written_to


def test_doctor_tails_the_service_log(wired, tmp_path, monkeypatch, capsys):
    """A user told "see the log" and left to find it has been told nothing:
    launchd rotates it and nobody reads it until something else breaks.

    Writes to `doctor.log_path()` rather than to a filename of its own, so
    it cannot pass while `doctor` tails a file the service never writes --
    the path itself is pinned by the test above.
    """
    from webcompanion import paths as pathsmod

    _pretend_installed(monkeypatch, tmp_path)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    monkeypatch.setattr(pathsmod, "state_root", lambda: state)
    doctor.log_path().write_text("\n".join(f"line {i}" for i in range(50)))

    doctor.run([])
    out = capsys.readouterr().out
    assert "line 49" in out
    assert "line 30" in out, "the tail is shorter than LOG_TAIL_LINES"
    assert "line 10" not in out, "the tail is not bounded"


def test_doctor_reports_a_refused_sweep_differently_from_a_failed_one(
        wired, tmp_path, monkeypatch, capsys):
    """A refusal is the safety mechanism working -- nothing was deleted. It
    must not read as a crash."""
    from webcompanion import paths as pathsmod

    _pretend_installed(monkeypatch, tmp_path)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "startup_sweep_failed.json").write_text(json.dumps({
        "when": 1234567890.0,
        "refused": "the registry is empty but ~/ws still holds sessions",
    }))
    monkeypatch.setattr(pathsmod, "state_root", lambda: state)

    assert doctor.run([]) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out
    assert "nothing was deleted" in out


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


def test_doctor_says_speech_is_optional_when_unset(capsys):
    doctor.run([])
    out = capsys.readouterr().out
    assert "speech: not set up (optional)" in out


def test_doctor_reports_speech_without_printing_the_key(capsys):
    from webcompanion import paths
    root = paths.state_root()
    root.mkdir(parents=True, exist_ok=True)
    env = root / "speech.env"
    env.write_text("AZURE_SPEECH_KEY=SECRET-KEY-123\nAZURE_SPEECH_REGION=westeurope\n")
    env.chmod(0o644)
    doctor.run([])
    out = capsys.readouterr().out
    assert "speech: key set, region westeurope" in out
    assert "expected mode 0600" in out
    assert "SECRET-KEY-123" not in out
