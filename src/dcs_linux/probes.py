"""Gather the machine's facts. No judgements are made here.

Probing and judging are kept apart so the checks in `dcs_linux.checks` are
pure functions over an `Environment` and can be tested against fixtures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from dcs_linux.distro import Distro, detect_distro
from dcs_linux.paths import Layout, resolve_layout
from dcs_linux.system import DiskUsage, System

DRM_ROOT = Path("/sys/class/drm")
NVIDIA_MODULE_VERSION = Path("/sys/module/nvidia/version")
NVIDIA_PROC_VERSION = Path("/proc/driver/nvidia/version")

PCI_VENDORS = {
    "0x10de": "NVIDIA",
    "0x1002": "AMD",
    "0x8086": "Intel",
}

# Tools later commands shell out to. umu's Steam runtime needs bubblewrap;
# the toolchain is fetched with curl and unpacked with tar (ADR-0003).
REQUIRED_TOOLS = ("curl", "tar", "bwrap")

# The Segoe names the AH-64D asks for. corefonts ships 42 fonts and none of
# them are Segoe, so "corefonts is installed" proves nothing here.
SEGOE_FONTS = ("segoeui.ttf", "seguisb.ttf", "seguisym.ttf")

D3DCOMPILER = "d3dcompiler_47"


@dataclass(frozen=True)
class Gpu:
    """One graphics device, as the kernel describes it."""

    vendor: str
    kernel_driver: str | None
    driver_version: str | None


@dataclass(frozen=True)
class Umu:
    """State of the umu zipapp (ADR-0003)."""

    path: Path | None
    usable: bool
    version: str | None


@dataclass(frozen=True)
class InstallState:
    """What, if anything, is already installed.

    Every field is optional-by-absence: with no DCS installed the directories
    simply do not exist, and the checks that depend on them are skipped.
    """

    prefix_exists: bool = False
    game_exists: bool = False
    missing_segoe_fonts: tuple[str, ...] = ()
    d3dcompiler_installed: bool = False
    saved_games_mapped: bool = False
    game_under_drive_c: bool = False
    upscaling: str | None = None


@dataclass(frozen=True)
class Environment:
    """Everything `check` reports on."""

    layout: Layout
    distro: Distro
    gpus: tuple[Gpu, ...] = ()
    umu: Umu = Umu(path=None, usable=False, version=None)
    proton_builds: tuple[str, ...] = ()
    missing_tools: tuple[str, ...] = ()
    disk: DiskUsage | None = None
    filesystem: str | None = None
    install: InstallState = field(default_factory=InstallState)


def probe(system: System) -> Environment:
    """Read the whole machine."""
    layout = resolve_layout(system)
    return Environment(
        layout=layout,
        distro=detect_distro(system),
        gpus=probe_gpus(system),
        umu=probe_umu(system, layout),
        proton_builds=probe_proton_builds(system, layout),
        missing_tools=probe_missing_tools(system),
        disk=system.disk_usage(layout.game),
        filesystem=system.filesystem_type(layout.game),
        install=probe_install(system, layout),
    )


def probe_gpus(system: System) -> tuple[Gpu, ...]:
    """Every rendering GPU the kernel exposes under /sys/class/drm."""
    gpus: list[Gpu] = []
    for name in system.list_dir(DRM_ROOT):
        # cardN is a device; cardN-HDMI-A-1 is one of its connectors.
        if not re.fullmatch(r"card\d+", name):
            continue
        device = DRM_ROOT / name / "device"
        vendor_id = (system.read_text(device / "vendor") or "").strip().lower()
        vendor = PCI_VENDORS.get(vendor_id)
        if vendor is None:
            continue
        driver = _uevent_field(system.read_text(device / "uevent"), "DRIVER")
        gpus.append(
            Gpu(
                vendor=vendor,
                kernel_driver=driver,
                driver_version=_driver_version(system, vendor),
            )
        )
    # On a hybrid machine card0 is usually the integrated GPU, but DCS renders
    # on the discrete one, so that is the card the report must lead with.
    return tuple(sorted(gpus, key=lambda gpu: gpu.vendor == "Intel"))


def _uevent_field(text: str | None, key: str) -> str | None:
    for line in (text or "").splitlines():
        name, _, value = line.partition("=")
        if name == key and value:
            return value
    return None


def _driver_version(system: System, vendor: str) -> str | None:
    if vendor == "NVIDIA":
        return _nvidia_version(system)
    return _mesa_version(system)


def _nvidia_version(system: System) -> str | None:
    module = system.read_text(NVIDIA_MODULE_VERSION)
    if module and module.strip():
        return module.strip()
    proc = system.read_text(NVIDIA_PROC_VERSION) or ""
    match = re.search(r"Kernel Module\s+(\d[\d.]*)", proc)
    return match.group(1) if match else None


def _mesa_version(system: System) -> str | None:
    """Mesa's version, if glxinfo is available to ask.

    There is no file to read this from, so an absent glxinfo means the version
    is unknown — which the checks report as unknown rather than as a fault.
    """
    if system.which("glxinfo") is None:
        return None
    result = system.run(["glxinfo", "-B"])
    if result is None or result.returncode != 0:
        return None
    match = re.search(r"Mesa\s+(\d[\w.\-]*)", result.stdout)
    return match.group(1) if match else None


def probe_umu(system: System, layout: Layout) -> Umu:
    """Find umu-run: the toolchain zipapp first, then PATH."""
    path: Path | None = None
    if system.exists(layout.umu_run):
        path = layout.umu_run
    else:
        on_path = system.which("umu-run")
        if on_path is not None:
            path = Path(on_path)

    if path is None:
        return Umu(path=None, usable=False, version=None)

    result = system.run([str(path), "--version"])
    if result is None:
        # Could not be executed at all: not executable, or the wrong
        # architecture. A non-zero exit only means this build does not answer
        # --version, which is not a reason to call a present umu broken.
        return Umu(path=path, usable=False, version=None)
    version = result.stdout.strip() if result.returncode == 0 else ""
    return Umu(path=path, usable=True, version=version or None)


def probe_proton_builds(system: System, layout: Layout) -> tuple[str, ...]:
    """Proton builds already unpacked, ours or Steam's."""
    found: list[str] = []
    for directory in layout.proton_search_path(system.home()):
        for name in system.list_dir(directory):
            if name not in found and system.exists(directory / name / "proton"):
                found.append(name)
    return tuple(sorted(found))


def probe_missing_tools(system: System) -> tuple[str, ...]:
    return tuple(tool for tool in REQUIRED_TOOLS if system.which(tool) is None)


def probe_install(system: System, layout: Layout) -> InstallState:
    """Read the state of an existing install, if there is one."""
    prefix_exists = system.exists(layout.prefix)
    fonts = set(system.list_dir(layout.prefix_fonts))

    return InstallState(
        prefix_exists=prefix_exists,
        game_exists=system.exists(layout.game),
        missing_segoe_fonts=tuple(
            name for name in SEGOE_FONTS if name.lower() not in {f.lower() for f in fonts}
        ),
        d3dcompiler_installed=has_dll_override(system.read_text(layout.user_reg), D3DCOMPILER),
        saved_games_mapped=system.is_symlink(layout.prefix_saved_games),
        game_under_drive_c=_game_under_drive_c(system, layout),
        upscaling=read_upscaling(_read_options_lua(system, layout)),
    )


def _read_options_lua(system: System, layout: Layout) -> str | None:
    for candidate in layout.options_lua_candidates:
        text = system.read_text(candidate)
        if text is not None:
            return text
    return None


def _game_under_drive_c(system: System, layout: Layout) -> bool:
    """A DCS install inside the prefix dies on the next prefix rebuild."""
    drive_c = layout.prefix / "drive_c"
    for candidate in ("Program Files/Eagle Dynamics/DCS World", "DCS World", "Games/DCS World"):
        if system.exists(drive_c / candidate):
            return True
    return False


def has_dll_override(user_reg: str | None, dll: str) -> bool:
    """Whether the prefix overrides `dll` to the native implementation.

    The file's presence proves nothing: wine's default prefix already ships
    `d3dcompiler_47.dll` as a builtin stub. What `winetricks d3dcompiler_47`
    actually changes is the DllOverrides entry in `user.reg`, so that is what
    distinguishes a patched prefix from a fresh one.
    """
    if user_reg is None:
        return False
    match = re.search(
        rf'^"{re.escape(dll)}"\s*=\s*"([^"]*)"',
        user_reg,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    return match is not None and "native" in match.group(1).lower()


def read_upscaling(options_lua: str | None) -> str | None:
    """The `["Upscaling"]` value from options.lua, or None if not set."""
    if options_lua is None:
        return None
    match = re.search(r'\[\s*"Upscaling"\s*\]\s*=\s*"([^"]*)"', options_lua)
    return match.group(1) if match else None
