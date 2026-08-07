"""Start DCS without judging its log."""

from __future__ import annotations

from dataclasses import dataclass

from dcs_linux.installs import DCS_EXE
from dcs_linux.prefix import (
    GAMEID,
    GE_PROTON_VERSION,
    LAUNCH_ENVIRONMENT,
    PREFIX_MARKER,
    UMU_VERSION,
    WINETRICKS_VERBS,
    points_at,
    read_prefix_manifest,
)
from dcs_linux.probes import Environment
from dcs_linux.runner import Runner
from dcs_linux.system import System

NO_LAUNCHER = "--no-launcher"


@dataclass(frozen=True)
class LaunchResult:
    """How starting DCS ended; its exit code is reported, not judged."""

    started: bool
    returncode: int | None
    detail: str

    @property
    def ok(self) -> bool:
        return self.started


def launch_dcs(
    system: System,
    runner: Runner,
    environment: Environment,
    *,
    timeout: float | None = None,
) -> LaunchResult:
    """Start the targeted install."""
    targeted = environment.targeted
    if targeted is None:
        return LaunchResult(False, None, "no DCS install targeted")
    executable = targeted.game / DCS_EXE
    if not system.exists(executable):
        return LaunchResult(
            False,
            None,
            f"no {DCS_EXE} in {targeted.game}; the install is unfinished",
        )
    prefix = targeted.prefix
    runtime = read_prefix_manifest(system, prefix) if prefix is not None else None
    if prefix is None or not system.exists(prefix / PREFIX_MARKER) or runtime is None:
        return LaunchResult(
            False,
            None,
            "the targeted prefix was not prepared by this tool; run dcs-linux install",
        )
    resolved_prefix = system.resolve(prefix)
    resolved_game = system.resolve(runtime.game)
    resolved_saved_games = system.resolve(runtime.saved_games)
    targeted_game = system.resolve(targeted.game)
    if system.resolve(runtime.prefix) != resolved_prefix or (
        resolved_game != targeted_game and resolved_game != targeted_game.parent
    ):
        return LaunchResult(
            False,
            None,
            "the prefix manifest does not describe the targeted install; run dcs-linux install",
        )
    if (
        runtime.umu_version != UMU_VERSION
        or runtime.ge_proton != GE_PROTON_VERSION
        or runtime.gameid != GAMEID
        or runtime.environment != LAUNCH_ENVIRONMENT
    ):
        return LaunchResult(
            False,
            None,
            "the prefix does not use the current pinned runtime; run dcs-linux install",
        )
    if not set(WINETRICKS_VERBS).issubset(runtime.verbs):
        return LaunchResult(
            False,
            None,
            "the prefix is missing required winetricks verbs; run dcs-linux install",
        )
    if any(
        path == resolved_prefix or path.is_relative_to(resolved_prefix)
        for path in (resolved_game, resolved_saved_games)
    ):
        return LaunchResult(
            False,
            None,
            "the game directory and saved games must be outside the prefix",
        )
    mappings = (
        (prefix / "dosdevices" / "d:", resolved_game),
        (
            prefix / "drive_c" / "users" / "steamuser" / "Saved Games",
            resolved_saved_games,
        ),
    )
    if not all(points_at(system, link, target) for link, target in mappings):
        return LaunchResult(
            False,
            None,
            "the prepared prefix lifetime mapping has drifted; run dcs-linux install",
        )
    proton = environment.layout.ge_proton_build(GE_PROTON_VERSION)
    umu_version = (system.read_text(environment.layout.umu_version_marker) or "").strip()
    if (
        not system.exists(environment.layout.umu_run)
        or umu_version != UMU_VERSION
        or not system.exists(proton / "proton")
    ):
        return LaunchResult(
            False,
            None,
            "the pinned runtime is incomplete; run dcs-linux install",
        )
    completed = runner.run(
        [str(environment.layout.umu_run), str(executable), NO_LAUNCHER],
        {
            "WINEPREFIX": str(resolved_prefix),
            "GAMEID": GAMEID,
            "PROTONPATH": str(proton),
            **LAUNCH_ENVIRONMENT,
        },
        timeout,
        own_session=True,
    )
    if not completed.started:
        return LaunchResult(False, None, f"DCS did not start: {completed.detail}")
    return LaunchResult(
        True,
        completed.returncode,
        f"DCS closed with exit code {completed.returncode}",
    )
