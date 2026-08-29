from __future__ import annotations

import os
import socket
import threading
import time

import pytest

from webcompanion import config as cfgmod
from webcompanion.commands import serve


def test_serve_refuses_to_bind_when_the_port_is_already_held(tmp_path, monkeypatch, capsys):
    """Two processes on one port with SO_REUSEADDR split requests at random,
    and a supervisor that restarts on crash makes that reachable -- so serve
    must refuse rather than bind alongside a stale process."""
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        p = tmp_path / "config.json"
        cfgmod.write(cfgmod.Config(port=port, token="t", bind="127.0.0.1"), p)
        monkeypatch.setattr(cfgmod, "config_path", lambda: p)

        rc = serve.run([])

        assert rc != 0
        err = capsys.readouterr().err
        assert str(port) in err
    finally:
        holder.close()


def test_serve_names_the_port_holder_when_it_can_be_determined(tmp_path, monkeypatch, capsys):
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        p = tmp_path / "config.json"
        cfgmod.write(cfgmod.Config(port=port, token="t", bind="127.0.0.1"), p)
        monkeypatch.setattr(cfgmod, "config_path", lambda: p)
        monkeypatch.setattr(serve, "port_holder", lambda port: ("node", 4821))

        rc = serve.run([])

        assert rc != 0
        err = capsys.readouterr().err
        assert "node" in err and "4821" in err
    finally:
        holder.close()


def test_serve_writes_a_pidfile_while_running_and_removes_it_on_stop(tmp_path, monkeypatch):
    from webcompanion import paths as pathsmod

    p = tmp_path / "config.json"
    cfgmod.write(cfgmod.Config(port=0, token="t", bind="127.0.0.1"), p)
    monkeypatch.setattr(cfgmod, "config_path", lambda: p)
    monkeypatch.setattr(pathsmod, "state_root", lambda: tmp_path / "state")

    pidfile = tmp_path / "state" / "webcompanion.pid"
    stop_event = threading.Event()
    seen: dict = {}

    def _watch():
        for _ in range(500):
            if pidfile.exists():
                seen["pid"] = pidfile.read_text().strip()
                stop_event.set()
                return
            time.sleep(0.01)
        stop_event.set()  # don't hang the test if the pidfile never shows up

    threading.Thread(target=_watch, daemon=True).start()

    rc = serve.run([], _stop_event=stop_event)

    assert rc == 0
    assert seen.get("pid") == str(os.getpid())
    assert not pidfile.exists(), "pidfile must be removed on clean shutdown"


# ── the port check must follow the configured bind's address family ──────

def test_the_port_check_works_on_an_ipv6_loopback_bind():
    """It was AF_INET-only, so a configured `bind = "::1"` could never bind
    here -- _port_is_free returned False forever, serve refused to start,
    and the daemon died in a supervised restart loop. gate.py went to real
    trouble to make IPv6 loopback work as an owner; this made it unusable."""
    import socket

    if not socket.has_ipv6:
        pytest.skip("no IPv6 on this host")
    probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        probe.bind(("::1", 0))
    except OSError:
        pytest.skip("IPv6 loopback is not usable on this host")
    port = probe.getsockname()[1]
    probe.close()

    assert serve._port_is_free("::1", port) is True


def test_the_port_check_still_detects_a_real_listener_on_ipv6():
    import socket

    if not socket.has_ipv6:
        pytest.skip("no IPv6 on this host")
    holder = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        holder.bind(("::1", 0))
    except OSError:
        pytest.skip("IPv6 loopback is not usable on this host")
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        assert serve._port_is_free("::1", port) is False
    finally:
        holder.close()


def test_the_port_in_use_message_points_at_the_config_file(tmp_path, monkeypatch, capsys):
    """"port already held" is the moment a user needs to know where the port
    is configured -- and the config file was documented nowhere at all."""
    import socket

    from webcompanion import config as cfgmod

    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    cfg_file = tmp_path / "config.json"
    cfgmod.write(cfgmod.Config(port=port), cfg_file)
    monkeypatch.setattr(cfgmod, "config_path", lambda: cfg_file)
    try:
        assert serve.run([]) == 1
    finally:
        holder.close()
    err = capsys.readouterr().err
    assert str(cfg_file) in err
    assert '"port"' in err
