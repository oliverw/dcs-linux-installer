"""The seams through which this tool pulls files off the network.

The umu zipapp and pinned GE-Proton build (ADR-0003) arrive as archives that
are immediately unpacked. Their interface is one call, `fetch_archive`, rather
than a download primitive plus an extract primitive: a half-downloaded tarball
is never a state anything else needs to see. The desktop shortcut's small,
pinned game icon is fetched as bytes and written atomically through `Writer`.

Every URL is pinned. Nothing here asks an API what the newest asset is, because
network content that changes under the user is content nobody can reproduce a
bug report against.
"""

from __future__ import annotations

import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# Long enough for GE-Proton (about 500 MB) on a slow connection, short enough
# that a dead mirror does not hang the command indefinitely.
FETCH_TIMEOUT = 600
FILE_FETCH_TIMEOUT = 30
MAX_FILE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class DownloadResult:
    """One small downloaded file, or why it could not be fetched."""

    data: bytes | None = None
    failure: str | None = None


class FileFetcher(Protocol):
    """Fetches a small file without writing it to the machine."""

    def fetch_file(self, url: str) -> DownloadResult: ...


class RealFileFetcher:
    """`FileFetcher` backed by HTTPS."""

    def fetch_file(self, url: str) -> DownloadResult:
        try:
            with urllib.request.urlopen(url, timeout=FILE_FETCH_TIMEOUT) as response:
                data = response.read(MAX_FILE_BYTES + 1)
        except (urllib.error.URLError, OSError, ValueError) as error:
            return DownloadResult(failure=f"could not download {url}: {error}")
        if len(data) > MAX_FILE_BYTES:
            return DownloadResult(failure=f"download from {url} exceeded {MAX_FILE_BYTES} bytes")
        return DownloadResult(data=data)


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
