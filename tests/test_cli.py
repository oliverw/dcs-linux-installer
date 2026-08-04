import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dcs_linux import __version__
from dcs_linux.cli import app

runner = CliRunner()

SUBCOMMANDS = ["check", "install", "patch", "verify", "report"]

# Every one of them is now implemented, so this file is down to the surface the
# whole CLI shares: the flags, the help, and the installed script. What each
# command does is tested in test_<name>_command.py.


def test_version_flag_prints_version_and_exits_zero() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_help_flag_exits_zero_and_lists_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in SUBCOMMANDS:
        assert name in result.stdout


def test_bare_invocation_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in result.stdout
    for name in SUBCOMMANDS:
        assert name in result.stdout


@pytest.mark.parametrize("name", SUBCOMMANDS)
def test_every_subcommand_documents_itself(name: str) -> None:
    result = runner.invoke(app, [name, "--help"])
    assert result.exit_code == 0
    assert "Usage" in result.stdout


def test_no_color_flag_is_accepted_and_strips_ansi() -> None:
    result = runner.invoke(app, ["--no-color", "report"])
    assert result.exit_code == 0
    assert "\x1b[" not in result.stdout


def test_output_has_no_ansi_codes_off_tty_even_without_no_color_flag() -> None:
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0
    assert "\x1b[" not in result.stdout


def test_console_script_is_installed_and_runs() -> None:
    script = Path(sys.executable).with_name("dcs-linux")
    completed = subprocess.run(
        [str(script), "--version"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == __version__
