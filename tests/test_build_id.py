"""A running daemon must be able to say WHICH build it is, not just which version.

The package reaches a machine two ways with different update semantics -- an
editable install that tracks a working tree, and a frozen zipapp that only
changes when `install-service` rebuilds it -- and `__version__` cannot tell
them apart. These tests pin the behaviour that makes the resulting skew
visible instead of surfacing as a bare 404 from a route the daemon has never
heard of.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from webcompanion import build


def _pkg(tmp_path: Path, **files: str) -> Path:
    pkg = tmp_path / "webcompanion"
    pkg.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (pkg / name).write_text(text)
    return pkg


def test_the_same_sources_hash_the_same_way(tmp_path):
    a = _pkg(tmp_path / "a", **{"server.py": "x = 1\n", "cli.py": "y = 2\n"})
    b = _pkg(tmp_path / "b", **{"cli.py": "y = 2\n", "server.py": "x = 1\n"})

    assert build.compute(a) == build.compute(b)


def test_changing_one_source_line_changes_the_build_id(tmp_path):
    pkg = _pkg(tmp_path, **{"server.py": "x = 1\n"})
    before = build.compute(pkg)

    (pkg / "server.py").write_text("x = 2\n")

    assert build.compute(pkg) != before


def test_a_new_route_changes_the_build_id(tmp_path):
    """The exact failure this exists to catch: a route added to one copy and
    not the other must make the two copies announce different builds."""
    old = _pkg(tmp_path / "old", **{"server.py": "def do_DELETE(self):\n    pass\n"})
    new = _pkg(tmp_path / "new", **{
        "server.py": "def do_DELETE(self):\n    return self._forget()\n"})

    assert build.compute(old) != build.compute(new)


def test_non_python_files_do_not_move_the_build_id(tmp_path):
    """Static assets churn constantly and are served, not executed. Hashing
    them would cry skew on every unrelated edit and train the user to ignore
    the warning that matters."""
    pkg = _pkg(tmp_path, **{"server.py": "x = 1\n"})
    before = build.compute(pkg)

    (pkg / "notes.txt").write_text("anything at all")

    assert build.compute(pkg) == before


def test_a_baked_stamp_wins_over_recomputing(tmp_path):
    """A zipapp carries the id it was built with. Recomputing inside one
    would work, but it would also mean a corrupt archive quietly reports a
    plausible id instead of the one it shipped as."""
    pkg = _pkg(tmp_path, **{"server.py": "x = 1\n"})
    (pkg / build.STAMP_NAME).write_text("stamped-id\n")

    assert build.read_stamp(pkg) == "stamped-id"
    assert build.resolve(pkg) == "stamped-id"
    assert build.is_frozen(pkg) is True


def test_without_a_stamp_the_id_is_computed_from_source(tmp_path):
    pkg = _pkg(tmp_path, **{"server.py": "x = 1\n"})

    assert build.read_stamp(pkg) is None
    assert build.resolve(pkg) == build.compute(pkg)
    assert build.is_frozen(pkg) is False


def test_build_id_of_the_installed_package_is_a_short_hex_string():
    got = build.build_id()

    assert isinstance(got, str)
    assert 8 <= len(got) <= 64
    assert all(c in "0123456789abcdef" for c in got)


def test_the_zipapp_carries_a_build_stamp(tmp_path):
    """Without this the daemon has nothing to report, and the whole chain
    -- health, status, preflight -- has nothing to compare."""
    from webcompanion.commands import install_service as svc

    pyz = svc.build_zipapp(tmp_path / "webcompanion.pyz")

    with zipfile.ZipFile(pyz) as z:
        names = z.namelist()
        assert f"webcompanion/{build.STAMP_NAME}" in names
        stamped = z.read(f"webcompanion/{build.STAMP_NAME}").decode().strip()

    assert stamped == build.build_id()


def test_the_build_id_is_readable_from_inside_the_zipapp(tmp_path):
    """The one that matters, and the one the archive-contents test missed.

    The daemon does not read the zipapp, it RUNS from inside it, under the
    system python rather than the one the tests use. `as_file()` on a
    directory inside a zip yields an EMPTY temporary directory on Python
    3.9 -- it materialises the directory, not its contents -- so a build id
    computed by walking that path hashes nothing at all and every daemon
    reports the digest of the empty string. Asserting only that the stamp is
    present in the archive cannot see this; the id has to be read back by a
    process actually running from it.
    """
    import subprocess
    import sys

    from webcompanion.commands import install_service as svc

    pyz = svc.build_zipapp(tmp_path / "webcompanion.pyz")
    with zipfile.ZipFile(pyz) as z:
        stamped = z.read(f"webcompanion/{build.STAMP_NAME}").decode().strip()

    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, sys.argv[1]);"
         "from webcompanion import build; print(build.build_id())",
         str(pyz)],
        capture_output=True, text=True, timeout=30,
    )

    assert out.returncode == 0, out.stderr
    reported = out.stdout.strip()
    assert reported == stamped

    # An empty tree hashes to the digest of nothing. That value is what a
    # directory-walking build_id() produces inside a zip, so it must never be
    # what a daemon announces.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert reported != build.compute(empty)


def test_a_stamped_copy_is_never_hashed(monkeypatch):
    """The deterministic half of the test above, independent of interpreter.

    The zipapp test only reproduces the failure on a python whose
    `as_file()` cannot materialise a zip directory, which is the one the
    service runs under and not necessarily the one the suite runs under.
    This pins the invariant directly: when a stamp can be read, nothing
    walks a directory -- so the bug cannot come back on any python.
    """
    def explode(_pkg_dir):
        raise AssertionError("build_id() hashed a directory despite a readable stamp")

    monkeypatch.setattr(build, "_stamp_from_resources", lambda: "deadbeefdeadbeef")
    monkeypatch.setattr(build, "compute", explode)

    assert build.build_id() == "deadbeefdeadbeef"


def test_an_unstamped_source_install_still_reports_a_real_id(monkeypatch):
    monkeypatch.setattr(build, "_stamp_from_resources", lambda: None)

    got = build.build_id()

    assert len(got) == 16
    assert got != build.compute(Path(__file__).parent / "does-not-exist")


def test_health_reports_the_build(wired):
    from webcompanion.commands._common import client_from_config

    health = client_from_config().health()

    assert health["build"] == build.build_id()


def test_status_prints_the_build(wired, capsys):
    from webcompanion.commands import status

    assert status.run([]) == 0

    assert "build:" in capsys.readouterr().out.lower()


def test_preflight_warns_when_the_daemon_build_differs(wired, monkeypatch, capsys):
    """The bug this whole module exists for: the daemon is serving older code
    than the CLI is calling, and says nothing about it."""
    from webcompanion.commands import _common

    monkeypatch.setattr(build, "build_id", lambda: "0000000000000000")

    assert _common.preflight(_common.client_from_config()) is None

    err = capsys.readouterr().err.lower()
    assert "older" in err or "differ" in err
    assert "install-service" in err


def test_preflight_warns_when_the_daemon_reports_no_build_at_all(wired, monkeypatch, capsys):
    """A daemon from before build stamps existed. It cannot tell us what it is
    running, which is itself the answer: it predates this, so it is stale."""
    from webcompanion.commands import _common
    from webcompanion.client import Client

    real = Client.health

    def health_without_build(self):
        out = dict(real(self))
        out.pop("build", None)
        return out

    monkeypatch.setattr(Client, "health", health_without_build)

    assert _common.preflight(_common.client_from_config()) is None

    err = capsys.readouterr().err.lower()
    assert "install-service" in err


def test_preflight_is_silent_when_the_builds_agree(wired, capsys):
    from webcompanion.commands import _common

    assert _common.preflight(_common.client_from_config()) is None

    assert capsys.readouterr().err == ""
