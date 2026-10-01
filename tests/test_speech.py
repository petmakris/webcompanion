from __future__ import annotations

import os
import stat
import warnings

import pytest

from webcompanion import speech


def _exe(path, body="#!/bin/sh\nexit 0\n"):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_the_env_file_reads_plain_quoted_and_exported_lines(tmp_path):
    p = tmp_path / "speech.env"
    p.write_text('# a comment\n\nAZURE_SPEECH_KEY="abc"\n'
                 "export AZURE_SPEECH_REGION='westeurope'\nCLAUDE_BIN=/x/claude\nnot a line\n")
    assert speech.read_env_file(p) == {
        "AZURE_SPEECH_KEY": "abc", "AZURE_SPEECH_REGION": "westeurope",
        "CLAUDE_BIN": "/x/claude"}


def test_a_missing_env_file_is_an_empty_config(tmp_path):
    cfg = speech.load(tmp_path)
    assert cfg == speech.SpeechConfig(key=None, region=None, claude_bin=None)


def test_claude_is_found_from_claude_bin_first(tmp_path, monkeypatch):
    monkeypatch.setattr(speech.shutil, "which", lambda _: None)
    exe = _exe(tmp_path / "my-claude")
    cfg = speech.SpeechConfig(key=None, region=None, claude_bin=str(exe))
    assert speech.resolve_claude(cfg) == str(exe)


def test_claude_falls_back_to_local_bin(tmp_path, monkeypatch):
    # HOME is redirected to tmp_path/_home by conftest.
    monkeypatch.setattr(speech.shutil, "which", lambda _: None)
    local = tmp_path / "_home" / ".local" / "bin"
    local.mkdir(parents=True)
    exe = _exe(local / "claude")
    assert speech.resolve_claude(speech.SpeechConfig(None, None, None)) == str(exe)


def test_a_claude_bin_that_is_not_executable_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(speech.shutil, "which", lambda _: None)
    f = tmp_path / "claude"
    f.write_text("not executable")
    assert speech.resolve_claude(speech.SpeechConfig(None, None, str(f))) is None


def test_status_names_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(speech.shutil, "which", lambda _: None)
    s = speech.status(speech.SpeechConfig(None, None, None))
    assert s["configured"] is False and s["claude"] is False
    assert "AZURE_SPEECH_KEY" in s["reason"]

    exe = _exe(tmp_path / "claude")
    s = speech.status(speech.SpeechConfig("k", "westeurope", str(exe)))
    assert s == {"configured": True, "claude": True, "region": "westeurope"}


def test_status_never_contains_the_key():
    s = speech.status(speech.SpeechConfig("SECRET-KEY-123", "westeurope", None))
    assert "SECRET-KEY-123" not in repr(s)


import http.server
import socket
import threading


class _FakeSTS:
    """A local stand-in for Azure's issueToken endpoint."""

    def __init__(self, status=200, body=b"tok-123"):
        seen = self.seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                seen.append({"path": self.path,
                             "key": self.headers.get("Ocp-Apim-Subscription-Key")})
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=False)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/" + "{region}/sts"

    def close(self):
        self.srv.shutdown()
        self.thread.join()
        self.srv.server_close()


@pytest.fixture
def sts(monkeypatch):
    made = []

    def make(**kw):
        f = _FakeSTS(**kw)
        made.append(f)
        monkeypatch.setattr(speech, "STS_URL", f.url)
        return f
    yield make
    for f in made:
        f.close()


def test_a_token_is_minted_with_the_key_in_the_header(sts):
    fake = sts()
    out = speech.mint_token(speech.SpeechConfig("KEY-1", "westeurope", None))
    assert out == {"token": "tok-123", "region": "westeurope", "expires_in": 540}
    assert fake.seen == [{"path": "/westeurope/sts", "key": "KEY-1"}]


def test_no_key_is_a_503_before_any_request(sts):
    fake = sts()
    with pytest.raises(speech.SpeechError) as e:
        speech.mint_token(speech.SpeechConfig(None, "westeurope", None))
    assert e.value.status == 503
    assert fake.seen == []


def test_an_azure_refusal_is_a_502_naming_the_status_and_not_the_key(sts):
    sts(status=401, body=b"Access denied due to invalid subscription key")
    with pytest.raises(speech.SpeechError) as e:
        speech.mint_token(speech.SpeechConfig("SECRET-KEY-123", "westeurope", None))
    assert e.value.status == 502
    assert "HTTP 401" in e.value.message
    assert "SECRET-KEY-123" not in e.value.message


def test_an_unreachable_azure_is_a_502(monkeypatch):
    monkeypatch.setattr(speech, "STS_URL", "http://127.0.0.1:9/{region}")
    with pytest.raises(speech.SpeechError) as e:
        speech.mint_token(speech.SpeechConfig("k", "westeurope", None))
    assert e.value.status == 502
    assert "did not answer" in e.value.message


def test_a_non_latin1_key_raises_502_not_exposing_the_key(sts):
    sts()
    with pytest.raises(speech.SpeechError) as e:
        speech.mint_token(speech.SpeechConfig("ké€-SECRET", "westeurope", None))
    assert e.value.status == 502
    assert "SECRET" not in e.value.message


def test_an_empty_token_is_a_502(sts):
    sts(body=b"")
    with pytest.raises(speech.SpeechError) as e:
        speech.mint_token(speech.SpeechConfig("k", "westeurope", None))
    assert e.value.status == 502
    assert "empty token" in e.value.message


def test_missing_region_is_a_503_before_any_request(sts):
    fake = sts()
    with pytest.raises(speech.SpeechError) as e:
        speech.mint_token(speech.SpeechConfig("k", None, None))
    assert e.value.status == 503
    assert fake.seen == []
