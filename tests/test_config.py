from __future__ import annotations

import json
import stat

import pytest

from webcompanion import config as cfgmod


def test_defaults_when_no_file(tmp_path):
    cfg = cfgmod.load(tmp_path / "absent.json")
    assert cfg.port == 3080
    assert cfg.bind == "127.0.0.1"
    assert cfg.retention_days is None
    # Unlike retention_days, auto-expiry is a safety net that ships ON --
    # see docs/2026-09-02-session-lifecycle-design.md Decision 1.
    assert cfg.idle_expiry_hours == cfgmod.DEFAULT_IDLE_EXPIRY_HOURS
    assert cfg.workspace_root is None
    assert cfg.token == ""
    assert cfg.tailscale_owner_login is None


def test_write_then_load_roundtrips(tmp_path):
    p = tmp_path / "config.json"
    cfg = cfgmod.Config(port=3999, token="abc", bind="127.0.0.1",
                        retention_days=30, idle_expiry_hours=6,
                        workspace_root=tmp_path / "ws",
                        tailscale_owner_login="me@example.com")
    cfgmod.write(cfg, p)
    back = cfgmod.load(p)
    assert back.port == 3999
    assert back.token == "abc"
    assert back.retention_days == 30
    assert back.idle_expiry_hours == 6
    assert back.workspace_root == tmp_path / "ws"
    assert back.tailscale_owner_login == "me@example.com"


def test_tailscale_owner_login_defaults_to_none_for_a_config_file_written_before_the_field_existed(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"port": 3080, "token": "t", "bind": "127.0.0.1"}))
    assert cfgmod.load(p).tailscale_owner_login is None


def test_a_blank_tailscale_owner_login_is_treated_as_unset(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"tailscale_owner_login": "   "}))
    assert cfgmod.load(p).tailscale_owner_login is None


def test_idle_expiry_hours_defaults_for_a_config_file_written_before_the_field_existed(tmp_path):
    """A config file on disk from before this field existed has no
    `idle_expiry_hours` key at all -- it must not read as "disabled"
    (that would silently turn the safety net off for every upgraded
    install), it must fall back to the same default a fresh install gets."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"port": 3080, "token": "t", "bind": "127.0.0.1"}))
    assert cfgmod.load(p).idle_expiry_hours == cfgmod.DEFAULT_IDLE_EXPIRY_HOURS


def test_idle_expiry_hours_can_be_explicitly_disabled_via_null(tmp_path):
    """An explicit `null` (as opposed to the key being absent) is the only
    way to turn the safety net off from the config file, since the default
    is a real number rather than None -- see `config._int_or_none`."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"idle_expiry_hours": None}))
    assert cfgmod.load(p).idle_expiry_hours is None


def test_written_file_is_owner_only(tmp_path):
    p = tmp_path / "config.json"
    cfgmod.write(cfgmod.Config(port=3080, token=cfgmod.mint_token()), p)
    mode = stat.S_IMODE(p.stat().st_mode)
    assert mode == 0o600, f"config holds the write token; got {oct(mode)}"


def test_corrupt_file_falls_back_to_defaults_without_raising(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{not json")
    cfg = cfgmod.load(p)
    assert cfg.port == 3080


def test_relative_workspace_root_is_rejected(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"workspace_root": "relative/path"}))
    assert cfgmod.load(p).workspace_root is None


def test_environment_is_never_consulted(tmp_path, monkeypatch):
    # Under launchd the daemon has a fixed environment; a setting that only
    # works when exported from an interactive shell is a setting that
    # silently stops working after install-service.
    monkeypatch.setenv("WEBCOMPANION_BIND", "0.0.0.0")
    monkeypatch.setenv("WEBCOMPANION_RETENTION_DAYS", "5")
    cfg = cfgmod.load(tmp_path / "absent.json")
    assert cfg.bind == "127.0.0.1"
    assert cfg.retention_days is None


def test_mint_token_is_unguessable_and_unique():
    a, b = cfgmod.mint_token(), cfgmod.mint_token()
    assert a != b
    assert len(a) >= 32
