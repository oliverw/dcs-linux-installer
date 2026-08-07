#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Scripted DCS World Standalone install spike (issue #2).

Not the product. This is the empirical ground truth #10 (prefix creation)
and #11 (updater handoff) lift their logic from, plus the transcript that
`docs/manual-install.md` is generated from.

Two halves, kept apart on purpose:

  * INSTALL LOGIC     -- what #10/#11 will carry into the real tool.
  * ITERATION HARNESS -- dev-only scaffolding that makes rebuilding against a
                         536 GB install affordable, by snapshotting it with
                         btrfs/xfs reflinks. Never ships. Every harness symbol
                         is prefixed `harness_`.

Layout, three independent lifetimes under --data-root
(default /run/media/oliver/Data):

    <data-root>/dcs-linux-spike/
        prefix/          wiped every iteration
        game/            not wiped; --cold restores it from gold/ by reflink
        saved-games/     not wiped; ED login, config, keybinds, Logs/dcs.log

    <data-root>/.cache/dcs-linux/
        toolchain/       umu, GE-Proton, DCS_updater, winetricks payloads
        gold/            known-good copy of the finished game directory

Every run writes spikes/runs/NNNN/ with pinned versions, every command run
and its output, the captured dcs.log, and the human verdict.

Usage:
    uv run spikes/dcs_install.py                 # warm: wipe+rebuild prefix,
                                                 # updater handoff, then launch
    uv run spikes/dcs_install.py --launch-only   # skip the updater; unattended
    uv run spikes/dcs_install.py --cold          # restore game/ from gold/ first
    uv run spikes/dcs_install.py --seed-gold     # snapshot game/ into gold/
    uv run spikes/dcs_install.py --dry-run       # print the plan, touch nothing

There is no HTTP cache. The updater downloads over HTTPS, so a proxy cache
cannot see the traffic; gold/ + reflink is the iteration mechanism instead.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SPIKES_DIR = Path(__file__).resolve().parent
RUNS_DIR = SPIKES_DIR / "runs"

DEFAULT_DATA_ROOT = Path("/run/media/oliver/Data")

# umu's own protonfixes db is keyed by GAMEID. Piggybacking Steam's DCS World
# Steam Edition id pulls in fixes maintained for the Steam build even though
# we run standalone. Confirm on the first real run whether that helps or hurts
# (see issue #2 acceptance criteria) and record the answer in the run journal.
STEAM_DCS_GAMEID = "umu-223750"

GE_PROTON_RELEASES_API = (
    "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases/latest"
)

DCS_WEB_INSTALLER_URL = "https://www.digitalcombatsimulator.com/en/downloads/world/"
DCS_INSTALLER_NAME = "DCS_World_web.exe"

# RUN 0001: umu-launcher is NOT on PyPI (404), so `uv tool run --from
# umu-launcher` can never work. Upstream ships an official fc44 RPM and a
# distro-agnostic zipapp. The zipapp wins here: no root, no dnf, works on
# immutable distros -- which issue #1 explicitly cares about.
UMU_ZIPAPP_URL = (
    "https://github.com/Open-Wine-Components/umu-launcher/releases/download/"
    "{version}/umu-launcher-{version}-zipapp.tar"
)
UMU_VERSION = "1.4.4"


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    data_root: Path

    @property
    def work_root(self) -> Path:
        return self.data_root / "dcs-linux-spike"

    @property
    def prefix_dir(self) -> Path:
        return self.work_root / "prefix"

    @property
    def game_dir(self) -> Path:
        return self.work_root / "game"

    @property
    def saved_games_dir(self) -> Path:
        return self.work_root / "saved-games"

    @property
    def cache_root(self) -> Path:
        return self.data_root / ".cache" / "dcs-linux"

    @property
    def toolchain_dir(self) -> Path:
        return self.cache_root / "toolchain"

    @property
    def ge_proton_dir(self) -> Path:
        return self.toolchain_dir / "ge-proton"

    @property
    def umu_run(self) -> Path:
        """The extracted umu zipapp -- a single self-contained executable."""
        return self.toolchain_dir / "umu" / "umu-run"

    @property
    def gold_dir(self) -> Path:
        return self.cache_root / "gold"


_GE_PROTON_VERSION_RE = re.compile(r"GE-Proton(\d+)-(\d+)")


def _latest_ge_proton_dir(layout: Layout) -> Path | None:
    """The newest cached GE-Proton build for this architecture.

    Sorted numerically, not lexically: lexical sort ranks "GE-Proton9-27"
    above "GE-Proton10-1". Names that don't parse are dropped rather than
    sorted to zero -- RUN 0001 left a stale "GE-Proton11-3-aarch64" here,
    and a wrong-arch build must never be selectable.
    """
    versioned: list[tuple[tuple[int, int], Path]] = []
    for path in layout.ge_proton_dir.glob("GE-Proton*"):
        match = _GE_PROTON_VERSION_RE.fullmatch(path.name)
        if match:
            versioned.append(((int(match[1]), int(match[2])), path))
    return max(versioned)[1] if versioned else None


# --------------------------------------------------------------------------
# Run journal
# --------------------------------------------------------------------------


class RunJournal:
    """Everything needed to diagnose a run without re-running it."""

    def __init__(self, runs_dir: Path, dry_run: bool = False) -> None:
        self.runs_dir = runs_dir
        self.dry_run = dry_run
        self.number = self._next_run_number(runs_dir)
        self.dir = runs_dir / f"{self.number:04d}"
        self._commands_log: list[dict[str, Any]] = []
        if not dry_run:
            self.dir.mkdir(parents=True, exist_ok=False)

    @staticmethod
    def _next_run_number(runs_dir: Path) -> int:
        if not runs_dir.exists():
            return 1
        existing = [p.name for p in runs_dir.iterdir() if p.is_dir() and p.name.isdigit()]
        return max((int(n) for n in existing), default=0) + 1

    def write_versions(self, versions: dict[str, str]) -> None:
        self._write_json("versions.json", versions)

    def record_command(self, cmd: list[str], result: subprocess.CompletedProcess[str]) -> None:
        entry = {
            "cmd": cmd,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "at": datetime.now(UTC).isoformat(),
        }
        self._commands_log.append(entry)
        if not self.dry_run:
            with (self.dir / "commands.log").open("a") as f:
                f.write(json.dumps(entry) + "\n")

    def capture_dcs_log(self, saved_games_dir: Path) -> Path | None:
        """Copy the newest dcs.log into the journal.

        This silently produced nothing for the first 13 runs: it only ran on
        the success path, and most runs raised before reaching it. It is now
        called from a finally, so a crashed run -- the case where the log
        matters most -- still gets one.
        """
        candidates = sorted(
            saved_games_dir.glob("DCS*/Logs/dcs.log"),
            key=lambda p: p.stat().st_mtime,
        )
        if not candidates:
            print("no dcs.log to capture")
            return None
        dest = self.dir / "dcs.log"
        if self.dry_run:
            return dest
        shutil.copyfile(candidates[-1], dest)
        print(f"captured dcs.log -> {dest}")
        return dest

    def write_verdict(
        self, verdict: str, notes: str = "", versions: dict[str, str] | None = None
    ) -> None:
        payload: dict[str, Any] = {"verdict": verdict, "notes": notes}
        if versions:
            payload["versions"] = versions
        self._write_json("verdict.json", payload)

    def _write_json(self, name: str, payload: dict[str, Any]) -> None:
        if self.dry_run:
            return
        (self.dir / name).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


# --------------------------------------------------------------------------
# Shared command runner -- every subprocess call goes through here so the
# journal has a complete transcript.
# --------------------------------------------------------------------------


def run_cmd(
    cmd: list[str],
    journal: RunJournal,
    *,
    dry_run: bool,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    print(f"+ {' '.join(cmd)}")
    if dry_run:
        result = subprocess.CompletedProcess(cmd, 0, stdout="(dry-run)", stderr="")
        journal.record_command(cmd, result)
        return result
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    journal.record_command(cmd, result)
    if check and result.returncode != 0:
        print(result.stdout, file=sys.stdout)
        print(result.stderr, file=sys.stderr)
        raise subprocess.CalledProcessError(result.returncode, cmd)
    return result


def pinned_versions(layout: Layout) -> dict[str, str]:
    """Snapshot everything that could explain why a run behaves differently."""
    ge_proton_dir = _latest_ge_proton_dir(layout)
    return {
        "umu_version": _tool_version([str(layout.umu_run), "--version"]),
        "ge_proton_version": ge_proton_dir.name if ge_proton_dir else "not-installed",
        "gameid": STEAM_DCS_GAMEID,
        "gpu_driver": _tool_version(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"]
        ),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "captured_at": datetime.now(UTC).isoformat(),
    }


def _tool_version(cmd: list[str]) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
        return result.stdout.strip() or result.stderr.strip() or "unknown"
    except FileNotFoundError:
        return "not-installed"
    except subprocess.TimeoutExpired:
        return "timed-out"


# --------------------------------------------------------------------------
# INSTALL LOGIC -- lifted by #10 (prefix creation) and #11 (updater handoff).
# --------------------------------------------------------------------------


def ensure_toolchain(layout: Layout, journal: RunJournal, *, dry_run: bool) -> None:
    """Fetch umu, GE-Proton and the DCS updater into the cache toolchain.

    Idempotent: skips anything already present. Must not assume umu-run or
    winetricks are on PATH -- the development machine has neither, and
    umu-launcher is not on PyPI (RUN 0001), so it is fetched as a zipapp.
    """
    if not dry_run:
        layout.toolchain_dir.mkdir(parents=True, exist_ok=True)

    if not layout.umu_run.exists():
        print(f"fetching umu-launcher {UMU_VERSION} zipapp into {layout.umu_run.parent}")
        if not dry_run:
            _download_umu_zipapp(layout)
        else:
            print(f"  (dry-run) would: GET {UMU_ZIPAPP_URL.format(version=UMU_VERSION)}")

    if not _latest_ge_proton_dir(layout):
        print(f"fetching latest GE-Proton into {layout.ge_proton_dir}")
        if not dry_run:
            layout.ge_proton_dir.mkdir(parents=True, exist_ok=True)
            _download_ge_proton(layout.ge_proton_dir)
        else:
            print(f"  (dry-run) would: GET {GE_PROTON_RELEASES_API}, extract tarball")

    if find_dcs_installer(layout) is None:
        installer_path = layout.toolchain_dir / DCS_INSTALLER_NAME
        print(f"DCS web installer not found at {installer_path}")
        if not dry_run:
            print(f"  MANUAL: download from {DCS_WEB_INSTALLER_URL} to {installer_path}")
            print("  (the installer page requires a browser session; not scripted yet)")
        else:
            print(f"  (dry-run) would: fetch installer from {DCS_WEB_INSTALLER_URL}")


def _find_game_binary(layout: Layout, name: str) -> Path | None:
    """First <game>/*/bin/<name>, or None."""
    return next(iter(sorted(layout.game_dir.glob(f"*/bin/{name}"))), None)


def find_installed_updater(layout: Layout) -> Path | None:
    """The updater inside an existing install, if there is one.

    RUN 0010: once DCS_World_web.exe has bootstrapped an install it refuses
    to reuse the directory, so re-running the web installer is a dead end.
    The installed bin/DCS_updater.exe is what continues an install -- and
    adopting an existing install is what issue #1's ownership model wants
    anyway.
    """
    return _find_game_binary(layout, "DCS_updater.exe")


def find_dcs_installer(layout: Layout) -> Path | None:
    """Locate the hand-dropped DCS web installer, case-insensitively.

    ED ships it as "DCS_World_web.exe" but capitalisation varies by mirror
    and by browser, and this file arrives by hand -- so match on a folded
    name rather than betting on one spelling.
    """
    if not layout.toolchain_dir.is_dir():
        return None
    for path in layout.toolchain_dir.iterdir():
        if path.is_file() and path.name.casefold() == DCS_INSTALLER_NAME.casefold():
            return path
    return None


def _download_umu_zipapp(layout: Layout) -> None:
    """Fetch and extract the umu zipapp -- yields <toolchain>/umu/umu-run."""
    layout.umu_run.parent.parent.mkdir(parents=True, exist_ok=True)
    url = UMU_ZIPAPP_URL.format(version=UMU_VERSION)
    tarball_path = layout.toolchain_dir / f"umu-launcher-{UMU_VERSION}-zipapp.tar"
    urllib.request.urlretrieve(url, tarball_path)
    # The tar contains umu/umu-run, so extract at toolchain_dir level.
    with tarfile.open(tarball_path) as tf:
        tf.extractall(layout.toolchain_dir, filter="data")
    tarball_path.unlink()
    layout.umu_run.chmod(0o755)


def _download_ge_proton(dest_dir: Path) -> None:
    """Fetch the x86_64 GE-Proton build.

    RUN 0001 grabbed the first .tar.gz asset and landed the aarch64 build on
    an x86_64 machine. Upstream names the x86_64 one without an arch suffix
    ("GE-Proton11-3.tar.gz" vs "GE-Proton11-3-aarch64.tar.gz"), so match the
    unsuffixed form explicitly rather than taking whatever comes first.
    """
    with urllib.request.urlopen(GE_PROTON_RELEASES_API) as resp:
        release = json.loads(resp.read())
    wanted = f"{release['tag_name']}.tar.gz"
    asset = next(a for a in release["assets"] if a["name"] == wanted)
    tarball_path = dest_dir / asset["name"]
    urllib.request.urlretrieve(asset["browser_download_url"], tarball_path)
    with tarfile.open(tarball_path) as tf:
        tf.extractall(dest_dir, filter="data")
    tarball_path.unlink()


def build_prefix(layout: Layout, journal: RunJournal, *, dry_run: bool) -> dict[str, str]:
    """Wipe and recreate the prefix, pinned to the cached GE-Proton."""
    if not dry_run:
        if layout.prefix_dir.exists():
            shutil.rmtree(layout.prefix_dir)
        layout.prefix_dir.mkdir(parents=True, exist_ok=True)

    ge_proton_dir = _latest_ge_proton_dir(layout)
    proton_path = str(ge_proton_dir) if ge_proton_dir else "GE-Proton"

    # Layer on top of the real environment -- umu-run needs a working
    # HOME/DISPLAY/XDG_RUNTIME_DIR to do anything, interactive or not.
    env = {
        **os.environ,
        "WINEPREFIX": str(layout.prefix_dir),
        "GAMEID": STEAM_DCS_GAMEID,
        "PROTONPATH": proton_path,
    }
    # umu-run with an empty target builds the prefix, then exits 1 trying to
    # ShellExecute "" ("Application could not be started") -- RUN 0002. The
    # prefix is fully created regardless, so verify the postcondition
    # (system.reg) instead of trusting the exit code, and still fail loudly
    # if the prefix genuinely didn't appear.
    run_cmd([str(layout.umu_run), ""], journal, dry_run=dry_run, env=env, check=False)
    if not dry_run and not (layout.prefix_dir / "system.reg").exists():
        raise RuntimeError(
            f"prefix creation failed: no system.reg under {layout.prefix_dir} "
            "-- see commands.log in the run journal"
        )
    return env


# Verbs from the community guides. vcrun2019 is BLACKLISTED (issue #1: it
# causes a system RAM leak) -- use 2015 or 2022 if a vcrun turns out to be
# needed at all.
WINETRICKS_VERBS = ("corefonts", "xact", "d3dcompiler_47")


def apply_winetricks(
    layout: Layout, env: dict[str, str], journal: RunJournal, *, dry_run: bool
) -> None:
    """Apply winetricks verbs to the umu-managed prefix.

    Issue #2 lists this as a known gap ("How winetricks verbs are applied to
    a umu-managed prefix is documented"). RUN 0002 answer: umu-run takes a
    `winetricks` positional argument -- `umu-run winetricks <verbs>` -- so
    winetricks never needs to be on PATH or told about the prefix itself.
    """
    # Marker lives inside the prefix so it can never claim verbs are present
    # in a prefix that lacks them. Note this saves nothing on a warm run --
    # build_prefix wipes the prefix, taking the marker with it. It only helps
    # if a future mode reuses an existing prefix.
    marker = layout.prefix_dir / ".winetricks-applied"
    if not dry_run and marker.exists() and marker.read_text().split() == list(WINETRICKS_VERBS):
        print("winetricks verbs already applied to this prefix; skipping")
        return

    print(f"applying winetricks verbs: {' '.join(WINETRICKS_VERBS)}")
    run_cmd(
        [str(layout.umu_run), "winetricks", *WINETRICKS_VERBS],
        journal,
        dry_run=dry_run,
        env=env,
    )
    if not dry_run:
        marker.write_text(" ".join(WINETRICKS_VERBS))


# RUN 0013: the AH-64D crashes entering a mission with
#   UIBASERENDERER: Cannot create font [] size 30!
#   C0000005 ACCESS_VIOLATION in CockpitBase.dll, via ah64d
# The cockpit asks for a Segoe font, gets an empty name back and
# dereferences null. corefonts installs 42 fonts, none of them Segoe.
#
# IC-SAFE: this writes only into the wine prefix. No hashed game file is
# touched, so it is safe for pure-client multiplayer servers.
#
# seguisym.ttf is Microsoft-licensed and not redistributable, so a locally
# installed font is substituted. Glyph coverage will not match Microsoft's
# exactly; symbology that relies on private-use codepoints may still render
# wrong even though the crash is gone.
FONT_SUBSTITUTE_CANDIDATES = (
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/liberation-sans/LiberationSans-Regular.ttf",
)
SEGOE_FONT_NAMES = ("seguisym.ttf", "seguisb.ttf", "segoeui.ttf")


def apply_font_patch(layout: Layout, journal: RunJournal, *, dry_run: bool) -> None:
    """Give the prefix a Segoe stand-in so the Apache cockpit can start."""
    fonts_dir = layout.prefix_dir / "drive_c" / "windows" / "Fonts"
    source = next((Path(c) for c in FONT_SUBSTITUTE_CANDIDATES if Path(c).exists()), None)
    if source is None:
        print("WARNING: no substitute font found; the AH-64D will crash entering a mission")
        return
    print(f"font patch: {source.name} -> {', '.join(SEGOE_FONT_NAMES)} (IC-safe)")
    if dry_run:
        return
    fonts_dir.mkdir(parents=True, exist_ok=True)
    for name in SEGOE_FONT_NAMES:
        shutil.copyfile(source, fonts_dir / name)
    journal.record_command(
        ["<font-patch>", str(source), *SEGOE_FONT_NAMES],
        subprocess.CompletedProcess([], 0, stdout=f"copied into {fonts_dir}", stderr=""),
    )


def map_game_and_saved_games(layout: Layout, *, dry_run: bool) -> None:
    """Map game/ and Saved Games/DCS out of the prefix.

    Both must live outside the disposable prefix: deleting and rebuilding the
    prefix (the repair the whole architecture rests on) must not destroy the
    150 GB install or the ED login / keybinds / dcs.log that live here.

    game/ is mapped as a wine drive letter (dosdevices/d: -> game/), since
    that's the mechanism wine/umu expect for an install living outside
    drive_c. Saved Games is symlinked directly into the user profile, where
    DCS looks for it by default. Confirm both hold up on the first real run
    and record any deviation in the run journal -- see issue #2 acceptance
    criteria.
    """
    users_dir = layout.prefix_dir / "drive_c" / "users" / "steamuser"
    saved_games_target = users_dir / "Saved Games"
    game_drive_target = layout.prefix_dir / "dosdevices" / "d:"
    print(f"mapping {layout.saved_games_dir} -> {saved_games_target}")
    print(f"mapping {layout.game_dir} -> {game_drive_target}")
    if not dry_run:
        layout.game_dir.mkdir(parents=True, exist_ok=True)
        layout.saved_games_dir.mkdir(parents=True, exist_ok=True)
        users_dir.mkdir(parents=True, exist_ok=True)
        _replace_with_symlink(saved_games_target, layout.saved_games_dir)

        game_drive_target.parent.mkdir(parents=True, exist_ok=True)
        _replace_with_symlink(game_drive_target, layout.game_dir)


def _replace_with_symlink(path: Path, target: Path) -> None:
    """Point path at target, whether path is currently absent, a symlink, or
    a real directory -- a fresh wine profile creates "Saved Games" for real.
    """
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    path.symlink_to(target)


def launch_dcs_updater(
    layout: Layout,
    env: dict[str, str],
    journal: RunJournal,
    *,
    dry_run: bool,
) -> None:
    """Hand off to the DCS updater GUI for login and module selection.

    This is interactive by design (see issue #1's Flow decision) -- a human
    logs in and picks modules. The script's job stops at getting the updater
    on screen with the prefix built and the D:\\ mapping already in place --
    without that mapping, the install lands inside the disposable prefix.
    """
    installed = find_installed_updater(layout)
    if installed is not None:
        print(f"existing install found; continuing via {installed}")
        cmd = [str(layout.umu_run), str(installed), "update"]
    else:
        installer_path = find_dcs_installer(layout) or layout.toolchain_dir / DCS_INSTALLER_NAME
        if not dry_run and not installer_path.exists():
            raise RuntimeError(
                f"DCS web installer missing at {installer_path}.\n"
                f"Download it from {DCS_WEB_INSTALLER_URL} (the page needs a browser\n"
                f"session, so this step cannot be scripted) and save it to that path."
            )
        print(f"launching {installer_path} for interactive login/module selection")
        print("MANUAL: set the install path to D:\\ (mapped to game/)")
        cmd = [str(layout.umu_run), str(installer_path)]
    # Torrent was only ever disabled to force traffic through the HTTP
    # cache. With no cache, P2P is simply the faster path.
    print("MANUAL: leave torrent/P2P ENABLED in updater settings -- it is faster")
    run_cmd(cmd, journal, dry_run=dry_run, env=env)


def find_dcs_exe(layout: Layout) -> Path | None:
    """bin/DCS.exe inside the installed game, if present."""
    return _find_game_binary(layout, "DCS.exe")


# Safe (non-IC-risky) launch tweaks from the community guides, per issue #1's
# patch registry seed. Applied here as environment/args rather than file edits,
# so nothing hashed by the integrity check is touched.
LAUNCH_ENV = {
    # 2.9.12.5336+ hangs querying WMI under wine
    "WINEDLLOVERRIDES": "wbemprox=n",
    "WINE_SIMULATE_WRITECOPY": "1",
}
LAUNCH_ARGS = ("--no-launcher",)  # skip the black-screen launcher


def launch_dcs(layout: Layout, env: dict[str, str], journal: RunJournal, *, dry_run: bool) -> None:
    """Launch DCS itself -- the success bar is a main menu and a flyable mission.

    Starting and running are different failures on Linux: the known
    breakages surface in a mission, not at the menu. So this only gets the
    game up; the human decides whether it actually flew.
    """
    dcs_exe = find_dcs_exe(layout)
    if dcs_exe is None:
        if dry_run:
            print("  (dry-run) would: launch bin/DCS.exe --no-launcher")
            return
        raise RuntimeError(f"no bin/DCS.exe under {layout.game_dir}; the install did not complete")
    print(f"launching {dcs_exe} {' '.join(LAUNCH_ARGS)}")
    print("SUCCESS BAR: main menu -> Instant Action free flight -> ~60s flyable")
    run_cmd(
        [str(layout.umu_run), str(dcs_exe), *LAUNCH_ARGS],
        journal,
        dry_run=dry_run,
        env={**env, **LAUNCH_ENV},
        check=False,  # a crash is a result to journal, not a reason to abort
    )


def record_human_verdict(
    journal: RunJournal,
    *,
    dry_run: bool,
    verdict: str | None = None,
    versions: dict[str, str] | None = None,
) -> None:
    """Success bar: main menu, Instant Action free flight, ~60s flyable.

    The verdict is a human judgement by design, but --verdict lets a run be
    driven non-interactively (and stdin may not be a TTY at all).
    """
    if dry_run:
        journal.write_verdict("dry-run", "no verdict collected in dry-run mode", versions)
        return
    if verdict is not None:
        journal.write_verdict(verdict, "supplied via --verdict", versions)
        return
    if not sys.stdin.isatty():
        journal.write_verdict("unrecorded", "no TTY and no --verdict; record by hand", versions)
        print("no TTY: verdict left unrecorded -- rerun with --verdict, or edit verdict.json")
        return
    answer = input("Reached main menu, flew Instant Action for ~60s? [pass/fail]: ").strip()
    notes = input("Notes (symptom if failed, anything notable if passed): ").strip()
    journal.write_verdict(answer, notes, versions)


# --------------------------------------------------------------------------
# ITERATION HARNESS -- dev-only. Throwaway scaffolding that makes rebuilding
# against a 536 GB install affordable, by snapshotting and restoring it with
# btrfs/xfs reflinks. MUST NEVER appear in the shipped tool (src/dcs_linux/).
#
# Unlike the deleted HTTP cache, this does not distort what is being tested:
# a restored install is byte-identical to the one the updater produced.
# --------------------------------------------------------------------------


def harness_seed_gold(layout: Layout, journal: RunJournal, *, dry_run: bool) -> None:
    """Snapshot the current install into gold/ by reflink.

    Measured: 251 GB in 1.3 s with no additional disk used, because btrfs
    shares the extents. This is what makes iteration cheap now that the
    HTTP cache turned out to be impossible.
    """
    if not dry_run and not layout.game_dir.is_dir():
        raise RuntimeError(f"nothing to snapshot: {layout.game_dir} does not exist")
    staging = layout.gold_dir.with_name(layout.gold_dir.name + ".seeding")
    if not dry_run:
        layout.gold_dir.parent.mkdir(parents=True, exist_ok=True)
        if staging.exists():
            shutil.rmtree(staging)
    print(f"reflink-snapshotting {layout.game_dir} -> {layout.gold_dir}")
    run_cmd(
        ["cp", "-a", "--reflink=always", str(layout.game_dir), str(staging)],
        journal,
        dry_run=dry_run,
    )
    if not dry_run:
        if layout.gold_dir.exists():
            shutil.rmtree(layout.gold_dir)
        staging.rename(layout.gold_dir)
    print("gold/ seeded; --cold now restores this install in seconds")


def harness_restore_game_from_gold(layout: Layout, journal: RunJournal, *, dry_run: bool) -> None:
    """--cold: restore game/ from the gold/ copy by btrfs/xfs reflink.

    Same filesystem as game/, so this is seconds, not a 150 GB copy -- as
    long as --data-root is a reflink-capable filesystem (btrfs/xfs). Fails
    loudly rather than silently falling back to a slow copy, since a silent
    fallback would look like success and burn the iteration budget.
    """
    if not layout.gold_dir.exists():
        raise RuntimeError(
            f"--cold requested but no gold copy at {layout.gold_dir}; "
            "run a warm install through to completion first, then seed gold/ manually."
        )
    # Reflink into a staging dir first and swap only on success, so a failed
    # copy can't leave game/ empty or half-restored.
    staging_dir = layout.game_dir.with_name(layout.game_dir.name + ".restoring")
    print(f"reflink-restoring {layout.game_dir} from {layout.gold_dir}")
    if not dry_run and staging_dir.exists():
        shutil.rmtree(staging_dir)
    run_cmd(
        ["cp", "--reflink=always", "-a", str(layout.gold_dir), str(staging_dir)],
        journal,
        dry_run=dry_run,
    )
    if not dry_run:
        if layout.game_dir.exists():
            shutil.rmtree(layout.game_dir)
        staging_dir.rename(layout.game_dir)


# POSTMORTEM (run 0010): there is no HTTP cache harness, and there cannot be
# one. Issue #2 assumed "the HTTP servers are plain http://, so the
# traffic is cacheable with no TLS interception". The host does offer plain
# http, but the updater does not use it -- it downloads over HTTPS to cdn77.
# An HTTP proxy cache never sees that traffic, and intercepting it needs a
# MITM certificate, which the ticket rules out.
#
# A working nginx/podman cache plus an /etc/hosts redirect was built and
# verified (MISS then HIT against the real CDN) before this was noticed. It
# cached the probe perfectly and zero bytes of DCS. Deleted rather than left
# as dead machinery, along with the machine state it required. No step in this
# script gates on it, and torrent/P2P is left at the setting a real user has.
#
# The one consequence that outlived the harness is in ADR-0002: never redirect
# www.digitalcombatsimulator.com. It is the HTTPS-only API/auth host, and
# pointing it at an http listener kills the updater outright (RUN 0009).


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


@dataclass
class Args:
    data_root: Path
    cold: bool
    dry_run: bool
    seed_gold: bool
    launch_only: bool
    update_only: bool
    verdict: str | None


def parse_args(argv: list[str] | None = None) -> Args:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--cold", action="store_true", help="also restore game/ from gold/")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, touch nothing")
    parser.add_argument(
        "--launch-only",
        action="store_true",
        help="skip the interactive updater; rebuild and launch DCS unattended",
    )
    parser.add_argument(
        "--seed-gold",
        action="store_true",
        help="snapshot the current install into gold/ by reflink, then exit",
    )
    parser.add_argument(
        "--update-only",
        action="store_true",
        help="run the DCS updater and stop; do not launch the game",
    )
    parser.add_argument(
        "--verdict",
        default=None,
        help="record this verdict instead of prompting (pass/fail/...)",
    )
    ns = parser.parse_args(argv)
    return Args(
        data_root=ns.data_root,
        cold=ns.cold,
        dry_run=ns.dry_run,
        seed_gold=ns.seed_gold,
        launch_only=ns.launch_only,
        update_only=ns.update_only,
        verdict=ns.verdict,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    layout = Layout(data_root=args.data_root)
    journal = RunJournal(RUNS_DIR, dry_run=args.dry_run)

    print(
        f"run {journal.number:04d} -- data_root={layout.data_root} cold={args.cold} "
        f"dry_run={args.dry_run}"
    )

    if args.seed_gold:
        harness_seed_gold(layout, journal, dry_run=args.dry_run)
        return 0

    if args.cold:
        harness_restore_game_from_gold(layout, journal, dry_run=args.dry_run)

    ensure_toolchain(layout, journal, dry_run=args.dry_run)
    journal.write_versions(pinned_versions(layout))
    env = build_prefix(layout, journal, dry_run=args.dry_run)
    apply_winetricks(layout, env, journal, dry_run=args.dry_run)
    apply_font_patch(layout, journal, dry_run=args.dry_run)
    map_game_and_saved_games(layout, dry_run=args.dry_run)

    versions = pinned_versions(layout)
    try:
        if not args.launch_only:
            launch_dcs_updater(layout, env, journal, dry_run=args.dry_run)
        if not args.update_only:
            launch_dcs(layout, env, journal, dry_run=args.dry_run)
        record_human_verdict(journal, dry_run=args.dry_run, verdict=args.verdict, versions=versions)
    finally:
        # A crashed run is exactly when the log matters, so capture it on
        # every exit path -- not only the happy one.
        journal.capture_dcs_log(layout.saved_games_dir)

    print(f"run {journal.number:04d} journal: {journal.dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
