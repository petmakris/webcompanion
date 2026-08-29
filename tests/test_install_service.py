from __future__ import annotations

import json
import plistlib
import sys
import zipfile

import pytest

from webcompanion import config as cfgmod
from webcompanion.commands import install_service as svc


def test_the_plist_runs_a_zipapp_with_the_system_python(tmp_path):
    xml = svc.render_plist(pyz=tmp_path / "webcompanion.pyz",
                           log_dir=tmp_path, label="dev.webcompanion")
    plist = plistlib.loads(xml.encode())
    args = plist["ProgramArguments"]
    assert args[0] == "/usr/bin/env"
    assert args[1] == "python3"
    assert args[2].endswith("webcompanion.pyz")
    assert args[3] == "serve"
    assert "site-packages" not in xml and "pipx" not in xml, (
        "a venv-bound interpreter dangles when its python is replaced")


def test_the_plist_sets_keepalive_a_throttle_and_a_log(tmp_path):
    plist = plistlib.loads(
        svc.render_plist(pyz=tmp_path / "w.pyz", log_dir=tmp_path,
                         label="dev.webcompanion").encode())
    assert plist["KeepAlive"] is True
    assert plist["ThrottleInterval"] >= 10
    assert plist["StandardErrorPath"].endswith(".log")


def test_the_systemd_unit_restarts_always(tmp_path):
    unit = svc.render_unit(pyz=tmp_path / "w.pyz")
    assert "Restart=always" in unit
    assert "RestartSec=" in unit


def test_the_zipapp_is_self_contained_and_executable(tmp_path):
    pyz = svc.build_zipapp(tmp_path / "webcompanion.pyz")
    assert pyz.is_file()
    with zipfile.ZipFile(pyz) as z:
        names = z.namelist()
    assert "__main__.py" in names
    assert any(n.endswith("webcompanion/server.py") for n in names)
    assert any(n.endswith("webcompanion/static/core.js") for n in names), (
        "static assets must be inside the zipapp; the daemon reads them "
        "through importlib.resources")


def test_install_mints_a_token_only_when_there_is_not_one(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    monkeypatch.setattr(cfgmod, "config_path", lambda: p)
    cfgmod.write(cfgmod.Config(port=3080, token="keep-me"), p)
    svc.ensure_config()
    assert cfgmod.load(p).token == "keep-me", (
        "reminting on every install invalidates the IDE plugin's credential")


def test_install_mints_a_token_when_there_is_none(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    monkeypatch.setattr(cfgmod, "config_path", lambda: p)
    svc.ensure_config()
    assert len(cfgmod.load(p).token) >= 32


def test_install_service_never_touches_the_real_login_session_for_a_throwaway_target(
        tmp_path, monkeypatch):
    """The HARD LIMIT: exercising the whole install path against a throwaway
    target_dir/label must never call launchctl/systemctl. `_load_and_restart`
    is the only place those subprocess calls live; asserting it is not
    called (by making it raise if it is) is a stronger guarantee than trusting
    the label."""
    p = tmp_path / "config.json"
    monkeypatch.setattr(cfgmod, "config_path", lambda: p)

    def _boom(*a, **k):
        raise AssertionError("must never load/restart a throwaway install")

    monkeypatch.setattr(svc, "_load_and_restart", _boom)
    monkeypatch.setattr(svc, "build_zipapp", lambda dest: dest)

    target = tmp_path / "throwaway-launchagents"
    rc = svc.run([], target_dir=target, label="throwaway.test.webcompanion")
    assert rc == 0
    written = list(target.iterdir())
    assert len(written) == 1
    expected = ("throwaway.test.webcompanion.plist" if sys.platform == "darwin"
                else "webcompanion.service")
    assert written[0].name == expected


def test_ensure_config_refuses_to_remint_over_a_corrupt_config(tmp_path, monkeypatch):
    """config.load() tolerates a corrupt file by returning bare defaults --
    right for a read path, but minting a fresh token here would silently
    discard the write token an IDE plugin is already using, plus every
    other setting, and dress a disk error up as a first install."""
    p = tmp_path / "config.json"
    p.write_text("{not valid json")
    monkeypatch.setattr(cfgmod, "config_path", lambda: p)

    with pytest.raises(svc.ConfigUnreadable):
        svc.ensure_config()

    assert p.read_text() == "{not valid json", "a corrupt file must not be overwritten"


def test_install_service_run_fails_loudly_on_a_corrupt_config_without_touching_it(
        tmp_path, monkeypatch, capsys):
    p = tmp_path / "config.json"
    original = json.dumps({"port": 3080, "token": "THE-PLUGIN-IS-USING-THIS",
                           "retention_days": 90})[:-1]  # truncated -> invalid JSON
    p.write_text(original)
    monkeypatch.setattr(cfgmod, "config_path", lambda: p)
    monkeypatch.setattr(svc, "_load_and_restart",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not reach load-and-restart")))

    rc = svc.run([], target_dir=tmp_path / "throwaway",
                  label="throwaway.corrupt.webcompanion")

    assert rc != 0
    assert p.read_text() == original, "a corrupt config must survive a failed install untouched"
    err = capsys.readouterr().err
    assert str(p) in err
    assert not (tmp_path / "throwaway").exists() or not any(
        (tmp_path / "throwaway").iterdir()), "must not write a plist/unit over a corrupt config"


# ── the service is addressed by the name that was written, and failure is
#    reported ─────────────────────────────────────────────────────────────
#
# Every test below fakes subprocess.run, so no real service is ever
# registered, unregistered, or restarted.

def _record_commands(monkeypatch, returncode=0, stderr=""):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))

        class Result:
            pass
        r = Result()
        r.returncode = returncode
        r.stdout = ""
        r.stderr = stderr
        return r

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    return calls


def test_the_systemd_calls_name_the_unit_file_that_was_written(tmp_path, monkeypatch):
    """install-service wrote `webcompanion.service` and then ran systemctl
    against `dev.webcompanion`, the launchd label. Every call named a unit
    that does not exist, so a Linux install started nothing."""
    calls = _record_commands(monkeypatch)
    unit_path = tmp_path / svc.DEFAULT_SERVICE_NAME

    problems = svc._load_and_restart("linux", unit_path, svc.DEFAULT_LABEL)

    assert problems == []
    named = [c[-1] for c in calls if c[-1] != "daemon-reload"]
    assert named and all(n == svc.DEFAULT_SERVICE_NAME for n in named), calls
    assert svc.DEFAULT_LABEL not in [tok for c in calls for tok in c]


def test_a_failed_systemctl_call_is_reported_not_swallowed(tmp_path, monkeypatch):
    """Every call was check=False, capture_output=True with the result
    discarded, so `installed ... and restarted` printed no matter what."""
    _record_commands(monkeypatch, returncode=5,
                     stderr="Failed to enable unit: does not exist")

    problems = svc._load_and_restart(
        "linux", tmp_path / svc.DEFAULT_SERVICE_NAME, svc.DEFAULT_LABEL)

    assert len(problems) == 3
    assert all("exited 5" in p for p in problems)
    assert any("does not exist" in p for p in problems)


def test_a_first_install_tolerates_only_the_launchd_bootout(tmp_path, monkeypatch):
    """`launchctl bootout` fails when nothing is loaded yet -- the normal
    first install. It is the one step allowed to fail; bootstrap and
    kickstart are not."""
    _record_commands(monkeypatch, returncode=1)

    problems = svc._load_and_restart(
        "darwin", tmp_path / "dev.webcompanion.plist", svc.DEFAULT_LABEL)

    assert len(problems) == 2
    assert not any("bootout" in p for p in problems)
    assert any("bootstrap" in p for p in problems)
    assert any("kickstart" in p for p in problems)


def test_run_returns_nonzero_and_says_so_when_the_service_would_not_start(
        tmp_path, monkeypatch, capsys):
    """The user-visible half: a failed start must not print success."""
    monkeypatch.setattr(cfgmod, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(svc, "build_zipapp", lambda dest: tmp_path / "w.pyz")
    monkeypatch.setattr(svc, "default_plist_path",
                        lambda label=svc.DEFAULT_LABEL: tmp_path / f"{label}.plist")
    monkeypatch.setattr(svc, "default_unit_path",
                        lambda name=svc.DEFAULT_SERVICE_NAME: tmp_path / name)
    monkeypatch.setattr(svc, "_load_and_restart",
                        lambda system, unit_path, label: ["`systemctl ...` exited 5"])

    assert svc.run([]) == 1
    err = capsys.readouterr().err
    assert "could not start" in err
    assert "exited 5" in err
