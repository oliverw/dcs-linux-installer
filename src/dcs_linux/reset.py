"""Plan the removal of the tool state without touching DCS itself."""

from __future__ import annotations

from dataclasses import dataclass

from dcs_linux.patchstate import PatchStore, load
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


def stores_with_records(system: System, stores: tuple[PatchStore, ...]) -> tuple[PatchStore, ...]:
    """Stores that still hold a patch revert needs."""
    return tuple(store for store in stores if load(system, store))


def apply(writer: Writer, layout: Layout, reset: ResetPlan) -> None:
    """Carry out an already-confirmed plan."""
    if reset.register_exists:
        writer.remove(layout.installs_register)
    for store in reset.stores:
        writer.remove_tree(store.directory)


def _stores(system: System, layout: Layout) -> tuple[PatchStore, ...]:
    """Known patch-store directories directly below the state root."""
    return tuple(
        PatchStore(layout.state / name)
        for name in system.list_dir(layout.state)
        if name != layout.installs_register.name
        and system.exists(layout.state / name / "state.json")
    )
