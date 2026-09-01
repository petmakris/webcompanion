"""Packaged resources must be reachable the way the SERVICE reaches them.

The installed service is a zipapp, so `importlib.resources.files()` hands it
a `zipfile.Path`, not a `pathlib.Path`. The two disagree on `joinpath`:
multi-argument joinpath arrived for `zipfile.Path` only in 3.12, and the
launchd job runs on whatever `/usr/bin/python3` is — 3.9 on a stock macOS.

That combination is invisible to an ordinary test run: a source checkout
gives `pathlib.Path`, which has accepted multiple segments since 3.4, so
`joinpath("static", "shell.html")` passes every test and then 500s the shell
page once installed. It did exactly that, and the traceback only surfaced
because a restart happened to be watched.

These tests assert against the zipfile.Path shape directly, so the failure
mode is reproduced on the machine writing the code rather than the one
running it.
"""
from __future__ import annotations

import io
import zipfile

import pytest


def _zip_path(*names: str) -> zipfile.Path:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in names:
            z.writestr(n, "x")
    return zipfile.Path(zipfile.ZipFile(buf))


def test_a_zipfile_path_is_reached_by_chaining_not_by_multiple_segments():
    """Pins the property the source has to hold, on the type the service sees."""
    root = _zip_path("webcompanion/static/shell.html")
    assert root.joinpath("webcompanion").joinpath("static").joinpath(
        "shell.html").read_text() == "x"


@pytest.mark.parametrize("module,helper", [
    ("webcompanion.server", "_static_file"),
    ("webcompanion.commands.install_service", "_read_template"),
])
def test_no_resource_helper_uses_multi_segment_joinpath(module, helper):
    """The static check, which is the one that fails the day someone
    "tidies" a chain back into a single call."""
    import importlib
    import inspect
    import re

    mod = importlib.import_module(module)
    fn = getattr(mod, helper, None)
    if fn is None:
        pytest.skip(f"{module}.{helper} no longer exists")
    # Comments are stripped first: the fix for this bug is explained in a
    # comment that necessarily quotes the broken form, and a scanner that
    # cannot tell the two apart fails on its own documentation.
    src = "\n".join(
        line.split("#", 1)[0] for line in inspect.getsource(fn).splitlines())
    offending = re.search(r"joinpath\(\s*[^)]*,\s*[^)]*\)", src)
    assert not offending, (
        f"{module}.{helper} calls joinpath with more than one segment: "
        f"{offending.group(0)!r}. zipfile.Path (which files() returns inside "
        f"the installed zipapp) only accepts one before 3.12.")
