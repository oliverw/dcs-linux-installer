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


@dataclass(frozen=True)
class Layout:
    """The paths `check` inspects."""

    root: Path
    toolchain: Path

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


def resolve_layout(system: System) -> Layout:
    """The layout for this machine, honouring the environment overrides."""
    home = system.home()
    root = system.environ(ROOT_ENV)
    toolchain = system.environ(TOOLCHAIN_ENV)
    return Layout(
        root=Path(root) if root else home / "dcs-linux",
        toolchain=Path(toolchain) if toolchain else home / ".cache" / "dcs-linux" / "toolchain",
    )
