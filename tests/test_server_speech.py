from __future__ import annotations

import json
import stat

import pytest

from webcompanion import paths, speech

CROSS_SITE = {"Sec-Fetch-Site": "cross-site"}
KEY = "SECRET-KEY-123"


@pytest.fixture
def configured(tmp_path):
    exe = tmp_path / "fake-claude"
    out = tmp_path / "fake.out"
    out.write_text(json.dumps({"type": "result", "is_error": False, "structured_output": {
        "pieces": [{"say": "It says hello.", "src": "hello"}]}}))
    exe.write_text(f'#!/bin/sh\ncat "{out}"\n')
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    root = paths.state_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "speech.env").write_text(
        f"AZURE_SPEECH_KEY={KEY}\nAZURE_SPEECH_REGION=westeurope\nCLAUDE_BIN={exe}\n")
    return root


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/speech/status"), ("POST", "/api/speech/token"),
    ("POST", "/api/speech/script")])
def test_every_speech_route_refuses_a_non_owner(call, configured, method, path):
    status, _ = call(method, path, {"selection": "hello"} if method == "POST" else None,
                     headers=CROSS_SITE)
    assert status == 403


def test_status_reports_configured_without_the_key(call, configured):
    status, body = call("GET", "/api/speech/status")
    assert status == 200
    assert body == {"configured": True, "claude": True, "region": "westeurope"}
    assert KEY not in json.dumps(body)


def test_status_when_nothing_is_set_up(call):
    status, body = call("GET", "/api/speech/status")
    assert status == 200
    assert body["configured"] is False


def test_script_route_returns_pieces(call, configured):
    status, body = call("POST", "/api/speech/script",
                        {"selection": "hello world", "context": "", "glossary": [],
                         "page_title": "T"})
    assert status == 200
    assert body == {"pieces": [{"say": "It says hello.", "src": "hello"}], "cached": False}
    assert (configured / speech.CACHE_DIR).is_dir()


def test_script_route_maps_errors_to_status_and_message(call, configured):
    status, body = call("POST", "/api/speech/script", {"selection": "x" * 4001})
    assert status == 413
    assert "4000" in body
    status, body = call("POST", "/api/speech/script", {})
    assert status == 400


def test_token_route_reports_azure_failure_without_the_key(call, configured, monkeypatch):
    monkeypatch.setattr(speech, "STS_URL", "http://127.0.0.1:9/{region}")
    status, body = call("POST", "/api/speech/token")
    assert status == 502
    assert KEY not in body


def test_editing_speech_env_needs_no_restart(call, configured):
    assert call("GET", "/api/speech/status")[1]["configured"] is True
    (configured / "speech.env").write_text("AZURE_SPEECH_REGION=westeurope\n")
    assert call("GET", "/api/speech/status")[1]["configured"] is False
