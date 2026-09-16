"""Shared plumbing for every command module: building a `Client` from the
one configuration file, and turning a failed call into one of the three
diagnostic messages a user needs.

The daemon is never started from here or anywhere else in this package.
Requirements are the user's to install; ours to state clearly.
"""
from __future__ import annotations

import sys

from webcompanion import build
from webcompanion import config as cfgmod
from webcompanion.client import (Client, ContractMismatch, DaemonNotConfigured,
                                 DaemonUnreachable, HttpError)

REPORTABLE = (DaemonNotConfigured, DaemonUnreachable, ContractMismatch, HttpError)


def warn_on_build_skew(health: dict) -> None:
    """Say so, on stderr, when the daemon is not running this CLI's code.

    A warning rather than a refusal: the daemon is answering and most routes
    predate the skew, so failing here would break working commands to report
    a problem they do not have. What it buys is that the one command that
    IS affected fails with its cause already printed above it, instead of a
    bare 404 that reads as a bug in the route.
    """
    message = build.skew_message(health.get("build"))
    if message:
        print(message, file=sys.stderr)


def client_from_config() -> Client:
    cfg = cfgmod.load()
    return Client(f"http://{cfg.bind}:{cfg.port}", cfg.token)


def report(exc) -> int:
    """One message per distinguishable failure. Requirements are the user's
    to install; ours to state clearly. Never install anything."""
    if isinstance(exc, DaemonNotConfigured):
        print("webcompanion: the companion service is not installed.\n"
              "\n"
              "  pipx install webcompanion && webcompanion install-service\n",
              file=sys.stderr)
    elif isinstance(exc, DaemonUnreachable) and not cfgmod.config_path().exists():
        print("webcompanion: the companion service is not installed.\n"
              "\n"
              "  pipx install webcompanion && webcompanion install-service\n",
              file=sys.stderr)
    elif isinstance(exc, DaemonUnreachable):
        print(f"webcompanion: the service is installed but not answering on "
              f"{exc.url}.\n"
              f"\n"
              f"  webcompanion status\n"
              f"  launchctl kickstart -k gui/$UID/dev.webcompanion   # macOS\n"
              f"  systemctl --user restart webcompanion              # Linux\n"
              f"\n"
              f"Log: {exc.log_path}\n", file=sys.stderr)
    elif isinstance(exc, ContractMismatch):
        print(f"webcompanion: {exc}\n", file=sys.stderr)
    else:
        print(f"webcompanion: {exc}\n", file=sys.stderr)
    return 1


def preflight(client: Client):
    """None if the daemon is reachable and speaks our contract; otherwise a
    diagnostic has already been printed and this returns the exit code to
    use. Every command calls this before touching a session, so a down or
    mismatched daemon is reported the same way no matter which subcommand
    found out.

    The configuration check comes first and never opens a socket. Without
    it a machine with no config still gets a usable `Client` -- pointed at
    the default host and port by `config.load()`'s fallback -- and quietly
    reads and writes whatever daemon is listening there, which is how a
    test suite that meant to assert "no daemon" wrote real sessions into a
    real one instead."""
    if not cfgmod.config_path().exists():
        return report(DaemonNotConfigured(cfgmod.config_path()))
    try:
        health = client.health()
    except REPORTABLE as e:
        return report(e)
    # The health response was already fetched and thrown away here, so the
    # skew check costs no extra request -- which is why it belongs in the
    # one function every command already calls rather than in each of them.
    warn_on_build_skew(health if isinstance(health, dict) else {})
    return None
