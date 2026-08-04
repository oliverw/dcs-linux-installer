"""The named fixes, and the engine that applies, reverts and re-applies them.

A patch here is not a one-off edit. `DCS_updater` overwrites patched files on
every update and repair, so the interesting state is not "was this applied?"
but "is it applied *right now*?" — which is why every patch is a plan that can
be recomputed, and every applied file is remembered by content hash rather
than by a flag (`dcs_linux.patchstate`).

Three rules shape the design:

- **Nothing is written until the whole plan is known.** A patch that cannot
  find what it needs returns a refusal and the engine writes nothing at all,
  rather than leaving an install half-fixed.
- **Nothing is matched by position.** Targets move between DCS versions, so a
  patch locates its work by content — a file's presence, a pattern in it —
  never by a line number.
- **IC-safe by default** (ADR-0004). Every patch carries `ic_risk`, and the
  one patch in the registry so far touches only the wine prefix, so no hashed
  game file is involved and multiplayer is not at stake.

The engine knows nothing about fonts. It knows about plans, backups, hashes
and state, so the next patch is a planner function and a registry entry.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dcs_linux.patchstate import (
    FileRecord,
    PatchRecord,
    PatchStore,
    digest,
    digest_of,
    load,
    save,
)
from dcs_linux.paths import TargetPaths
from dcs_linux.system import System
from dcs_linux.writer import Writer

# The names the AH-64D asks for. Without them it dies entering a mission
# (`Cannot create font [] size 30` → ACCESS_VIOLATION in CockpitBase.dll), and
# `winetricks corefonts` does not help: it ships 42 fonts, none of them Segoe.
SEGOE_FONT_NAMES = ("segoeui.ttf", "seguisb.ttf", "seguisym.ttf")

# Any of these will do. Verified in-cockpit on 2.9.28.26385 that a DejaVu
# substitute renders EUFD, MFD, HMD and the Keyboard Unit correctly (ADR-0004),
# so Microsoft's own file is neither required nor redistributed.
SUBSTITUTE_FONTS = (
    "DejaVuSans.ttf",
    "LiberationSans-Regular.ttf",
    "NotoSans-Regular.ttf",
    "FreeSans.ttf",
)

FONT_ROOTS = (
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
    Path("/run/host/fonts"),
)

# Distros nest fonts by vendor and family (`/usr/share/fonts/dejavu-sans-fonts`,
# `.../truetype/dejavu`), but no deeper. Bounded so a pathological font tree
# cannot turn `patch --list` into a filesystem walk.
FONT_SEARCH_DEPTH = 3


class PatchStatus(StrEnum):
    """Whether a patch is in place on the targeted install."""

    APPLIED = "applied"
    # Applied once, but the files no longer hold what we wrote — almost always
    # a DCS update or a `repair` having overwritten them.
    DRIFTED = "drifted"
    NOT_APPLIED = "not-applied"
    # No install was targeted, so there is nothing to be applied *to*.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FileWrite:
    """One file a patch wants to put on disk, contents and all."""

    path: Path
    content: bytes


@dataclass(frozen=True)
class Plan:
    """What a patch would do, or why it will not do anything.

    A plan carries the finished bytes rather than instructions for producing
    them, so the engine can back up, write and record without knowing what
    kind of fix it is holding — and so a patch that cannot assemble its
    content says so before a single byte is written.
    """

    writes: tuple[FileWrite, ...] = ()
    refusal: str | None = None


@dataclass(frozen=True)
class Patch:
    """A named fix: what it is, what it risks, and how to plan it."""

    id: str
    summary: str
    ic_risk: bool
    plan: Callable[[System, TargetPaths], Plan]


@dataclass(frozen=True)
class PatchState:
    """One patch's standing on the targeted install."""

    patch: Patch
    status: PatchStatus
    detail: str

    @property
    def is_drifted(self) -> bool:
        return self.status is PatchStatus.DRIFTED


@dataclass(frozen=True)
class Outcome:
    """The result of applying or reverting one patch."""

    patch: Patch
    ok: bool
    changed: bool
    detail: str


def find_substitute_font(system: System) -> Path | None:
    """A locally installed sans font to stand in for Segoe.

    Preferred names are searched before falling back to fontconfig, so the
    answer is the same on two machines with the same fonts installed —
    `fc-match sans` is configuration-dependent and can return a font with no
    symbol coverage at all.
    """
    for name in SUBSTITUTE_FONTS:
        for root in _font_roots(system):
            found = _find_file(system, root, name, FONT_SEARCH_DEPTH)
            if found is not None:
                return found
    return _fontconfig_match(system)


def _font_roots(system: System) -> tuple[Path, ...]:
    home = system.home()
    return (*FONT_ROOTS, home / ".local" / "share" / "fonts", home / ".fonts")


def _find_file(system: System, root: Path, name: str, depth: int) -> Path | None:
    if depth <= 0:
        return None
    candidate = root / name
    if system.exists(candidate):
        return candidate
    for entry in system.list_dir(root):
        found = _find_file(system, root / entry, name, depth - 1)
        if found is not None:
            return found
    return None


def _fontconfig_match(system: System) -> Path | None:
    """Whatever fontconfig calls `sans`, if fc-match is installed."""
    if system.which("fc-match") is None:
        return None
    result = system.run(["fc-match", "--format=%{file}", "sans"])
    if result is None or result.returncode != 0:
        return None
    path = result.stdout.strip()
    return Path(path) if path else None


def plan_segoe_fonts(system: System, paths: TargetPaths) -> Plan:
    """Put a sans font into the prefix under the three Segoe names.

    Entirely inside the wine prefix, so it is IC-safe: DCS hashes game files,
    and this touches none of them. It is also the patch most likely to be
    undone without anybody noticing — rebuilding the prefix is this project's
    standard repair, and it takes the fonts with it.
    """
    if not system.exists(paths.prefix):
        return Plan(refusal=f"no wine prefix at {paths.prefix} yet")

    source = find_substitute_font(system)
    if source is None:
        return Plan(
            refusal="no substitute sans font found on this machine; "
            f"install one of {', '.join(SUBSTITUTE_FONTS)} (the dejavu-sans package on "
            "most distros) and run this again"
        )

    content = system.read_bytes(source)
    if content is None:
        return Plan(refusal=f"{source} could not be read")

    return Plan(writes=tuple(FileWrite(paths.fonts / name, content) for name in SEGOE_FONT_NAMES))


SEGOE_FONT_PATCH = Patch(
    id="segoe-fonts",
    summary="Segoe stand-in fonts in the prefix (the AH-64D crashes without them)",
    ic_risk=False,
    plan=plan_segoe_fonts,
)

REGISTRY: tuple[Patch, ...] = (SEGOE_FONT_PATCH,)


MULTIPLAYER_WARNING = (
    "edits a file DCS hashes: servers running pure-client integrity checks "
    "will reject this install until the patch is reverted"
)


def by_id(patch_id: str) -> Patch | None:
    return next((patch for patch in REGISTRY if patch.id == patch_id), None)


def safe_patches(patches: tuple[Patch, ...] = REGISTRY) -> tuple[Patch, ...]:
    """The patches that may be applied without being asked for by name.

    ADR-0004: a patch that edits a hashed game file costs the user multiplayer
    access, so it can never be swept up by a bare `patch apply`. Enforced here
    rather than trusted to each patch, so a registry entry cannot opt itself in
    by omission.
    """
    return tuple(patch for patch in patches if not patch.ic_risk)


def states(
    system: System,
    store: PatchStore,
    patches: tuple[Patch, ...] = REGISTRY,
) -> tuple[PatchState, ...]:
    """Every patch's standing, read from the store and the files themselves."""
    records = load(system, store)
    return tuple(_state(system, records.get(patch.id), patch) for patch in patches)


def unknown_states(patches: tuple[Patch, ...] = REGISTRY) -> tuple[PatchState, ...]:
    """Standings when no install is targeted, so nothing can be inspected."""
    return tuple(
        PatchState(patch=patch, status=PatchStatus.UNKNOWN, detail="no install targeted")
        for patch in patches
    )


def _state(system: System, record: PatchRecord | None, patch: Patch) -> PatchState:
    if record is None:
        return PatchState(patch=patch, status=PatchStatus.NOT_APPLIED, detail="not applied")

    drifted = [file.path for file in record.files if digest_of(system, file.path) != file.sha256]
    if drifted:
        names = ", ".join(sorted(path.name for path in drifted))
        return PatchState(
            patch=patch,
            status=PatchStatus.DRIFTED,
            # Named as the usual cause, not as damage: this is what a DCS
            # update looks like from here, and it is expected, not alarming.
            detail=f"overwritten since it was applied ({names}); a DCS update undoes this",
        )

    applied_to = f" to DCS {record.dcs_version}" if record.dcs_version else ""
    return PatchState(
        patch=patch,
        status=PatchStatus.APPLIED,
        detail=f"applied{applied_to} ({len(record.files)} file(s))",
    )


def apply_patch(
    system: System,
    writer: Writer,
    store: PatchStore,
    patch: Patch,
    paths: TargetPaths,
    dcs_version: str | None,
) -> Outcome:
    """Apply `patch`, or re-apply it if a DCS update has undone it.

    Already applied is a no-op, not a second application: the check is on the
    files' current contents, so it holds whether the last apply was a second
    ago or three DCS versions back.
    """
    records = load(system, store)
    state = _state(system, records.get(patch.id), patch)
    if state.status is PatchStatus.APPLIED:
        return Outcome(patch=patch, ok=True, changed=False, detail="already applied")

    plan = patch.plan(system, paths)
    if plan.refusal is not None:
        return Outcome(patch=patch, ok=False, changed=False, detail=plan.refusal)
    if not plan.writes:
        return Outcome(patch=patch, ok=True, changed=False, detail="nothing to do")

    reapplied = state.status is PatchStatus.DRIFTED
    previous = records.get(patch.id)
    known = {file.path: file for file in previous.files} if previous else {}
    written: list[FileRecord] = []
    try:
        for index, write in enumerate(plan.writes):
            backup = _preserve(
                system, writer, store, patch, index, write.path, known.get(write.path)
            )
            writer.write_bytes(write.path, write.content)
            written.append(FileRecord(path=write.path, sha256=digest(write.content), backup=backup))
    except OSError as error:
        # Whatever landed is recorded before the failure is reported, so a
        # half-applied patch is still fully revertible. On a re-apply the
        # files this attempt never reached are still patched from last time,
        # so their old records are carried over rather than dropped — losing
        # them would leave patched files that nothing knows how to undo.
        reached = {file.path for file in written}
        untouched = [file for file in known.values() if file.path not in reached]
        _record(writer, store, records, patch, dcs_version, written + untouched)
        return Outcome(patch=patch, ok=False, changed=bool(written), detail=str(error))

    _record(writer, store, records, patch, dcs_version, written)
    verb = "re-applied after a DCS update" if reapplied else "applied"
    return Outcome(
        patch=patch,
        ok=True,
        changed=True,
        detail=f"{verb} ({len(written)} file(s))",
    )


def _preserve(
    system: System,
    writer: Writer,
    store: PatchStore,
    patch: Patch,
    index: int,
    path: Path,
    previous: FileRecord | None,
) -> str | None:
    """The pristine copy to restore on revert, or None if there was no file.

    Drift is usually partial — a DCS update replaces one file and leaves the
    rest of ours alone — so re-applying must not blindly back up what it finds.
    A file still holding exactly what this patch last wrote is *ours*: backing
    it up again would enshrine our own content as the pristine copy, and revert
    would then restore the very patch it was asked to undo. Its original backup
    is carried forward instead.

    Anything else is content we did not write — what the updater left behind —
    and that is what the user must get back, so it is backed up afresh.

    The index prefix keeps two writes to same-named files in different
    directories apart, which the file name alone would not.
    """
    if previous is not None and digest_of(system, path) == previous.sha256:
        # Ours, untouched. Carried forward only if the backup is still there —
        # a store that has been cleared out leaves nothing to restore, and
        # deleting the file we wrote is then the honest revert.
        if previous.backup is None or system.exists(store.absolute(previous.backup)):
            return previous.backup

    if not system.exists(path):
        return None
    backup = store.backups_for(patch.id) / f"{index:02d}-{path.name}"
    writer.copy_file(path, backup)
    return store.relative(backup)


def _record(
    writer: Writer,
    store: PatchStore,
    records: dict[str, PatchRecord],
    patch: Patch,
    dcs_version: str | None,
    files: list[FileRecord],
) -> None:
    records[patch.id] = PatchRecord(patch_id=patch.id, dcs_version=dcs_version, files=tuple(files))
    save(writer, store, records)


def revert_patch(
    system: System,
    writer: Writer,
    store: PatchStore,
    patch: Patch,
) -> Outcome:
    """Put every file this patch touched back exactly as it was.

    Files that existed before are restored from the pristine backup; files the
    patch created are deleted. Either way the install ends up in the state it
    was in the moment before the patch ran.
    """
    records = load(system, store)
    record = records.get(patch.id)
    if record is None:
        return Outcome(patch=patch, ok=True, changed=False, detail="not applied")

    try:
        for file in record.files:
            if file.backup is None:
                writer.remove(file.path)
            else:
                writer.copy_file(store.absolute(file.backup), file.path)
    except OSError as error:
        # The record is deliberately left in place: some files are still
        # patched, and dropping the record would lose the only list of them.
        return Outcome(patch=patch, ok=False, changed=True, detail=str(error))

    writer.remove_tree(store.backups_for(patch.id))
    del records[patch.id]
    save(writer, store, records)
    return Outcome(
        patch=patch, ok=True, changed=True, detail=f"reverted ({len(record.files)} file(s))"
    )
