"""`dcs-linux report` end to end, with the machine replaced by a fixture."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dcs_linux import cli, dcslog
from dcs_linux.installs import DcsInstall, Launcher, select
from dcs_linux.probes import Environment
from tests.environments import OWN_INSTALL, bare_environment, healthy_environment

runner = CliRunner()

LOG_TEXT = (
    "=== Log opened UTC 2026-08-02 21:23:52\r\n"
    "2026-08-02 21:23:55.011 INFO    APP (Main): DCS/2.9.28.26385 (x86_64; MT)\r\n"
    "2026-08-02 21:24:32.557 INFO    UIBASERENDERER (Main): Cannot create font [] size 30!\r\n"
    "2026-08-02 21:26:33.473 ERROR   EDCORE (Main): Can't open "
    "'/home/oliver/dcs-linux/saved-games/DCS/Logs/voice_chat.log'\r\n"
    "=== Log closed.\r\n"
)

STEAM_INSTALL = DcsInstall(
    game=Path("/mnt/games/SteamLibrary/steamapps/common/DCSWorld"),
    launcher=Launcher.STEAM,
)


def use(
    monkeypatch: pytest.MonkeyPatch,
    environment: Environment,
    log: dcslog.DcsLog | None = None,
) -> None:
    """Replace the machine, its home and its dcs.log with fixtures."""

    def fake_probe(system: object, identifier: str | None = None) -> Environment:
        if identifier is None:
            return environment
        return replace(environment, targeted=select(environment.installs, identifier))

    monkeypatch.setattr(cli, "probe", fake_probe)
    monkeypatch.setattr(cli, "read_log", lambda system, paths: log)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/oliver")))


def logged() -> dcslog.DcsLog:
    return dcslog.DcsLog(
        path=Path("/home/oliver/dcs-linux/saved-games/DCS/Logs/dcs.log"),
        excerpts=dcslog.excerpt(LOG_TEXT),
    )


def test_it_prints_pasteable_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, healthy_environment())
    result = runner.invoke(cli.app, ["report"])
    assert result.exit_code == 0
    assert result.stdout.startswith("# dcs-linux report")


def test_a_broken_machine_still_gets_a_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blocking failures are what the user is reporting, not a reason to refuse."""
    use(monkeypatch, bare_environment())
    result = runner.invoke(cli.app, ["report"])
    assert result.exit_code == 0
    assert "FAIL" in result.stdout
    assert "No DCS install found" in result.stdout


def test_the_log_is_excerpted_into_the_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, healthy_environment(), logged())
    result = runner.invoke(cli.app, ["report"])
    assert "Cannot create font" in result.stdout
    assert "Log closed" in result.stdout


def test_the_home_directory_never_reaches_the_output(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, healthy_environment(), logged())
    result = runner.invoke(cli.app, ["report"])
    assert "/home/oliver" not in result.stdout
    assert "~/dcs-linux" in result.stdout


def test_redaction_can_be_turned_off_deliberately(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, healthy_environment(), logged())
    result = runner.invoke(cli.app, ["report", "--no-redact"])
    assert "/home/oliver/dcs-linux" in result.stdout


def test_an_install_can_be_targeted(monkeypatch: pytest.MonkeyPatch) -> None:
    installs = (OWN_INSTALL, STEAM_INSTALL)
    use(monkeypatch, healthy_environment(installs=installs, targeted=None))
    result = runner.invoke(cli.app, ["report", "--install", STEAM_INSTALL.install_id])
    assert result.exit_code == 0
    assert "-> | " + STEAM_INSTALL.install_id in result.stdout


def test_an_unknown_install_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, healthy_environment(installs=(OWN_INSTALL, STEAM_INSTALL), targeted=None))
    result = runner.invoke(cli.app, ["report", "--install", "nosuchid"])
    assert result.exit_code == 2


def test_json_carries_the_same_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, healthy_environment(), logged())
    result = runner.invoke(cli.app, ["--json", "report"])
    payload = json.loads(result.stdout)
    assert payload["command"] == "report"
    assert payload["redacted"] is True
    assert payload["markdown"].startswith("# dcs-linux report")


def test_the_markdown_is_not_wrapped_or_coloured(monkeypatch: pytest.MonkeyPatch) -> None:
    """It gets pasted verbatim, so nothing may reflow or decorate it."""
    use(monkeypatch, healthy_environment())
    result = runner.invoke(cli.app, ["report"])
    assert "\x1b[" not in result.stdout
    assert all(len(line) < 300 for line in result.stdout.splitlines())
