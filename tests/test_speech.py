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
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/" + "{region}/sts"

    def close(self):
        self.srv.shutdown()
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


# append to tests/test_speech.py
import json as _json

ENVELOPE = {"type": "result", "subtype": "success", "is_error": False,
            "result": "...",
            "structured_output": {"pieces": [
                {"src": "The spec (use case 06a/07a)", "say": "The spec, in use cases zero-six-A and zero-seven-A,"},
                {"src": "a paraphrase not in the text", "say": "says Europe and Switzerland."},
                {"src": "", "say": "   "}]}}
SELECTION = "The spec (use case 06a/07a) says Europe & CH First/Premium."


def _fake_claude(tmp_path, envelope=ENVELOPE, exit_code=0, sleep=0):
    """A stand-in for `claude -p`. It records its argv and how often it ran,
    and prints a fixed envelope."""
    out = tmp_path / "fake.out"
    out.write_text(_json.dumps(envelope) if isinstance(envelope, dict) else envelope)
    exe = tmp_path / "fake-claude"
    _exe(exe, "#!/bin/sh\n"
              f'echo run >> "{tmp_path}/fake.count"\n'
              f'printf "%s\\n" "$@" > "{tmp_path}/fake.argv"\n'
              f"sleep {sleep}\n"
              f'cat "{out}"\n'
              f"exit {exit_code}\n")
    return speech.SpeechConfig("k", "westeurope", str(exe))


def _runs(tmp_path):
    p = tmp_path / "fake.count"
    return len(p.read_text().splitlines()) if p.exists() else 0


def test_pieces_come_back_with_unmatched_src_blanked_and_empty_say_dropped(tmp_path):
    cfg = _fake_claude(tmp_path)
    out = speech.write_script(cfg, tmp_path / "cache", SELECTION, "ctx", [], "PMP-310")
    assert out == {"cached": False, "pieces": [
        {"say": "The spec, in use cases zero-six-A and zero-seven-A,",
         "src": "The spec (use case 06a/07a)"},
        {"say": "says Europe and Switzerland.", "src": ""}]}


def test_claude_is_called_headless_with_opus_no_tools_and_the_schema(tmp_path):
    cfg = _fake_claude(tmp_path)
    speech.write_script(cfg, tmp_path / "cache", SELECTION)
    argv = (tmp_path / "fake.argv").read_text().split("\n")
    for flag in ("-p", "--no-session-persistence", "--output-format", "--json-schema",
                 "--system-prompt"):
        assert flag in argv
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert _json.loads(argv[argv.index("--json-schema") + 1]) == speech.SCRIPT_SCHEMA


def test_a_second_identical_request_is_served_from_the_cache(tmp_path):
    cfg = _fake_claude(tmp_path)
    a = speech.write_script(cfg, tmp_path / "cache", SELECTION, "ctx", [{"term": "EDR"}], "T")
    b = speech.write_script(cfg, tmp_path / "cache", SELECTION, "ctx", [{"term": "EDR"}], "T")
    assert b == {**a, "cached": True}
    assert _runs(tmp_path) == 1
    speech.write_script(cfg, tmp_path / "cache", SELECTION, "other ctx", [{"term": "EDR"}], "T")
    assert _runs(tmp_path) == 2


def test_old_cache_entries_are_swept_on_write(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    old = cache / ("0" * 64 + ".json")
    old.write_text("{}")
    os.utime(old, (1, 1))
    speech.write_script(_fake_claude(tmp_path), cache, SELECTION)
    assert not old.exists()


@pytest.mark.parametrize("selection", [None, 42, "", "   \n "])
def test_a_missing_or_blank_selection_is_a_400(tmp_path, selection):
    with pytest.raises(speech.SpeechError) as e:
        speech.write_script(_fake_claude(tmp_path), tmp_path / "c", selection)
    assert e.value.status == 400
    assert _runs(tmp_path) == 0


def test_a_selection_over_the_limit_is_a_413(tmp_path):
    with pytest.raises(speech.SpeechError) as e:
        speech.write_script(_fake_claude(tmp_path), tmp_path / "c", "x" * 4001)
    assert e.value.status == 413


def test_a_glossary_that_is_not_a_list_is_a_400(tmp_path):
    with pytest.raises(speech.SpeechError) as e:
        speech.write_script(_fake_claude(tmp_path), tmp_path / "c", SELECTION, "", "EDR")
    assert e.value.status == 400


def test_glossary_entries_without_a_term_are_ignored_not_fatal(tmp_path):
    out = speech.write_script(_fake_claude(tmp_path), tmp_path / "c", SELECTION, "",
                              [{"definition": "no term"}, "junk", {"term": "EDR", "definition": "x"}])
    assert out["pieces"]


def test_claude_not_found_is_a_503(tmp_path, monkeypatch):
    monkeypatch.setattr(speech.shutil, "which", lambda _: None)
    cfg = speech.SpeechConfig("k", "r", str(tmp_path / "nope"))
    with pytest.raises(speech.SpeechError) as e:
        speech.write_script(cfg, tmp_path / "c", SELECTION)
    assert e.value.status == 503
    assert "claude was not found" in e.value.message


def test_claude_not_logged_in_is_a_502_with_its_own_words(tmp_path):
    cfg = _fake_claude(tmp_path, envelope={"type": "result", "is_error": True,
                                           "result": "Not logged in · Please run /login"},
                       exit_code=1)
    with pytest.raises(speech.SpeechError) as e:
        speech.write_script(cfg, tmp_path / "c", SELECTION)
    assert e.value.status == 502
    assert "Not logged in" in e.value.message


def test_output_that_is_not_json_is_a_502(tmp_path):
    cfg = _fake_claude(tmp_path, envelope="segfault\n", exit_code=139)
    with pytest.raises(speech.SpeechError) as e:
        speech.write_script(cfg, tmp_path / "c", SELECTION)
    assert e.value.status == 502


def test_a_slow_claude_is_a_504(tmp_path, monkeypatch):
    monkeypatch.setattr(speech, "SCRIPT_TIMEOUT_S", 1)
    with pytest.raises(speech.SpeechError) as e:
        speech.write_script(_fake_claude(tmp_path, sleep=3), tmp_path / "c", SELECTION)
    assert e.value.status == 504


def test_no_usable_piece_is_a_502(tmp_path):
    env = {**ENVELOPE, "structured_output": {"pieces": [{"src": "", "say": ""}]}}
    with pytest.raises(speech.SpeechError) as e:
        speech.write_script(_fake_claude(tmp_path, envelope=env), tmp_path / "c", SELECTION)
    assert e.value.status == 502


def test_the_prompt_forbids_guessing_and_spells_undefined_abbreviations():
    p = speech.SYSTEM_PROMPT.lower()
    assert "never guess" in p
    assert "letter by letter" in p
    assert "exact" in p and "substring" in p


def test_the_user_prompt_carries_glossary_context_and_title():
    p = speech.build_prompt(SELECTION, "the paragraph",
                            [{"term": "EDR", "definition": "External Data Reference", "role": "confirms segments"}],
                            "PMP-310 ticket draft")
    assert "PMP-310 ticket draft" in p and "the paragraph" in p
    assert "EDR: External Data Reference" in p
    assert p.rstrip().endswith(SELECTION)
