"""`webcompanion status` -- the service state, its configured port, the
health response, and the number of live sessions."""
from __future__ import annotations

from webcompanion import config as cfgmod
from webcompanion.client import Client, ContractMismatch, DaemonUnreachable, HttpError
from webcompanion.commands._common import report

REPORTABLE = (DaemonUnreachable, ContractMismatch, HttpError)


def run(argv: list[str]) -> int:
    cfg = cfgmod.load()
    print(f"config: {cfgmod.config_path()}")
    print(f"port: {cfg.port} (bind {cfg.bind})")

    client = Client(f"http://{cfg.bind}:{cfg.port}", cfg.token)
    try:
        health = client.health()
    except REPORTABLE as e:
        print("status: not running")
        return report(e)

    print(f"status: running -- {health.get('banner', '')}")
    print(f"sessions: {health.get('sessions', 0)}")
    print(f"uptime: {health.get('uptime', 0):.0f}s")
    return 0
