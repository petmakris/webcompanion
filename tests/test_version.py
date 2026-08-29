from __future__ import annotations

import subprocess
import sys

import webcompanion
from webcompanion.cli import main


def test_package_reports_a_version_and_contract():
    assert isinstance(webcompanion.__version__, str)
    assert webcompanion.__version__.count(".") == 2
    assert webcompanion.CONTRACT == 1


def test_cli_version_flag_prints_version_and_contract(capsys):
    rc = main(["--version"])
    out = capsys.readouterr().out
    assert rc == 0
    assert webcompanion.__version__ in out
    assert "contract 1" in out


def test_cli_unknown_subcommand_is_an_error():
    assert main(["nonesuch"]) == 2
