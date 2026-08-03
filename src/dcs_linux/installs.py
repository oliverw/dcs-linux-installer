"""A DCS install, whoever put it there.

Most people who want this tool already have DCS — installed through Lutris,
Heroic or Steam — and will not re-download 150 GB. Everything this tool does
to an existing install therefore starts by finding it, and an install found
by one launcher has to mean the same thing as one found by another.

So identity lives here, apart from the launcher-specific searching in
`dcs_linux.launchers`: what counts as an install, what to call it, and which
one a command works on when the user did not say.

Nothing in this module writes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dcs_linux.system import System

# Short enough to retype off a bug report, long enough that two installs on
# one machine will not collide.
ID_LENGTH = 8

AUTOUPDATE_CFG = "autoupdate.cfg"
DCS_EXE = Path("bin") / "DCS.exe"
UPDATER_EXE = Path("bin") / "DCS_updater.exe"

# How far above a launcher's configured executable the install root can be.
# Every DCS binary a launcher points at lives in `<install>/bin`, so the
# install is the executable's directory or the one above it.
EXE_SEARCH_DEPTH = 2


class Launcher(StrEnum):
    """Who manages an install. Values are what `--json` and `--install` use."""

    DCS_LINUX = "dcs-linux"
    STEAM = "steam"
    LUTRIS = "lutris"
    HEROIC = "heroic"


class Edition(StrEnum):
    STANDALONE = "standalone"
    STEAM = "steam"
    UNKNOWN = "unknown"


# How an edition is written for a human. Kept beside the enum so the two
# places that render it — `check`'s table and the diagnostics bundle — cannot
# drift into calling the same edition two different things.
EDITION_LABELS = {
    Edition.STANDALONE: "Standalone",
    Edition.STEAM: "Steam",
    Edition.UNKNOWN: "unknown",
}


class InstallNotFound(LookupError):
    """No install matched the identifier the user gave."""


class AmbiguousInstall(LookupError):
    """More than one install matched, so the user has to be more specific."""


@dataclass(frozen=True)
class DcsInstall:
    """One DCS install, described well enough to act on."""

    game: Path
    launcher: Launcher
    prefix: Path | None = None
    runtime: str | None = None
    edition: Edition = Edition.UNKNOWN
    version: str | None = None

    @property
    def install_id(self) -> str:
        """Stable handle other commands accept to target this install.

        Derived from the game directory alone, so it survives a prefix
        rebuild, a change of launcher, and a re-run of discovery — the game
        directory is the durable, expensive thing (ADR-0001).
        """
        return hashlib.sha256(str(self.game).encode()).hexdigest()[:ID_LENGTH]

    @property
    def under_prefix(self) -> bool:
        """The game sits inside its own prefix, where a rebuild would delete it."""
        if self.prefix is None:
            return False
        return self.game.is_relative_to(self.prefix / "drive_c")


def is_dcs_install(system: System, path: Path) -> bool:
    """Whether `path` is the root of a DCS install.

    Any one marker is enough. A download interrupted before the game binary
    landed has only `autoupdate.cfg` and the updater, and continuing a
    half-finished install is exactly what a user in that state needs.
    """
    markers = (Path(AUTOUPDATE_CFG), DCS_EXE, UPDATER_EXE)
    return any(system.exists(path / marker) for marker in markers)


def find_install_root(system: System, path: Path) -> Path | None:
    """The install at `path`, or in one of its immediate subdirectories.

    The updater creates `<chosen directory>/DCS World`, so the directory a
    user points at is usually the parent of the install rather than the
    install itself.
    """
    if is_dcs_install(system, path):
        return path
    for name in system.list_dir(path):
        candidate = path / name
        if is_dcs_install(system, candidate):
            return candidate
    return None


def install_root_for_exe(system: System, exe: Path) -> Path | None:
    """The install above a launcher's configured executable, if it is DCS.

    Launchers record which binary to run, not which game is installed, so
    this is also the test that a configured game *is* DCS.
    """
    for parent in list(exe.parents)[:EXE_SEARCH_DEPTH]:
        if is_dcs_install(system, parent):
            return parent
    return None


def read_version(system: System, game: Path) -> str | None:
    """The DCS version, as the updater records it in `autoupdate.cfg`."""
    text = system.read_text(game / AUTOUPDATE_CFG)
    if text is None:
        return None
    try:
        config = json.loads(text)
    except ValueError:
        # Being read mid-write, or truncated: an unknown version, not a crash.
        return None
    version = config.get("version") if isinstance(config, dict) else None
    return version if isinstance(version, str) else None


def detect_edition(system: System, game: Path, launcher: Launcher) -> Edition:
    """Standalone or Steam.

    Steam's app manifest for appid 223750 is proof. `DCS_updater.exe` is the
    only other signal available statically: the standalone edition is updated
    by it, and the Steam edition is updated by Steam. **Unverified** — no
    Steam copy of DCS has been inspected, so a Steam install found through
    some other launcher may be reported as standalone.

    With neither signal the edition stays unknown rather than being guessed:
    it decides whether `install` may hand off to the updater at all.
    """
    if launcher is Launcher.STEAM:
        return Edition.STEAM
    if system.exists(game / UPDATER_EXE):
        return Edition.STANDALONE
    return Edition.UNKNOWN


def select(installs: Sequence[DcsInstall], identifier: str) -> DcsInstall:
    """The install named by an id, an unambiguous id prefix, or a game path."""
    wanted = identifier.strip()
    if not wanted:
        raise InstallNotFound(identifier)

    matches = [found for found in installs if found.install_id.startswith(wanted.lower())]
    if not matches:
        matches = [found for found in installs if str(found.game) == wanted]
    if not matches:
        raise InstallNotFound(identifier)
    if len(matches) > 1:
        raise AmbiguousInstall(identifier)
    return matches[0]


def default_install(installs: Sequence[DcsInstall]) -> DcsInstall | None:
    """The install to work on when the user named none.

    Ours if exactly one is ours, since that is the install this tool built and
    the one it can repair. Otherwise only an unambiguous single install:
    choosing between someone's several installs is their decision, not ours,
    and the ambiguous case here is a real one — a game installed into our own
    prefix's drive_c is a second install of ours, and the wrong one to pick.
    """
    ours = [found for found in installs if found.launcher is Launcher.DCS_LINUX]
    if len(ours) == 1:
        return ours[0]
    return installs[0] if len(installs) == 1 else None
