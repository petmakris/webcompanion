"""`webcompanion status` -- the service state, its configured port, the
health response, and the number of live sessions."""
from __future__ import annotations

from webcompanion import build
from webcompanion import config as cfgmod
from webcompanion.client import Client, ContractMismatch, DaemonUnreachable, HttpError
from webcompanion.commands._common import report, warn_on_build_skew

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
    # Both builds, always, even when they agree: this is the command someone
    # runs when they are already suspicious, and "the daemon is the code I
    # have" is worth being able to confirm rather than merely assume.
    print(f"build: daemon {health.get('build') or 'unstamped'} / cli {build.build_id()}")
    print(f"sessions: {health.get('sessions', 0)}")
    print(f"uptime: {health.get('uptime', 0):.0f}s")
    warn_on_build_skew(health)
    return 0
