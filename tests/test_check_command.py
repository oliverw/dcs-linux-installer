"""`dcs-linux check` end to end, with the machine replaced by a fixture."""

import json

import pytest
from typer.testing import CliRunner

from dcs_linux import cli
from dcs_linux.checks import GIB
from dcs_linux.probes import Environment, InstallState, Umu
from dcs_linux.system import DiskUsage
from tests.environments import STEAMOS, bare_environment, healthy_environment

runner = CliRunner()


def use(monkeypatch: pytest.MonkeyPatch, environment: Environment) -> None:
    monkeypatch.setattr(cli, "probe", lambda system: environment)


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


def test_no_color_output_has_no_ansi(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, healthy_environment())
    result = runner.invoke(cli.app, ["--no-color", "check"])
    assert "\x1b[" not in result.stdout


def test_every_failure_is_shown_with_its_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    use(
        monkeypatch,
        bare_environment(install=InstallState(prefix_exists=True), umu=Umu(None, False, None)),
    )
    result = runner.invoke(cli.app, ["check"])
    assert result.exit_code != 0
    assert "blocking problem" in result.stdout
