"""The CLI subcommands `webcompanion` dispatches to.

Every module here exposes `run(argv: list[str]) -> int`, imported lazily by
`webcompanion.cli._dispatch`. None of them ever start the daemon -- see each
module's docstring.
"""
from __future__ import annotations
