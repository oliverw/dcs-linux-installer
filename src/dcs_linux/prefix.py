"""Building the runtime DCS needs: toolchain, prefix, verbs, mapping.

This is #2's spike hardened into a command. Everything here was established by
running it on real hardware — see `CONTEXT.md` and ADR-0001/0003 — so the
surprises are all recorded as comments rather than rediscovered.

Three ideas shape it:

- **The prefix is disposable, and nothing valuable may be inside it**
  (ADR-0001). The game directory is mapped in as `D:` and saved games is
  symlinked into the profile, so wiping the prefix costs a rebuild and never a
  download or a login. Every rebuild re-does the mapping, because a prefix
  without it is a prefix that eats the user's data on the *next* rebuild.
- **The toolchain is pinned, never resolved.** The umu and GE-Proton versions
  below are the ones flown to the success bar, not whatever is newest. A
  toolchain that moves under the user is one no bug report can be reproduced
  against.
- **Every step reports itself, and a failed step stops the rest.** Steps are
  independently skippable, so re-running the command on a healthy install
  writes nothing and says so.

The command is non-interactive throughout: it stops at a working prefix and an
empty game directory. Logging in and choosing modules is the updater's job,
which #11 hands off to.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dcs_linux.fetcher import Fetcher
from dcs_linux.paths import Layout
from dcs_linux.runner import Runner
from dcs_linux.system import System
from dcs_linux.writer import Writer

# Pinned, and pinned to what was verified: run 0015 reached the success bar on
# these two exact builds (`spikes/runs/0015/versions.json`). Bumping either is
# a deliberate change with a run journal behind it, not a routine refresh.
UMU_VERSION = "1.4.4"
GE_PROTON_VERSION = "GE-Proton11-3"

UMU_ZIPAPP_URL = (
    "https://github.com/Open-Wine-Components/umu-launcher/releases/download/"
    "{version}/umu-launcher-{version}-zipapp.tar"
)
# The x86_64 asset is the *unsuffixed* tarball. The same release also ships
# `-aarch64`, and run 0001 unpacked that one by taking "the first .tar.gz",
# which fails much later and for reasons that look nothing like a wrong build.
GE_PROTON_URL = (
    "https://github.com/GloriousEggroll/proton-ge-custom/releases/download/"
    "{version}/{version}.tar.gz"
)

# umu's protonfix key. `umu-223750` is DCS World's Steam appid: ProtonFixes
# recognises the name and then applies nothing at all (ADR-0003). Kept because
# it is the honest identifier for what is being run, not because it helps.
GAMEID = "umu-223750"

# From the community guides, and the set the spike proved sufficient.
WINETRICKS_VERBS = ("corefonts", "xact", "d3dcompiler_47")

# `vcrun2019` leaks system RAM until the machine swaps itself to death (#1).
# Refused rather than silently substituted: a user who asked for it is
# following a guide that says to, and needs to be told the guide is wrong.
BLACKLISTED_VERBS = {
    "vcrun2019": "it causes a system-wide RAM leak; use vcrun2015 or vcrun2022 instead",
}

# Applied to every launch of DCS, and recorded in the manifest so the launch
# and verify commands use exactly these. Environment rather than edits to game
# files, which is what keeps them integrity-check safe (ADR-0004): a dll
# override lives in the process, and multiplayer servers never see it.
LAUNCH_ENVIRONMENT = {
    # DCS 2.9.12.5336+ hangs querying WMI under wine.
    "WINEDLLOVERRIDES": "wbemprox=n",
    "WINE_SIMULATE_WRITECOPY": "1",
}

# umu creates the prefix and then exits 1 trying to ShellExecute the empty
# string (ADR-0003). The exit code is not a success signal; this file is.
PREFIX_MARKER = "system.reg"


class StepStatus(StrEnum):
    DONE = "done"
    # Already the case. A re-run of a healthy install is all skips.
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class Step:
    """One stage of the install, and how it went."""

    name: str
    status: StepStatus
    detail: str

    @property
    def failed(self) -> bool:
        return self.status is StepStatus.FAILED


@dataclass(frozen=True)
class Runtime:
    """What was built, and out of what.

    Written into the prefix as a manifest so a later command — or a bug report
    — can say which builds are in play without guessing from directory names,
    and so a re-run can tell an up-to-date prefix from one built with an older
    pin or a different set of verbs.
    """

    umu_version: str
    ge_proton: str
    gameid: str
    verbs: tuple[str, ...]
    environment: dict[str, str]
    prefix: Path
    game: Path
    saved_games: Path

    def as_json(self) -> dict[str, object]:
        return {
            "umu_version": self.umu_version,
            "ge_proton": self.ge_proton,
            "gameid": self.gameid,
            "verbs": list(self.verbs),
            "environment": dict(self.environment),
            "prefix": str(self.prefix),
            "game": str(self.game),
            "saved_games": str(self.saved_games),
        }


@dataclass(frozen=True)
class BuildResult:
    """Every step that ran, and the runtime if one was finished."""

    steps: tuple[Step, ...]
    runtime: Runtime | None = None

    @property
    def ok(self) -> bool:
        return not any(step.failed for step in self.steps)


@dataclass(frozen=True)
class Verbs:
    """The winetricks verbs to apply, or why a requested one is refused.

    Shaped like `patches.Plan`: a **refusal** is a first-class outcome in this
    codebase, not an empty result the caller has to interpret.
    """

    verbs: tuple[str, ...] = ()
    refusal: str | None = None


def resolve_verbs(extra: Sequence[str] = ()) -> Verbs:
    """The winetricks verbs to apply, or the reason a requested one is refused.

    Extras are appended rather than replacing the defaults: the defaults are
    what the install was verified with, and a user adding a verb is adding to
    that, not choosing to do without it.
    """
    verbs = list(WINETRICKS_VERBS)
    for verb in extra:
        if verb in BLACKLISTED_VERBS:
            return Verbs(refusal=f"refusing {verb}: {BLACKLISTED_VERBS[verb]}")
        if verb not in verbs:
            verbs.append(verb)
    return Verbs(verbs=tuple(verbs))


def build(
    system: System,
    writer: Writer,
    runner: Runner,
    fetcher: Fetcher,
    layout: Layout,
    *,
    verbs: tuple[str, ...] = WINETRICKS_VERBS,
    rebuild: bool = False,
) -> BuildResult:
    """Create the runtime environment DCS needs, stopping short of the game.

    Idempotent: on a prefix already built from the same pins and verbs every
    step skips, so this is safe to re-run and is how a rebuilt prefix gets its
    mapping back. `rebuild` wipes the prefix first — and only the prefix.
    """
    runtime = _runtime_for(layout, verbs)
    steps: list[Step] = []

    def take(step: Step) -> bool:
        steps.append(step)
        return not step.failed

    if not take(_wipe_prefix(system, writer, layout, rebuild=rebuild)):
        return BuildResult(steps=tuple(steps))
    if not take(_fetch_umu(system, writer, fetcher, layout)):
        return BuildResult(steps=tuple(steps))
    if not take(_fetch_ge_proton(system, fetcher, layout)):
        return BuildResult(steps=tuple(steps))

    installed = _installed_runtime(system, layout)
    if not take(_create_prefix(system, runner, layout, runtime, current=installed)):
        return BuildResult(steps=tuple(steps))
    if not take(_apply_verbs(runner, layout, runtime, current=installed)):
        return BuildResult(steps=tuple(steps))
    # The mapping is re-asserted on every run, skips or not. It is the one step
    # whose absence loses data rather than merely leaving something undone.
    if not take(_map_lifetimes(system, writer, layout)):
        return BuildResult(steps=tuple(steps))

    take(_record(system, writer, layout, runtime))
    return BuildResult(steps=tuple(steps), runtime=runtime)


def _runtime_for(layout: Layout, verbs: tuple[str, ...]) -> Runtime:
    return Runtime(
        umu_version=UMU_VERSION,
        ge_proton=GE_PROTON_VERSION,
        gameid=GAMEID,
        verbs=verbs,
        environment=dict(LAUNCH_ENVIRONMENT),
        prefix=layout.prefix,
        game=layout.game,
        saved_games=layout.saved_games,
    )


def _wipe_prefix(system: System, writer: Writer, layout: Layout, *, rebuild: bool) -> Step:
    """Delete the prefix, once it is certain nothing durable is inside it.

    The guard is the whole point. `--rebuild` is the repair every other
    recovery path rests on, and it is only cheap because the game directory and
    saved games live outside — so if either is inside this prefix, deleting it
    is not a repair, it is the accident the architecture exists to prevent.
    """
    if not rebuild:
        return Step("prefix wipe", StepStatus.SKIPPED, "not requested")
    inside = durable_inside_prefix(system, layout)
    if inside:
        return Step(
            "prefix wipe",
            StepStatus.FAILED,
            f"refusing to wipe {layout.prefix}: {inside[0]} is inside it and would be "
            "destroyed; move it out first",
        )
    if not system.exists(layout.prefix):
        return Step("prefix wipe", StepStatus.SKIPPED, f"no prefix at {layout.prefix}")
    writer.remove_tree(layout.prefix)
    return Step("prefix wipe", StepStatus.DONE, f"deleted {layout.prefix}")


def durable_inside_prefix(system: System, layout: Layout) -> list[Path]:
    """Which durable directories are inside the disposable prefix (ADR-0001).

    The one guard between `--rebuild` and a 536 GB download, so it compares
    *resolved* paths: `--game-dir ../prefix/game` and a game directory reached
    through a symlink both name something inside the prefix while looking to a
    string comparison like they do not.
    """
    prefix = system.resolve(layout.prefix)
    return [
        path
        for path in (layout.game, layout.saved_games)
        if _contains(prefix, system.resolve(path))
    ]


def _contains(container: Path, path: Path) -> bool:
    """Whether `path` is `container` itself or somewhere beneath it."""
    return container == path or container in path.parents


def _fetch_umu(system: System, writer: Writer, fetcher: Fetcher, layout: Layout) -> Step:
    """The umu zipapp (ADR-0003): not on PyPI, so it is fetched by hand.

    Skipped only when the *pinned* version is the one on disk. A zipapp that
    is merely present says nothing about which build it is, and a re-fetch
    after a bump is what keeps the manifest honest (ADR-0008).
    """
    installed = (system.read_text(layout.umu_version_marker) or "").strip()
    if system.exists(layout.umu_run) and installed == UMU_VERSION:
        return Step("umu-launcher", StepStatus.SKIPPED, f"{UMU_VERSION} at {layout.umu_run}")
    url = UMU_ZIPAPP_URL.format(version=UMU_VERSION)
    # The tar holds `umu/umu-run`, so it unpacks at the toolchain root.
    failure = fetcher.fetch_archive(url, layout.toolchain)
    if failure is not None:
        return Step("umu-launcher", StepStatus.FAILED, failure)
    if not system.exists(layout.umu_run):
        return Step(
            "umu-launcher",
            StepStatus.FAILED,
            f"unpacked {url} but no umu-run at {layout.umu_run}",
        )
    writer.make_executable(layout.umu_run)
    writer.write_bytes(layout.umu_version_marker, f"{UMU_VERSION}\n".encode())
    return Step("umu-launcher", StepStatus.DONE, f"{UMU_VERSION} at {layout.umu_run}")


def _fetch_ge_proton(system: System, fetcher: Fetcher, layout: Layout) -> Step:
    build_dir = layout.ge_proton_build(GE_PROTON_VERSION)
    if system.exists(build_dir / "proton"):
        return Step("GE-Proton", StepStatus.SKIPPED, f"{GE_PROTON_VERSION} already at {build_dir}")
    url = GE_PROTON_URL.format(version=GE_PROTON_VERSION)
    failure = fetcher.fetch_archive(url, layout.ge_proton)
    if failure is not None:
        return Step("GE-Proton", StepStatus.FAILED, failure)
    if not system.exists(build_dir / "proton"):
        return Step(
            "GE-Proton", StepStatus.FAILED, f"unpacked {url} but no proton script in {build_dir}"
        )
    return Step("GE-Proton", StepStatus.DONE, f"{GE_PROTON_VERSION} at {build_dir}")


def prefix_environment(layout: Layout) -> dict[str, str]:
    """What umu needs in the environment to act on our prefix.

    Not the launch environment: these three say *which* prefix and *which*
    Proton, and are needed by winetricks and the updater as much as by the
    game. `LAUNCH_ENVIRONMENT` is layered on top only when DCS itself runs.
    """
    return {
        "WINEPREFIX": str(layout.prefix),
        "GAMEID": GAMEID,
        "PROTONPATH": str(layout.ge_proton_build(GE_PROTON_VERSION)),
    }


def _create_prefix(
    system: System, runner: Runner, layout: Layout, runtime: Runtime, *, current: Runtime | None
) -> Step:
    """Build the wine prefix, judging success by what is on disk.

    `umu-run ""` creates the prefix and then exits 1 failing to ShellExecute
    the empty string (ADR-0003), so the exit code is worthless here and
    `system.reg` is the postcondition that is checked instead.
    """
    built = system.exists(layout.prefix / PREFIX_MARKER)
    # Only the Proton build is compared, not the whole pin: umu drives Proton
    # but puts nothing of its own into the prefix, so a umu bump is no reason
    # to throw away a working one.
    if built and current is not None and current.ge_proton == runtime.ge_proton:
        return Step("prefix", StepStatus.SKIPPED, f"{layout.prefix} already built")

    completed = runner.run([str(layout.umu_run), ""], prefix_environment(layout))
    if not completed.started:
        return Step("prefix", StepStatus.FAILED, f"could not run umu-run: {completed.detail}")
    if not system.exists(layout.prefix / PREFIX_MARKER):
        return Step(
            "prefix",
            StepStatus.FAILED,
            f"umu-run left no {PREFIX_MARKER} in {layout.prefix}; the prefix was not created",
        )
    return Step("prefix", StepStatus.DONE, f"built at {layout.prefix} on {runtime.ge_proton}")


def _apply_verbs(
    runner: Runner, layout: Layout, runtime: Runtime, *, current: Runtime | None
) -> Step:
    """Apply the winetricks verbs to the umu-managed prefix.

    `umu-run winetricks <verbs>` — winetricks needs no separate install and
    never needs telling where the prefix is (ADR-0003).
    """
    if current is not None and current.verbs == runtime.verbs:
        return Step("winetricks", StepStatus.SKIPPED, f"already applied: {_listed(runtime.verbs)}")
    command = [str(layout.umu_run), "winetricks", *runtime.verbs]
    completed = runner.run(command, prefix_environment(layout))
    if not completed.started:
        return Step("winetricks", StepStatus.FAILED, f"winetricks did not run: {completed.detail}")
    if completed.returncode != 0:
        return Step(
            "winetricks",
            StepStatus.FAILED,
            f"winetricks exited {completed.returncode} applying {_listed(runtime.verbs)}",
        )
    return Step("winetricks", StepStatus.DONE, _listed(runtime.verbs))


def _listed(verbs: tuple[str, ...]) -> str:
    return ", ".join(verbs)


def _map_lifetimes(system: System, writer: Writer, layout: Layout) -> Step:
    """Map the two durable directories into the disposable prefix (ADR-0001).

    The game directory becomes `D:` and saved games is symlinked into the
    profile. Both are created if absent, so the updater has somewhere to
    install to and DCS has somewhere to keep the login.
    """
    inside = durable_inside_prefix(system, layout)
    if inside:
        return Step(
            "mapping",
            StepStatus.FAILED,
            f"{inside[0]} is inside {layout.prefix}; the game directory and saved games "
            "must live outside it, or rebuilding the prefix would destroy them",
        )
    writer.make_dirs(layout.game)
    writer.make_dirs(layout.saved_games)
    detail = f"D: → {layout.game}, Saved Games → {layout.saved_games}"
    mapped = (
        (layout.prefix_game_drive, layout.game),
        (layout.prefix_saved_games, layout.saved_games),
    )
    if all(points_at(system, link, target) for link, target in mapped):
        return Step("mapping", StepStatus.SKIPPED, detail)
    for link, target in mapped:
        writer.symlink(link, target)
    return Step("mapping", StepStatus.DONE, detail)


def points_at(system: System, link: Path, target: Path) -> bool:
    """Whether `link` is a symlink already resolving to `target`.

    A link to somewhere *else* is the dangerous case — a prefix mapped at a
    previous game directory — so this asks where it points, not merely whether
    it is a link.
    """
    return system.is_symlink(link) and system.resolve(link) == target


def _record(system: System, writer: Writer, layout: Layout, runtime: Runtime) -> Step:
    """Write the runtime manifest, unless it already says exactly this.

    Re-running on a healthy install has to write *nothing* — a step that
    reports DONE having changed nothing makes every other DONE unreadable.
    """
    if read_manifest(system, layout) == runtime:
        return Step("manifest", StepStatus.SKIPPED, f"unchanged in {layout.manifest}")
    writer.write_bytes(
        layout.manifest, (json.dumps(runtime.as_json(), indent=2, sort_keys=True) + "\n").encode()
    )
    return Step("manifest", StepStatus.DONE, f"pins recorded in {layout.manifest}")


def _installed_runtime(system: System, layout: Layout) -> Runtime | None:
    """What the manifest says built this prefix, if it is still standing.

    A manifest without a prefix around it describes nothing, so the marker is
    checked too — otherwise a wiped prefix would be reported as up to date.
    """
    if not system.exists(layout.prefix / PREFIX_MARKER):
        return None
    return read_manifest(system, layout)


def read_manifest(system: System, layout: Layout) -> Runtime | None:
    """The recorded runtime for this prefix, or None if there is not one.

    Unreadable or malformed reads as absent: the manifest is a record, and a
    damaged record must cause a rebuild rather than an error.
    """
    text = system.read_text(layout.manifest)
    if text is None:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return Runtime(
            umu_version=str(payload["umu_version"]),
            ge_proton=str(payload["ge_proton"]),
            gameid=str(payload["gameid"]),
            verbs=tuple(str(verb) for verb in payload["verbs"]),
            environment={str(key): str(value) for key, value in payload["environment"].items()},
            prefix=Path(str(payload["prefix"])),
            game=Path(str(payload["game"])),
            saved_games=Path(str(payload["saved_games"])),
        )
    except (KeyError, TypeError, AttributeError):
        return None
