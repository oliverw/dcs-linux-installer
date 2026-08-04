"""A `System` made of fixtures, so detection is tested without this machine."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from dcs_linux.runner import Completed
from dcs_linux.system import CommandResult, DiskUsage


class FakeSystem:
    """In-memory machine.

    `files` maps a path to its contents; directories are implied by the files
    inside them, and `directories` adds empty ones. Contents are held as bytes
    because patch targets are binary — text given to the constructor is
    encoded, and `read_text` decodes it back.

    `symlinks` records which paths are links; `links` additionally says where
    a link points, which is what makes two spellings of one directory —
    `~/.steam/root` and `~/.local/share/Steam` — resolve to the same place.
    """

    def __init__(
        self,
        *,
        files: dict[str, str] | None = None,
        blobs: dict[str, bytes] | None = None,
        directories: set[str] | None = None,
        symlinks: set[str] | None = None,
        links: dict[str, str] | None = None,
        executables: dict[str, str] | None = None,
        commands: dict[str, CommandResult] | None = None,
        binary_commands: dict[str, bytes] | None = None,
        disk: DiskUsage | None = None,
        filesystem: str | None = None,
        env: dict[str, str] | None = None,
        home: str = "/home/pilot",
    ) -> None:
        self.executable_bits: set[Path] = set()
        self.files = {Path(path): text.encode() for path, text in (files or {}).items()}
        self.files.update({Path(path): data for path, data in (blobs or {}).items()})
        self.directories = {Path(path) for path in (directories or set())}
        self.links = {Path(source): Path(target) for source, target in (links or {}).items()}
        self.symlinks = {Path(path) for path in (symlinks or set())} | set(self.links)
        self.executables = executables or {}
        self.commands = commands or {}
        self.binary_commands = binary_commands or {}
        self.disk = disk
        self.filesystem = filesystem
        self.env = env or {}
        self._home = Path(home)

    def read_text(self, path: Path) -> str | None:
        data = self.read_bytes(path)
        return None if data is None else data.decode(errors="replace")

    def read_bytes(self, path: Path) -> bytes | None:
        return self.files.get(self.resolve(path))

    def exists(self, path: Path) -> bool:
        path = self.resolve(path)
        if path in self.files or path in self.directories or path in self.symlinks:
            return True
        return any(path in known.parents for known in self._all_paths())

    def is_symlink(self, path: Path) -> bool:
        # Deliberately not resolved: the question is about this path itself.
        return path in self.symlinks

    def resolve(self, path: Path) -> Path:
        for source, target in self.links.items():
            if path == source:
                return target
            if source in path.parents:
                return target / path.relative_to(source)
        return path

    def list_dir(self, path: Path) -> list[str]:
        path = self.resolve(path)
        names = {
            known.relative_to(path).parts[0] for known in self._all_paths() if path in known.parents
        }
        return sorted(names)

    def which(self, name: str) -> str | None:
        return self.executables.get(name)

    def run(self, command: list[str]) -> CommandResult | None:
        return self.commands.get(" ".join(command))

    def run_binary(self, command: list[str]) -> bytes | None:
        return self.binary_commands.get(" ".join(command))

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


class FakeWriter:
    """A `Writer` onto a `FakeSystem`.

    Deliberately the *same* fixture the system reads from, so a test can apply
    a patch and then ask the machine what happened exactly as `check` would —
    which is the only way drift, revert and re-apply are worth testing at all.
    """

    def __init__(self, system: FakeSystem) -> None:
        self.system = system

    def make_dirs(self, path: Path) -> None:
        self.system.directories.add(path)

    def write_bytes(self, path: Path, data: bytes) -> None:
        self.make_dirs(path.parent)
        self.system.files[path] = data

    def copy_file(self, source: Path, destination: Path) -> None:
        data = self.system.read_bytes(source)
        if data is None:
            raise OSError(f"no such file: {source}")
        self.write_bytes(destination, data)

    def remove(self, path: Path) -> None:
        self.system.files.pop(path, None)

    def remove_tree(self, path: Path) -> None:
        for known in [*self.system.files, *self.system.directories, *self.system.symlinks]:
            if known == path or path in known.parents:
                self.system.files.pop(known, None)
                self.system.directories.discard(known)
                self.system.symlinks.discard(known)
                self.system.links.pop(known, None)

    def symlink(self, path: Path, target: Path) -> None:
        self.remove_tree(path)
        self.make_dirs(path.parent)
        self.system.symlinks.add(path)
        self.system.links[path] = target

    def make_executable(self, path: Path) -> None:
        self.system.executable_bits.add(path)


class FakeRunner:
    """A `Runner` that records what it was asked to run.

    Commands are keyed by their first argument — `""` for prefix creation,
    `winetricks` for the verbs — because that is the whole vocabulary this
    tool speaks to umu, and keying by the full command line would make every
    test depend on the toolchain paths.
    """

    def __init__(
        self,
        *,
        results: dict[str, Completed] | None = None,
        effects: dict[str, Callable[[], None]] | None = None,
    ) -> None:
        self.results = results or {}
        self.effects = effects or {}
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def run(
        self, command: list[str], environment: dict[str, str], timeout: float = 0.0
    ) -> Completed:
        self.calls.append((command, environment))
        key = self._key(command)
        effect = self.effects.get(key)
        if effect is not None:
            effect()
        return self.results.get(key, Completed(returncode=0))

    def commands(self) -> list[str]:
        return [self._key(command) for command, _ in self.calls]

    @staticmethod
    def _key(command: list[str]) -> str:
        return command[1] if len(command) > 1 and command[1] else "prefix"


class FakeFetcher:
    """A `Fetcher` that unpacks fixtures instead of the network.

    `payloads` maps a fragment of the URL to the files that archive contains,
    relative to the destination — so a test can hand back a real-looking umu
    zipapp, an empty archive, or nothing at all.
    """

    def __init__(
        self,
        system: FakeSystem,
        *,
        payloads: dict[str, dict[str, str]] | None = None,
        failures: dict[str, str] | None = None,
    ) -> None:
        self.system = system
        self.payloads = payloads or {}
        self.failures = failures or {}
        self.urls: list[str] = []

    def fetch_archive(self, url: str, destination: Path) -> str | None:
        self.urls.append(url)
        for fragment, reason in self.failures.items():
            if fragment in url:
                return reason
        self.system.directories.add(destination)
        for fragment, files in self.payloads.items():
            if fragment in url:
                for relative, text in files.items():
                    self.system.files[destination / relative] = text.encode()
        return None
