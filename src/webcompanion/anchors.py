"""Code anchors — resolving an item's {file, line, snippet} to real source.

An item may carry a `code` list; each entry names a file and a line in the
session's project, and the daemon reads that file when the item is REQUESTED.
The item never carries code text, which is what makes an anchor cheap enough
to write generously and impossible to leave stale relative to the working
tree. A client edits its repository while a session is open, so an anchor
snapshotted at push time is wrong within a turn.

This is the one thing the daemon knows that looks client-specific. It is here
because it cannot live anywhere else — resolution must happen inside the
request — and because it contains no client concepts: a file, a line, and a
snippet mean the same thing to every client that pushes items.

Two rules shape everything below:

  * `file` is untrusted. Anchors are model-authored, so every path is
    resolved and then required to stay under the session's root. Without that
    check an item could name ../../.ssh/id_rsa and the page would print it to
    anyone holding the read-only share link.

  * A failure is a marker, never an exception. One bad anchor must not blank
    the page.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

# Limits, from docs/superpowers/specs/2026-08-20-code-anchors-design.md.
MAX_ANCHORS = 3        # past three, the item is a tour — use /walkthrough
MAX_WINDOW = 40        # a pane you must scroll has stopped being a glance
CONTEXT_LINES = 2      # dimmed lines either side, so a line has a home
DRIFT_RADIUS = 40      # how far to hunt for a snippet that moved

# The client refetches /raw once a second, per open tab, and the read-only
# share link makes that reachable by anyone holding it. MAX_WINDOW bounds how
# many lines an anchor's payload carries, but nothing bounded how long any one
# line could BE -- a 40-line window over a minified or generated file could
# still be a multi-megabyte JSON body every tick. A source file an anchor
# realistically points at (something a person reads and cites a line of) is
# well under a megabyte; past that, whatever it is isn't the kind of file
# this feature was built for.
MAX_BYTES = 1_000_000   # refuse to read a file bigger than this
MAX_LINE_CHARS = 2000   # truncate any single rendered line past this many chars


def _is_int(v: Any) -> bool:
    """bool is an int subclass; True must not pass as line 1."""
    return isinstance(v, int) and not isinstance(v, bool)


def anchor_problem(a: Any) -> Optional[str]:
    """Return one human-readable problem with this anchor, or None."""
    if not isinstance(a, dict):
        return "must be an object"

    f = a.get("file")
    if not isinstance(f, str) or not f.strip():
        return "file must be a non-empty string"
    if Path(f).is_absolute():
        return "file must be relative to the repository root"

    line = a.get("line")
    if not _is_int(line) or line < 1:
        return "line must be a positive integer"

    end_line = a.get("end_line")
    if end_line is not None:
        if not _is_int(end_line) or end_line < 1:
            return "end_line must be a positive integer"
        if end_line < line:
            return "end_line must not precede line"

    snippet = a.get("snippet")
    if not isinstance(snippet, str) or not snippet.strip():
        return "snippet must be non-empty (it is what survives the file moving)"

    # No `note` check: the field it validated is gone. An anchor written
    # before the removal may still carry one, and this function is a list of
    # checks rather than a reject-unknown-keys gate, so such an anchor keeps
    # passing -- it simply stops being rendered.
    return None


def resolve_all(body: dict, root: Path) -> list:
    """Resolve up to MAX_ANCHORS code anchors declared by this item body.

    Past three anchors an item has stopped being a glance and become a tour;
    the cap is a product decision, not a performance one.
    """
    code = body.get("code") if isinstance(body, dict) else None
    if not isinstance(code, list):
        return []
    return [resolve_anchor(a, root) for a in code[:MAX_ANCHORS]
            if isinstance(a, dict)]


def _fail(a: dict, status: str, message: str) -> dict:
    """A failing anchor still names itself, so the pane can say what is lost."""
    out = {
        "file": a.get("file") if isinstance(a.get("file"), str) else "",
        "line": a.get("line") if _is_int(a.get("line")) else 0,
        "status": status,
        "message": message,
    }
    return out


def _read_lines(path: Path) -> list:
    """File as a list of lines, newline stripped. Undecodable bytes replaced
    rather than raising: a pane showing a mojibake line is still better than
    a block that failed to render."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")
    # A trailing newline yields a final empty element that is not a line.
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def resolve_anchor(a: dict, root) -> dict:
    """Read the source an anchor names. Never raises; failures are statuses."""
    problem = anchor_problem(a)
    if problem:
        return _fail(a if isinstance(a, dict) else {}, "refused", problem)

    rel = a["file"]
    try:
        root_real = Path(root).resolve()
        target = (root_real / rel).resolve()
    except (OSError, ValueError, TypeError) as e:
        # Bounded to the exception's class name, not its stringified form:
        # a TypeError's message can carry a repr of whatever `root` was, and
        # an OSError's can carry the absolute path -- see the read-failure
        # branch below for why that must never reach this payload.
        return _fail(a, "refused", "%s: path could not be resolved (%s)"
                     % (rel, e.__class__.__name__))

    # resolve() follows symlinks BEFORE this test, which is the point: a link
    # inside the repo pointing out of it must not smuggle a file through.
    if not target.is_relative_to(root_real):
        return _fail(a, "refused", "%s: resolves outside the workspace" % rel)
    if not target.is_file():
        return _fail(a, "missing", "%s: no such file in the workspace" % rel)

    try:
        size = target.stat().st_size
    except OSError as e:
        # Same rule as the read-failure branch below: report the relative
        # path the model already knows, never the resolved absolute one.
        detail = e.strerror or e.__class__.__name__
        if e.errno is not None:
            detail = "errno %d: %s" % (e.errno, detail)
        return _fail(a, "missing", "%s: could not be read (%s)" % (rel, detail))
    if size > MAX_BYTES:
        return _fail(a, "missing",
                     "%s: too large to anchor (%d bytes)" % (rel, size))

    try:
        lines = _read_lines(target)
    except OSError as e:
        # e's stringified form embeds the resolved *absolute* path (target),
        # which this payload may reach via a read-only share link. Report
        # only what the model already knows -- the relative path -- plus the
        # OS-level reason, never the raw exception text. No str(e) fallback:
        # a plain OSError.__str__() embeds .filename (the absolute path) when
        # set, which would reopen this exact leak.
        detail = e.strerror or e.__class__.__name__
        if e.errno is not None:
            detail = "errno %d: %s" % (e.errno, detail)
        return _fail(a, "missing", "%s: could not be read (%s)" % (rel, detail))

    return _build(a, lines)


def _build(a: dict, lines: list) -> dict:
    """Locate the anchor in `lines` and lay out the window around it."""
    authored = a["line"]
    actual = _locate(lines, a["snippet"], authored)
    if actual is None:
        return _fail(
            a, "stale",
            "%s: the anchored line is no longer at or near line %d "
            "(looked for %r)" % (a["file"], authored, a["snippet"].strip()),
        )

    shift = actual - authored
    end = (a.get("end_line") or authored) + shift
    end = min(end, len(lines))

    truncated = 0
    span = end - actual + 1
    if span > MAX_WINDOW:
        truncated = span - MAX_WINDOW
        end = actual + MAX_WINDOW - 1

    first = max(1, actual - CONTEXT_LINES)
    last = min(len(lines), end + CONTEXT_LINES)

    out_lines = []
    for n in range(first, last + 1):
        if n == actual:
            role = "anchor"
        elif actual <= n <= end:
            role = "window"
        else:
            role = "context"
        text = lines[n - 1]
        if len(text) > MAX_LINE_CHARS:
            text = text[:MAX_LINE_CHARS] + " … [line truncated]"
        out_lines.append({"n": n, "text": text, "role": role})

    out = {
        "file": a["file"],
        "line": authored,
        "actual_line": actual,
        "status": "moved" if shift else "ok",
        "lines": out_lines,
    }
    if shift:
        out["message"] = ("moved: authored at line %d, now at line %d"
                          % (authored, actual))
    if truncated:
        out["truncated"] = truncated
    return out


def _locate(lines: list, snippet: str, authored: int):
    """Line number where `snippet` really is, or None.

    Compared stripped, so re-indenting a line is not treated as drift — it is
    the same line. On several matches take the one nearest the authored line;
    on a tie, the earlier one.
    """
    want = snippet.strip()
    if 1 <= authored <= len(lines) and lines[authored - 1].strip() == want:
        return authored
    # Bounded to the drift window directly, rather than scanning every line
    # in the file and filtering by distance -- DRIFT_RADIUS already promises
    # "40 lines either way"; a large file (or the pathological one MAX_BYTES
    # doesn't catch because it's mostly long lines) should not pay for a scan
    # past what that promise covers.
    lo = max(1, authored - DRIFT_RADIUS)
    hi = min(len(lines), authored + DRIFT_RADIUS)
    candidates = [
        n for n in range(lo, hi + 1)
        if lines[n - 1].strip() == want
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda n: (abs(n - authored), n))
