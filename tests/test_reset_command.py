"""`dcs-linux reset` end to end, with the machine replaced by a fixture."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from dcs_linux import cli
from tests.environments import LAYOUT
from tests.fakes import FakeSystem, FakeWriter

runner = CliRunner()
STORE = LAYOUT.patch_store("abc12345")


def use(monkeypatch: pytest.MonkeyPatch, system: FakeSystem) -> FakeSystem:
    if "DCS_LINUX_STATE" not in system.env and "XDG_STATE_HOME" not in system.env:
        system.env["DCS_LINUX_STATE"] = str(LAYOUT.state)
    monkeypatch.setattr(cli, "RealSystem", lambda: system)
    monkeypatch.setattr(cli, "RealWriter", lambda: FakeWriter(system))
    return system


def test_reset_lists_the_register_and_declining_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = use(monkeypatch, FakeSystem(files={str(LAYOUT.installs_register): "{}"}))

    result = runner.invoke(cli.app, ["reset"], input="n\n")

    assert result.exit_code == 0
    assert str(LAYOUT.installs_register) in result.stdout
    assert "Continue?" in result.stdout
    assert system.read_text(LAYOUT.installs_register) == "{}"


def test_reset_clears_only_the_register_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    system = use(
        monkeypatch,
        FakeSystem(
            files={
                str(LAYOUT.installs_register): "{}",
                str(STORE / "state.json"): '{"version": 1, "patches": {}}',
                str(STORE / "backups" / "segoe-fonts" / "0"): "pristine",
            }
        ),
    )

    result = runner.invoke(cli.app, ["reset", "--yes"])

    assert result.exit_code == 0
    assert system.read_text(LAYOUT.installs_register) is None
    assert system.read_text(STORE / "state.json") is not None
    assert system.read_text(STORE / "backups" / "segoe-fonts" / "0") == "pristine"


def test_reset_patches_clears_patch_stores_when_none_are_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = use(
        monkeypatch,
        FakeSystem(
            files={
                str(LAYOUT.installs_register): "{}",
                str(STORE / "state.json"): '{"version": 1, "patches": {}}',
                str(STORE / "backups" / "segoe-fonts" / "0"): "pristine",
            }
        ),
    )

    result = runner.invoke(cli.app, ["reset", "--patches", "--yes"])

    assert result.exit_code == 0
    assert system.read_text(LAYOUT.installs_register) is None
    assert system.read_text(STORE / "state.json") is None
    assert system.read_text(STORE / "backups" / "segoe-fonts" / "0") is None


def test_reset_patches_refuses_while_a_patch_remains_revertible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = '{"version": 1, "patches": {"segoe-fonts": {"dcs_version": null, "files": []}}}'
    system = use(
        monkeypatch,
        FakeSystem(files={str(LAYOUT.installs_register): "{}", str(STORE / "state.json"): state}),
    )

    result = runner.invoke(cli.app, ["reset", "--patches", "--yes"])

    assert result.exit_code == 1
    assert "dcs-linux patch revert" in result.stdout
    assert system.read_text(LAYOUT.installs_register) == "{}"
    assert system.read_text(STORE / "state.json") == state


def test_reset_honours_the_state_override_and_never_touches_the_three_lifetimes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overridden = LAYOUT.state / "elsewhere"
    register = overridden / "installs.json"
    system = use(
        monkeypatch,
        FakeSystem(
            files={
                str(register): "{}",
                str(LAYOUT.prefix / "system.reg"): "prefix",
                str(LAYOUT.game / "bin" / "DCS.exe"): "game",
                str(LAYOUT.saved_games / "DCS" / "Config" / "authdata.bin"): "saved",
            },
            env={"DCS_LINUX_STATE": str(overridden)},
        ),
    )

    result = runner.invoke(cli.app, ["reset", "--yes"])

    assert result.exit_code == 0
    assert system.read_text(register) is None
    assert system.read_text(LAYOUT.prefix / "system.reg") == "prefix"
    assert system.read_text(LAYOUT.game / "bin" / "DCS.exe") == "game"
    assert system.read_text(LAYOUT.saved_games / "DCS" / "Config" / "authdata.bin") == "saved"


def test_reset_honours_xdg_state_home(monkeypatch: pytest.MonkeyPatch) -> None:
    state = Path("/run/user/1000/state/dcs-linux")
    register = state / "installs.json"
    system = use(
        monkeypatch,
        FakeSystem(files={str(register): "{}"}, env={"XDG_STATE_HOME": "/run/user/1000/state"}),
    )

    result = runner.invoke(cli.app, ["reset", "--yes"])

    assert result.exit_code == 0
    assert system.read_text(register) is None


def test_reset_on_an_empty_state_is_successful(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, FakeSystem())

    result = runner.invoke(cli.app, ["reset", "--yes"])

    assert result.exit_code == 0
    assert "Nothing to delete" in result.stdout
