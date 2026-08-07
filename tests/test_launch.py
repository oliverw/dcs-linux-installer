"""Starting DCS through the runtime this tool prepared."""

from __future__ import annotations

import json
from pathlib import Path

from dcs_linux.launch import launch_dcs
from dcs_linux.prefix import (
    GAMEID,
    GE_PROTON_VERSION,
    LAUNCH_ENVIRONMENT,
    UMU_VERSION,
    WINETRICKS_VERBS,
)
from dcs_linux.runner import Completed
from tests.environments import LAYOUT, OWN_INSTALL, healthy_environment
from tests.fakes import FakeRunner, FakeSystem


def prepared_machine() -> FakeSystem:
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
        files={
            str(OWN_INSTALL.game / "bin" / "DCS.exe"): "MZ",
            str(LAYOUT.umu_run): "#!/usr/bin/env python",
            str(LAYOUT.umu_version_marker): f"{UMU_VERSION}\n",
            str(LAYOUT.ge_proton_build(GE_PROTON_VERSION) / "proton"): "#!/usr/bin/env python",
            str(LAYOUT.prefix / "system.reg"): "WINE REGISTRY Version 2",
            str(LAYOUT.manifest): json.dumps(manifest),
        },
        links={
            str(LAYOUT.prefix_game_drive): str(LAYOUT.game),
            str(LAYOUT.prefix_saved_games): str(LAYOUT.saved_games),
        },
    )


def test_a_prepared_install_uses_the_pinned_runtime_and_mapped_lifetimes() -> None:
    runner = FakeRunner()

    result = launch_dcs(prepared_machine(), runner, healthy_environment())

    assert result.ok
    command, environment = runner.calls[0]
    assert command == [
        str(LAYOUT.umu_run),
        str(OWN_INSTALL.game / "bin" / "DCS.exe"),
        "--no-launcher",
    ]
    assert environment == {
        "WINEPREFIX": str(LAYOUT.prefix),
        "GAMEID": GAMEID,
        "PROTONPATH": str(LAYOUT.ge_proton_build(GE_PROTON_VERSION)),
        **LAUNCH_ENVIRONMENT,
    }
    assert runner.sessions == [True]
    assert runner.timeouts == [None]


def test_an_incomplete_install_is_refused_before_starting() -> None:
    system = prepared_machine()
    system.files.pop(OWN_INSTALL.game / "bin" / "DCS.exe")
    runner = FakeRunner()

    result = launch_dcs(system, runner, healthy_environment())

    assert not result.ok
    assert "unfinished" in result.detail
    assert runner.calls == []


def test_a_prefix_without_our_manifest_is_refused() -> None:
    system = prepared_machine()
    system.files.pop(LAYOUT.manifest)
    runner = FakeRunner()

    result = launch_dcs(system, runner, healthy_environment())

    assert not result.ok
    assert "not prepared by this tool" in result.detail
    assert runner.calls == []


def test_a_prefix_built_with_an_old_pin_is_refused() -> None:
    system = prepared_machine()
    payload = json.loads(system.files[LAYOUT.manifest])
    payload["ge_proton"] = "GE-Proton10-1"
    system.files[LAYOUT.manifest] = json.dumps(payload).encode()
    runner = FakeRunner()

    result = launch_dcs(system, runner, healthy_environment())

    assert not result.ok
    assert "pinned runtime" in result.detail
    assert runner.calls == []


def test_drifted_lifetime_mappings_are_refused() -> None:
    system = prepared_machine()
    system.links[LAYOUT.prefix_game_drive] = LAYOUT.root / "wrong-game"
    runner = FakeRunner()

    result = launch_dcs(system, runner, healthy_environment())

    assert not result.ok
    assert "mapping" in result.detail
    assert runner.calls == []


def test_a_manifest_for_another_install_is_refused() -> None:
    system = prepared_machine()
    payload = json.loads(system.files[LAYOUT.manifest])
    payload["game"] = "/data/some-other-game"
    system.files[LAYOUT.manifest] = json.dumps(payload).encode()
    runner = FakeRunner()

    result = launch_dcs(system, runner, healthy_environment())

    assert not result.ok
    assert "does not describe" in result.detail
    assert runner.calls == []


def test_a_missing_pinned_runtime_is_a_start_failure() -> None:
    system = prepared_machine()
    system.files.pop(LAYOUT.ge_proton_build(GE_PROTON_VERSION) / "proton")
    runner = FakeRunner()

    result = launch_dcs(system, runner, healthy_environment())

    assert not result.ok
    assert "pinned runtime is incomplete" in result.detail
    assert runner.calls == []


def test_the_umu_version_marker_must_match_the_pin() -> None:
    system = prepared_machine()
    system.files[LAYOUT.umu_version_marker] = b"1.3.0\n"
    runner = FakeRunner()

    result = launch_dcs(system, runner, healthy_environment())

    assert not result.ok
    assert "pinned runtime is incomplete" in result.detail
    assert runner.calls == []


def test_a_prefix_missing_a_required_verb_is_refused() -> None:
    system = prepared_machine()
    payload = json.loads(system.files[LAYOUT.manifest])
    payload["verbs"] = ["corefonts", "xact"]
    system.files[LAYOUT.manifest] = json.dumps(payload).encode()
    runner = FakeRunner()

    result = launch_dcs(system, runner, healthy_environment())

    assert not result.ok
    assert "required winetricks verbs" in result.detail
    assert runner.calls == []


def test_durable_lifetimes_inside_the_prefix_are_refused() -> None:
    system = prepared_machine()
    unsafe_saved_games = LAYOUT.prefix / "saved-games"
    payload = json.loads(system.files[LAYOUT.manifest])
    payload["saved_games"] = str(unsafe_saved_games)
    system.files[LAYOUT.manifest] = json.dumps(payload).encode()
    system.links[LAYOUT.prefix_saved_games] = unsafe_saved_games
    runner = FakeRunner()

    result = launch_dcs(system, runner, healthy_environment())

    assert not result.ok
    assert "outside the prefix" in result.detail
    assert runner.calls == []


def test_manifest_paths_are_compared_after_resolving_symlinks() -> None:
    system = prepared_machine()
    payload = json.loads(system.files[LAYOUT.manifest])
    payload.update(
        {
            "prefix": "/alias/prefix",
            "game": "/alias/game",
            "saved_games": "/alias/saved-games",
        }
    )
    system.files[LAYOUT.manifest] = json.dumps(payload).encode()
    system.links.update(
        {
            Path("/alias/prefix"): LAYOUT.prefix,
            Path("/alias/game"): LAYOUT.game,
            Path("/alias/saved-games"): LAYOUT.saved_games,
        }
    )

    result = launch_dcs(system, FakeRunner(), healthy_environment())

    assert result.ok


def test_a_normal_nonzero_dcs_exit_is_reported_not_judged() -> None:
    executable = str(OWN_INSTALL.game / "bin" / "DCS.exe")
    runner = FakeRunner(results={executable: Completed(returncode=3)})

    result = launch_dcs(prepared_machine(), runner, healthy_environment())

    assert result.ok
    assert result.returncode == 3
    assert result.detail == "DCS closed with exit code 3"


def test_a_process_start_failure_is_reported() -> None:
    executable = str(OWN_INSTALL.game / "bin" / "DCS.exe")
    runner = FakeRunner(
        results={executable: Completed(returncode=None, detail="umu-run could not be executed")}
    )

    result = launch_dcs(prepared_machine(), runner, healthy_environment())

    assert not result.ok
    assert result.returncode is None
    assert result.detail == "DCS did not start: umu-run could not be executed"
