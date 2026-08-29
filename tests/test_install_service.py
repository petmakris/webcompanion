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
