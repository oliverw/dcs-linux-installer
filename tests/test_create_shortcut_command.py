"""`dcs-linux create-shortcut` through its public command-line interface."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from typer.testing import CliRunner

from dcs_linux import cli
from dcs_linux.installs import Launcher
from tests.environments import OWN_INSTALL, healthy_environment
from tests.fakes import FakeSystem, FakeWriter

runner = CliRunner()


def test_create_shortcut_creates_a_launcher_for_the_targeted_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = FakeSystem(env={"XDG_CURRENT_DESKTOP": "KDE"})
    writer = FakeWriter(system)
    monkeypatch.setattr(cli, "RealSystem", lambda: system)
    monkeypatch.setattr(cli, "RealWriter", lambda: writer)
    monkeypatch.setattr(
        cli,
        "probe",
        lambda system, identifier=None: healthy_environment(),  # noqa: ARG005
    )

    result = runner.invoke(cli.app, ["create-shortcut"])

    assert result.exit_code == 0, result.output
    launcher = (
        system.home()
        / ".local/share/applications"
        / (f"dcs-linux-{OWN_INSTALL.install_id}.desktop")
    )
    assert "desktop shortcut created" in result.output
    assert f"Exec=dcs-linux launch --install {OWN_INSTALL.install_id}" in (
        system.read_text(launcher) or ""
    )


def test_create_shortcut_refuses_an_install_not_prepared_by_this_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = FakeSystem(env={"XDG_CURRENT_DESKTOP": "KDE"})
    writer = FakeWriter(system)
    unprepared = replace(OWN_INSTALL, launcher=Launcher.STEAM, prefix=None, runtime=None)
    environment = healthy_environment(installs=(unprepared,), targeted=unprepared)
    monkeypatch.setattr(cli, "RealSystem", lambda: system)
    monkeypatch.setattr(cli, "RealWriter", lambda: writer)
    monkeypatch.setattr(
        cli,
        "probe",
        lambda system, identifier=None: environment,  # noqa: ARG005
    )

    result = runner.invoke(cli.app, ["create-shortcut"])

    assert result.exit_code == 1
    assert "not prepared by this tool" in result.output
    assert not [path for path in system.files if path.suffix == ".desktop"]


def test_create_shortcut_forwards_the_selector_and_reports_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = FakeSystem(env={"XDG_CURRENT_DESKTOP": "GNOME"})
    writer = FakeWriter(system)
    identifiers: list[str | None] = []

    def probe(system: object, identifier: str | None = None) -> object:
        identifiers.append(identifier)
        return healthy_environment()

    monkeypatch.setattr(cli, "RealSystem", lambda: system)
    monkeypatch.setattr(cli, "RealWriter", lambda: writer)
    monkeypatch.setattr(cli, "probe", probe)

    result = runner.invoke(
        cli.app,
        ["--json", "create-shortcut", "--install", OWN_INSTALL.install_id],
    )

    assert result.exit_code == 0, result.output
    assert identifiers == [OWN_INSTALL.install_id]
    assert json.loads(result.stdout)["shortcut"]["status"] == "created"


def test_create_shortcut_reports_an_unsupported_desktop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = FakeSystem(env={"XDG_CURRENT_DESKTOP": "sway"})
    monkeypatch.setattr(cli, "RealSystem", lambda: system)
    monkeypatch.setattr(cli, "RealWriter", lambda: FakeWriter(system))
    monkeypatch.setattr(
        cli,
        "probe",
        lambda system, identifier=None: healthy_environment(),  # noqa: ARG005
    )

    result = runner.invoke(cli.app, ["create-shortcut"])

    assert result.exit_code == 1
    assert "current desktop is not KDE or GNOME" in result.output


def test_create_shortcut_is_idempotent_through_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = FakeSystem(env={"XDG_CURRENT_DESKTOP": "KDE"})
    writer = FakeWriter(system)
    monkeypatch.setattr(cli, "RealSystem", lambda: system)
    monkeypatch.setattr(cli, "RealWriter", lambda: writer)
    monkeypatch.setattr(
        cli,
        "probe",
        lambda system, identifier=None: healthy_environment(),  # noqa: ARG005
    )

    first = runner.invoke(cli.app, ["create-shortcut"])
    second = runner.invoke(cli.app, ["create-shortcut"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "already exists" in second.output
    assert len([path for path in system.files if path.suffix == ".desktop"]) == 1
