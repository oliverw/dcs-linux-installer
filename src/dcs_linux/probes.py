"""Gather the machine's facts. No judgements are made here.

Probing and judging are kept apart so the checks in `dcs_linux.checks` are
pure functions over an `Environment` and can be tested against fixtures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from dcs_linux.distro import Distro, detect_distro
from dcs_linux.installs import DcsInstall, Launcher, default_install, select
from dcs_linux.launchers import discover
from dcs_linux.paths import Layout, resolve_layout
from dcs_linux.system import DiskUsage, System

DRM_ROOT = Path("/sys/class/drm")
KERNEL_RELEASE = Path("/proc/sys/kernel/osrelease")
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
class TargetPaths:
    """Where to read the targeted install's state.

    Discovery finds installs; these are the paths of the one being reported
    on. With no DCS anywhere they fall back to where this tool would put
    things, so a bare machine still has coherent paths to answer against.
    """

    game: Path
    prefix: Path
    prefix_saved_games: Path
    # Only known for an install of ours. Somebody else's saved games are
    # wherever they mapped them, which we cannot know (ADR-0007).
    saved_games: Path | None

    @property
    def fonts(self) -> Path:
        return self.prefix / "drive_c" / "windows" / "Fonts"

    @property
    def user_reg(self) -> Path:
        return self.prefix / "user.reg"

    @property
    def options_lua_candidates(self) -> tuple[Path, ...]:
        """Where this install's `options.lua` may be, mapped out or not.

        The in-prefix path comes first because it is the one this prefix
        actually reads — mapped out, it resolves to the durable copy anyway.
        Our own durable path is a fallback only for our own install: for
        anyone else's, it is a different install's settings entirely.
        """
        config = Path("DCS") / "Config" / "options.lua"
        durable = (self.saved_games / config,) if self.saved_games else ()
        return (self.prefix_saved_games / config, *durable)


@dataclass(frozen=True)
class InstallState:
    """The condition of the targeted install.

    Every field is optional-by-absence: with no DCS installed the directories
    simply do not exist, and the checks that depend on them are skipped.
    """

    prefix_exists: bool = False
    missing_segoe_fonts: tuple[str, ...] = ()
    d3dcompiler_installed: bool = False
    saved_games_mapped: bool = False
    saved_games_target: Path | None = None
    upscaling: str | None = None
    # The `["graphics"]` table from options.lua, verbatim. Read for `report`:
    # graphics settings are the first thing anyone asks a bug reporter for,
    # and the rest of options.lua is neither cheap nor non-sensitive.
    graphics_options: str | None = None


@dataclass(frozen=True)
class Environment:
    """Everything `check` reports on."""

    layout: Layout
    distro: Distro
    paths: TargetPaths
    kernel: str | None = None
    gpus: tuple[Gpu, ...] = ()
    umu: Umu = Umu(path=None, usable=False, version=None)
    proton_builds: tuple[str, ...] = ()
    missing_tools: tuple[str, ...] = ()
    disk: DiskUsage | None = None
    filesystem: str | None = None
    installs: tuple[DcsInstall, ...] = ()
    targeted: DcsInstall | None = None
    install_state: InstallState = field(default_factory=InstallState)


def probe(system: System, identifier: str | None = None) -> Environment:
    """Read the whole machine, reporting on the install `identifier` names.

    Raises `InstallNotFound` or `AmbiguousInstall` if it names none.
    """
    layout = resolve_layout(system)
    installs = discover(system, layout)
    targeted = select(installs, identifier) if identifier else default_install(installs)
    paths = target_paths(system, layout, targeted)
    return Environment(
        layout=layout,
        distro=detect_distro(system),
        paths=paths,
        kernel=probe_kernel(system),
        gpus=probe_gpus(system),
        umu=probe_umu(system, layout),
        proton_builds=probe_proton_builds(system, layout),
        missing_tools=probe_missing_tools(system),
        disk=system.disk_usage(paths.game),
        filesystem=system.filesystem_type(paths.game),
        installs=installs,
        targeted=targeted,
        install_state=probe_install(system, paths),
    )


def target_paths(system: System, layout: Layout, targeted: DcsInstall | None) -> TargetPaths:
    """The paths the install-dependent checks read."""
    ours = targeted is None or targeted.launcher is Launcher.DCS_LINUX
    game = targeted.game if targeted is not None else layout.game
    prefix = targeted.prefix if targeted is not None and targeted.prefix else layout.prefix
    return TargetPaths(
        game=game,
        prefix=prefix,
        saved_games=layout.saved_games if ours else None,
        prefix_saved_games=find_prefix_saved_games(system, prefix),
    )


def find_prefix_saved_games(system: System, prefix: Path) -> Path:
    """Where DCS puts `Saved Games` inside a given prefix.

    umu creates a `steamuser`, but a Lutris or Heroic prefix names the user
    directory after the actual user, so the name cannot be assumed. The
    steamuser path is still the answer when the prefix does not exist yet:
    it is where this tool's own prefix will put it.
    """
    users = prefix / "drive_c" / "users"
    # Public is wine's shared profile, never the one holding Saved Games.
    names = [name for name in system.list_dir(users) if name != "Public"]
    for name in ("steamuser", *names):
        candidate = users / name / "Saved Games"
        # A mapped-out Saved Games can be a symlink whose target is missing,
        # which is a state worth reporting rather than one to skip past.
        if system.exists(candidate) or system.is_symlink(candidate):
            return candidate
    return users / "steamuser" / "Saved Games"


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


def probe_kernel(system: System) -> str | None:
    """The running kernel release.

    Read from /proc rather than `uname` so it costs nothing and cannot fail
    on a machine where the checks are already in trouble.
    """
    release = system.read_text(KERNEL_RELEASE)
    return release.strip() or None if release else None


def probe_missing_tools(system: System) -> tuple[str, ...]:
    return tuple(tool for tool in REQUIRED_TOOLS if system.which(tool) is None)


def probe_install(system: System, paths: TargetPaths) -> InstallState:
    """Read the state of the targeted install, if there is one."""
    fonts = set(system.list_dir(paths.fonts))
    user_reg = system.read_text(paths.user_reg)
    mapped = system.is_symlink(paths.prefix_saved_games)
    options_lua = _read_options_lua(system, paths)

    return InstallState(
        prefix_exists=system.exists(paths.prefix),
        missing_segoe_fonts=tuple(
            name for name in SEGOE_FONTS if name.lower() not in {f.lower() for f in fonts}
        ),
        d3dcompiler_installed=has_dll_override(user_reg, D3DCOMPILER),
        saved_games_mapped=mapped,
        saved_games_target=system.resolve(paths.prefix_saved_games) if mapped else None,
        upscaling=read_upscaling(options_lua),
        graphics_options=read_graphics_block(options_lua),
    )


def _read_options_lua(system: System, paths: TargetPaths) -> str | None:
    for candidate in paths.options_lua_candidates:
        text = system.read_text(candidate)
        if text is not None:
            return text
    return None


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


def read_graphics_block(options_lua: str | None) -> str | None:
    """The `["graphics"]` table of options.lua, verbatim, braces and all.

    Matched by counting braces rather than by regex: the table nests, and a
    non-greedy match would stop at the first inner `}`. Everything outside
    the graphics table is left where it is — options.lua also holds plugin
    and account-adjacent settings that have no place in a public paste.
    """
    if options_lua is None:
        return None
    match = re.search(r'\[\s*"graphics"\s*\]\s*=\s*\{', options_lua)
    if match is None:
        return None

    depth = 0
    for index in range(match.end() - 1, len(options_lua)):
        character = options_lua[index]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return options_lua[match.start() : index + 1]
    # Truncated mid-write, or hand-edited into something unbalanced. Unknown
    # beats a half-table that reads as complete.
    return None


def read_upscaling(options_lua: str | None) -> str | None:
    """The `["Upscaling"]` value from options.lua, or None if not set."""
    if options_lua is None:
        return None
    match = re.search(r'\[\s*"Upscaling"\s*\]\s*=\s*"([^"]*)"', options_lua)
    return match.group(1) if match else None
