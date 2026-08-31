from __future__ import annotations

import json

from webcompanion.commands import push, reply
from webcompanion.commands._common import client_from_config


def test_reply_appends_to_the_thread(wired, tmp_path, capsys):
    doc = tmp_path / "doc.json"
    doc.write_text(json.dumps({"items": {}}))
    push.run(["--kind", "show-diff", "--cwd", str(tmp_path), "--title", "T",
              "--items", str(doc), "--eval"])
    sid = [l for l in capsys.readouterr().out.splitlines()
           if l.startswith("WC_SID=")][0][len("WC_SID="):]

    answer = tmp_path / "answer.md"
    answer.write_text("It's a guard clause for the empty-list case.")
    rc = reply.run(["--sid", sid, "--anchor", "a.py:R:1", "--text", str(answer)])
    assert rc == 0

    thread = client_from_config().get_thread(sid, "a.py:R:1")
    assert thread["messages"][-1]["text"] == "It's a guard clause for the empty-list case."
    assert thread["messages"][-1]["role"] == "agent"


def test_reply_reports_a_missing_file(tmp_path, capsys):
    rc = reply.run(["--sid", "whatever", "--anchor", "a.py:R:1",
                     "--text", str(tmp_path / "missing.md")])
    assert rc == 1
    assert "could not read" in capsys.readouterr().err
