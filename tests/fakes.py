"""A `System` made of fixtures, so detection is tested without this machine."""

from __future__ import annotations

from pathlib import Path

from dcs_linux.system import CommandResult, DiskUsage


class FakeSystem:
    """In-memory machine.

    `files` maps a path to its contents; directories are implied by the files
    inside them, and `directories` adds empty ones.
    """

    def __init__(
        self,
        *,
        files: dict[str, str] | None = None,
        directories: set[str] | None = None,
        symlinks: set[str] | None = None,
        executables: dict[str, str] | None = None,
        commands: dict[str, CommandResult] | None = None,
        disk: DiskUsage | None = None,
        filesystem: str | None = None,
        env: dict[str, str] | None = None,
        home: str = "/home/pilot",
    ) -> None:
        self.files = {Path(path): text for path, text in (files or {}).items()}
        self.directories = {Path(path) for path in (directories or set())}
        self.symlinks = {Path(path) for path in (symlinks or set())}
        self.executables = executables or {}
        self.commands = commands or {}
        self.disk = disk
        self.filesystem = filesystem
        self.env = env or {}
        self._home = Path(home)

    def read_text(self, path: Path) -> str | None:
        return self.files.get(path)

    def exists(self, path: Path) -> bool:
        if path in self.files or path in self.directories or path in self.symlinks:
            return True
        return any(path in known.parents for known in self._all_paths())

    def is_symlink(self, path: Path) -> bool:
        return path in self.symlinks

    def list_dir(self, path: Path) -> list[str]:
        names = {
            known.relative_to(path).parts[0] for known in self._all_paths() if path in known.parents
        }
        return sorted(names)

    def which(self, name: str) -> str | None:
        return self.executables.get(name)

    def run(self, command: list[str]) -> CommandResult | None:
        return self.commands.get(" ".join(command))

    def disk_usage(self, path: Path) -> DiskUsage | None:
        return self.disk

    def filesystem_type(self, path: Path) -> str | None:
        return self.filesystem

    def environ(self, name: str) -> str | None:
        return self.env.get(name)

    def home(self) -> Path:
        return self._home

    def _all_paths(self) -> set[Path]:
        return set(self.files) | self.directories | self.symlinks
