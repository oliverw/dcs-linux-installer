"""`dcs-linux verify` end to end, with DCS itself replaced by a fixture."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from dcs_linux import cli
from dcs_linux.checks import Status
from dcs_linux.probes import Environment
from dcs_linux.verify import Finding, Verification
from tests.environments import LAYOUT, bare_environment, healthy_environment

runner = CliRunner()

WORKING = Verification(
    findings=(Finding("Launch", Status.PASS, "DCS closed with exit code 0"),),
    log_path=LAYOUT.saved_games / "DCS" / "Logs" / "dcs.log",
)

BROKEN = Verification(
    findings=(
        Finding(
            "Fonts",
            Status.FAIL,
            "a font was requested by no name at all",
            remediation="dcs-linux patch apply segoe-fonts",
            patch="segoe-fonts",
        ),
    )
)


class Spy:
    """Stands in for `verify.verify_install`, recording what the CLI asked for."""

    def __init__(self, result: Verification = WORKING) -> None:
        self.result = result
        self.calls = 0
        self.kwargs: dict[str, object] = {}

    def __call__(self, *args: object, **kwargs: object) -> Verification:
        self.calls += 1
        self.kwargs = kwargs
        announce = kwargs.get("announce")
        if callable(announce):
            announce("fly to the success bar")
        return self.result


def use(
    monkeypatch: pytest.MonkeyPatch,
    environment: Environment | None = None,
    result: Verification = WORKING,
) -> Spy:
    monkeypatch.setattr(
        cli,
        "probe",
        lambda system, identifier=None, *, layout=None: (
            environment  # noqa: ARG005
            or healthy_environment()
        ),
    )
    spy = Spy(result)
    monkeypatch.setattr(cli, "verify_install", spy)
    return spy


def test_a_working_install_verifies_and_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = use(monkeypatch)
    result = runner.invoke(cli.app, ["verify"])

    assert result.exit_code == 0, result.stdout
    assert spy.calls == 1
    assert spy.kwargs["launch"] is True
    assert "DCS is working" in result.stdout


def test_a_broken_install_exits_non_zero_and_names_the_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use(monkeypatch, result=BROKEN)
    result = runner.invoke(cli.app, ["verify"])

    assert result.exit_code == 1
    assert "patch apply segoe-fonts" in result.stdout


def test_no_launch_starts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = use(monkeypatch)
    result = runner.invoke(cli.app, ["verify", "--no-launch"])

    assert result.exit_code == 0, result.stdout
    assert spy.kwargs["launch"] is False


def test_with_no_install_there_is_nothing_to_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    """A usage error, not a failed verification: nothing was judged."""
    spy = use(monkeypatch, bare_environment())
    result = runner.invoke(cli.app, ["verify"])

    assert result.exit_code == 2
    assert spy.calls == 0
    assert "no DCS install found" in result.output


def test_json_carries_the_findings_and_the_patch_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, result=BROKEN)
    result = runner.invoke(cli.app, ["--json", "verify"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["command"] == "verify"
    assert payload["ok"] is False
    assert payload["findings"][0]["patch"] == "segoe-fonts"


def test_the_briefing_stays_out_of_the_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """It is read while DCS is starting, so it goes to stderr like the handoff's."""
    use(monkeypatch)
    result = runner.invoke(cli.app, ["--json", "verify"])

    assert json.loads(result.stdout)["ok"] is True
