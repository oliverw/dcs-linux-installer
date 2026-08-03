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

from dcs_linux.probes import REQUIRED_TOOLS, Environment

GIB = 1024**3

# DCS is 536 GB with 33 modules; the base game alone needs well over 100 GB.
REQUIRED_FREE_BYTES = 120 * GIB
RECOMMENDED_FREE_BYTES = 250 * GIB

# Below these, Proton support for a current DCS is unreliable.
MINIMUM_NVIDIA_DRIVER = "535"
MINIMUM_MESA_VERSION = "23.1"

# Only these give reflink, which is what makes a gold snapshot free.
REFLINK_FILESYSTEMS = frozenset({"btrfs", "xfs"})


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
        check_ge_proton,
        check_external_tools,
        check_disk_space,
        check_reflink_filesystem,
        check_upscaling,
        check_segoe_fonts,
        check_d3dcompiler,
        check_saved_games_mapping,
        check_game_location,
    )
    return [check(environment) for check in checks]


def has_blocking_failure(results: list[CheckResult]) -> bool:
    return any(result.is_blocking for result in results)


def check_distro(environment: Environment) -> CheckResult:
    """Report the distro. Immutability is a fact to state, not a fault."""
    distro = environment.distro
    if distro.id == "unknown":
        return CheckResult(
            name="Distro",
            status=Status.WARN,
            detail="could not read /etc/os-release",
            remediation="remediation below falls back to generic advice",
        )
    filesystem = "immutable filesystem" if distro.is_immutable else "mutable filesystem"
    return CheckResult(
        name="Distro",
        status=Status.PASS,
        detail=f"{distro.name} ({filesystem})",
    )


def check_gpu(environment: Environment) -> CheckResult:
    """A supported GPU with a driver new enough for current Proton."""
    if not environment.gpus:
        return CheckResult(
            name="GPU",
            status=Status.FAIL,
            detail="no AMD, Intel or NVIDIA GPU found",
            remediation="DCS needs a discrete-class GPU with a working kernel driver",
        )

    gpu = environment.gpus[0]
    driver = gpu.kernel_driver or "unknown driver"

    if gpu.driver_version is None:
        if gpu.vendor == "NVIDIA":
            return CheckResult(
                name="GPU",
                status=Status.WARN,
                detail=f"{gpu.vendor} ({driver}), driver version unknown",
                remediation="the proprietary NVIDIA driver does not appear to be loaded",
            )
        return CheckResult(
            name="GPU",
            status=Status.WARN,
            detail=f"{gpu.vendor} ({driver}), Mesa version unknown",
            remediation=environment.distro.install_hint("glxinfo"),
        )

    minimum = MINIMUM_NVIDIA_DRIVER if gpu.vendor == "NVIDIA" else MINIMUM_MESA_VERSION
    detail = f"{gpu.vendor} ({driver}) {gpu.driver_version}"
    if version_below(gpu.driver_version, minimum):
        return CheckResult(
            name="GPU",
            status=Status.WARN,
            detail=f"{detail}, older than the tested {minimum}",
            remediation="update the graphics driver through your distro's usual channel",
        )
    return CheckResult(name="GPU", status=Status.PASS, detail=detail)


def check_umu(environment: Environment) -> CheckResult:
    """umu-launcher, installed as a zipapp (ADR-0003)."""
    umu = environment.umu
    hint = "dcs-linux install fetches the umu zipapp into the toolchain directory"

    if umu.path is None:
        return CheckResult(
            name="umu-launcher",
            status=Status.FAIL,
            detail="not found",
            # umu is not on PyPI, so pip/uv cannot install it (ADR-0003).
            remediation=hint,
        )
    if not umu.usable:
        return CheckResult(
            name="umu-launcher",
            status=Status.FAIL,
            detail=f"found at {umu.path} but it does not run",
            remediation=f"delete {umu.path.parent} and re-fetch it; {hint}",
        )
    return CheckResult(
        name="umu-launcher",
        status=Status.PASS,
        detail=f"{umu.version or 'present'} at {umu.path}",
    )


def check_ge_proton(environment: Environment) -> CheckResult:
    versions = environment.ge_proton_versions
    if not versions:
        return CheckResult(
            name="GE-Proton",
            status=Status.FAIL,
            detail=f"none unpacked in {environment.layout.ge_proton}",
            remediation="dcs-linux install fetches the pinned x86_64 GE-Proton build",
        )
    return CheckResult(name="GE-Proton", status=Status.PASS, detail=", ".join(versions))


def check_external_tools(environment: Environment) -> CheckResult:
    missing = environment.missing_tools
    if not missing:
        return CheckResult(
            name="External tools",
            status=Status.PASS,
            detail=", ".join(REQUIRED_TOOLS),
        )
    return CheckResult(
        name="External tools",
        status=Status.FAIL,
        detail=f"missing: {', '.join(missing)}",
        remediation=environment.distro.install_hint(*missing),
    )


def check_disk_space(environment: Environment) -> CheckResult:
    disk = environment.disk
    game = environment.layout.game
    if disk is None:
        return CheckResult(
            name="Disk space",
            status=Status.WARN,
            detail=f"could not read free space for {game}",
        )

    free = f"{disk.free / GIB:.0f} GiB free at {game}"
    if disk.free < REQUIRED_FREE_BYTES:
        return CheckResult(
            name="Disk space",
            status=Status.FAIL,
            detail=f"{free}, below the {REQUIRED_FREE_BYTES // GIB} GiB a base install needs",
            remediation=f"free up space, or point {game.name} at a larger drive with "
            "DCS_LINUX_ROOT",
        )
    if disk.free < RECOMMENDED_FREE_BYTES:
        return CheckResult(
            name="Disk space",
            status=Status.WARN,
            detail=f"{free}; enough to start, but modules add up fast (536 GB with 33 modules)",
            remediation=f"{RECOMMENDED_FREE_BYTES // GIB} GiB is a comfortable headroom",
        )
    return CheckResult(name="Disk space", status=Status.PASS, detail=free)


def check_reflink_filesystem(environment: Environment) -> CheckResult:
    """btrfs/xfs make a gold snapshot free; other filesystems still work."""
    filesystem = environment.filesystem
    if filesystem is None:
        return CheckResult(
            name="Filesystem",
            status=Status.WARN,
            detail=f"could not determine the filesystem for {environment.layout.game}",
        )
    if filesystem in REFLINK_FILESYSTEMS:
        return CheckResult(
            name="Filesystem",
            status=Status.PASS,
            detail=f"{filesystem} (reflink snapshots available)",
        )
    return CheckResult(
        name="Filesystem",
        status=Status.WARN,
        detail=f"{filesystem} has no reflink, so snapshots cost a full copy",
        remediation="optional: put the game directory on btrfs or xfs",
    )


def check_upscaling(environment: Environment) -> CheckResult:
    """DLSS upscaling flickers violently under Proton and logs nothing.

    The highest-value check here: it presents exactly like a broken install,
    and users blame wine for it.
    """
    upscaling = environment.install.upscaling
    if upscaling is None:
        return CheckResult(
            name="Upscaling",
            status=Status.SKIP,
            detail="no options.lua yet",
        )
    if upscaling.upper() == "DLSS":
        return CheckResult(
            name="Upscaling",
            status=Status.FAIL,
            detail="DLSS is enabled; it flickers violently under Proton and logs nothing",
            remediation="in DCS: Options -> Graphics -> Upscaling -> OFF "
            "(change it in-game; DCS rewrites options.lua on exit)",
        )
    return CheckResult(name="Upscaling", status=Status.PASS, detail=upscaling)


def check_segoe_fonts(environment: Environment) -> CheckResult:
    """Without a Segoe stand-in the AH-64D crashes entering a mission.

    `winetricks corefonts` installs 42 fonts and none of them are Segoe, so
    having run corefonts is not sufficient.
    """
    install = environment.install
    if not install.prefix_exists:
        return CheckResult(name="Segoe fonts", status=Status.SKIP, detail="no prefix yet")
    missing = install.missing_segoe_fonts
    if missing:
        return CheckResult(
            name="Segoe fonts",
            status=Status.FAIL,
            detail=f"missing from the prefix: {', '.join(missing)}; the AH-64D crashes "
            "entering a mission",
            remediation="dcs-linux patch copies a local sans font in under the Segoe "
            "names (integrity-check safe: prefix only)",
        )
    return CheckResult(name="Segoe fonts", status=Status.PASS, detail="present in the prefix")


def check_d3dcompiler(environment: Environment) -> CheckResult:
    if not environment.install.prefix_exists:
        return CheckResult(name="d3dcompiler_47", status=Status.SKIP, detail="no prefix yet")
    if not environment.install.d3dcompiler_installed:
        return CheckResult(
            name="d3dcompiler_47",
            status=Status.FAIL,
            detail="not installed in the prefix; fx_5_0 shader compiles fail",
            remediation="umu-run winetricks d3dcompiler_47",
        )
    return CheckResult(name="d3dcompiler_47", status=Status.PASS, detail="installed in the prefix")


def check_saved_games_mapping(environment: Environment) -> CheckResult:
    """Saved games must live outside the disposable prefix (ADR-0001)."""
    install = environment.install
    if not install.prefix_exists:
        return CheckResult(name="Saved Games mapping", status=Status.SKIP, detail="no prefix yet")
    if not install.saved_games_mapped:
        return CheckResult(
            name="Saved Games mapping",
            status=Status.FAIL,
            detail="Saved Games sits inside the prefix, so rebuilding the prefix would "
            "destroy the ED login and keybinds",
            remediation=f"symlink it out: ln -sfn {environment.layout.saved_games} "
            f'"{environment.layout.prefix_saved_games}"',
        )
    return CheckResult(
        name="Saved Games mapping",
        status=Status.PASS,
        detail=f"mapped to {environment.layout.saved_games}",
    )


def check_game_location(environment: Environment) -> CheckResult:
    """A game directory under drive_c dies on the next prefix rebuild."""
    install = environment.install
    if not install.prefix_exists:
        return CheckResult(name="Game location", status=Status.SKIP, detail="no prefix yet")
    if install.game_under_drive_c:
        return CheckResult(
            name="Game location",
            status=Status.FAIL,
            detail="DCS is installed under drive_c, inside the disposable prefix",
            remediation=f"move the install to {environment.layout.game} and map it as D: "
            "— rebuilding the prefix would otherwise delete it",
        )
    if not install.game_exists:
        return CheckResult(
            name="Game location",
            status=Status.SKIP,
            detail="no game directory yet",
        )
    return CheckResult(
        name="Game location",
        status=Status.PASS,
        detail=f"{environment.layout.game} (outside the prefix)",
    )


def version_below(version: str, minimum: str) -> bool:
    """Compare dotted numeric versions, ignoring any trailing suffix."""
    return _numeric_parts(version) < _numeric_parts(minimum)


def _numeric_parts(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version)[:3])
