"""webcompanion — a local always-on companion server.

The package is deliberately dependency-free. Everything it needs is in the
standard library, which is what lets the service ship as a zipapp run by the
system python instead of a virtualenv that dangles when that python is
replaced.
"""
from __future__ import annotations

__version__ = "0.1.0"

# The HTTP contract version. Bumped ONLY on a breaking change to routes or
# payload shapes. Clients send it in X-WebCompanion-Contract; a mismatch is
# answered 426 rather than being silently tolerated, because the four
# artifacts that speak this contract (this package, two Claude Code plugins,
# and a hand-downloaded IntelliJ zip) update on different schedules.
CONTRACT = 1
