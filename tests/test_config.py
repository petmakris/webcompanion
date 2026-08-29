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
    assert cfg.workspace_root is None
    assert cfg.token == ""


def test_write_then_load_roundtrips(tmp_path):
    p = tmp_path / "config.json"
    cfg = cfgmod.Config(port=3999, token="abc", bind="127.0.0.1",
                        retention_days=30, workspace_root=tmp_path / "ws",
                        public_host=None)
    cfgmod.write(cfg, p)
    back = cfgmod.load(p)
    assert back.port == 3999
    assert back.token == "abc"
    assert back.retention_days == 30
    assert back.workspace_root == tmp_path / "ws"


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
