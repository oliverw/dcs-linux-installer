"""`dcs-linux install` end to end, with the machine replaced by a fixture."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dcs_linux import cli
from dcs_linux.checks import GIB
from dcs_linux.installs import DcsInstall, Launcher
from dcs_linux.launchers import discover
from dcs_linux.paths import Layout
from dcs_linux.prefix import BuildResult, Runtime, Step, StepStatus
from dcs_linux.probes import Environment
from dcs_linux.system import DiskUsage
from dcs_linux.updater import HandoffResult, Progress, Stage
from tests.environments import LAYOUT, bare_environment, healthy_environment
from tests.fakes import FakeFileFetcher, FakeSystem, FakeWriter

runner = CliRunner()
ICON = b"\xff\xd8\xffDCS icon"

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

RISKY_GAME = Path("/mnt/games/SteamLibrary/steamapps/common/DCS World")


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
    monkeypatch.setattr(cli, "discover", lambda system, layout: (), raising=False)
    monkeypatch.setattr(cli, "RealFileFetcher", lambda: FakeFileFetcher(data=ICON))
    spy = Spy(result)
    monkeypatch.setattr(cli, "build", spy)
    handoff_spy = HandoffSpy(handoff)
    monkeypatch.setattr(cli, "handoff", handoff_spy)
    return spy, handoff_spy


def risky_adopted_install() -> DcsInstall:
    return DcsInstall(game=RISKY_GAME, launcher=Launcher.ADOPTED)


def use_adopt_result(
    monkeypatch: pytest.MonkeyPatch,
    install: DcsInstall,
) -> None:
    monkeypatch.setattr(cli, "adopt", lambda system, path: install, raising=False)


def completed_adoption(game: Path) -> HandoffResult:
    return HandoffResult(
        steps=(
            Step("DCS", StepStatus.DONE, "DCS 2.9.28.26385 installed"),
            Step("register", StepStatus.DONE, "install registered"),
        ),
        progress=Progress(stage=Stage.COMPLETE, game_root=game),
    )


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


def test_accepting_the_adoption_shortcut_prompt_creates_the_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adopted = DcsInstall(game=Path("/mnt/games/DCS World"), launcher=Launcher.ADOPTED)
    system = FakeSystem(env={"XDG_CURRENT_DESKTOP": "KDE"})
    writer = FakeWriter(system)
    use(monkeypatch, bare_environment(), handoff=completed_adoption(adopted.game))
    use_adopt_result(monkeypatch, adopted)
    monkeypatch.setattr(cli, "RealSystem", lambda: system)
    monkeypatch.setattr(cli, "RealWriter", lambda: writer)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True, raising=False)

    result = runner.invoke(
        cli.app,
        ["install", "--game-dir", str(adopted.game)],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    launcher = (
        system.home() / ".local/share/applications" / (f"dcs-linux-{adopted.install_id}.desktop")
    )
    assert "Create a KDE desktop shortcut?" in result.output
    assert f"Exec=dcs-linux launch --install {adopted.install_id}" in (
        system.read_text(launcher) or ""
    )


def test_json_shortcut_choice_is_scriptable_and_stays_machine_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adopted = DcsInstall(game=Path("/mnt/games/DCS World"), launcher=Launcher.ADOPTED)
    system = FakeSystem(env={"XDG_CURRENT_DESKTOP": "GNOME"})
    writer = FakeWriter(system)
    use(monkeypatch, bare_environment(), handoff=completed_adoption(adopted.game))
    use_adopt_result(monkeypatch, adopted)
    monkeypatch.setattr(cli, "RealSystem", lambda: system)
    monkeypatch.setattr(cli, "RealWriter", lambda: writer)

    result = runner.invoke(
        cli.app,
        ["--json", "install", "--game-dir", str(adopted.game), "--shortcut"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["shortcut"] == {
        "status": "created",
        "desktop": "gnome",
        "path": str(
            system.home() / ".local/share/applications" / f"dcs-linux-{adopted.install_id}.desktop"
        ),
        "detail": "desktop shortcut created",
    }
    assert "Create a" not in result.stderr


def test_an_adoption_shortcut_uses_the_resolved_game_directory_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    linked_game = Path("/mnt/link/DCS World")
    resolved_game = Path("/mnt/games/DCS World")
    adopted = DcsInstall(game=resolved_game, launcher=Launcher.ADOPTED)
    system = FakeSystem(
        env={"XDG_CURRENT_DESKTOP": "KDE"},
        links={str(linked_game): str(resolved_game)},
    )
    writer = FakeWriter(system)
    use(monkeypatch, bare_environment(), handoff=completed_adoption(linked_game))
    use_adopt_result(monkeypatch, adopted)
    monkeypatch.setattr(cli, "RealSystem", lambda: system)
    monkeypatch.setattr(cli, "RealWriter", lambda: writer)

    result = runner.invoke(
        cli.app,
        ["install", "--game-dir", str(linked_game), "--shortcut"],
    )

    assert result.exit_code == 0, result.output
    launcher = (
        system.home() / ".local/share/applications" / (f"dcs-linux-{adopted.install_id}.desktop")
    )
    assert f"--install {adopted.install_id}" in (system.read_text(launcher) or "")


def test_a_registered_external_install_is_not_offered_an_adoption_shortcut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game = Path("/mnt/games/DCS World")
    system = FakeSystem(
        env={"XDG_CURRENT_DESKTOP": "KDE"},
        files={
            str(game / "bin/DCS.exe"): "MZ",
            str(LAYOUT.installs_register): json.dumps(
                {"installs": [{"game": str(game), "prefix": str(LAYOUT.prefix)}]}
            ),
        },
    )
    use(monkeypatch, bare_environment(), handoff=completed_adoption(game))
    monkeypatch.setattr(cli, "RealSystem", lambda: system)
    monkeypatch.setattr(cli, "RealWriter", lambda: FakeWriter(system))
    monkeypatch.setattr(cli, "discover", discover)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)

    result = runner.invoke(cli.app, ["install", "--game-dir", str(game)])

    assert result.exit_code == 0, result.output
    assert "Create a" not in result.output


def test_declining_the_shortcut_prompt_creates_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adopted = DcsInstall(game=Path("/mnt/games/DCS World"), launcher=Launcher.ADOPTED)
    system = FakeSystem(env={"XDG_CURRENT_DESKTOP": "GNOME"})
    use(monkeypatch, bare_environment(), handoff=completed_adoption(adopted.game))
    use_adopt_result(monkeypatch, adopted)
    monkeypatch.setattr(cli, "RealSystem", lambda: system)
    monkeypatch.setattr(cli, "RealWriter", lambda: FakeWriter(system))
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)

    result = runner.invoke(
        cli.app,
        ["install", "--game-dir", str(adopted.game)],
        input="n\n",
    )

    assert result.exit_code == 0, result.output
    assert "Desktop shortcut skipped" in result.output
    assert not [path for path in system.files if path.suffix == ".desktop"]


def test_an_unsupported_desktop_skips_the_prompt_with_an_explanation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adopted = DcsInstall(game=Path("/mnt/games/DCS World"), launcher=Launcher.ADOPTED)
    system = FakeSystem(env={"XDG_CURRENT_DESKTOP": "sway"})
    use(monkeypatch, bare_environment(), handoff=completed_adoption(adopted.game))
    use_adopt_result(monkeypatch, adopted)
    monkeypatch.setattr(cli, "RealSystem", lambda: system)
    monkeypatch.setattr(cli, "RealWriter", lambda: FakeWriter(system))
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)

    result = runner.invoke(cli.app, ["install", "--game-dir", str(adopted.game)])

    assert result.exit_code == 0, result.output
    assert "current desktop is not KDE or GNOME" in result.output
    assert "Create a" not in result.output


def test_noninteractive_adoption_without_a_choice_never_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adopted = DcsInstall(game=Path("/mnt/games/DCS World"), launcher=Launcher.ADOPTED)
    system = FakeSystem(env={"XDG_CURRENT_DESKTOP": "KDE"})
    use(monkeypatch, bare_environment(), handoff=completed_adoption(adopted.game))
    use_adopt_result(monkeypatch, adopted)
    monkeypatch.setattr(cli, "RealSystem", lambda: system)
    monkeypatch.setattr(cli, "RealWriter", lambda: FakeWriter(system))
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)

    result = runner.invoke(cli.app, ["install", "--game-dir", str(adopted.game)])

    assert result.exit_code == 0, result.output
    assert "use --shortcut in non-interactive mode" in result.output
    assert "Create a" not in result.output


def test_fresh_and_prefix_only_installs_do_not_offer_a_shortcut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = FakeSystem(env={"XDG_CURRENT_DESKTOP": "KDE"})
    use(monkeypatch, bare_environment())
    monkeypatch.setattr(cli, "RealSystem", lambda: system)
    monkeypatch.setattr(cli, "RealWriter", lambda: FakeWriter(system))
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)

    fresh = runner.invoke(cli.app, ["install"])

    adopted = DcsInstall(game=Path("/mnt/games/DCS World"), launcher=Launcher.ADOPTED)
    use_adopt_result(monkeypatch, adopted)
    prefix_only = runner.invoke(
        cli.app,
        ["install", "--game-dir", str(adopted.game), "--prefix-only"],
    )

    assert fresh.exit_code == 0, fresh.output
    assert prefix_only.exit_code == 0, prefix_only.output
    assert "Create a" not in fresh.output
    assert "Create a" not in prefix_only.output


def test_a_failed_adoption_never_offers_a_shortcut(monkeypatch: pytest.MonkeyPatch) -> None:
    adopted = DcsInstall(game=Path("/mnt/games/DCS World"), launcher=Launcher.ADOPTED)
    failed = HandoffResult(
        steps=(Step("DCS", StepStatus.FAILED, "nothing was installed"),),
        progress=Progress(stage=Stage.ABSENT),
    )
    system = FakeSystem(env={"XDG_CURRENT_DESKTOP": "KDE"})
    use(monkeypatch, bare_environment(), handoff=failed)
    use_adopt_result(monkeypatch, adopted)
    monkeypatch.setattr(cli, "RealSystem", lambda: system)
    monkeypatch.setattr(cli, "RealWriter", lambda: FakeWriter(system))
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)

    result = runner.invoke(cli.app, ["install", "--game-dir", str(adopted.game)])

    assert result.exit_code == 1
    assert "Create a" not in result.output
    assert not [path for path in system.files if path.suffix == ".desktop"]


def test_a_shortcut_write_failure_does_not_roll_back_the_adoption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingWriter(FakeWriter):
        def write_bytes(self, path: Path, data: bytes) -> None:
            if path.suffix == ".desktop":
                raise OSError("desktop directory is read-only")
            super().write_bytes(path, data)

    adopted = DcsInstall(game=Path("/mnt/games/DCS World"), launcher=Launcher.ADOPTED)
    system = FakeSystem(env={"XDG_CURRENT_DESKTOP": "KDE"})
    use(monkeypatch, bare_environment(), handoff=completed_adoption(adopted.game))
    use_adopt_result(monkeypatch, adopted)
    monkeypatch.setattr(cli, "RealSystem", lambda: system)
    monkeypatch.setattr(cli, "RealWriter", lambda: FailingWriter(system))

    result = runner.invoke(
        cli.app,
        ["install", "--game-dir", str(adopted.game), "--shortcut"],
    )

    assert result.exit_code == 0, result.output
    assert "could not write desktop shortcut" in result.output
    assert "desktop directory is read-only" in result.output


def test_an_icon_download_failure_does_not_roll_back_the_adoption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adopted = DcsInstall(game=Path("/mnt/games/DCS World"), launcher=Launcher.ADOPTED)
    system = FakeSystem(env={"XDG_CURRENT_DESKTOP": "KDE"})
    use(monkeypatch, bare_environment(), handoff=completed_adoption(adopted.game))
    use_adopt_result(monkeypatch, adopted)
    monkeypatch.setattr(cli, "RealSystem", lambda: system)
    monkeypatch.setattr(cli, "RealWriter", lambda: FakeWriter(system))
    monkeypatch.setattr(
        cli,
        "RealFileFetcher",
        lambda: FakeFileFetcher(failure="network unavailable"),
    )

    result = runner.invoke(
        cli.app,
        ["install", "--game-dir", str(adopted.game), "--shortcut"],
    )

    assert result.exit_code == 0, result.output
    assert "could not download DCS World icon" in result.output
    assert "network unavailable" in result.output
    assert not [path for path in system.files if path.suffix == ".desktop"]


def test_declining_a_risky_takeover_leaves_the_install_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declining happens before the prefix, mapping, and later register write."""
    spy, handoff = use(monkeypatch, bare_environment())
    adopted = risky_adopted_install()
    use_adopt_result(monkeypatch, adopted)

    result = runner.invoke(
        cli.app,
        ["install", "--game-dir", str(adopted.game)],
        input="n\n",
    )

    assert result.exit_code == 0, result.output
    assert "D:" in result.output
    assert "dcs-linux" in result.output
    assert "Saved Games" in result.output
    assert spy.layout is None
    assert handoff.calls == 0


def test_yes_accepts_a_risky_takeover_without_prompting(monkeypatch: pytest.MonkeyPatch) -> None:
    spy, _ = use(monkeypatch, bare_environment())
    adopted = risky_adopted_install()
    use_adopt_result(monkeypatch, adopted)

    result = runner.invoke(cli.app, ["install", "--game-dir", str(adopted.game), "--yes"])

    assert result.exit_code == 0, result.output
    assert "Take over" not in result.output
    assert spy.layout is not None


def test_an_unrisky_adopted_directory_installs_without_a_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy, _ = use(monkeypatch, bare_environment())
    adopted = DcsInstall(game=Path("/mnt/games/DCS World"), launcher=Launcher.ADOPTED)
    monkeypatch.setattr(cli, "adopt", lambda system, path: adopted, raising=False)

    result = runner.invoke(cli.app, ["install", "--game-dir", str(adopted.game)])

    assert result.exit_code == 0, result.output
    assert "Take over" not in result.output
    assert spy.layout is not None


def test_an_install_already_ours_does_not_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    spy, _ = use(monkeypatch, bare_environment())
    adopted = risky_adopted_install()
    ours = replace(adopted, launcher=Launcher.DCS_LINUX)
    use_adopt_result(monkeypatch, adopted)
    monkeypatch.setattr(cli, "discover", lambda system, layout: (ours,), raising=False)

    result = runner.invoke(cli.app, ["install", "--game-dir", str(adopted.game)])

    assert result.exit_code == 0, result.output
    assert "Take over" not in result.output
    assert spy.layout is not None


def test_a_discovered_other_launcher_still_requires_takeover_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy, _ = use(monkeypatch, bare_environment())
    adopted = risky_adopted_install()
    steam = replace(adopted, launcher=Launcher.STEAM)
    use_adopt_result(monkeypatch, adopted)
    monkeypatch.setattr(cli, "discover", lambda system, layout: (steam,), raising=False)

    result = runner.invoke(
        cli.app,
        ["install", "--game-dir", str(adopted.game)],
        input="n\n",
    )

    assert result.exit_code == 0, result.output
    assert "Take over" in result.output
    assert spy.layout is None


def test_a_real_steam_discovery_still_requires_takeover_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit game directory must not make another launcher's game ours."""
    spy, _ = use(monkeypatch, bare_environment())
    steam_root = "/home/pilot/.steam/root"
    library = "/mnt/games/SteamLibrary"
    system = FakeSystem(
        files={
            f"{steam_root}/steamapps/libraryfolders.vdf": (
                '"libraryfolders" { "0" { "path" "' + library + '" } }'
            ),
            f"{library}/steamapps/appmanifest_223750.acf": (
                '"AppState" { "appid" "223750" "installdir" "DCS World" }'
            ),
            str(RISKY_GAME / "bin" / "DCS.exe"): "",
        }
    )
    monkeypatch.setattr(cli, "RealSystem", lambda: system)
    monkeypatch.setattr(cli, "discover", discover)

    result = runner.invoke(cli.app, ["install", "--game-dir", str(RISKY_GAME)], input="n\n")

    assert result.exit_code == 0, result.output
    assert "Take over" in result.output
    assert spy.layout is None


def test_a_game_inside_the_prefix_is_refused_before_a_takeover_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = DcsInstall(
        game=LAYOUT.prefix / "drive_c" / "Games" / "DCS World",
        launcher=Launcher.ADOPTED,
        prefix=LAYOUT.prefix,
    )
    spy, _ = use(monkeypatch, healthy_environment(installs=(blocked,), targeted=blocked))
    use_adopt_result(monkeypatch, blocked)

    result = runner.invoke(cli.app, ["install", "--game-dir", str(blocked.game)], input="y\n")

    assert result.exit_code == 1
    assert "Game location" in result.output
    assert "Take over" not in result.output
    assert spy.layout is None


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
