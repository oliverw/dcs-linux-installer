"""`dcs-linux launch` through its public command-line interface."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dcs_linux import cli
from dcs_linux.launch import LaunchResult
from tests.environments import OWN_INSTALL, bare_environment, healthy_environment

runner = CliRunner()


def test_a_prepared_install_launches_and_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "probe", lambda system, identifier=None: healthy_environment())
    monkeypatch.setattr(
        cli,
        "launch_dcs",
        lambda *args: LaunchResult(
            started=True, returncode=3, detail="DCS closed with exit code 3"
        ),
    )

    result = runner.invoke(cli.app, ["launch"])

    assert result.exit_code == 0, result.output
    assert "DCS closed with exit code 3" in result.stdout
    assert "working" not in result.stdout


def test_json_reports_start_and_exit_without_a_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "probe", lambda system, identifier=None: healthy_environment())
    monkeypatch.setattr(
        cli,
        "launch_dcs",
        lambda *args: LaunchResult(
            started=True, returncode=3, detail="DCS closed with exit code 3"
        ),
    )

    result = runner.invoke(cli.app, ["--json", "launch"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "command": "launch",
        "ok": True,
        "started": True,
        "exit_code": 3,
        "detail": "DCS closed with exit code 3",
    }


def test_a_start_failure_is_clear_and_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "probe", lambda system, identifier=None: healthy_environment())
    monkeypatch.setattr(
        cli,
        "launch_dcs",
        lambda *args: LaunchResult(
            started=False,
            returncode=None,
            detail="DCS did not start: umu-run could not be executed",
        ),
    )

    result = runner.invoke(cli.app, ["launch"])

    assert result.exit_code == 1
    assert "DCS did not start" in result.stdout
    assert "umu-run could not be executed" in result.stdout


def test_the_existing_install_selector_is_passed_to_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifiers: list[str | None] = []

    def probe(system: object, identifier: str | None = None) -> object:
        identifiers.append(identifier)
        return healthy_environment()

    monkeypatch.setattr(cli, "probe", probe)
    monkeypatch.setattr(
        cli,
        "launch_dcs",
        lambda *args: LaunchResult(True, 0, "DCS closed with exit code 0"),
    )

    result = runner.invoke(cli.app, ["launch", "--install", "7976"])

    assert result.exit_code == 0
    assert identifiers == ["7976"]


def test_with_no_install_launch_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "probe", lambda system, identifier=None: bare_environment())

    result = runner.invoke(cli.app, ["launch"])

    assert result.exit_code == 2
    assert "no DCS install found" in result.output


def test_multiple_installs_without_a_selector_are_a_usage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = replace(OWN_INSTALL, game=Path("/mnt/other/DCS World"))
    environment = healthy_environment(installs=(OWN_INSTALL, other), targeted=None)
    monkeypatch.setattr(cli, "probe", lambda system, identifier=None: environment)

    result = runner.invoke(cli.app, ["launch"])

    assert result.exit_code == 2
    assert "2 installs found" in result.output
    assert "--install ID" in result.output
