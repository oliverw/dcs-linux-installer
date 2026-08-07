"""`dcs-linux check` end to end, with the machine replaced by a fixture."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dcs_linux import cli
from dcs_linux.checks import GIB
from dcs_linux.installs import DcsInstall, Edition, Launcher, select
from dcs_linux.probes import Environment, InstallState, Umu
from dcs_linux.system import DiskUsage
from tests.environments import (
    OWN_INSTALL,
    STEAMOS,
    bare_environment,
    healthy_environment,
    with_tracker,
)

runner = CliRunner()


def use(monkeypatch: pytest.MonkeyPatch, environment: Environment) -> None:
    """Replace the machine with a fixture, honouring --install as probe does."""

    def fake_probe(system: object, identifier: str | None = None) -> Environment:
        if identifier is None:
            return environment
        return replace(environment, targeted=select(environment.installs, identifier))

    monkeypatch.setattr(cli, "probe", fake_probe)


def test_healthy_machine_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, healthy_environment())
    result = runner.invoke(cli.app, ["check"])
    assert result.exit_code == 0
    assert "No blocking problems" in result.stdout


def test_blocking_problem_exits_non_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, bare_environment())
    result = runner.invoke(cli.app, ["check"])
    assert result.exit_code != 0
    assert "umu-launcher" in result.stdout


def test_runs_with_no_dcs_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first thing a new user runs must not need an install to exist."""
    use(monkeypatch, bare_environment())
    result = runner.invoke(cli.app, ["check"])
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_json_carries_the_same_results(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, bare_environment())
    result = runner.invoke(cli.app, ["--json", "check"])

    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["command"] == "check"
    assert payload["ok"] is False
    assert payload["distro"]["id"] == "fedora"
    assert payload["distro"]["immutable"] is False

    statuses = {check["key"]: check["status"] for check in payload["checks"]}
    assert statuses["umu_launcher"] == "fail"
    assert statuses["segoe_fonts"] == "skip"
    failures = [check for check in payload["checks"] if check["status"] == "fail"]
    assert failures and all(check["remediation"] for check in failures)


def test_json_reports_ok_for_a_healthy_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, healthy_environment())
    result = runner.invoke(cli.app, ["--json", "check"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["ok"] is True


def test_json_marks_an_immutable_distro(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, bare_environment(distro=STEAMOS))
    result = runner.invoke(cli.app, ["--json", "check"])
    payload = json.loads(result.stdout)
    assert payload["distro"]["immutable"] is True
    assert payload["distro"]["immutability"] == "read-only"


def test_warnings_alone_do_not_fail_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    use(
        monkeypatch,
        healthy_environment(
            filesystem="ext4",
            disk=DiskUsage(total=400 * GIB, free=150 * GIB),
        ),
    )
    result = runner.invoke(cli.app, ["--json", "check"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert any(check["status"] == "warn" for check in payload["checks"])


def test_adopted_location_warning_does_not_fail_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    adopted = replace(
        OWN_INSTALL,
        game=Path("/mnt/bottles/dcs/drive_c/Games/DCS World"),
        launcher=Launcher.ADOPTED,
        prefix=None,
    )
    use(monkeypatch, healthy_environment(installs=(adopted,), targeted=adopted))

    result = runner.invoke(cli.app, ["--json", "check"])

    assert result.exit_code == 0
    rows = {row["key"]: row for row in json.loads(result.stdout)["checks"]}
    assert rows["game_location"]["status"] == "warn"


def test_no_color_output_has_no_ansi(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, healthy_environment())
    result = runner.invoke(cli.app, ["--no-color", "check"])
    assert "\x1b[" not in result.stdout


def test_every_failure_is_shown_with_its_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    use(
        monkeypatch,
        bare_environment(
            install_state=InstallState(prefix_exists=True), umu=Umu(None, False, None)
        ),
    )
    result = runner.invoke(cli.app, ["check"])
    assert result.exit_code != 0
    assert "blocking problem" in result.stdout


STEAM_INSTALL = DcsInstall(
    game=Path("/mnt/games/SteamLibrary/steamapps/common/DCSWorld"),
    launcher=Launcher.STEAM,
    prefix=Path("/mnt/games/SteamLibrary/steamapps/compatdata/223750/pfx"),
    runtime="GE-Proton11-3",
    edition=Edition.STEAM,
    version="2.9.28.26385",
)


def several_installs() -> Environment:
    """Two installs and nothing chosen — the state `--install` exists for."""
    installs = (OWN_INSTALL, STEAM_INSTALL)
    return healthy_environment(installs=installs, targeted=None)


class TestDiscoveryOutput:
    def test_every_install_is_listed_with_its_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use(monkeypatch, several_installs())
        result = runner.invoke(cli.app, ["--no-color", "check"])
        for install in (OWN_INSTALL, STEAM_INSTALL):
            assert install.install_id in result.stdout
        assert "steam" in result.stdout
        assert "lutris" not in result.stdout

    def test_no_installs_says_so_without_failing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A machine with no DCS is the normal starting point, not a fault."""
        use(monkeypatch, healthy_environment(installs=(), targeted=None))
        result = runner.invoke(cli.app, ["--no-color", "check"])
        assert result.exit_code == 0
        assert "No DCS install found" in result.stdout

    def test_an_install_can_be_targeted_by_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use(monkeypatch, several_installs())
        result = runner.invoke(cli.app, ["--json", "check", "--install", STEAM_INSTALL.install_id])
        assert result.exit_code == 0
        selected = [i for i in json.loads(result.stdout)["installs"] if i["targeted"]]
        assert [i["id"] for i in selected] == [STEAM_INSTALL.install_id]

    def test_an_unknown_id_is_a_usage_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use(monkeypatch, several_installs())
        result = runner.invoke(cli.app, ["check", "--install", "nosuchid"])
        assert result.exit_code == 2

    def test_json_reports_every_install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use(monkeypatch, several_installs())
        payload = json.loads(runner.invoke(cli.app, ["--json", "check"]).stdout)
        steam = payload["installs"][1]
        assert steam["id"] == STEAM_INSTALL.install_id
        assert steam["launcher"] == "steam"
        assert steam["edition"] == "steam"
        assert steam["version"] == "2.9.28.26385"
        assert steam["runtime"] == "GE-Proton11-3"
        assert steam["game"] == str(STEAM_INSTALL.game)
        assert steam["prefix"] == str(STEAM_INSTALL.prefix)
        assert steam["targeted"] is False


def test_head_tracking_rows_reach_the_table(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, healthy_environment(head_tracking=with_tracker(access=False)))
    result = runner.invoke(cli.app, ["--no-color", "check"])
    assert "TrackIR 5" in result.stdout
    assert "udev" in result.stdout


def test_an_unreadable_tracker_does_not_make_check_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """DCS flies without head tracking, so it can never be a blocking problem."""
    use(monkeypatch, healthy_environment(head_tracking=with_tracker(access=False)))
    result = runner.invoke(cli.app, ["check"])
    assert result.exit_code == 0
    assert "No blocking problems" in result.stdout


def test_a_machine_with_no_tracker_says_so_without_erroring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use(monkeypatch, healthy_environment())
    result = runner.invoke(cli.app, ["--no-color", "check"])
    assert result.exit_code == 0
    assert "no NaturalPoint or TrackIR device connected" in _unwrapped(result.stdout)


def test_head_tracking_rows_are_in_the_json(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, healthy_environment(head_tracking=with_tracker()))
    result = runner.invoke(cli.app, ["--json", "check"])
    keys = {check["key"] for check in json.loads(result.stdout)["checks"]}
    assert {"head_tracker", "opentrack", "head_tracking_in_dcs"} <= keys


def _unwrapped(text: str) -> str:
    """Rich folds long cells, so a sentence can arrive split across lines."""
    return " ".join(text.split())
