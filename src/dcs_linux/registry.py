"""The installs this tool created, remembered rather than searched for.

Discovery (`dcs_linux.launchers`) finds installs by reading what Lutris,
Heroic and Steam wrote about them. Our own installs have no such file: the
game directory is wherever the user pointed `--game-dir`, which is a place
nothing on the machine records. Without this register, `dcs-linux install
--game-dir /mnt/big` would finish and then be invisible to `check`, `patch`
and `verify` unless the same flag were repeated every time.

So `install` writes one line here when a download completes, and discovery
reads it back. Two rules keep it from becoming a second source of truth:

- The register says *where to look*, never *what is there*. Every entry is
  re-verified against the disk before it becomes an install, so a directory
  that has since been deleted simply stops being reported (ADR-0007: an
  install is its game directory).
- It lives in the state directory beside the patch stores, outside every
  install, where `DCS_updater repair` cannot reach it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from dcs_linux.paths import Layout
from dcs_linux.system import System
from dcs_linux.writer import Writer


@dataclass(frozen=True)
class Registration:
    """One install this tool created, and the prefix it was created with."""

    game: Path
    prefix: Path

    def as_json(self) -> dict[str, str]:
        return {"game": str(self.game), "prefix": str(self.prefix)}


def registered(system: System, layout: Layout) -> tuple[Registration, ...]:
    """Every install recorded here, in the order they were added.

    An unreadable or malformed register reads as empty. It is a convenience
    over discovery, never the only way an install can be found, so a damaged
    one must cost a re-run of `install` and never an error from `check`.
    """
    text = system.read_text(layout.installs_register)
    if text is None:
        return ()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return ()
    entries = payload.get("installs") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return ()
    return tuple(
        Registration(game=Path(str(entry["game"])), prefix=Path(str(entry["prefix"])))
        for entry in entries
        if isinstance(entry, dict) and "game" in entry and "prefix" in entry
    )


def register(system: System, writer: Writer, layout: Layout, *, game: Path, prefix: Path) -> bool:
    """Record an install, returning whether that changed anything.

    Keyed by the game directory, which is the install's identity (ADR-0007):
    re-running `install` against the same directory updates the prefix it
    names instead of adding a second entry for one install.
    """
    entry = Registration(game=game, prefix=prefix)
    existing = registered(system, layout)
    if entry in existing:
        return False
    entries = [found for found in existing if found.game != game] + [entry]
    payload = {"installs": [found.as_json() for found in entries]}
    writer.write_bytes(layout.installs_register, (json.dumps(payload, indent=2) + "\n").encode())
    return True
