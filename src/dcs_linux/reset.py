"""Plan the removal of the tool state without touching DCS itself."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from dcs_linux.patchstate import FORMAT_VERSION, PatchStore
from dcs_linux.paths import Layout
from dcs_linux.system import System
from dcs_linux.writer import Writer


@dataclass(frozen=True)
class ResetPlan:
    """The register and optional patch stores a reset may remove."""

    register_exists: bool
    stores: tuple[PatchStore, ...]

    @property
    def has_deletions(self) -> bool:
        return self.register_exists or bool(self.stores)


def plan(system: System, layout: Layout, *, patches: bool) -> ResetPlan:
    """Find the state files reset owns, without looking outside state."""
    stores = _stores(system, layout) if patches else ()
    return ResetPlan(register_exists=system.exists(layout.installs_register), stores=stores)


def unsafe_stores(system: System, stores: tuple[PatchStore, ...]) -> tuple[PatchStore, ...]:
    """Stores that are not provably empty and safe to delete.

    Patch state records are what make revert possible. A damaged or newer
    state file cannot prove no patch was applied, so deletion must refuse
    rather than discard the only pristine backups.
    """
    return tuple(store for store in stores if not _is_known_empty(system, store))


def apply(writer: Writer, layout: Layout, reset: ResetPlan) -> None:
    """Carry out an already-confirmed plan."""
    if reset.register_exists:
        writer.remove(layout.installs_register)
    for store in reset.stores:
        writer.remove_tree(store.directory)


def overlaps_lifetime(system: System, layout: Layout) -> bool:
    """Whether the configured state directory could contain a DCS lifetime."""
    state = system.resolve(layout.state)
    return any(_overlaps(state, system.resolve(lifetime)) for lifetime in _lifetimes(layout))


def _stores(system: System, layout: Layout) -> tuple[PatchStore, ...]:
    """Known patch-store directories directly below the state root."""
    return tuple(
        PatchStore(layout.state / name)
        for name in system.list_dir(layout.state)
        if name != layout.installs_register.name
        and (
            system.exists(layout.state / name / "state.json")
            or system.exists(layout.state / name / "backups")
        )
    )


def _is_known_empty(system: System, store: PatchStore) -> bool:
    text = system.read_text(store.state_file)
    if text is None:
        # A backup-only store has no state left to protect; a state file that
        # exists but cannot be read is unsafe, because its records are unknown.
        return not system.exists(store.state_file)
    try:
        document: object = json.loads(text)
    except ValueError:
        return False
    return isinstance(document, dict) and document == {
        "version": FORMAT_VERSION,
        "patches": {},
    }


def _lifetimes(layout: Layout) -> tuple[Path, Path, Path]:
    return layout.prefix, layout.game, layout.saved_games


def _overlaps(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents
