from __future__ import annotations

import json

from webcompanion import cli
from webcompanion.commands import event


def _dirs(tmp_path):
    events_dir = tmp_path / "events"
    consumed_dir = tmp_path / "consumed"
    events_dir.mkdir()
    consumed_dir.mkdir()
    return {"events_dir": str(events_dir), "consumed_dir": str(consumed_dir)}


def _long_round():
    reactions = [{"scope": "unit", "kind": "comment", "block_id": "section-2",
                  "selected_text": "x" * 400, "text": f"comment {n}"}
                 for n in range(6)]
    return json.dumps({"anchor": "section-2",
                       "text": json.dumps({"type": "round", "reactions": reactions})})


def test_prints_a_waiting_event_in_full(tmp_path, monkeypatch, capsys):
    dirs = _dirs(tmp_path)
    payload = _long_round()
    (tmp_path / "events" / "e1.json").write_text(payload)
    monkeypatch.setattr(event.watch, "resolve_session_dirs", lambda kind, sid: dirs)

    assert event.run(["--sid", "s1", "--event-id", "e1"]) == 0

    out = capsys.readouterr().out
    assert out == payload + "\n"
    assert len(json.loads(json.loads(out)["text"])["reactions"]) == 6


def test_reads_an_event_already_archived_after_its_ack(tmp_path, monkeypatch, capsys):
    dirs = _dirs(tmp_path)
    (tmp_path / "consumed" / "e2.json").write_text('{"anchor": "a"}')
    monkeypatch.setattr(event.watch, "resolve_session_dirs", lambda kind, sid: dirs)

    assert event.run(["--sid", "s1", "--event-id", "e2"]) == 0
    assert capsys.readouterr().out == '{"anchor": "a"}\n'


def test_an_event_in_neither_directory_is_an_error(tmp_path, monkeypatch, capsys):
    dirs = _dirs(tmp_path)
    monkeypatch.setattr(event.watch, "resolve_session_dirs", lambda kind, sid: dirs)

    assert event.run(["--sid", "s1", "--event-id", "e3"]) == 1
    assert "no event e3" in capsys.readouterr().err


def test_refuses_an_unknown_sid_and_a_path_shaped_event_id(capsys):
    assert event.run(["--sid", "nope", "--event-id", "e1"]) == 1
    assert "no such session" in capsys.readouterr().err
    assert event.run(["--sid", "nope", "--event-id", "../../etc/passwd"]) == 1
    assert "not a valid event id" in capsys.readouterr().err


def test_event_is_a_registered_subcommand():
    assert "event" in cli.SUBCOMMANDS
