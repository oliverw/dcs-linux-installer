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
    points_at,
    read_prefix_manifest,
)
from dcs_linux.probes import Environment
from dcs_linux.runner import Runner
from dcs_linux.system import System

NO_LAUNCHER = "--no-launcher"
LAUNCH_TIMEOUT = 4 * 60 * 60.0


@dataclass(frozen=True)
class LaunchResult:
    """How starting DCS ended; its exit code is reported, not judged."""

    started: bool
    returncode: int | None
    detail: str

    @property
    def ok(self) -> bool:
        return self.started


def launch_dcs(system: System, runner: Runner, environment: Environment) -> LaunchResult:
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
    if runtime.prefix != prefix or (
        runtime.game != targeted.game and runtime.game != targeted.game.parent
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
    mappings = (
        (prefix / "dosdevices" / "d:", runtime.game),
        (
            prefix / "drive_c" / "users" / "steamuser" / "Saved Games",
            runtime.saved_games,
        ),
    )
    if not all(points_at(system, link, target) for link, target in mappings):
        return LaunchResult(
            False,
            None,
            "the prepared prefix lifetime mapping has drifted; run dcs-linux install",
        )
    proton = environment.layout.ge_proton_build(GE_PROTON_VERSION)
    if not system.exists(environment.layout.umu_run) or not system.exists(proton / "proton"):
        return LaunchResult(
            False,
            None,
            "the pinned runtime is incomplete; run dcs-linux install",
        )
    completed = runner.run(
        [str(environment.layout.umu_run), str(executable), NO_LAUNCHER],
        {
            "WINEPREFIX": str(prefix),
            "GAMEID": GAMEID,
            "PROTONPATH": str(proton),
            **LAUNCH_ENVIRONMENT,
        },
        LAUNCH_TIMEOUT,
        own_session=True,
    )
    if not completed.started:
        return LaunchResult(False, None, f"DCS did not start: {completed.detail}")
    return LaunchResult(
        True,
        completed.returncode,
        f"DCS closed with exit code {completed.returncode}",
    )
