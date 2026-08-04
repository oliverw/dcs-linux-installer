"""`dcs-linux install` end to end, with the machine replaced by a fixture."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dcs_linux import cli
from dcs_linux.checks import GIB
from dcs_linux.paths import Layout
from dcs_linux.prefix import BuildResult, Runtime, Step, StepStatus
from dcs_linux.probes import Environment
from dcs_linux.system import DiskUsage
from dcs_linux.updater import HandoffResult, Progress, Stage
from tests.environments import LAYOUT, bare_environment, healthy_environment

runner = CliRunner()

RUNTIME = Runtime(
    umu_version="1.4.4",
    ge_proton="GE-Proton11-3",
    gameid="umu-223750",
    verbs=("corefonts",),
    environment={"WINEDLLOVERRIDES": "wbemprox=n"},
    prefix=LAYOUT.prefix,
    game=LAYOUT.game,
    saved_games=LAYOUT.saved_games,
)

BUILT = BuildResult(
    steps=(
        Step("prefix", StepStatus.DONE, "built"),
        Step("mapping", StepStatus.SKIPPED, "already mapped"),
    ),
    runtime=RUNTIME,
)


class Spy:
    """Stands in for `prefix.build`, recording what the CLI asked for."""

    def __init__(self, result: BuildResult = BUILT) -> None:
        self.result = result
        self.layout: Layout | None = None
        self.kwargs: dict[str, object] = {}

    def __call__(self, *args: object, **kwargs: object) -> BuildResult:
        layout = args[4]
        assert isinstance(layout, Layout)
        self.layout = layout
        self.kwargs = kwargs
        return self.result


HANDED_OFF = HandoffResult(
    steps=(Step("DCS", StepStatus.DONE, "DCS 2.9.28.26385 installed"),),
    progress=Progress(stage=Stage.COMPLETE, game_root=LAYOUT.game / "DCS World"),
)


class HandoffSpy:
    """Stands in for `updater.handoff`, recording what the CLI asked for."""

    def __init__(self, result: HandoffResult = HANDED_OFF) -> None:
        self.result = result
        self.calls = 0
        self.kwargs: dict[str, object] = {}

    def __call__(self, *args: object, **kwargs: object) -> HandoffResult:
        self.calls += 1
        self.kwargs = kwargs
        announce = kwargs.get("announce")
        if callable(announce):
            announce("open the updater")
        return self.result


def use(
    monkeypatch: pytest.MonkeyPatch,
    environment: Environment,
    result: BuildResult = BUILT,
    handoff: HandoffResult = HANDED_OFF,
) -> tuple[Spy, HandoffSpy]:
    """Replace the machine, the builder and the handoff, leaving the command real."""
    monkeypatch.setattr(
        cli,
        "probe",
        lambda system, identifier=None, *, layout=None: environment,  # noqa: ARG005
    )
    monkeypatch.setattr(cli, "resolve_layout", lambda system: LAYOUT)  # noqa: ARG005
    spy = Spy(result)
    monkeypatch.setattr(cli, "build", spy)
    handoff_spy = HandoffSpy(handoff)
    monkeypatch.setattr(cli, "handoff", handoff_spy)
    return spy, handoff_spy


def test_a_bare_machine_is_exactly_what_install_is_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """No umu, no Proton, no prefix: all blocking for `check`, none for this."""
    spy, _ = use(monkeypatch, bare_environment())
    result = runner.invoke(cli.app, ["install"])

    assert result.exit_code == 0, result.stdout
    assert spy.layout == LAYOUT
    assert "prefix" in result.stdout


def test_a_missing_gpu_stops_the_install(monkeypatch: pytest.MonkeyPatch) -> None:
    spy, _ = use(monkeypatch, bare_environment(gpus=()))
    result = runner.invoke(cli.app, ["install"])

    assert result.exit_code == 1
    assert "GPU" in result.output
    assert spy.layout is None


def test_a_full_disk_stops_the_install_before_any_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy, _ = use(monkeypatch, bare_environment(disk=DiskUsage(total=500 * GIB, free=10 * GIB)))
    result = runner.invoke(cli.app, ["install"])

    assert result.exit_code == 1
    assert "Disk space" in result.output
    assert spy.layout is None


def test_missing_external_tools_stop_the_install(monkeypatch: pytest.MonkeyPatch) -> None:
    spy, _ = use(monkeypatch, bare_environment(missing_tools=("bwrap",)))
    result = runner.invoke(cli.app, ["install"])

    assert result.exit_code == 1
    assert "bwrap" in result.output
    assert spy.layout is None


def test_the_game_directory_is_the_users_to_choose(monkeypatch: pytest.MonkeyPatch) -> None:
    spy, _ = use(monkeypatch, bare_environment())
    result = runner.invoke(cli.app, ["install", "--game-dir", "/mnt/big/DCS"])

    assert result.exit_code == 0, result.stdout
    assert spy.layout is not None
    assert spy.layout.game == Path("/mnt/big/DCS")


def test_vcrun2019_is_refused_before_anything_is_built(monkeypatch: pytest.MonkeyPatch) -> None:
    spy, _ = use(monkeypatch, bare_environment())
    result = runner.invoke(cli.app, ["install", "--verb", "vcrun2019"])

    assert result.exit_code == 2
    assert "RAM leak" in result.output
    assert spy.layout is None


def test_an_extra_verb_reaches_the_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    spy, _ = use(monkeypatch, bare_environment())
    result = runner.invoke(cli.app, ["install", "--verb", "vcrun2022"])

    assert result.exit_code == 0, result.stdout
    assert spy.kwargs["verbs"] == ("corefonts", "xact", "d3dcompiler_47", "vcrun2022")


def test_rebuild_is_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    spy, _ = use(monkeypatch, healthy_environment())
    result = runner.invoke(cli.app, ["install", "--rebuild"])

    assert result.exit_code == 0, result.stdout
    assert spy.kwargs["rebuild"] is True


def test_a_failed_step_exits_non_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    failed = BuildResult(steps=(Step("prefix", StepStatus.FAILED, "no system.reg"),))
    use(monkeypatch, bare_environment(), failed)
    result = runner.invoke(cli.app, ["install"])

    assert result.exit_code == 1
    assert "no system.reg" in result.stdout


def test_json_carries_the_steps_and_the_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, bare_environment())
    result = runner.invoke(cli.app, ["--json", "install"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["command"] == "install"
    assert payload["ok"] is True
    assert [step["status"] for step in payload["steps"]] == ["done", "skipped"]
    assert payload["runtime"]["ge_proton"] == "GE-Proton11-3"
    assert payload["runtime"]["game"] == str(LAYOUT.game)


def test_json_reports_a_failure_as_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    failed = BuildResult(steps=(Step("GE-Proton", StepStatus.FAILED, "download failed"),))
    use(monkeypatch, bare_environment(), failed)
    result = runner.invoke(cli.app, ["--json", "install"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["runtime"] is None


def test_the_disk_check_answers_about_the_chosen_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-flight must probe the layout being built, not the default one."""
    seen: list[Layout | None] = []

    def fake_probe(
        system: object, identifier: str | None = None, *, layout: Layout | None = None
    ) -> Environment:
        seen.append(layout)
        return replace(bare_environment(), layout=layout or LAYOUT)

    monkeypatch.setattr(cli, "probe", fake_probe)
    monkeypatch.setattr(cli, "resolve_layout", lambda system: LAYOUT)  # noqa: ARG005
    monkeypatch.setattr(cli, "build", Spy())
    monkeypatch.setattr(cli, "handoff", HandoffSpy())

    result = runner.invoke(cli.app, ["install", "--game-dir", "/mnt/big/DCS"])

    assert result.exit_code == 0, result.stdout
    assert seen and seen[0] is not None
    assert seen[0].game == Path("/mnt/big/DCS")


def test_the_updater_handoff_follows_a_built_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two phases are one command: prefix, then log in and download."""
    _, handoff = use(monkeypatch, bare_environment())
    result = runner.invoke(cli.app, ["install"])

    assert result.exit_code == 0, result.stdout
    assert handoff.calls == 1
    assert "patch apply" in result.stdout


def test_prefix_only_stops_before_the_updater(monkeypatch: pytest.MonkeyPatch) -> None:
    _, handoff = use(monkeypatch, bare_environment())
    result = runner.invoke(cli.app, ["install", "--prefix-only"])

    assert result.exit_code == 0, result.stdout
    assert handoff.calls == 0
    assert "not installed yet" in result.stdout


def test_a_failed_prefix_never_reaches_the_updater(monkeypatch: pytest.MonkeyPatch) -> None:
    failed = BuildResult(steps=(Step("prefix", StepStatus.FAILED, "no system.reg"),))
    _, handoff = use(monkeypatch, bare_environment(), failed)
    result = runner.invoke(cli.app, ["install"])

    assert result.exit_code == 1
    assert handoff.calls == 0


def test_a_named_installer_reaches_the_handoff(monkeypatch: pytest.MonkeyPatch) -> None:
    _, handoff = use(monkeypatch, bare_environment())
    result = runner.invoke(cli.app, ["install", "--installer", "/downloads/DCS_World_web.exe"])

    assert result.exit_code == 0, result.stdout
    assert handoff.kwargs["installer"] == Path("/downloads/DCS_World_web.exe")


def test_an_abandoned_download_exits_non_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    abandoned = HandoffResult(
        steps=(Step("DCS", StepStatus.FAILED, "nothing was installed"),),
        progress=Progress(stage=Stage.ABSENT),
    )
    use(monkeypatch, bare_environment(), handoff=abandoned)
    result = runner.invoke(cli.app, ["install"])

    assert result.exit_code == 1
    assert "nothing was installed" in result.stdout
    assert "run dcs-linux install again" in result.stdout.lower()


def test_json_carries_the_handoff_beside_the_build(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, bare_environment())
    result = runner.invoke(cli.app, ["--json", "install"])

    payload = json.loads(result.stdout)
    assert payload["dcs"]["stage"] == "complete"
    assert payload["dcs"]["game"] == str(LAYOUT.game / "DCS World")


def test_json_stays_parseable_while_the_user_is_being_briefed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The briefing goes to stderr, so `--json` stdout is the payload alone."""
    use(monkeypatch, bare_environment())
    result = runner.invoke(cli.app, ["--json", "install"])

    assert json.loads(result.stdout)["ok"] is True


def test_prefix_only_leaves_the_dcs_section_out(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, bare_environment())
    result = runner.invoke(cli.app, ["--json", "install", "--prefix-only"])

    assert json.loads(result.stdout)["dcs"] is None
