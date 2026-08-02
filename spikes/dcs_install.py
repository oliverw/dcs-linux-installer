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

  * INSTALL LOGIC   -- what #10/#11 will carry into the real tool.
  * CACHE HARNESS   -- dev-only scaffolding that makes iteration affordable.
                       Never ships. Every harness symbol is prefixed `harness_`.

Layout, three independent lifetimes under --data-root
(default /run/media/oliver/Data):

    <data-root>/dcs-linux-spike/
        prefix/          wiped every iteration
        game/            not wiped; --cold restores it from gold/ by reflink
        saved-games/     not wiped; ED login, config, keybinds, Logs/dcs.log

    <data-root>/.cache/dcs-linux/
        toolchain/       umu, GE-Proton, DCS_updater, winetricks payloads
        http/            nginx cache of http://*.digitalcombatsimulator.com
        gold/            known-good copy of the finished game directory

Every run writes spikes/runs/NNNN/ with pinned versions, every command run
and its output, the captured dcs.log, and the human verdict.

Usage:
    uv run spikes/dcs_install.py                 # warm: wipe+rebuild prefix, then
                                                   # hand off to the updater (needs
                                                   # the cache harness -- see below)
    uv run spikes/dcs_install.py --cold          # also restore game/ from gold/
    uv run spikes/dcs_install.py --dry-run       # print the plan, touch nothing
    uv run spikes/dcs_install.py --skip-harness  # real CDN, no cache (see #14)

The cache harness is not wired up yet: toolchain fetch and prefix creation
run fine without it, but the run refuses to hand off to the updater (where
the 150 GB download would happen) until it's implemented or --skip-harness
is passed.
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
DCS_UPDATER_HOST = "updates.digitalcombatsimulator.com"

# umu-launcher has no stable on-PATH install story yet (not on this machine,
# no umu-launcher system package on Fedora at time of writing), so invoke it
# through `uv tool run`, which resolves and caches it on demand -- no
# separate install step needed.
UMU_RUN_CMD = ("uv", "tool", "run", "--from", "umu-launcher", "umu-run")


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
    def http_cache_dir(self) -> Path:
        return self.cache_root / "http"

    @property
    def gold_dir(self) -> Path:
        return self.cache_root / "gold"


_GE_PROTON_VERSION_RE = re.compile(r"GE-Proton(\d+)-(\d+)")


def _latest_ge_proton_dir(layout: Layout) -> Path | None:
    """The newest cached GE-Proton build, sorted numerically (not lexically).

    Lexical sort would rank "GE-Proton9-27" above "GE-Proton10-1" -- wrong,
    and exactly the kind of drift `pinned_versions` exists to catch.
    """

    def sort_key(path: Path) -> tuple[int, int]:
        match = _GE_PROTON_VERSION_RE.fullmatch(path.name)
        return (int(match[1]), int(match[2])) if match else (0, 0)

    candidates = sorted(layout.ge_proton_dir.glob("GE-Proton*"), key=sort_key)
    return candidates[-1] if candidates else None


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
        candidates = sorted(saved_games_dir.glob("DCS*/Logs/dcs.log"))
        if not candidates:
            return None
        dest = self.dir / "dcs.log"
        if not self.dry_run:
            shutil.copyfile(candidates[-1], dest)
        return dest

    def write_verdict(self, verdict: str, notes: str = "") -> None:
        self._write_json("verdict.json", {"verdict": verdict, "notes": notes})

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
        "umu_version": _tool_version([*UMU_RUN_CMD, "--version"]),
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
    """Fetch GE-Proton and the DCS updater into the cache toolchain.

    Idempotent: skips anything already present. Must not assume umu-run or
    winetricks are on PATH -- the development machine has neither. umu-run
    itself needs no separate install step: `uv tool run --from umu-launcher
    umu-run` resolves and caches it on first use.
    """
    if not dry_run:
        layout.toolchain_dir.mkdir(parents=True, exist_ok=True)

    if not _latest_ge_proton_dir(layout):
        print(f"fetching latest GE-Proton into {layout.ge_proton_dir}")
        if not dry_run:
            layout.ge_proton_dir.mkdir(parents=True, exist_ok=True)
            _download_ge_proton(layout.ge_proton_dir)
        else:
            print(f"  (dry-run) would: GET {GE_PROTON_RELEASES_API}, extract tarball")

    installer_path = layout.toolchain_dir / "DCS_World_Web.exe"
    if not installer_path.exists():
        print(f"fetching DCS web installer into {installer_path}")
        if not dry_run:
            print(f"  MANUAL: download from {DCS_WEB_INSTALLER_URL} to {installer_path}")
            print("  (the installer page requires a browser session; not scripted yet)")
        else:
            print(f"  (dry-run) would: fetch installer from {DCS_WEB_INSTALLER_URL}")


def _download_ge_proton(dest_dir: Path) -> None:
    with urllib.request.urlopen(GE_PROTON_RELEASES_API) as resp:
        release = json.loads(resp.read())
    asset = next(a for a in release["assets"] if a["name"].endswith(".tar.gz"))
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
    # umu-run with an empty target just builds the prefix and exits -- this
    # is the documented way to create one without launching anything yet.
    # No check=False here: if prefix creation fails, everything downstream
    # (drive mapping, the updater launch) would be operating on a broken
    # prefix, so fail loudly rather than limping on.
    run_cmd([*UMU_RUN_CMD, ""], journal, dry_run=dry_run, env=env)
    return env


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
    layout: Layout, env: dict[str, str], journal: RunJournal, *, dry_run: bool
) -> None:
    """Hand off to the DCS updater GUI for login and module selection.

    This is interactive by design (see issue #1's Flow decision) -- a human
    logs in and picks modules. The script's job stops at getting the updater
    on screen with the cache harness in place.
    """
    installer_path = layout.toolchain_dir / "DCS_World_Web.exe"
    print(f"launching {installer_path} for interactive login/module selection")
    print("MANUAL: in the installer, set the install path to D:\\ (mapped to game/)")
    print("MANUAL: in DCS_updater.exe settings, disable torrent downloads (see #2)")
    run_cmd([*UMU_RUN_CMD, str(installer_path)], journal, dry_run=dry_run, env=env)


def record_human_verdict(journal: RunJournal, *, dry_run: bool) -> None:
    """Success bar: main menu, Instant Action free flight, ~60s flyable."""
    if dry_run:
        journal.write_verdict("dry-run", "no verdict collected in dry-run mode")
        return
    verdict = input("Reached main menu, flew Instant Action for ~60s? [pass/fail]: ").strip()
    notes = input("Notes (symptom if failed, anything notable if passed): ").strip()
    journal.write_verdict(verdict, notes)


# --------------------------------------------------------------------------
# CACHE HARNESS -- dev-only. Every symbol here is throwaway scaffolding that
# makes iteration on a 150 GB game affordable. MUST NEVER appear in the
# shipped tool (src/dcs_linux/). See issue #2's "known deviation": every run
# made with this harness active is not representative of a real user's
# environment -- that's what #14 validates separately.
# --------------------------------------------------------------------------


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


def harness_ensure_cache_active(layout: Layout, *, dry_run: bool) -> None:
    """Disable torrent, redirect the updater's HTTP traffic at the local cache.

    Called right before the updater launches -- that's where the 150 GB
    download happens, so that's the only step this needs to gate. Toolchain
    fetch and prefix creation don't touch the ED CDN and run fine without it.

    Asserts a cache hit and fails loudly on a miss -- see module docstring.
    Mechanism (netns + nginx on :80, /etc/hosts pointed at it, no root, no
    MITM cert; DCS_updater.exe settings for the torrent toggle) is the plan
    from issue #2; the concrete implementation is one of the empirical
    unknowns this ticket exists to pin down. Until it's wired up, refuse to
    proceed rather than silently falling back to the real CDN -- a silent
    fallback looks exactly like success.
    """
    if dry_run:
        print(
            f"  (dry-run) would: disable torrent, wire up rootless netns + nginx cache at "
            f"{layout.http_cache_dir}, fail loudly on a cache miss"
        )
        return
    raise NotImplementedError(
        "cache harness not wired up yet (#2) -- pass --skip-harness to hit the real CDN "
        "(slow, and only representative of the unharnessed run in #14)"
    )


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


@dataclass
class Args:
    data_root: Path
    cold: bool
    dry_run: bool
    skip_harness: bool


def parse_args(argv: list[str] | None = None) -> Args:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--cold", action="store_true", help="also restore game/ from gold/")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, touch nothing")
    parser.add_argument(
        "--skip-harness",
        action="store_true",
        help="real CDN, no local cache -- the unharnessed validation run (#14)",
    )
    ns = parser.parse_args(argv)
    return Args(
        data_root=ns.data_root, cold=ns.cold, dry_run=ns.dry_run, skip_harness=ns.skip_harness
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    layout = Layout(data_root=args.data_root)
    journal = RunJournal(RUNS_DIR, dry_run=args.dry_run)

    print(
        f"run {journal.number:04d} -- data_root={layout.data_root} cold={args.cold} "
        f"dry_run={args.dry_run} skip_harness={args.skip_harness}"
    )

    if args.cold:
        harness_restore_game_from_gold(layout, journal, dry_run=args.dry_run)

    ensure_toolchain(layout, journal, dry_run=args.dry_run)
    journal.write_versions(pinned_versions(layout))
    env = build_prefix(layout, journal, dry_run=args.dry_run)
    map_game_and_saved_games(layout, dry_run=args.dry_run)

    # Gate only the step that actually hits the ED CDN -- everything above
    # (toolchain, prefix, drive mapping) works fine without the harness.
    if not args.skip_harness:
        harness_ensure_cache_active(layout, dry_run=args.dry_run)
    launch_dcs_updater(layout, env, journal, dry_run=args.dry_run)
    record_human_verdict(journal, dry_run=args.dry_run)
    journal.capture_dcs_log(layout.saved_games_dir)

    print(f"run {journal.number:04d} journal: {journal.dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
