"""Fixture machines the check tests are written against."""

from dataclasses import replace
from pathlib import Path

from dcs_linux.checks import GIB
from dcs_linux.distro import Distro, Family, Immutability
from dcs_linux.headtracking import HeadTracking, Tracker
from dcs_linux.installs import DcsInstall, Launcher
from dcs_linux.patches import REGISTRY, PatchState, PatchStatus, unknown_states
from dcs_linux.paths import Layout, TargetPaths
from dcs_linux.probes import Environment, Gpu, InstallState, Umu
from dcs_linux.system import DiskUsage

LAYOUT = Layout(
    root=Path("/data/dcs"),
    toolchain=Path("/data/toolchain"),
    state=Path("/data/state"),
)

OWN_INSTALL = DcsInstall(
    game=LAYOUT.game / "DCS World",
    launcher=Launcher.DCS_LINUX,
    prefix=LAYOUT.prefix,
    runtime="GE-Proton11-3",
    version="2.9.28.26385",
)

PATHS = TargetPaths(
    game=OWN_INSTALL.game,
    prefix=LAYOUT.prefix,
    saved_games=LAYOUT.saved_games,
    prefix_saved_games=LAYOUT.prefix / "drive_c" / "users" / "steamuser" / "Saved Games",
)

BARE_PATHS = replace(PATHS, game=LAYOUT.game)

FEDORA = Distro(
    id="fedora",
    name="Fedora Linux 44",
    version="44",
    family=Family.FEDORA,
    immutability=Immutability.MUTABLE,
)
BAZZITE = replace(FEDORA, id="bazzite", name="Bazzite 42", immutability=Immutability.OSTREE)
STEAMOS = Distro(
    id="steamos",
    name="SteamOS 3.6.21",
    version="3.6.21",
    family=Family.ARCH,
    immutability=Immutability.READ_ONLY,
)


TRACKER = Tracker(name="TrackIR 5", node=Path("/dev/bus/usb/001/007"), accessible=True)


def with_tracker(
    *, access: bool = True, rule: Path | None = None, **overrides: object
) -> HeadTracking:
    """A machine with a TrackIR plugged in, which most machines are not.

    `flatpak` defaults to present because the remediation branches on it, and
    the interesting branch is the one where opentrack is missing rather than
    the one where the tooling to install it is.
    """
    base = HeadTracking(
        trackers=(replace(TRACKER, accessible=access),),
        udev_rule=rule,
        flatpak=True,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def patch_states(status: PatchStatus, detail: str) -> tuple[PatchState, ...]:
    """Every IC-safe patch in one standing, as `probe_patches` would report.

    The risky ones are left not-applied: a healthy install is an *unmodified*
    one, and a fixture that quietly carried a hashed-file edit would make the
    integrity-check row warn on every test that builds on it.
    """
    return tuple(
        PatchState(
            patch=patch,
            status=status if not patch.ic_risk else PatchStatus.NOT_APPLIED,
            detail=detail if not patch.ic_risk else "not applied",
        )
        for patch in REGISTRY
    )


def healthy_environment(**overrides: object) -> Environment:
    """A machine that passes everything, as the baseline to break."""
    base = Environment(
        layout=LAYOUT,
        distro=FEDORA,
        paths=PATHS,
        gpus=(Gpu(vendor="NVIDIA", kernel_driver="nvidia", driver_version="610.43.03"),),
        umu=Umu(path=LAYOUT.umu_run, usable=True, version="1.4.4"),
        proton_builds=("GE-Proton11-3",),
        missing_tools=(),
        disk=DiskUsage(total=2000 * GIB, free=600 * GIB),
        filesystem="btrfs",
        installs=(OWN_INSTALL,),
        targeted=OWN_INSTALL,
        install_state=InstallState(
            prefix_exists=True,
            missing_segoe_fonts=(),
            d3dcompiler_installed=True,
            saved_games_mapped=True,
            saved_games_target=LAYOUT.saved_games,
            upscaling="OFF",
        ),
        patches=patch_states(PatchStatus.APPLIED, "applied to DCS 2.9.28.26385"),
        # No tracker: the common case, and the one a quiet table depends on.
        head_tracking=HeadTracking(flatpak=True),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def bare_environment(**overrides: object) -> Environment:
    """A machine with no DCS at all — the first thing a new user runs."""
    defaults: dict[str, object] = {
        "umu": Umu(path=None, usable=False, version=None),
        "proton_builds": (),
        "paths": BARE_PATHS,
        "installs": (),
        "targeted": None,
        "install_state": InstallState(),
        "patches": unknown_states(),
    }
    return healthy_environment(**{**defaults, **overrides})
