"""The seam through which this tool changes the machine.

`dcs_linux.system.System` is deliberately read-only: discovery and `check`
must never write, and keeping the reading interface incapable of writing is
what enforces that. Everything that *does* write — so far, only the patch
engine — goes through `Writer` instead.

Splitting them this way also makes the patch tests real: a `Writer` backed by
the same in-memory fixture the `System` reads from means a test can apply a
patch and then observe the machine the way `check` would.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Protocol


class Writer(Protocol):
    """Everything this tool is allowed to do to the filesystem."""

    def make_dirs(self, path: Path) -> None:
        """Create `path` and its parents. Existing directories are fine."""

    def write_bytes(self, path: Path, data: bytes) -> None:
        """Replace `path` with `data`, creating it if it does not exist."""

    def copy_file(self, source: Path, destination: Path) -> None:
        """Copy `source` over `destination`, creating parent directories."""

    def remove(self, path: Path) -> None:
        """Delete a file. A path that is already gone is not an error."""

    def remove_tree(self, path: Path) -> None:
        """Delete a directory and everything under it, if it exists."""


class RealWriter:
    """`Writer` backed by the actual filesystem."""

    def make_dirs(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    def write_bytes(self, path: Path, data: bytes) -> None:
        # Written beside the target and renamed into place: a patch interrupted
        # halfway leaves the old file, never a half-written one that the engine
        # would afterwards read back as drift it cannot explain.
        self.make_dirs(path.parent)
        temporary = path.with_name(f".{path.name}.dcs-linux-tmp")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def copy_file(self, source: Path, destination: Path) -> None:
        self.make_dirs(destination.parent)
        shutil.copyfile(source, destination)

    def remove(self, path: Path) -> None:
        path.unlink(missing_ok=True)

    def remove_tree(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)
