from __future__ import annotations

from webcompanion.commands._common import client_from_config


def test_get_item_and_list_items_round_trip(wired, tmp_path):
    client = client_from_config()
    created = client.create("show-diff", str(tmp_path), title="T")
    client.put_items(created["sid"], {"a.py:R:1": {"checkout": str(tmp_path)}})

    items = client.list_items(created["sid"])
    assert items["a.py:R:1"]["body"] == {"checkout": str(tmp_path)}

    one = client.get_item(created["sid"], "a.py:R:1")
    assert one["body"] == {"checkout": str(tmp_path)}
    assert one["version"] == 1


def test_append_thread_then_get_thread_round_trip(wired, tmp_path):
    client = client_from_config()
    created = client.create("show-diff", str(tmp_path), title="T")

    appended = client.append_thread(created["sid"], "a.py:R:1", "why is this here?",
                                     role="user")
    assert appended["version"] == 1

    thread = client.get_thread(created["sid"], "a.py:R:1")
    assert thread["messages"][0]["text"] == "why is this here?"
    assert thread["messages"][0]["role"] == "user"


def test_list_sessions_filters_by_cwd_and_kind(wired, tmp_path):
    client = client_from_config()
    created = client.create("show-diff", str(tmp_path), title="T")

    rows = client.list_sessions(str(tmp_path), kind="show-diff")
    assert any(r["sid"] == created["sid"] for r in rows)

    other = client.list_sessions(str(tmp_path), kind="annotate")
    assert not any(r["sid"] == created["sid"] for r in other)
