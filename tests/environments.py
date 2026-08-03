"""Fixture machines the check tests are written against."""

from dataclasses import replace
from pathlib import Path

from dcs_linux.checks import GIB
from dcs_linux.distro import Distro, Family, Immutability
from dcs_linux.installs import DcsInstall, Launcher
from dcs_linux.paths import Layout
from dcs_linux.probes import Environment, Gpu, InstallState, Target, Umu
from dcs_linux.system import DiskUsage

LAYOUT = Layout(root=Path("/data/dcs"), toolchain=Path("/data/toolchain"))

OWN_INSTALL = DcsInstall(
    game=LAYOUT.game / "DCS World",
    launcher=Launcher.DCS_LINUX,
    prefix=LAYOUT.prefix,
    runtime="GE-Proton11-3",
    version="2.9.28.26385",
)

TARGET = Target(
    game=OWN_INSTALL.game,
    prefix=LAYOUT.prefix,
    saved_games=LAYOUT.saved_games,
    prefix_saved_games=LAYOUT.prefix / "drive_c" / "users" / "steamuser" / "Saved Games",
)

BARE_TARGET = replace(TARGET, game=LAYOUT.game)

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


def healthy_environment(**overrides: object) -> Environment:
    """A machine that passes everything, as the baseline to break."""
    base = Environment(
        layout=LAYOUT,
        distro=FEDORA,
        target=TARGET,
        gpus=(Gpu(vendor="NVIDIA", kernel_driver="nvidia", driver_version="610.43.03"),),
        umu=Umu(path=LAYOUT.umu_run, usable=True, version="1.4.4"),
        proton_builds=("GE-Proton11-3",),
        missing_tools=(),
        disk=DiskUsage(total=2000 * GIB, free=600 * GIB),
        filesystem="btrfs",
        installs=(OWN_INSTALL,),
        selected=OWN_INSTALL,
        install=InstallState(
            prefix_exists=True,
            missing_segoe_fonts=(),
            d3dcompiler_installed=True,
            saved_games_mapped=True,
            upscaling="OFF",
        ),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def bare_environment(**overrides: object) -> Environment:
    """A machine with no DCS at all — the first thing a new user runs."""
    defaults: dict[str, object] = {
        "umu": Umu(path=None, usable=False, version=None),
        "proton_builds": (),
        "target": BARE_TARGET,
        "installs": (),
        "selected": None,
        "install": InstallState(),
    }
    return healthy_environment(**{**defaults, **overrides})
