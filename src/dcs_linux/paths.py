"""Where this tool puts the three lifetimes and the toolchain.

The prefix, the game directory and saved games have independent lifetimes
(ADR-0001), so they are three separate paths, never nested in one another.

This is our layout only. Paths inside somebody else's prefix belong to the
install that was discovered there — see `dcs_linux.probes.Target`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dcs_linux.system import System

ROOT_ENV = "DCS_LINUX_ROOT"
TOOLCHAIN_ENV = "DCS_LINUX_TOOLCHAIN"
STATE_ENV = "DCS_LINUX_STATE"
XDG_STATE_ENV = "XDG_STATE_HOME"

# DCS's compiled-shader directories under `Saved Games/DCS`. `metashaders` is
# the pre-2.7 spelling and is still found on installs that have been upgraded
# in place, so both are cleared.
SHADER_CACHE_DIRS = ("fxo", "metashaders2", "metashaders")


@dataclass(frozen=True)
class Layout:
    """The paths `check` inspects."""

    root: Path
    toolchain: Path
    # Patch backups and patch state. Outside every install on purpose:
    # `DCS_updater repair` deletes files ED's manifest does not list, so a
    # pristine backup kept inside the install is a backup the updater destroys
    # exactly when it is needed. Keyed by install id, not by path, so it
    # survives the install being renamed or its prefix rebuilt.
    state: Path

    @property
    def prefix(self) -> Path:
        """Disposable: wiped and rebuilt freely."""
        return self.root / "prefix"

    @property
    def game(self) -> Path:
        """Durable: the expensive thing (hundreds of GB)."""
        return self.root / "game"

    @property
    def saved_games(self) -> Path:
        """Durable: the irreplaceable thing (login, keybinds, config)."""
        return self.root / "saved-games"

    @property
    def umu_run(self) -> Path:
        """The umu zipapp entry point (ADR-0003)."""
        return self.toolchain / "umu" / "umu-run"

    @property
    def ge_proton(self) -> Path:
        return self.toolchain / "ge-proton"

    def patch_store(self, install_id: str) -> Path:
        """Where one install's patch backups and patch state live."""
        return self.state / install_id

    def proton_search_path(self, home: Path) -> tuple[Path, ...]:
        """Everywhere a Proton build may already be unpacked.

        Gaming-first distros such as Bazzite and SteamOS ship Steam with
        compatibility tools already in place, so looking only in our own
        toolchain would report a build the user plainly has as missing.
        """
        return (
            self.ge_proton,
            home / ".local" / "share" / "umu" / "compatibilitytools",
            home / ".steam" / "root" / "compatibilitytools.d",
            home / ".local" / "share" / "Steam" / "compatibilitytools.d",
            home
            / ".var"
            / "app"
            / "com.valvesoftware.Steam"
            / "data"
            / "Steam"
            / "compatibilitytools.d",
            Path("/usr/share/steam/compatibilitytools.d"),
        )


@dataclass(frozen=True)
class TargetPaths:
    """Where to read the targeted install's state.

    Discovery finds installs; these are the paths of the one being reported
    on. With no DCS anywhere they fall back to where this tool would put
    things, so a bare machine still has coherent paths to answer against.

    Unlike `Layout`, these are not necessarily ours: an install adopted from
    Lutris, Heroic or Steam keeps its own prefix wherever that launcher put it.
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
        return self._in_saved_games(Path("DCS") / "Config" / "options.lua")

    @property
    def options_db(self) -> Path:
        """`optionsDb.lua`, the voice-chat patch's target.

        A **game** file, so DCS hashes it: anything written here is IC-risky
        (ADR-0004).
        """
        return self.game / "MissionEditor" / "modules" / "optionsDb.lua"

    @property
    def aircraft_mods(self) -> Path:
        """Where the aircraft modules, and their loose textures, live."""
        return self.game / "Mods" / "aircraft"

    @property
    def shader_caches(self) -> tuple[Path, ...]:
        """DCS's compiled-shader directories.

        In saved games, never in the game directory, which is what makes
        clearing them IC-safe: nothing DCS hashes is touched, and DCS
        recompiles whatever is missing on the next launch.
        """
        return tuple(
            directory
            for name in SHADER_CACHE_DIRS
            for directory in self._in_saved_games(Path("DCS") / name)
        )

    def _in_saved_games(self, relative: Path) -> tuple[Path, ...]:
        """One path per saved-games root this install might be reading.

        The in-prefix one comes first because it is the one this prefix
        actually reads — mapped out, it resolves to the durable copy anyway.
        """
        durable = (self.saved_games / relative,) if self.saved_games else ()
        return (self.prefix_saved_games / relative, *durable)


def resolve_layout(system: System) -> Layout:
    """The layout for this machine, honouring the environment overrides."""
    home = system.home()
    root = system.environ(ROOT_ENV)
    toolchain = system.environ(TOOLCHAIN_ENV)
    return Layout(
        root=Path(root) if root else home / "dcs-linux",
        toolchain=Path(toolchain) if toolchain else home / ".cache" / "dcs-linux" / "toolchain",
        state=_state_root(system, home),
    )


def _state_root(system: System, home: Path) -> Path:
    """`~/.local/state/dcs-linux`, honouring XDG_STATE_HOME.

    XDG is respected rather than hard-coded because on the immutable and
    container-first distros this tool targets, the state directory is
    routinely somewhere else — and losing track of it means losing the
    pristine backups.
    """
    override = system.environ(STATE_ENV)
    if override:
        return Path(override)
    xdg = system.environ(XDG_STATE_ENV)
    base = Path(xdg) if xdg else home / ".local" / "state"
    return base / "dcs-linux"
