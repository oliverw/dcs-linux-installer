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

The cache harness gates only the updater handoff (where the 150 GB
download happens); toolchain fetch and prefix creation run without it. It
needs rootless podman, and a one-time
`sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80` so the cache can
bind :80 -- the harness checks and tells you if that's missing.
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
import time
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
    def http_cache_dir(self) -> Path:
        return self.cache_root / "http"

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
    # The prefix is wiped every iteration, so this reruns every time and
    # costs minutes of font registering. Guard on a marker inside the prefix:
    # it dies with the prefix, so it can never mask a genuinely missing verb.
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
    on screen with the cache harness in place.
    """
    installer_path = find_dcs_installer(layout) or layout.toolchain_dir / DCS_INSTALLER_NAME
    if not dry_run and not installer_path.exists():
        raise RuntimeError(
            f"DCS web installer missing at {installer_path}.\n"
            f"Download it from {DCS_WEB_INSTALLER_URL} (the page needs a browser\n"
            f"session, so this step cannot be scripted) and save it to that path."
        )
    print(f"launching {installer_path} for interactive login/module selection")
    print("MANUAL: in the installer, set the install path to D:\\ (mapped to game/)")
    print("MANUAL: in DCS_updater.exe settings, disable torrent downloads (see #2)")
    run_cmd([str(layout.umu_run), str(installer_path)], journal, dry_run=dry_run, env=env)


def record_human_verdict(journal: RunJournal, *, dry_run: bool, verdict: str | None = None) -> None:
    """Success bar: main menu, Instant Action free flight, ~60s flyable.

    The verdict is a human judgement by design, but --verdict lets a run be
    driven non-interactively (and stdin may not be a TTY at all).
    """
    if dry_run:
        journal.write_verdict("dry-run", "no verdict collected in dry-run mode")
        return
    if verdict is not None:
        journal.write_verdict(verdict, "supplied via --verdict")
        return
    if not sys.stdin.isatty():
        journal.write_verdict("unrecorded", "no TTY and no --verdict; record this by hand")
        print("no TTY: verdict left unrecorded -- rerun with --verdict, or edit verdict.json")
        return
    answer = input("Reached main menu, flew Instant Action for ~60s? [pass/fail]: ").strip()
    notes = input("Notes (symptom if failed, anything notable if passed): ").strip()
    journal.write_verdict(answer, notes)


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


# Hosts the DCS updater pulls content from. Both v4 and v6 must be pinned:
# glibc still goes to DNS for AAAA if only an A record is in /etc/hosts,
# which silently defeats the whole redirect (RUN 0004).
# ONLY content hosts belong here. RUN 0009: redirecting
# www.digitalcombatsimulator.com breaks the updater outright --
# "ERROR: Conection to server 'www.digitalcombatsimulator.com' failed".
# www is the API/auth host and is HTTPS-only, so pointing it at an
# http-only cache means nothing is listening on 443 and the connection
# dies before any HTTP is spoken. Caching it would need TLS interception,
# which issue #2 rules out. updates.* refuses 443 outright, so it is
# genuinely http-only and safe to redirect.
ED_HOSTS = ("updates.digitalcombatsimulator.com",)

HARNESS_CONTAINER = "dcs-linux-spike-edcache"

# nginx as a caching forward-ish proxy: it serves whatever Host it is given
# and caches by host+URI, so one server covers every ED host. Verified
# against the real CDN in RUN 0004 (MISS then HIT).
NGINX_CONF = """\
events { worker_connections 1024; }
http {
  proxy_cache_path /cache levels=1:2 keys_zone=ed:64m max_size=200g inactive=365d use_temp_path=off;
  log_format cachelog '$status $upstream_cache_status $host$request_uri';
  access_log /dev/stdout cachelog;
  resolver 1.1.1.1 8.8.8.8 ipv6=off valid=300s;
  server {
    listen 80;
    server_name _;
    location / {
      proxy_cache ed;
      proxy_cache_valid 200 206 365d;
      proxy_cache_key "$host$request_uri";
      proxy_cache_lock on;
      proxy_set_header Host $host;
      add_header X-ED-Cache $upstream_cache_status always;
      proxy_pass http://$host;
    }
  }
}
"""


def harness_ensure_cache_active(layout: Layout, journal: RunJournal, *, dry_run: bool) -> None:
    """Stand up the local ED cache and prove it actually caches.

    Called right before the updater launches -- that's where the 150 GB
    download happens, so that's the only step this needs to gate. Toolchain
    fetch and prefix creation don't touch the ED CDN and run fine without it.

    Design, as validated in RUN 0004 rather than assumed:

      * The ED update hosts serve plain http:// with no https redirect, and
        return ETag/Last-Modified, so a stock nginx proxy_cache is enough --
        no TLS interception, no MITM certificate.
      * nginx runs in a rootless podman container, so no root and no
        system-wide nginx install (the dev machine has none).
      * The updater is pointed at it by a bind-mounted /etc/hosts inside a
        *mount* namespace only. The ticket's plan called for a network
        namespace too, but that turns out to be unnecessary and costs more:
        keeping the host network means GPU, X11/Wayland and audio all keep
        working with no pasta/slirp plumbing.

    Fails loudly rather than silently falling back to the real CDN -- a
    silent fallback looks exactly like success and would burn a day.
    """
    if dry_run:
        print(
            f"  (dry-run) would: start {HARNESS_CONTAINER} (nginx/podman) caching to "
            f"{layout.http_cache_dir}, then self-test for a cache HIT"
        )
        return

    if not shutil.which("podman"):
        raise RuntimeError("cache harness needs podman (rootless); not on PATH")

    # nginx must answer on :80 because that is where the updater will knock,
    # and rootless podman can only publish it if unprivileged ports reach
    # down that far. This is the one thing the harness cannot arrange itself.
    port_start = int(
        Path("/proc/sys/net/ipv4/ip_unprivileged_port_start").read_text().strip() or 1024
    )
    if port_start > 80:
        raise RuntimeError(
            f"cache harness needs to bind :80 but net.ipv4.ip_unprivileged_port_start is "
            f"{port_start}. Run once as root:\n"
            f"    sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80\n"
            f"(or pass --skip-harness to hit the real CDN -- see #14)"
        )

    layout.http_cache_dir.mkdir(parents=True, exist_ok=True)
    conf_path = layout.cache_root / "nginx.conf"
    conf_path.write_text(NGINX_CONF)

    run_cmd(["podman", "rm", "-f", HARNESS_CONTAINER], journal, dry_run=dry_run, check=False)
    run_cmd(
        [
            "podman",
            "run",
            "-d",
            "--name",
            HARNESS_CONTAINER,
            "-p",
            "127.0.0.1:80:80",
            "-v",
            f"{conf_path}:/etc/nginx/nginx.conf:ro,Z",
            "-v",
            f"{layout.http_cache_dir}:/cache:Z",
            "docker.io/library/nginx:alpine",
        ],
        journal,
        dry_run=dry_run,
    )
    _harness_wait_for_cache_hit(journal)
    harness_activate_hosts(layout, journal, dry_run=dry_run)
    print("cache harness active: ED hosts resolve to the local nginx cache")


def _harness_wait_for_cache_hit(journal: RunJournal, attempts: int = 15) -> None:
    """Prove the cache caches: same object twice, second must report HIT.

    This is the assertion issue #2 insists on. Without it a misconfigured
    proxy that quietly passes everything through to the real CDN would look
    exactly like a working harness.
    """
    probe = f"http://127.0.0.1/?harness-probe={datetime.now(UTC).timestamp()}"
    curl = [
        "curl",
        "-sS",
        "-o",
        "/dev/null",
        "--max-time",
        "30",
        "-H",
        f"Host: {ED_HOSTS[0]}",
        "-w",
        "%{http_code} %header{X-ED-Cache}",
        probe,
    ]

    for _attempt in range(attempts):
        first = subprocess.run(curl, capture_output=True, text=True, check=False)
        if first.returncode == 0 and first.stdout.startswith("200"):
            second = subprocess.run(curl, capture_output=True, text=True, check=False)
            journal.record_command(curl, second)
            if "HIT" in second.stdout:
                return
            raise RuntimeError(
                f"cache harness is up but does not cache: second request reported "
                f"{second.stdout!r}, expected HIT. Refusing to run against the real CDN."
            )
        time.sleep(1)
    raise RuntimeError(
        f"cache harness never became ready after {attempts}s (last: {first.stdout!r})"
    )


HOSTS_BEGIN = "# >>> dcs-linux-spike harness >>>"
HOSTS_END = "# <<< dcs-linux-spike harness <<<"


def _strip_hosts_block(journal: RunJournal, *, dry_run: bool) -> None:
    """Delete any existing harness block from /etc/hosts (no-op if absent)."""
    run_cmd(
        ["sudo", "-n", "sed", "-i", f"\\|{HOSTS_BEGIN}|,\\|{HOSTS_END}|d", "/etc/hosts"],
        journal,
        dry_run=dry_run,
    )


def harness_activate_hosts(layout: Layout, journal: RunJournal, *, dry_run: bool) -> None:
    """Point the ED hosts at the local cache, system-wide.

    RUN 0007 killed the namespace approach: bind-mounting /etc/hosts needs
    uid 0 inside the user namespace, but umu-run refuses to start as root,
    and dropping to a non-root uid there maps to an unrelated subuid with no
    access to our own files. There is no mapping that satisfies both.

    So the redirect is a marked block in the real /etc/hosts instead. It is
    system-wide while active -- nothing on this machine can reach the ED
    site -- so harness_deactivate_hosts() must run no matter how the run
    ends. main() does that in a finally.
    """
    print("pointing ED hosts at the local cache (system-wide, reverted on exit)")
    _strip_hosts_block(journal, dry_run=dry_run)
    lines = [HOSTS_BEGIN]
    for host in ED_HOSTS:
        lines += [f"127.0.0.1 {host}", f"::1 {host}"]
    lines.append(HOSTS_END)
    block = "\n".join(lines) + "\n"
    if not dry_run:
        proc = subprocess.run(
            ["sudo", "-n", "tee", "-a", "/etc/hosts"],
            input=block,
            capture_output=True,
            text=True,
            check=False,
        )
        journal.record_command(["sudo", "tee", "-a", "/etc/hosts"], proc)
        if proc.returncode != 0:
            raise RuntimeError(f"could not update /etc/hosts: {proc.stderr}")


def harness_deactivate_hosts(journal: RunJournal, *, dry_run: bool) -> None:
    """Strip the harness block from /etc/hosts. Safe to call when absent."""
    print("restoring /etc/hosts")
    _strip_hosts_block(journal, dry_run=dry_run)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


@dataclass
class Args:
    data_root: Path
    cold: bool
    dry_run: bool
    skip_harness: bool
    verdict: str | None


def parse_args(argv: list[str] | None = None) -> Args:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--cold", action="store_true", help="also restore game/ from gold/")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, touch nothing")
    parser.add_argument(
        "--verdict",
        default=None,
        help="record this verdict instead of prompting (pass/fail/...)",
    )
    parser.add_argument(
        "--skip-harness",
        action="store_true",
        help="real CDN, no local cache -- the unharnessed validation run (#14)",
    )
    ns = parser.parse_args(argv)
    return Args(
        data_root=ns.data_root,
        cold=ns.cold,
        dry_run=ns.dry_run,
        skip_harness=ns.skip_harness,
        verdict=ns.verdict,
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
    apply_winetricks(layout, env, journal, dry_run=args.dry_run)
    map_game_and_saved_games(layout, dry_run=args.dry_run)

    # Gate only the step that actually hits the ED CDN -- everything above
    # (toolchain, prefix, drive mapping) works fine without the harness.
    try:
        if not args.skip_harness:
            harness_ensure_cache_active(layout, journal, dry_run=args.dry_run)
        launch_dcs_updater(layout, env, journal, dry_run=args.dry_run)
        record_human_verdict(journal, dry_run=args.dry_run, verdict=args.verdict)
    finally:
        # /etc/hosts is a system-wide change: nothing on this machine can
        # reach the ED site while it stands. Restore it however we exit.
        if not args.skip_harness:
            harness_deactivate_hosts(journal, dry_run=args.dry_run)
    journal.capture_dcs_log(layout.saved_games_dir)

    print(f"run {journal.number:04d} journal: {journal.dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
