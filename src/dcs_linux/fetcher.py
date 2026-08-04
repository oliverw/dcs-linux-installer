"""The seam through which this tool pulls the toolchain off the network.

Only two things are ever fetched — the umu zipapp and the pinned GE-Proton
build (ADR-0003) — and both arrive as an archive that is immediately unpacked.
So the interface is one call, `fetch_archive`, rather than a download
primitive plus an extract primitive: a half-downloaded tarball is never a
state anything else needs to see, and a fake in a test can simply put the
files where the real one would.

Both URLs are pinned to explicit versions in `dcs_linux.prefix`. Nothing here
asks a release API what the newest build is, because a toolchain that changes
under the user is a toolchain nobody can reproduce a bug report against.
"""

from __future__ import annotations

import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol

# Long enough for GE-Proton (about 500 MB) on a slow connection, short enough
# that a dead mirror does not hang the command indefinitely.
FETCH_TIMEOUT = 600


class Fetcher(Protocol):
    """Fetches an archive and unpacks it."""

    def fetch_archive(self, url: str, destination: Path) -> str | None:
        """Download `url` and unpack it into `destination`.

        Returns None on success, or a human-readable reason it failed. Failure
        is returned rather than raised because fetching is one step of an
        install that reports every step's outcome, and a network error is an
        expected outcome of that step, not an exceptional one.
        """


class RealFetcher:
    """`Fetcher` backed by the network."""

    def fetch_archive(self, url: str, destination: Path) -> str | None:
        destination.mkdir(parents=True, exist_ok=True)
        # Unpacked from a temporary file rather than streamed, because tarfile
        # needs to seek and a partial transfer must never be handed to it.
        with tempfile.TemporaryDirectory() as scratch:
            archive = Path(scratch) / "download"
            try:
                with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as response:
                    archive.write_bytes(response.read())
            except (urllib.error.URLError, OSError, ValueError) as error:
                return f"could not download {url}: {error}"
            try:
                with tarfile.open(archive) as tar:
                    # `filter="data"` refuses absolute paths, `..` components,
                    # devices and setuid bits, so an archive cannot write
                    # outside the directory it was asked to unpack into.
                    tar.extractall(destination, filter="data")
            except (tarfile.TarError, OSError) as error:
                return f"could not unpack {url}: {error}"
        return None
