"""`dcs-linux shortcut` through its public command-line interface."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from typer.testing import CliRunner

from dcs_linux import cli
from dcs_linux.prefix import (
    GAMEID,
    GE_PROTON_VERSION,
    LAUNCH_ENVIRONMENT,
    UMU_VERSION,
    WINETRICKS_VERBS,
)
from tests.environments import LAYOUT, OWN_INSTALL, healthy_environment
from tests.fakes import FakeFileFetcher, FakeSystem, FakeWriter

runner = CliRunner()
ICON = b"\xff\xd8\xffDCS icon"


def use_icon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "RealFileFetcher", lambda: FakeFileFetcher(data=ICON))


def prepared_system(desktop: str) -> FakeSystem:
    manifest = {
        "umu_version": UMU_VERSION,
        "ge_proton": GE_PROTON_VERSION,
        "gameid": GAMEID,
        "verbs": list(WINETRICKS_VERBS),
        "environment": LAUNCH_ENVIRONMENT,
        "prefix": str(LAYOUT.prefix),
        "game": str(LAYOUT.game),
        "saved_games": str(LAYOUT.saved_games),
    }
    return FakeSystem(
        env={"XDG_CURRENT_DESKTOP": desktop},
        files={
            str(LAYOUT.prefix / "system.reg"): "WINE REGISTRY Version 2",
            str(LAYOUT.manifest): json.dumps(manifest),
        },
    )


def test_shortcut_creates_a_launcher_for_the_targeted_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = prepared_system("KDE")
    writer = FakeWriter(system)
    monkeypatch.setattr(cli, "RealSystem", lambda: system)
    monkeypatch.setattr(cli, "RealWriter", lambda: writer)
    use_icon(monkeypatch)
    monkeypatch.setattr(
        cli,
        "probe",
        lambda system, identifier=None: healthy_environment(),  # noqa: ARG005
    )

    result = runner.invoke(cli.app, ["shortcut"])

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


def test_shortcut_refuses_an_install_not_prepared_by_this_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = FakeSystem(env={"XDG_CURRENT_DESKTOP": "KDE"})
    writer = FakeWriter(system)
    unprepared = replace(OWN_INSTALL, runtime=None)
    environment = healthy_environment(installs=(unprepared,), targeted=unprepared)
    monkeypatch.setattr(cli, "RealSystem", lambda: system)
    monkeypatch.setattr(cli, "RealWriter", lambda: writer)
    use_icon(monkeypatch)
    monkeypatch.setattr(
        cli,
        "probe",
        lambda system, identifier=None: environment,  # noqa: ARG005
    )

    result = runner.invoke(cli.app, ["shortcut"])

    assert result.exit_code == 1
    assert "not prepared by this tool" in result.output
    assert not [path for path in system.files if path.suffix == ".desktop"]


def test_shortcut_forwards_the_selector_and_reports_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = prepared_system("GNOME")
    writer = FakeWriter(system)
    identifiers: list[str | None] = []

    def probe(system: object, identifier: str | None = None) -> object:
        identifiers.append(identifier)
        return healthy_environment()

    monkeypatch.setattr(cli, "RealSystem", lambda: system)
    monkeypatch.setattr(cli, "RealWriter", lambda: writer)
    use_icon(monkeypatch)
    monkeypatch.setattr(cli, "probe", probe)

    result = runner.invoke(
        cli.app,
        ["--json", "shortcut", "--install", OWN_INSTALL.install_id],
    )

    assert result.exit_code == 0, result.output
    assert identifiers == [OWN_INSTALL.install_id]
    payload = json.loads(result.stdout)
    assert payload["command"] == "shortcut"
    assert payload["shortcut"]["status"] == "created"


def test_shortcut_reports_an_unsupported_desktop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = prepared_system("sway")
    monkeypatch.setattr(cli, "RealSystem", lambda: system)
    monkeypatch.setattr(cli, "RealWriter", lambda: FakeWriter(system))
    monkeypatch.setattr(
        cli,
        "probe",
        lambda system, identifier=None: healthy_environment(),  # noqa: ARG005
    )

    result = runner.invoke(cli.app, ["shortcut"])

    assert result.exit_code == 1
    assert "current desktop is not KDE or GNOME" in result.output


def test_shortcut_is_idempotent_through_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = prepared_system("KDE")
    writer = FakeWriter(system)
    monkeypatch.setattr(cli, "RealSystem", lambda: system)
    monkeypatch.setattr(cli, "RealWriter", lambda: writer)
    use_icon(monkeypatch)
    monkeypatch.setattr(
        cli,
        "probe",
        lambda system, identifier=None: healthy_environment(),  # noqa: ARG005
    )

    first = runner.invoke(cli.app, ["shortcut"])
    second = runner.invoke(cli.app, ["shortcut"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "already exists" in second.output
    assert len([path for path in system.files if path.suffix == ".desktop"]) == 1


def test_json_reports_an_icon_download_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    system = prepared_system("KDE")
    monkeypatch.setattr(cli, "RealSystem", lambda: system)
    monkeypatch.setattr(cli, "RealWriter", lambda: FakeWriter(system))
    monkeypatch.setattr(
        cli,
        "RealFileFetcher",
        lambda: FakeFileFetcher(failure="network unavailable"),
    )
    monkeypatch.setattr(
        cli,
        "probe",
        lambda system, identifier=None: healthy_environment(),  # noqa: ARG005
    )

    result = runner.invoke(cli.app, ["--json", "shortcut"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["shortcut"]["status"] == "failed"
    assert "network unavailable" in payload["shortcut"]["detail"]
    assert not [path for path in system.files if path.suffix == ".desktop"]
