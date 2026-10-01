from __future__ import annotations

import os
import stat

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
