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
- **IC-safe by default** (ADR-0004). Every patch carries `ic_risk`. The two
  risky ones here rewrite files DCS hashes, and the engine will not let them
  near an install that did not name them: `safe_patches` is what a bare
  `patch apply` sweeps up, and it drops them.

The engine knows nothing about fonts. It knows about plans, backups, hashes
and state, so the next patch is a planner function and a registry entry.

Clearing the shader cache lives here too, but deliberately *not* as a patch:
it deletes regenerable files rather than writing any, so there is nothing to
back up, nothing to record and nothing to revert.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dcs_linux.distro import detect_distro
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


# Every spelling the voice-chat entries have gone by. Matched case-insensitively
# because optionsDb.lua mixes `voice_chat` keys with `VoiceChat` module names.
VOICE_CHAT = re.compile(r"voice[_ ]?chat", re.IGNORECASE)


@dataclass(frozen=True)
class CommentedOut:
    """The result of disabling some Lua lines by commenting them out."""

    text: str
    # The lines that were commented out, in file order.
    disabled: tuple[str, ...]
    # Matching lines left alone because commenting them out would have broken
    # the file — an opening brace whose partner is on a later line.
    unsafe: tuple[str, ...]


def comment_out(text: str, pattern: re.Pattern[str]) -> CommentedOut:
    """Comment out every line matching `pattern`, or report why it is not safe to.

    Lua's `--` comments to end of line, so this only works on a line that is a
    whole statement by itself and nothing else's. Three ways it would not be,
    all of which are reported rather than written:

    - the line opens a table (`["voice_chat"] = {`) — commenting it out leaves
      the table unterminated and the file unloadable;
    - the table opens on the *next* line, which is the same thing spelled over
      two lines;
    - the line carries other entries too (`{ ["voice_chat"] = true, x = 1 }`) —
      commenting it out would silently take `x` with it.

    A patch that cannot make a safe edit refuses instead of making an unsafe
    one, so anything but a plain one-line entry ends up in `unsafe`.
    """
    lines = text.splitlines(keepends=True)
    disabled: list[str] = []
    unsafe: list[str] = []
    result: list[str] = []

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not pattern.search(line) or stripped.startswith("--"):
            result.append(line)
            continue
        if _is_whole_statement(line, _next_code_line(lines, index)):
            disabled.append(stripped)
            indent = line[: len(line) - len(line.lstrip())]
            result.append(f"{indent}-- {line.lstrip()}")
        else:
            unsafe.append(stripped)
            result.append(line)

    return CommentedOut(text="".join(result), disabled=tuple(disabled), unsafe=tuple(unsafe))


def _next_code_line(lines: list[str], index: int) -> str:
    """The next line with anything on it, or "" at the end of the file."""
    return next((line.strip() for line in lines[index + 1 :] if line.strip()), "")


def _is_whole_statement(line: str, following: str) -> bool:
    """Whether commenting this line out removes exactly one entry and nothing else."""
    code = _without_strings(line)
    if "{" in code or "}" in code:
        return False
    if code.count("=") != 1:
        return False
    if not _balanced(code):
        return False
    # `["voice_chat"] =` with the table on the line below is the multi-line
    # form written differently, and just as unsafe to comment out.
    return not following.startswith("{")


def _without_strings(line: str) -> str:
    """The line with the contents of quoted strings removed.

    Brackets and `=` inside a string are text, not syntax, and counting them
    would make a perfectly safe line look unsafe.
    """
    return re.sub(r'"[^"]*"|\'[^\']*\'', '""', line)


def _balanced(line: str) -> bool:
    """Whether a line closes every bracket it opens."""
    depth = 0
    for character in line:
        if character in "([":
            depth += 1
        elif character in ")]":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def plan_voice_chat(system: System, paths: TargetPaths) -> Plan:
    """Disable the voice-chat entries in `optionsDb.lua`.

    IC-risky: `optionsDb.lua` is a game file, and DCS hashes it, so an install
    carrying this patch is rejected by servers running pure-client integrity
    checks until it is reverted.

    Widely cited for a voice-chat crash on Linux, and **not** reproduced here:
    on 2.9.28.26385 `VoiceChat.dll` loads clean with no edit (ADR-0004). It is
    in the registry so a user hitting the crash on some other version has the
    fix, not because it is expected to be needed — which is also why finding
    nothing to disable is a refusal rather than a silent success.
    """
    raw = system.read_bytes(paths.options_db)
    if raw is None:
        return Plan(refusal=f"no optionsDb.lua at {paths.options_db}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return Plan(refusal=f"{paths.options_db} is not valid UTF-8; refusing to rewrite it")

    result = comment_out(text, VOICE_CHAT)
    if result.unsafe:
        return Plan(
            refusal=f"the voice-chat entry in {paths.options_db} spans several lines "
            f"({result.unsafe[0]!r}); commenting it out would leave the file unloadable"
        )
    if not result.disabled:
        return Plan(
            refusal=f"nothing referring to voice chat in {paths.options_db}; "
            "on current DCS versions this fix is not needed (see ADR-0004)"
        )
    return Plan(writes=(FileWrite(paths.options_db, result.text.encode()),))


VOICE_CHAT_PATCH = Patch(
    id="voice-chat",
    summary="disable the voice-chat entries in optionsDb.lua (edits a hashed game file)",
    ic_risk=True,
    plan=plan_voice_chat,
)


# ImageMagick 7 renamed the binary; DCS-era distros still ship either.
IMAGEMAGICK_COMMANDS = ("magick", "convert")

# The MFD and TEDAC sight textures the conversion workaround is about. Matched
# on the name rather than by a fixed path: the AH-64D's texture directories are
# laid out differently between DCS versions.
MFD_TEXTURE = re.compile(r"^(MFD_LCD_AH64|TEDAC)", re.IGNORECASE)

# `Mods/aircraft/<module>/Textures/<pack>/<file>`. Bounded so a patch plan can
# never turn into a walk of a 536 GB game directory.
TEXTURE_SEARCH_DEPTH = 5


def find_mfd_textures(system: System, paths: TargetPaths) -> tuple[Path, ...]:
    """Loose MFD and sight textures under the aircraft modules.

    Loose only: DCS ships these inside `.zip` texture archives, and rewriting
    an archive's contents is not something this patch does.
    """
    found: list[Path] = []
    _collect_textures(system, paths.aircraft_mods, TEXTURE_SEARCH_DEPTH, found)
    return tuple(sorted(found))


def _collect_textures(system: System, root: Path, depth: int, found: list[Path]) -> None:
    if depth <= 0:
        return
    for entry in system.list_dir(root):
        candidate = root / entry
        if entry.lower().endswith(".dds"):
            if MFD_TEXTURE.match(entry):
                found.append(candidate)
            continue
        _collect_textures(system, candidate, depth - 1, found)


def plan_mfd_textures(system: System, paths: TargetPaths) -> Plan:
    """Re-encode the AH-64D MFD and sight textures as uncompressed DDS.

    IC-risky: these are game files, and DCS hashes them.

    Like the voice-chat fix, this is a workaround for a symptom not reproduced
    here — the TADS sight renders correctly on 2.9.28.26385 once first-use
    shader compilation settles (ADR-0004). The `texture ... not found` lines
    that make people reach for it name runtime render targets, not shipped
    files, so on a current install there is usually nothing to convert and this
    says so rather than pretending to have fixed something.
    """
    converter = _imagemagick(system)
    if converter is None:
        hint = detect_distro(system).install_hint("magick")
        return Plan(refusal=f"ImageMagick is needed to convert textures; install it: {hint}")

    targets = find_mfd_textures(system, paths)
    if not targets:
        # Said plainly, because it is the answer a stock install gets: DCS
        # ships these textures inside `.zip` archives, and this patch does not
        # open archives. Claiming "your install is fine" would be a clinical
        # finding this patch is in no position to make.
        return Plan(
            refusal=f"no loose MFD or sight textures under {paths.aircraft_mods}; DCS ships "
            "them inside .zip archives, which this patch does not open. On 2.9.28.26385 the "
            "sight renders correctly without any conversion (see ADR-0004)"
        )

    writes: list[FileWrite] = []
    for target in targets:
        converted = system.run_binary([converter, str(target), *DDS_CONVERSION_ARGS])
        if not converted:
            return Plan(refusal=f"{converter} could not convert {target}")
        writes.append(FileWrite(target, converted))
    return Plan(writes=tuple(writes))


# Uncompressed BGRA8, written to stdout so no temporary file is involved and
# the plan holds the finished bytes like every other plan does.
DDS_CONVERSION_ARGS = ("-define", "dds:compression=none", "dds:-")


def _imagemagick(system: System) -> str | None:
    return next((name for name in IMAGEMAGICK_COMMANDS if system.which(name)), None)


MFD_TEXTURE_PATCH = Patch(
    id="mfd-textures",
    summary="re-encode the AH-64D MFD and sight textures (edits hashed game files)",
    ic_risk=True,
    plan=plan_mfd_textures,
)


REGISTRY: tuple[Patch, ...] = (SEGOE_FONT_PATCH, VOICE_CHAT_PATCH, MFD_TEXTURE_PATCH)


@dataclass(frozen=True)
class Cleared:
    """What clearing the shader cache removed."""

    directories: tuple[Path, ...]
    detail: str


def clear_shader_cache(system: System, writer: Writer, paths: TargetPaths) -> Cleared:
    """Delete DCS's compiled-shader directories.

    Not a patch, and deliberately not in the registry. It writes nothing, so
    there is nothing to back up, nothing to record and nothing to revert; the
    files it deletes are ones DCS regenerates on the next launch. It is also
    unconditionally IC-safe — the caches live in saved games, and DCS hashes
    none of them.

    The cost is time, not risk: the launch after this one recompiles the whole
    cache and takes several minutes.
    """
    # Deduplicated by resolved path: on a mapped install the in-prefix saved
    # games *is* the durable one, so both spellings name a single directory
    # and reporting it twice would overstate what was cleared.
    # Resolved, and deduplicated on the result: on a mapped install the
    # in-prefix saved games *is* the durable one, so both spellings name a
    # single directory. Deleting it twice is harmless, but saying so is not.
    present = tuple(
        dict.fromkeys(
            system.resolve(directory)
            for directory in paths.shader_caches
            if system.exists(directory)
        )
    )

    for directory in present:
        writer.remove_tree(directory)
    if not present:
        return Cleared(directories=(), detail="no shader cache to clear")
    return Cleared(
        directories=present,
        detail=f"cleared {len(present)} shader cache director"
        f"{'ies' if len(present) > 1 else 'y'}; the next launch recompiles them "
        "and takes several minutes",
    )


# The consequence, in one place. It is load-bearing wording (ADR-0004 asks for
# a multiplayer warning on both the refusal and the opt-in path), and `check`
# and `patch --list` both have to say the same thing as the warning itself.
SERVERS_REJECT = "servers running pure-client integrity checks will reject this install"

MULTIPLAYER_WARNING = f"edits a file DCS hashes: {SERVERS_REJECT} until the patch is reverted"


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


def risky_in_place(states: tuple[PatchState, ...]) -> tuple[PatchState, ...]:
    """The IC-risky patches whose edits are on disk, wholly or partly.

    A drifted risky patch counts: drift is per-file, so a DCS update that put
    one file back can leave the others still modified — and still enough to
    fail an integrity check.
    """
    return tuple(
        state
        for state in states
        if state.patch.ic_risk and state.status in (PatchStatus.APPLIED, PatchStatus.DRIFTED)
    )


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
        # Ours, untouched: the original backup still describes what was here
        # before this patch, so it is carried forward rather than retaken.
        if previous.backup is None or system.exists(store.absolute(previous.backup)):
            return previous.backup
        # Ours, but the pristine copy has gone from the store. Backing the file
        # up now would enshrine our own content as the original — the very
        # thing this function exists to prevent. Recording no backup makes
        # revert delete the file, which is the honest answer: we know we wrote
        # it, and we no longer know what it replaced.
        return None

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
