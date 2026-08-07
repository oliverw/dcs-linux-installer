"""The rules `check` applies, as pure functions over a probed `Environment`.

Nothing here touches the machine. Everything here is testable from fixtures.

Only rules with an empirical basis are included — see `CONTEXT.md` for what
was verified on hardware, and in particular for the log signatures that look
fatal but are not, which `check` deliberately does not flag.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dcs_linux.headtracking import (
    FLATHUB_REMOTE,
    OPENTRACK_INSTALL,
    RULE_FILE,
    install_rule_command,
)
from dcs_linux.installs import Launcher
from dcs_linux.patches import SERVERS_REJECT, PatchState, PatchStatus, risky_in_place
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
HEAD_TRACKER = "Head tracker"
OPENTRACK = "opentrack"
DCS_HEAD_TRACKING = "Head tracking in DCS"


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
        *HEAD_TRACKING_CHECKS,
    )
    return [check(environment) for check in checks]


def has_blocking_failure(results: list[CheckResult]) -> bool:
    return any(result.is_blocking for result in results)


# What `install` must not proceed past. An allowlist, not "every failure":
# most of what `check` calls blocking on a fresh machine — no umu, no Proton
# build, no prefix, saved games unmapped — is precisely what `install` exists
# to fix, and refusing to run because of it would leave the tool unable to
# bootstrap itself. These four are the ones it cannot fix.
PREFLIGHT_CHECKS = (GPU, EXTERNAL_TOOLS, DISK_SPACE, GAME_LOCATION)


def blocking_preflight(results: Sequence[CheckResult]) -> list[CheckResult]:
    """The failures that stop an install before any long-running work starts.

    Disk space is in here on purpose: fetching a toolchain and unpacking Proton
    onto a full disk fails slowly, confusingly, and only after the user has
    already waited for it.
    """
    return [
        result
        for result in results
        if result.name in PREFLIGHT_CHECKS and result.status is Status.FAIL
    ]


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
    states = _inspectable(environment)
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
    states = _inspectable(environment)
    if not states:
        return CheckResult(
            name=INTEGRITY_CHECK, status=Status.SKIP, detail=_nothing_selected(environment)
        )

    risky = risky_in_place(states)
    if not risky:
        return CheckResult(
            name=INTEGRITY_CHECK,
            status=Status.PASS,
            detail="no IC-risky patch applied; multiplayer servers are unaffected",
        )

    names = ", ".join(state.patch.id for state in risky)
    return CheckResult(
        name=INTEGRITY_CHECK,
        status=Status.WARN,
        detail=f"game files DCS hashes were modified by: {names}; {SERVERS_REJECT}",
        remediation=f"dcs-linux patch revert {risky[0].patch.id}   # to play multiplayer again",
    )


def _inspectable(environment: Environment) -> tuple[PatchState, ...]:
    """The patch standings that describe a real install.

    With no install targeted there is nothing to have been applied *to*, so
    both patch rows skip rather than reporting an absence as a fact.
    """
    return tuple(state for state in environment.patches if state.status is not PatchStatus.UNKNOWN)


def _nothing_selected(environment: Environment) -> str:
    """Why no install is being reported on: there are none, or there are several."""
    if not environment.installs:
        return "no DCS install found"
    return f"{len(environment.installs)} installs found; pass --install ID to choose one"


def check_head_tracker(environment: Environment) -> CheckResult:
    """The connected tracker, and whether this user is allowed to open it.

    Never a failure: DCS flies without head tracking, and a blocking row here
    would stop `install` on a machine that is otherwise perfectly ready.
    """
    tracking = environment.head_tracking
    if not tracking.trackers:
        return CheckResult(
            name=HEAD_TRACKER,
            status=Status.SKIP,
            detail="no NaturalPoint or TrackIR device connected",
        )

    names = ", ".join(f"{tracker.name} at {tracker.node}" for tracker in tracking.trackers)
    rule = f"udev rule in {tracking.udev_rule}" if tracking.udev_rule else "no udev rule installed"
    blocked = tracking.inaccessible
    if not blocked:
        return CheckResult(
            name=HEAD_TRACKER,
            status=Status.PASS,
            detail=f"{names}; readable; {rule}",
        )

    return CheckResult(
        name=HEAD_TRACKER,
        status=Status.WARN,
        detail=f"{names}; {rule}, and this user cannot open the device, so nothing "
        "will move in the cockpit",
        remediation=_tracker_access_hint(tracking.udev_rule),
    )


RECONNECT = "   # then unplug and replug the tracker"


def _tracker_access_hint(rule: Path | None) -> str:
    """How to get access, which is not the same advice twice.

    Only *our* rule earns the shorter answer. It is correct by construction,
    so if it is in place and the device is still shut, nothing was wrong with
    the rule — udev applies rules when a device appears, and this one has not
    been applied yet.

    Any other rule is a rule that demonstrably is not working, and the usual
    reason is that it cannot: the recipes in circulation number theirs `99-`,
    which sets a `uaccess` tag after the only thing that reads it has run, or
    give it a group the user is not in. Withholding the working rule because
    a broken one exists would leave the user re-running a reload that can
    never help.

    Either way the tool prints the privileged command and runs none of it.
    """
    if rule == RULE_FILE:
        return (
            "the rule is installed but has not been applied: "
            f"sudo udevadm control --reload-rules && sudo udevadm trigger{RECONNECT}"
        )
    existing = f"{rule} does not grant access; install one that does: " if rule else ""
    return f"{existing}{install_rule_command()}{RECONNECT}"


def check_opentrack(environment: Environment) -> CheckResult:
    """opentrack, which is what turns a tracker into head movement in DCS."""
    tracking = environment.head_tracking
    if tracking.opentrack is not None:
        return CheckResult(name=OPENTRACK, status=Status.PASS, detail=tracking.opentrack)
    if not tracking.in_use:
        # Nothing on this machine says head tracking is wanted, so absence is
        # not a gap. A row that warns on every run teaches users to skim.
        return CheckResult(
            name=OPENTRACK,
            status=Status.SKIP,
            detail="not installed; only needed for head tracking",
        )
    return CheckResult(
        name=OPENTRACK,
        status=Status.WARN,
        detail="a head tracker is connected but opentrack is not installed, so DCS "
        "has nothing feeding it head movement",
        remediation=_opentrack_hint(environment),
    )


def _opentrack_hint(environment: Environment) -> str:
    """How to install opentrack on the machine reading this (ADR-0006).

    Flathub rather than a package manager: opentrack is in no mainstream
    distro's own repositories, and it is the one route that also works on the
    image-based bases. Flatpak itself is the part that varies, so that is the
    part the distro answers for.
    """
    install = f"{FLATHUB_REMOTE} && {OPENTRACK_INSTALL}"
    if environment.head_tracking.flatpak:
        return install
    if environment.distro.is_immutable:
        # flatpak is part of the image on every base like this, so a missing
        # one is a broken system rather than something to install. Asking
        # `install_hint` would answer "get it from a container", and a flatpak
        # inside a distrobox exports nothing to the host that could run DCS.
        return f"flatpak is missing from this system's image; once it is back: {install}"
    return f"{environment.distro.install_hint('flatpak')}, then {install}"


def check_dcs_head_tracking(environment: Environment) -> CheckResult:
    """Whether DCS can actually see the tracker, as far as that is knowable.

    DCS reaches a head tracker through NaturalPoint's client DLL, and finds it
    through a registry key in the prefix. The key is therefore the one part of
    the chain that is readable from disk — whether opentrack is running, and
    whether the user has bound the axes, is not.
    """
    tracking = environment.head_tracking
    if not tracking.in_use:
        return CheckResult(
            name=DCS_HEAD_TRACKING,
            status=Status.SKIP,
            detail="not in use",
        )
    if not environment.install_state.prefix_exists:
        return _no_prefix_yet(DCS_HEAD_TRACKING)
    if tracking.wine_bridge:
        return CheckResult(
            name=DCS_HEAD_TRACKING,
            status=Status.PASS,
            detail="the prefix points DCS at an NPClient bridge",
        )
    return CheckResult(
        name=DCS_HEAD_TRACKING,
        status=Status.WARN,
        detail="nothing in the prefix points DCS at an NPClient bridge, so head "
        "movement will not reach the cockpit",
        remediation=f"in opentrack set Output to 'Wine' and point it at {environment.paths.prefix}",
    )


# Head tracking, as its own tuple so the "these never block" rule is one thing
# to assert rather than three (#13).
HEAD_TRACKING_CHECKS: tuple[Callable[[Environment], CheckResult], ...] = (
    check_head_tracker,
    check_opentrack,
    check_dcs_head_tracking,
)
