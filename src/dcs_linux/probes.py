"""Gather the machine's facts. No judgements are made here.

Probing and judging are kept apart so the checks in `dcs_linux.checks` are
pure functions over an `Environment` and can be tested against fixtures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from dcs_linux.distro import Distro, detect_distro
from dcs_linux.headtracking import HeadTracking, detect_head_tracking
from dcs_linux.installs import (
    DcsInstall,
    InstallNotFound,
    Launcher,
    default_install,
    select,
)
from dcs_linux.launchers import adopt, discover
from dcs_linux.patches import SEGOE_FONT_NAMES, PatchState, states, unknown_states
from dcs_linux.patchstate import PatchStore
from dcs_linux.paths import Layout, TargetPaths, normalise, resolve_layout
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
    patches: tuple[PatchState, ...] = ()
    head_tracking: HeadTracking = field(default_factory=HeadTracking)


def probe(
    system: System, identifier: str | None = None, *, layout: Layout | None = None
) -> Environment:
    """Read the whole machine, reporting on the install `identifier` names.

    Raises `InstallNotFound` or `AmbiguousInstall` if it names none.

    `layout` overrides where this tool's own directories are. `install` passes
    the layout it is about to build into, so that the disk-space and location
    checks are answered about the directory the user chose rather than the
    default one they are overriding.
    """
    layout = layout if layout is not None else resolve_layout(system)
    installs, targeted = _installs(system, layout, identifier)
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
        patches=probe_patches(system, layout, targeted),
        head_tracking=detect_head_tracking(system, paths),
    )


def _installs(
    system: System, layout: Layout, identifier: str | None
) -> tuple[tuple[DcsInstall, ...], DcsInstall | None]:
    """Every install found, and the one being reported on.

    An identifier that matches nothing discovered is tried against the disk
    before it is refused: discovery only knows the installs some other
    program wrote a record of, so naming a directory is how a user reaches
    one that nothing on the machine records (`dcs_linux.launchers.adopt`).
    An adopted install joins the list, so it is listed and acted on exactly
    like a discovered one.
    """
    installs = discover(system, layout)
    if not identifier:
        return installs, default_install(installs)
    try:
        return installs, select(installs, identifier)
    except InstallNotFound:
        adopted = adopt(system, normalise(system, identifier))
        if adopted is None:
            raise
        return (*installs, adopted), adopted


def probe_patches(
    system: System, layout: Layout, targeted: DcsInstall | None
) -> tuple[PatchState, ...]:
    """Which patches are in place on the targeted install.

    Read-only, like everything else here: the standings come from the state
    store plus the hash of each patched file, so nothing has to be applied to
    find out that a DCS update undid it.
    """
    if targeted is None:
        return unknown_states()
    return states(system, patch_store_for(layout, targeted))


def patch_store_for(layout: Layout, targeted: DcsInstall) -> PatchStore:
    """The state directory for an install, keyed by its stable id."""
    return PatchStore(directory=layout.patch_store(targeted.install_id))


def target_paths(system: System, layout: Layout, targeted: DcsInstall | None) -> TargetPaths:
    """The paths the install-dependent checks read."""
    ours = targeted is None or targeted.launcher is Launcher.DCS_LINUX
    # An explicitly chosen game directory outranks discovery. `install
    # --game-dir /mnt/big` is a statement about where DCS is going, and
    # answering the disk-space check about some *other* install found on the
    # machine would measure the wrong drive and pass a full one.
    game = layout.game if layout.game_dir else _discovered_game(layout, targeted)
    prefix = targeted.prefix if targeted is not None and targeted.prefix else layout.prefix
    return TargetPaths(
        game=game,
        prefix=prefix,
        saved_games=_saved_games(layout, targeted, ours=ours),
        prefix_saved_games=find_prefix_saved_games(system, prefix),
    )


def _saved_games(layout: Layout, targeted: DcsInstall | None, *, ours: bool) -> Path | None:
    """Where the targeted install's saved games are, if that is knowable.

    An install that carries our runtime manifest states its own, which is the
    answer for one of ours that does not sit under the default layout — the
    default would otherwise name a directory belonging to a different install
    entirely.
    """
    if targeted is not None and targeted.saved_games is not None:
        return targeted.saved_games
    return layout.saved_games if ours else None


def _discovered_game(layout: Layout, targeted: DcsInstall | None) -> Path:
    """The targeted install's game directory, or where ours would go."""
    return targeted.game if targeted is not None else layout.game


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
            name for name in SEGOE_FONT_NAMES if name.lower() not in {f.lower() for f in fonts}
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

    The name is matched with an optional leading `*`, which winetricks writes
    for any DLL wine ships a builtin for -- `"*d3dcompiler_47"="native"`. Both
    spellings appear in one real block, since vcrun2022's DLLs displace no
    builtin and are written bare. Anchoring on the bare name alone reported a
    correctly patched prefix as unpatched, and sent the user back to a
    winetricks verb they had already run.
    """
    if user_reg is None:
        return False
    match = re.search(
        rf'^"\*?{re.escape(dll)}"\s*=\s*"([^"]*)"',
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
