"""Desktop-session integration for adopted installs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dcs_linux.installs import DcsInstall
from dcs_linux.system import System
from dcs_linux.writer import Writer


class Desktop(StrEnum):
    """Desktop environments for which a launcher is supported."""

    KDE = "kde"
    GNOME = "gnome"


class ShortcutStatus(StrEnum):
    """What happened when a desktop shortcut was requested."""

    CREATED = "created"
    EXISTS = "exists"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class ShortcutResult:
    """A shortcut outcome suitable for text and JSON reporting."""

    status: ShortcutStatus
    detail: str
    desktop: Desktop | None = None
    path: Path | None = None


def detect_desktop(system: System) -> Desktop | None:
    """Recognise KDE or GNOME from the current freedesktop session."""
    current = system.environ("XDG_CURRENT_DESKTOP")
    value = current if current else system.environ("DESKTOP_SESSION")
    names = {part.strip().lower() for part in (value or "").replace(";", ":").split(":")}
    if "kde" in names or any(name.startswith("plasma") for name in names):
        return Desktop.KDE
    if "gnome" in names or any(name.startswith("gnome-") for name in names):
        return Desktop.GNOME
    return None


def create_shortcut(
    system: System,
    writer: Writer,
    desktop: Desktop,
    install: DcsInstall,
) -> ShortcutResult:
    """Create the freedesktop launcher shared by KDE and GNOME."""
    data_home = system.environ("XDG_DATA_HOME")
    applications = (
        Path(data_home) if data_home else system.home() / ".local" / "share"
    ) / "applications"
    path = applications / f"dcs-linux-{install.install_id}.desktop"
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=DCS World\n"
        "Comment=Launch DCS World through dcs-linux\n"
        f"Exec=dcs-linux launch --install {install.install_id}\n"
        "Terminal=false\n"
        "Categories=Game;\n"
    ).encode()
    try:
        if system.read_bytes(path) == content:
            writer.make_executable(path)
            return ShortcutResult(
                ShortcutStatus.EXISTS,
                "desktop shortcut already exists",
                desktop,
                path,
            )
        writer.write_bytes(path, content)
        writer.make_executable(path)
    except OSError as error:
        return ShortcutResult(
            ShortcutStatus.FAILED,
            f"could not write desktop shortcut: {error}",
            desktop,
            path,
        )
    return ShortcutResult(ShortcutStatus.CREATED, "desktop shortcut created", desktop, path)
