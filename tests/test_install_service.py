from __future__ import annotations

import plistlib
import sys
import zipfile

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
