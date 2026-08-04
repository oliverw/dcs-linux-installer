"""Handing off to the DCS updater, and picking the install up afterwards.

The updater cannot be driven headlessly. It needs an Eagle Dynamics login and
a module selection through its own GUI, and no amount of automation gets round
that. So this module does not try: it makes the handoff *safe*, gets out of
the way, and then reads the disk to find out what happened.

Three things shape it.

- **Everything that decides where DCS lands is checked before the GUI opens.**
  The drive mapping is the whole reason the prefix stays disposable
  (ADR-0001): unmapped, the user picks `D:\\` in the installer and the
  updater writes 150 GB into a directory the next `--rebuild` deletes. That is
  a refusal, not a warning.
- **The download is measured in hours or days, and the user will interrupt
  it.** Nothing here writes into the game directory, and the install is only
  recorded once it is finished, so an interrupted run leaves exactly what the
  updater left. Re-running resumes it, because the updater resumes.
- **The web installer is a dead end after the first run.** `DCS_World_web.exe`
  refuses to reuse a directory it has already bootstrapped (#2), so anything
  past the first time goes through `DCS_updater.exe update` in the install
  itself — which is also how modules are added later and how an adopted
  install is updated.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dcs_linux.installs import DCS_EXE, UPDATER_EXE, find_install_root, read_version
from dcs_linux.paths import Layout
from dcs_linux.prefix import PREFIX_MARKER, Step, StepStatus, points_at, prefix_environment
from dcs_linux.registry import register
from dcs_linux.runner import Runner
from dcs_linux.system import System
from dcs_linux.writer import Writer

# It arrives by hand — the download page needs a browser session, so there is
# nothing to fetch from. Matched case-insensitively: the real filename has a
# lowercase `web` and hand-copied files acquire all sorts of spellings.
WEB_INSTALLER_NAME = "DCS_World_web.exe"
DOWNLOAD_PAGE = "https://www.digitalcombatsimulator.com/en/downloads/world/"

# `update` resumes a partial download and applies any pending update. Without
# it the updater opens its own menu, which is one more thing to explain.
UPDATE_ARGUMENT = "update"

# A Windows executable starts "MZ". The real installer is a few megabytes, so
# anything under this is a truncated download or a saved error page.
PE_MAGIC = b"MZ"
MIN_INSTALLER_BYTES = 1024 * 1024

# The user logs in, chooses modules and then waits — overnight, or over a
# weekend. `Runner`'s default hour would kill a perfectly healthy download, so
# this is deliberately longer than anyone's patience.
HANDOFF_TIMEOUT = 14 * 24 * 60 * 60.0

# What the install actually costs, from the runs behind this tool: a base
# install plus a couple of modules against the 33-module install measured in
# #2. Used for arithmetic only — see `briefing`.
BASE_INSTALL_GIB = 150
LARGE_INSTALL_GIB = 536


class Stage(StrEnum):
    """How far the install in the game directory has got."""

    ABSENT = "absent"
    # Bootstrapped and downloading, or abandoned partway. Not runnable, and
    # the two look identical on disk — which is why re-running resumes rather
    # than asking.
    PARTIAL = "partial"
    COMPLETE = "complete"


@dataclass(frozen=True)
class Progress:
    """What is in the game directory, read rather than remembered."""

    stage: Stage
    game_root: Path | None = None
    version: str | None = None

    @property
    def updater(self) -> Path | None:
        """The installed updater, which drives everything after bootstrap."""
        return self.game_root / UPDATER_EXE if self.game_root else None


@dataclass(frozen=True)
class Installer:
    """A web installer that has been checked before being run."""

    path: Path
    sha256: str
    size: int


@dataclass(frozen=True)
class Verified:
    """A checked installer, or why it will not be run.

    Shaped like `patches.Plan` and `prefix.Verbs`: a refusal is a first-class
    outcome here, not an empty result the caller has to interpret.
    """

    installer: Installer | None = None
    refusal: str | None = None


@dataclass(frozen=True)
class HandoffResult:
    """Every step of the handoff, and where the install ended up."""

    steps: tuple[Step, ...]
    progress: Progress
    installer: Installer | None = None

    @property
    def ok(self) -> bool:
        return not any(step.failed for step in self.steps)


def progress(system: System, layout: Layout) -> Progress:
    """How far the install under the game directory has got.

    The updater creates `<game>/DCS World`, so the install is a level below
    the directory the user chose. `DCS.exe` is what separates a finished
    install from a download that stopped: `autoupdate.cfg` and the updater
    both land early, long before the game can be run.
    """
    root = find_install_root(system, layout.game)
    if root is None:
        return Progress(stage=Stage.ABSENT)
    stage = Stage.COMPLETE if system.exists(root / DCS_EXE) else Stage.PARTIAL
    return Progress(stage=stage, game_root=root, version=read_version(system, root))


def find_installer(system: System, layout: Layout, given: Path | None = None) -> Path | None:
    """The web installer, where the user was told to put it or where they say.

    A named path is taken literally and never fallen back on: a user pointing
    at a specific file has a reason, and quietly running a different one is
    how the wrong edition gets installed.
    """
    if given is not None:
        return given if system.exists(given) else None
    wanted = WEB_INSTALLER_NAME.lower()
    for name in system.list_dir(layout.toolchain):
        if name.lower() == wanted:
            return layout.toolchain / name
    return None


def verify_installer(system: System, path: Path) -> Verified:
    """Check the installer is what it claims to be, before executing it.

    There is no published hash to compare against — the file comes off a page
    behind a browser session — so this verifies its *shape* and records the
    hash of what was actually run, which is the fact a bug report needs. The
    common failures are both caught: a saved HTML error page, and a transfer
    that stopped partway.
    """
    data = system.read_bytes(path)
    if data is None:
        return Verified(refusal=f"could not read {path}")
    if not data.startswith(PE_MAGIC):
        return Verified(
            refusal=f"{path} is not a Windows executable; re-download it from {DOWNLOAD_PAGE}"
        )
    if len(data) < MIN_INSTALLER_BYTES:
        return Verified(
            refusal=f"{path} is truncated at {len(data)} bytes; re-download it from {DOWNLOAD_PAGE}"
        )
    return Verified(
        installer=Installer(path=path, sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    )


def hours_at(gigabytes: int, megabits: float) -> float:
    """How long `gigabytes` takes at `megabits` per second, ideally."""
    return gigabytes * 8 * 1024 / (megabits * 3600)


def briefing(layout: Layout, current: Progress) -> str:
    """What the user has to do in the GUI, and what to expect of the wait.

    Printed before the window opens, because afterwards their attention is in
    it. Everything here was a way the manual install went wrong: installing to
    `C:\\` puts 150 GB inside the disposable prefix, and turning torrent off
    costs roughly an order of magnitude of throughput (ADR-0002).
    """
    resuming = current.stage is not Stage.ABSENT
    lines = [
        "The DCS updater is about to open. It needs your Eagle Dynamics login,",
        "so this part is yours — the tool picks up again when the window closes.",
        "",
    ]
    if resuming:
        lines += [
            "This install already exists, so the updater resumes it rather than",
            "starting again. Add modules here if you want them.",
        ]
    else:
        lines += [
            f"  1. Set the install path to D:\\ — that is {layout.game}, outside the",
            "     prefix. C:\\ puts the whole install inside the prefix, where the",
            "     next repair deletes it.",
            "  2. Leave torrent/P2P enabled. It is roughly ten times faster.",
            "  3. Log in and choose your modules.",
        ]
    lines += [
        "",
        f"Expect {BASE_INSTALL_GIB} GB or so for a base install, and up to "
        f"{LARGE_INSTALL_GIB} GB fully loaded.",
        "This tool has not measured Eagle Dynamics' real-world rate, so the times below are",
        "arithmetic from a link speed, not measured — treat them as a floor:",
        f"  100 Mbit/s → about {hours_at(BASE_INSTALL_GIB, 100):.0f} h for {BASE_INSTALL_GIB} GB",
        f"  500 Mbit/s → about {hours_at(BASE_INSTALL_GIB, 500):.0f} h for {BASE_INSTALL_GIB} GB",
        "",
        "Interrupting is safe. Close the terminal, reboot, come back tomorrow and",
        "run the same command: the updater resumes where it stopped.",
    ]
    return "\n".join(lines)


def handoff(
    system: System,
    writer: Writer,
    runner: Runner,
    layout: Layout,
    *,
    installer: Path | None = None,
    announce: Callable[[str], None] = lambda _: None,
) -> HandoffResult:
    """Run the updater against the prepared prefix, and record what it left.

    Idempotent in the only sense that matters here: run against a finished
    install it opens the updater, which finds nothing to do; run against a
    half-finished one it resumes. Nothing is written except the register
    entry, and that only once there is a game to register.
    """
    steps: list[Step] = []
    current = progress(system, layout)

    def take(step: Step) -> bool:
        steps.append(step)
        return not step.failed

    def stop() -> HandoffResult:
        return HandoffResult(steps=tuple(steps), progress=current)

    if not take(_require_prefix(system, layout)):
        return stop()
    if not take(_require_mapping(system, layout)):
        return stop()

    verified = Verified()
    if current.stage is Stage.ABSENT:
        verified = _verified_installer(system, layout, installer)
        if not take(_installer_step(verified)):
            return stop()

    announce(briefing(layout, current))
    if not take(_run_updater(runner, layout, current, verified.installer)):
        return stop()

    current = progress(system, layout)
    if not take(_completion(current)):
        return HandoffResult(steps=tuple(steps), progress=current, installer=verified.installer)

    take(_register(system, writer, layout, current))
    return HandoffResult(steps=tuple(steps), progress=current, installer=verified.installer)


def _require_prefix(system: System, layout: Layout) -> Step:
    """There has to be a prefix to install into, and `install` builds it."""
    if system.exists(layout.prefix / PREFIX_MARKER):
        return Step("prefix", StepStatus.SKIPPED, f"built at {layout.prefix}")
    return Step(
        "prefix",
        StepStatus.FAILED,
        f"no prefix at {layout.prefix}; run `dcs-linux install` to build one first",
    )


def _require_mapping(system: System, layout: Layout) -> Step:
    """The mapping decides where 150 GB lands, so it is checked, not assumed.

    The two halves fail differently, which is why they are judged apart.
    Without the drive, `D:\\` in the installer is not the game directory and
    the download goes inside the prefix — the accident ADR-0001 exists to
    prevent, so the handoff refuses. Without the saved-games link the install
    is fine and only the login is disposable, which is the user's to accept
    as long as they are told.
    """
    if not points_at(system, layout.prefix_game_drive, layout.game):
        return Step(
            "mapping",
            StepStatus.FAILED,
            f"D: does not point at {layout.game}, so the updater would install inside "
            f"{layout.prefix} and the next repair would delete it; "
            "run `dcs-linux install` to map it",
        )
    if not points_at(system, layout.prefix_saved_games, layout.saved_games):
        return Step(
            "mapping",
            StepStatus.DONE,
            f"D: → {layout.game}, but Saved Games is inside the prefix: your ED login "
            "(authdata.bin) and keybinds live there, so a prefix rebuild means you log "
            "in again; `dcs-linux install` maps it out",
        )
    return Step(
        "mapping",
        StepStatus.SKIPPED,
        f"D: → {layout.game}, Saved Games → {layout.saved_games}",
    )


def _verified_installer(system: System, layout: Layout, given: Path | None) -> Verified:
    found = find_installer(system, layout, given)
    if found is None:
        return Verified(
            refusal=f"no {WEB_INSTALLER_NAME} found. Download it from {DOWNLOAD_PAGE} — the "
            f"page needs a browser session, so this cannot be fetched for you — and put it "
            f"in {layout.toolchain}, or pass --installer PATH"
        )
    return verify_installer(system, found)


def _installer_step(verified: Verified) -> Step:
    if verified.refusal is not None or verified.installer is None:
        return Step("web installer", StepStatus.FAILED, verified.refusal or "not found")
    installer = verified.installer
    return Step(
        "web installer",
        StepStatus.DONE,
        f"{installer.path} verified, {installer.size // (1024 * 1024)} MB, "
        f"sha256 {installer.sha256[:16]}…",
    )


def _run_updater(
    runner: Runner, layout: Layout, current: Progress, installer: Installer | None
) -> Step:
    """Open the GUI and wait for the user to be done with it.

    The exit code is reported but not judged: the updater exits non-zero when
    the user closes it partway, which is a thing that happens on purpose. What
    was actually installed is read off the disk afterwards instead.
    """
    command = _updater_command(layout, current, installer)
    if command is None:
        return Step("updater", StepStatus.FAILED, "nothing to run the updater from")
    completed = runner.run(command, prefix_environment(layout), HANDOFF_TIMEOUT)
    if not completed.started:
        return Step("updater", StepStatus.FAILED, f"the updater did not run: {completed.detail}")
    return Step("updater", StepStatus.DONE, f"closed with exit code {completed.returncode}")


def _updater_command(
    layout: Layout, current: Progress, installer: Installer | None
) -> list[str] | None:
    """Bootstrap with the web installer, or update the install that exists.

    Never the web installer twice: it refuses a directory it has already
    bootstrapped (#2), and the installed updater is the path that resumes,
    adds modules and updates.
    """
    if current.updater is not None:
        return [str(layout.umu_run), str(current.updater), UPDATE_ARGUMENT]
    return [str(layout.umu_run), str(installer.path)] if installer else None


def _completion(current: Progress) -> Step:
    """Tell a finished install from an abandoned one, by what is on disk."""
    if current.stage is Stage.COMPLETE:
        version = f"DCS {current.version}" if current.version else "DCS"
        return Step("DCS", StepStatus.DONE, f"{version} installed in {current.game_root}")
    if current.stage is Stage.PARTIAL:
        return Step(
            "DCS",
            StepStatus.FAILED,
            f"the download in {current.game_root} is unfinished — no DCS.exe yet. Nothing "
            "is broken: run this command again and the updater resumes where it stopped",
        )
    return Step(
        "DCS",
        StepStatus.FAILED,
        "nothing was installed — the updater was closed before it started, or the login "
        "did not go through. Run this command again to try once more",
    )


def _register(system: System, writer: Writer, layout: Layout, current: Progress) -> Step:
    """Record the finished install so later commands need no rediscovery."""
    if current.game_root is None:
        return Step("register", StepStatus.SKIPPED, "nothing to register")
    changed = register(system, writer, layout, game=current.game_root, prefix=layout.prefix)
    detail = f"{current.game_root} in {layout.installs_register}"
    return Step("register", StepStatus.DONE if changed else StepStatus.SKIPPED, detail)
