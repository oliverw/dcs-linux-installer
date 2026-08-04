"""The rules `check` applies, as pure functions over a probed `Environment`.

Nothing here touches the machine. Everything here is testable from fixtures.

Only rules with an empirical basis are included — see `CONTEXT.md` for what
was verified on hardware, and in particular for the log signatures that look
fatal but are not, which `check` deliberately does not flag.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dcs_linux.installs import Launcher
from dcs_linux.patches import PatchStatus, risky_in_place
from dcs_linux.probes import REQUIRED_TOOLS, Environment

GIB = 1024**3

# DCS is 536 GB with 33 modules; the base game alone needs well over 100 GB.
REQUIRED_FREE_BYTES = 120 * GIB
RECOMMENDED_FREE_BYTES = 250 * GIB

# Only these give reflink, which is what makes a gold snapshot free.
REFLINK_FILESYSTEMS = frozenset({"btrfs", "xfs"})

# Check names, bound once. `CheckResult.key` derives the --json key from the
# name, so a name that drifted between branches would rename a documented key.
DISTRO = "Distro"
GPU = "GPU"
UMU = "umu-launcher"
PROTON_BUILDS = "Proton builds"
EXTERNAL_TOOLS = "External tools"
DISK_SPACE = "Disk space"
FILESYSTEM = "Filesystem"
UPSCALING = "Upscaling"
SEGOE_FONTS = "Segoe fonts"
D3DCOMPILER = "d3dcompiler_47"
SAVED_GAMES_MAPPING = "Saved Games mapping"
GAME_LOCATION = "Game location"
PATCHES = "Patches"
INTEGRITY_CHECK = "Integrity check"


class Status(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class CheckResult:
    """One row of the report."""

    name: str
    status: Status
    detail: str
    remediation: str | None = None

    @property
    def is_blocking(self) -> bool:
        return self.status is Status.FAIL

    @property
    def key(self) -> str:
        """Stable identifier for `--json` consumers.

        Derived from the display name, which therefore counts as part of the
        JSON contract: renaming a check renames its key.
        """
        return re.sub(r"[^a-z0-9]+", "_", self.name.lower()).strip("_")


def run_checks(environment: Environment) -> list[CheckResult]:
    """Every check, in report order."""
    checks: tuple[Callable[[Environment], CheckResult], ...] = (
        check_distro,
        check_gpu,
        check_umu,
        check_proton_builds,
        check_external_tools,
        check_disk_space,
        check_reflink_filesystem,
        check_upscaling,
        check_segoe_fonts,
        check_d3dcompiler,
        check_saved_games_mapping,
        check_game_location,
        check_patches,
        check_integrity,
    )
    return [check(environment) for check in checks]


def has_blocking_failure(results: list[CheckResult]) -> bool:
    return any(result.is_blocking for result in results)


def check_distro(environment: Environment) -> CheckResult:
    """Report the distro. Immutability is a fact to state, not a fault."""
    distro = environment.distro
    if distro.id == "unknown":
        return CheckResult(
            name=DISTRO,
            status=Status.WARN,
            detail="could not read /etc/os-release",
            remediation="remediation below falls back to generic advice",
        )
    # "immutable base system", never "immutable filesystem" — the Filesystem
    # row below means something else entirely (btrfs/xfs and reflink).
    base = "immutable base system" if distro.is_immutable else "mutable base system"
    return CheckResult(
        name=DISTRO,
        status=Status.PASS,
        detail=f"{distro.name} ({base})",
    )


def check_gpu(environment: Environment) -> CheckResult:
    """The GPU DCS will render on, and the version of its driver.

    There is no minimum version here on purpose: only one driver has ever been
    verified against DCS (610.43.03, see `CONTEXT.md`), and one data point is
    not a threshold. Reporting the version lets a bug report do that job.
    """
    if not environment.gpus:
        return CheckResult(
            name=GPU,
            status=Status.FAIL,
            detail="no AMD, Intel or NVIDIA GPU found",
            remediation="DCS needs a GPU with a working kernel driver; check `lspci` "
            "and whether the driver module is loaded",
        )

    gpu = environment.gpus[0]
    driver = gpu.kernel_driver or "unknown driver"
    others = ", ".join(f"also {other.vendor}" for other in environment.gpus[1:])
    suffix = f" ({others})" if others else ""

    if gpu.driver_version is None:
        if gpu.vendor == "NVIDIA":
            return CheckResult(
                name=GPU,
                status=Status.WARN,
                detail=f"NVIDIA ({driver}), driver version unknown{suffix}",
                remediation="the proprietary NVIDIA driver does not appear to be loaded",
            )
        return CheckResult(
            name=GPU,
            status=Status.WARN,
            detail=f"{gpu.vendor} ({driver}), Mesa version unknown{suffix}",
            remediation=environment.distro.install_hint("glxinfo"),
        )

    return CheckResult(
        name=GPU,
        status=Status.PASS,
        detail=f"{gpu.vendor} ({driver}) {gpu.driver_version}{suffix}",
    )


def check_umu(environment: Environment) -> CheckResult:
    """umu-launcher, installed as a zipapp (ADR-0003)."""
    umu = environment.umu
    hint = "dcs-linux install fetches the umu zipapp into the toolchain directory"

    if umu.path is None:
        return CheckResult(
            name=UMU,
            status=Status.FAIL,
            detail="not found",
            # umu is not on PyPI, so pip/uv cannot install it (ADR-0003).
            remediation=hint,
        )
    if not umu.usable:
        return CheckResult(
            name=UMU,
            status=Status.FAIL,
            detail=f"found at {umu.path} but it does not run",
            remediation=f"delete {umu.path.parent} and re-fetch it; {hint}",
        )
    return CheckResult(
        name=UMU,
        status=Status.PASS,
        detail=f"{umu.version or 'present'} at {umu.path}",
    )


def check_proton_builds(environment: Environment) -> CheckResult:
    """Proton builds available to run DCS with, GE-Proton or otherwise.

    Steam's compatibilitytools.d holds more than GE-Proton, and anything
    unpacked there is equally usable, so the row reports what is actually on
    the machine rather than only the builds we would have fetched ourselves.
    """
    versions = environment.proton_builds
    if not versions:
        return CheckResult(
            name=PROTON_BUILDS,
            status=Status.FAIL,
            detail="none found in the toolchain or in Steam's compatibilitytools.d",
            remediation="dcs-linux install fetches the pinned x86_64 GE-Proton build",
        )
    return CheckResult(name=PROTON_BUILDS, status=Status.PASS, detail=", ".join(versions))


def check_external_tools(environment: Environment) -> CheckResult:
    missing = environment.missing_tools
    if not missing:
        return CheckResult(
            name=EXTERNAL_TOOLS,
            status=Status.PASS,
            detail=", ".join(REQUIRED_TOOLS),
        )
    return CheckResult(
        name=EXTERNAL_TOOLS,
        status=Status.FAIL,
        detail=f"missing: {', '.join(missing)}",
        remediation=environment.distro.install_hint(*missing),
    )


def check_disk_space(environment: Environment) -> CheckResult:
    disk = environment.disk
    game = environment.paths.game
    if disk is None:
        return CheckResult(
            name=DISK_SPACE,
            status=Status.WARN,
            detail=f"could not read free space for {game}",
        )

    free = f"{disk.free / GIB:.0f} of {disk.total / GIB:.0f} GiB free at {game}"
    if disk.free < REQUIRED_FREE_BYTES:
        return CheckResult(
            name=DISK_SPACE,
            status=Status.FAIL,
            detail=f"{free}, below the {REQUIRED_FREE_BYTES // GIB} GiB a base install needs",
            remediation=_free_space_hint(environment),
        )
    if disk.free < RECOMMENDED_FREE_BYTES:
        return CheckResult(
            name=DISK_SPACE,
            status=Status.WARN,
            detail=f"{free}; enough to start, but modules add up fast (536 GB with 33 modules)",
            remediation=f"{RECOMMENDED_FREE_BYTES // GIB} GiB is a comfortable headroom",
        )
    return CheckResult(name=DISK_SPACE, status=Status.PASS, detail=free)


def _free_space_hint(environment: Environment) -> str:
    """Where to make room — only our own install can be pointed elsewhere."""
    targeted = environment.targeted
    if targeted is not None and targeted.launcher is not Launcher.DCS_LINUX:
        return f"free up space on the drive holding {targeted.game}"
    return "free up space, or point the game directory at a larger drive with DCS_LINUX_ROOT"


def check_reflink_filesystem(environment: Environment) -> CheckResult:
    """btrfs/xfs make a gold snapshot free; other filesystems still work."""
    filesystem = environment.filesystem
    if filesystem is None:
        return CheckResult(
            name=FILESYSTEM,
            status=Status.WARN,
            detail=f"could not determine the filesystem for {environment.paths.game}",
        )
    if filesystem in REFLINK_FILESYSTEMS:
        return CheckResult(
            name=FILESYSTEM,
            status=Status.PASS,
            detail=f"{filesystem} (reflink snapshots available)",
        )
    return CheckResult(
        name=FILESYSTEM,
        status=Status.WARN,
        detail=f"{filesystem} has no reflink, so snapshots cost a full copy",
        remediation="optional: put the game directory on btrfs or xfs",
    )


def check_upscaling(environment: Environment) -> CheckResult:
    """DLSS upscaling flickers violently under Proton and logs nothing.

    The highest-value check here: it presents exactly like a broken install,
    and users blame wine for it.
    """
    upscaling = environment.install_state.upscaling
    if upscaling is None:
        return CheckResult(
            name=UPSCALING,
            status=Status.SKIP,
            detail="no options.lua yet",
        )
    if upscaling.upper() == "DLSS":
        return CheckResult(
            name=UPSCALING,
            status=Status.FAIL,
            detail="DLSS is enabled; it flickers violently under Proton and logs nothing",
            remediation="in DCS: Options -> Graphics -> Upscaling -> OFF "
            "(change it in-game; DCS rewrites options.lua on exit)",
        )
    return CheckResult(name=UPSCALING, status=Status.PASS, detail=upscaling)


def _no_prefix_yet(name: str) -> CheckResult:
    """The row every prefix-dependent check shows before there is a prefix."""
    return CheckResult(name=name, status=Status.SKIP, detail="no prefix yet")


def check_segoe_fonts(environment: Environment) -> CheckResult:
    """Without a Segoe stand-in the AH-64D crashes entering a mission.

    `winetricks corefonts` installs 42 fonts and none of them are Segoe, so
    having run corefonts is not sufficient.
    """
    install = environment.install_state
    if not install.prefix_exists:
        return _no_prefix_yet(SEGOE_FONTS)
    missing = install.missing_segoe_fonts
    if missing:
        return CheckResult(
            name=SEGOE_FONTS,
            status=Status.FAIL,
            detail=f"missing from the prefix: {', '.join(missing)}; the AH-64D crashes "
            "entering a mission",
            remediation="dcs-linux patch copies a local sans font in under the Segoe "
            "names (integrity-check safe: prefix only)",
        )
    return CheckResult(name=SEGOE_FONTS, status=Status.PASS, detail="present in the prefix")


def check_d3dcompiler(environment: Environment) -> CheckResult:
    if not environment.install_state.prefix_exists:
        return _no_prefix_yet(D3DCOMPILER)
    if not environment.install_state.d3dcompiler_installed:
        return CheckResult(
            name=D3DCOMPILER,
            status=Status.FAIL,
            detail="not installed in the prefix; fx_5_0 shader compiles fail",
            remediation="umu-run winetricks d3dcompiler_47",
        )
    return CheckResult(name=D3DCOMPILER, status=Status.PASS, detail="installed in the prefix")


def check_saved_games_mapping(environment: Environment) -> CheckResult:
    """Saved games must live outside the disposable prefix (ADR-0001)."""
    install = environment.install_state
    if not install.prefix_exists:
        return _no_prefix_yet(SAVED_GAMES_MAPPING)
    if not install.saved_games_mapped:
        # Where to map it to is only ours to say for our own install; for
        # anyone else's, any durable directory outside the prefix will do.
        durable = environment.paths.saved_games or Path("/a/directory/outside/the/prefix")
        return CheckResult(
            name=SAVED_GAMES_MAPPING,
            status=Status.FAIL,
            detail="Saved Games sits inside the prefix, so rebuilding the prefix would "
            "destroy the ED login and keybinds",
            remediation=f"symlink it out: ln -sfn {durable} "
            f'"{environment.paths.prefix_saved_games}"',
        )
    return CheckResult(
        name=SAVED_GAMES_MAPPING,
        status=Status.PASS,
        detail=f"mapped to {install.saved_games_target}",
    )


def check_game_location(environment: Environment) -> CheckResult:
    """A game directory under drive_c dies on the next prefix rebuild."""
    install = environment.targeted
    if install is None:
        return CheckResult(
            name=GAME_LOCATION,
            status=Status.SKIP,
            detail=_nothing_selected(environment),
        )
    if install.under_prefix:
        return CheckResult(
            name=GAME_LOCATION,
            status=Status.FAIL,
            detail=f"{install.game} is inside the prefix at {install.prefix}",
            remediation=f"move the install out of the prefix and map it as D: "
            f"— rebuilding {install.prefix} would otherwise delete it",
        )
    return CheckResult(
        name=GAME_LOCATION,
        status=Status.PASS,
        detail=f"{install.game} (outside the prefix)",
    )


def check_patches(environment: Environment) -> CheckResult:
    """Whether the fixes this tool applied are still in place.

    A DCS update overwrites patched files, so a patch that was applied and is
    now gone is the normal way a working install stops working — and it looks
    to the user like DCS broke itself. Reported as a failure with a one-command
    fix, because that is exactly what it is.
    """
    states = [state for state in environment.patches if state.status is not PatchStatus.UNKNOWN]
    if not states:
        return CheckResult(name=PATCHES, status=Status.SKIP, detail=_nothing_selected(environment))

    drifted = [state for state in states if state.is_drifted]
    if drifted:
        return CheckResult(
            name=PATCHES,
            status=Status.FAIL,
            detail="undone by a DCS update: " + ", ".join(state.patch.id for state in drifted),
            remediation="dcs-linux patch apply",
        )

    applied = [state for state in states if state.status is PatchStatus.APPLIED]
    if not applied:
        return CheckResult(
            name=PATCHES,
            status=Status.SKIP,
            # No remediation: a SKIP is not a problem, and `render_table`
            # files every remediation under "fix the arrows above".
            detail=f"none applied, {len(states)} available",
        )
    return CheckResult(
        name=PATCHES,
        status=Status.PASS,
        detail=", ".join(state.patch.id for state in applied),
    )


def check_integrity(environment: Environment) -> CheckResult:
    """Whether this install currently carries modifications DCS hashes.

    Applying a risky patch is the user's own decision, so this is not a fault
    — but it is invisible until a server refuses to let them in, and by then
    nobody connects the refusal to a fix applied weeks earlier. Stated on
    every `check` so the answer to "why can't I join?" is already on screen.
    """
    states = [state for state in environment.patches if state.status is not PatchStatus.UNKNOWN]
    if not states:
        return CheckResult(
            name=INTEGRITY_CHECK, status=Status.SKIP, detail=_nothing_selected(environment)
        )

    risky = risky_in_place(tuple(states))
    if not risky:
        return CheckResult(
            name=INTEGRITY_CHECK,
            status=Status.PASS,
            detail="no integrity-check-risky patch applied; multiplayer servers are unaffected",
        )

    names = ", ".join(state.patch.id for state in risky)
    return CheckResult(
        name=INTEGRITY_CHECK,
        status=Status.WARN,
        detail=f"game files DCS hashes were modified by: {names}; servers running "
        "pure-client integrity checks will reject this install",
        remediation=f"dcs-linux patch revert {risky[0].patch.id}   # to play multiplayer again",
    )


def _nothing_selected(environment: Environment) -> str:
    """Why no install is being reported on: there are none, or there are several."""
    if not environment.installs:
        return "no DCS install found"
    return f"{len(environment.installs)} installs found; pass --install ID to choose one"
