"""What was patched, and what it looked like before.

The store is one directory per install, outside every install (ADR-0001 and
issue #8): `~/.local/state/dcs-linux/<install id>/`, holding

    state.json          which patches are applied, and to which files
    backups/<patch>/…   the pristine copy of every file a patch overwrote

Two properties matter more than the format.

**A record is a claim about file contents, not about history.** Each file is
recorded with the SHA-256 of what the patch wrote, so "is this still applied?"
is answered by hashing the file rather than by trusting the record. That is
the whole of drift detection: `DCS_updater` overwrites patched files on every
update and repair, and a state file that merely remembered "applied" would go
on saying so long after the fix was gone.

**Backups are addressed relative to the store.** The store directory can move
between distros and XDG layouts; a record full of absolute backup paths would
not survive that, and an unrestorable backup is worse than none.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from dcs_linux.system import System
from dcs_linux.writer import Writer

STATE_FILE = "state.json"
BACKUP_DIR = "backups"

# Bumped only if the on-disk shape changes incompatibly. An unreadable or
# newer state file is treated as "nothing applied", which is safe: the engine
# then refuses to claim a patch is in place, and re-applying is idempotent.
FORMAT_VERSION = 1


@dataclass(frozen=True)
class FileRecord:
    """One file a patch wrote, and how to put it back."""

    path: Path
    sha256: str
    # Relative to the store directory, or None when the patch created the file
    # and reverting therefore means deleting it rather than restoring it.
    backup: str | None


@dataclass(frozen=True)
class PatchRecord:
    """One applied patch."""

    patch_id: str
    # The DCS version at the time of applying. Recorded because a patch that
    # stopped being needed, or started needing a different target, does so at
    # a version boundary — this is the first thing a bug report needs.
    dcs_version: str | None
    files: tuple[FileRecord, ...]


@dataclass(frozen=True)
class PatchStore:
    """The state directory for one install."""

    directory: Path

    @property
    def state_file(self) -> Path:
        return self.directory / STATE_FILE

    def backups_for(self, patch_id: str) -> Path:
        return self.directory / BACKUP_DIR / patch_id

    def absolute(self, backup: str) -> Path:
        """A recorded backup, resolved against wherever the store is now."""
        return self.directory / backup

    def relative(self, backup: Path) -> str:
        return str(backup.relative_to(self.directory))


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_of(system: System, path: Path) -> str | None:
    """The hash of a file on disk, or None if it is missing or unreadable."""
    data = system.read_bytes(path)
    return None if data is None else digest(data)


def load(system: System, store: PatchStore) -> dict[str, PatchRecord]:
    """Every applied patch, keyed by patch id.

    Anything unreadable, malformed or from a future format version reads as an
    empty store rather than raising: `check` and `report` both call this on
    machines that are already broken, and a state file is not worth crashing
    the diagnostics that would explain it.
    """
    text = system.read_text(store.state_file)
    if text is None:
        return {}
    try:
        document = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(document, dict) or document.get("version") != FORMAT_VERSION:
        return {}
    patches = document.get("patches")
    if not isinstance(patches, dict):
        return {}

    records: dict[str, PatchRecord] = {}
    for patch_id, entry in patches.items():
        record = _record(str(patch_id), entry)
        if record is not None:
            records[record.patch_id] = record
    return records


def _record(patch_id: str, entry: object) -> PatchRecord | None:
    if not isinstance(entry, dict):
        return None
    raw_files = entry.get("files")
    if not isinstance(raw_files, list):
        return None
    files = [parsed for parsed in (_file(item) for item in raw_files) if parsed is not None]
    if len(files) != len(raw_files):
        # A partly-parseable record would under-report which files the patch
        # touched, and revert would leave some of them behind.
        return None
    version = entry.get("dcs_version")
    return PatchRecord(
        patch_id=patch_id,
        dcs_version=version if isinstance(version, str) else None,
        files=tuple(files),
    )


def _file(item: object) -> FileRecord | None:
    if not isinstance(item, dict):
        return None
    path, sha256, backup = item.get("path"), item.get("sha256"), item.get("backup")
    if not isinstance(path, str) or not isinstance(sha256, str):
        return None
    return FileRecord(
        path=Path(path),
        sha256=sha256,
        backup=backup if isinstance(backup, str) else None,
    )


def save(writer: Writer, store: PatchStore, records: dict[str, PatchRecord]) -> None:
    """Replace the state file with `records`."""
    document = {
        "version": FORMAT_VERSION,
        "patches": {
            record.patch_id: {
                "dcs_version": record.dcs_version,
                "files": [
                    {"path": str(file.path), "sha256": file.sha256, "backup": file.backup}
                    for file in record.files
                ],
            }
            for record in records.values()
        },
    }
    writer.make_dirs(store.directory)
    writer.write_bytes(store.state_file, (json.dumps(document, indent=2) + "\n").encode())
