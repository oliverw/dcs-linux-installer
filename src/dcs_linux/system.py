"""The seam between the checks and the machine they inspect.

Every fact `check` reports is read through this interface, so the detection
logic can be unit-tested against fixtures instead of the developer's own
machine.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class CommandResult:
    """Outcome of running an external command."""

    returncode: int
    stdout: str


@dataclass(frozen=True)
class DiskUsage:
    """Bytes on the filesystem holding a given path."""

    total: int
    free: int


class System(Protocol):
    """Read-only view of the machine."""

    def read_text(self, path: Path) -> str | None:
        """File contents, or None if it is missing or unreadable."""

    def exists(self, path: Path) -> bool:
        """True if the path exists (following symlinks)."""

    def is_symlink(self, path: Path) -> bool: ...

    def resolve(self, path: Path) -> Path:
        """The path with symlinks followed, or unchanged if it cannot be."""

    def list_dir(self, path: Path) -> list[str]:
        """Entry names, sorted. Empty if the directory is missing."""

    def which(self, name: str) -> str | None:
        """Absolute path to an executable on PATH, or None."""

    def run(self, command: list[str]) -> CommandResult | None:
        """Run a command; None if it could not be executed at all."""

    def disk_usage(self, path: Path) -> DiskUsage | None:
        """Usage for the filesystem holding `path`, or None if unknowable."""

    def filesystem_type(self, path: Path) -> str | None:
        """Filesystem type ("btrfs", "ext4", ...) for the mount holding `path`."""

    def environ(self, name: str) -> str | None:
        """Environment variable value, or None."""

    def home(self) -> Path: ...


class RealSystem:
    """`System` backed by the actual machine."""

    def read_text(self, path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def exists(self, path: Path) -> bool:
        return path.exists()

    def is_symlink(self, path: Path) -> bool:
        return path.is_symlink()

    def resolve(self, path: Path) -> Path:
        try:
            return path.resolve()
        except OSError:
            return path

    def list_dir(self, path: Path) -> list[str]:
        try:
            return sorted(entry.name for entry in path.iterdir())
        except OSError:
            return []

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def run(self, command: list[str]) -> CommandResult | None:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return CommandResult(returncode=completed.returncode, stdout=completed.stdout)

    def disk_usage(self, path: Path) -> DiskUsage | None:
        probe = _nearest_existing(path)
        if probe is None:
            return None
        try:
            usage = shutil.disk_usage(probe)
        except OSError:
            return None
        return DiskUsage(total=usage.total, free=usage.free)

    def filesystem_type(self, path: Path) -> str | None:
        probe = _nearest_existing(path)
        if probe is None:
            return None
        mounts = self.read_text(Path("/proc/self/mounts"))
        if mounts is None:
            return None
        return filesystem_type_from_mounts(mounts, probe.resolve())

    def environ(self, name: str) -> str | None:
        return os.environ.get(name)

    def home(self) -> Path:
        return Path.home()


def _nearest_existing(path: Path) -> Path | None:
    """The path itself, or its closest existing ancestor."""
    for candidate in [path, *path.parents]:
        if candidate.exists():
            return candidate
    return None


def filesystem_type_from_mounts(mounts: str, path: Path) -> str | None:
    """Longest-prefix match of `path` against the mount table.

    Exposed separately from `RealSystem` so the parsing is testable without a
    real /proc.
    """
    best: tuple[int, str] | None = None
    for line in mounts.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        mount_point, fstype = _unescape_mount(fields[1]), fields[2]
        if path == Path(mount_point) or Path(mount_point) in path.parents:
            depth = len(Path(mount_point).parts)
            if best is None or depth > best[0]:
                best = (depth, fstype)
    return None if best is None else best[1]


def _unescape_mount(field: str) -> str:
    """/proc mount fields octal-escape spaces and friends."""
    for escape, literal in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        field = field.replace(escape, literal)
    return field
