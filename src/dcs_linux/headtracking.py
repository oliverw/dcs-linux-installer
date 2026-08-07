"""Head tracking: the one peripheral in scope.

TrackIR and opentrack are close to essential for a flight sim, and on Linux
they mostly fail for a mundane reason — the device is present and the user is
not allowed to open it. That is a permissions fact, readable from `/sys` and
`/dev` without a tracker plugged in, which is why everything here is a
fixture test rather than a hardware one.

Scope discipline (#13): this is head tracking, not peripherals. Detection
matches the NaturalPoint vendor id and nothing else, so the joysticks and
throttles sharing this plumbing stay out of the tool. HOTAS is deliberately
out of scope and adding a second vendor id here is how that decision would
get quietly reversed.

Nothing here escalates. Installing a udev rule needs root, so the tool prints
the exact command and lets the user run it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from dcs_linux.paths import TargetPaths
from dcs_linux.system import System

USB_DEVICES = Path("/sys/bus/usb/devices")

# NaturalPoint, who make TrackIR. The only vendor this module knows.
NATURALPOINT_VENDOR = "131d"

# Where udev reads rules from, in the order it does.
#
# The rule this module emits is the one remediation in the tool with no
# per-distro variant, and that is deliberate rather than an oversight of
# ADR-0006. A udev rule is not a package: there is nothing for a package
# manager to install, and the container answer ADR-0006 gives an image-based
# base is actively wrong here — a distrobox has no udev of its own, and a rule
# written inside one governs nothing. `/etc` is writable on every base this
# tool targets, and both ostree and SteamOS carry `/etc` changes across an
# update. **Unverified on SteamOS**: reasoned from how its updates are
# documented, not observed.
UDEV_RULE_DIRS = (
    Path("/etc/udev/rules.d"),
    Path("/run/udev/rules.d"),
    Path("/usr/lib/udev/rules.d"),
    Path("/lib/udev/rules.d"),
)

# `70-`, not the `99-` the old TrackIR recipes use. systemd applies uaccess
# ACLs from `73-seat-late.rules`, so a rule numbered above 73 sets the tag
# after the only thing that reads it has already run — the rule installs
# cleanly, changes nothing, and looks like the tag does not work.
RULE_FILE = Path("/etc/udev/rules.d/70-trackir.rules")

# `uaccess` hands the device to whoever is logged in at the seat. Narrower
# than the MODE="0666" of the old recipes, which gives every account on the
# machine write access to it. `usb_device` keeps the tag off the interfaces,
# which have no node for an ACL to sit on.
RULE_LINE = (
    f'SUBSYSTEM=="usb", ENV{{DEVTYPE}}=="usb_device", '
    f'ATTRS{{idVendor}}=="{NATURALPOINT_VENDOR}", TAG+="uaccess"'
)

OPENTRACK_FLATPAK = "io.github.opentrack.opentrack"

# opentrack is in no mainstream distro's own repositories, so Flathub is the
# one path that works on all four packaging families and on the immutable
# bases as well. Naming a package manager here would be the invented command
# ADR-0006 exists to prevent.
OPENTRACK_INSTALL = f"flatpak install flathub {OPENTRACK_FLATPAK}"

# A flatpak that was only just installed has no remotes, so the install above
# exits with `Remote "flathub" not found`. Idempotent, so it costs a user who
# already has the remote nothing.
FLATHUB_REMOTE = (
    "flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo"
)

# The key every TrackIR-aware game reads to find NPClient.dll, and what
# opentrack's Wine output protocol writes into the prefix. Reasoned from the
# NaturalPoint client interface rather than observed on hardware — see
# CONTEXT.md.
# Written verbatim as wine escapes it in `user.reg`, and escaped before use.
NPCLIENT_KEY = r"Software\\NaturalPoint\\NATURALPOINT\\NPClient Location"


@dataclass(frozen=True)
class Tracker:
    """One connected NaturalPoint device."""

    name: str
    node: Path
    accessible: bool


@dataclass(frozen=True)
class HeadTracking:
    """Everything `check` reports about head tracking."""

    trackers: tuple[Tracker, ...] = ()
    # The rules file that mentions the vendor, wherever udev found it.
    udev_rule: Path | None = None
    # How opentrack is installed, in words, or None.
    opentrack: str | None = None
    flatpak: bool = False
    # Whether the prefix points DCS at an NPClient bridge.
    wine_bridge: bool = False

    @property
    def in_use(self) -> bool:
        """Whether this machine shows any sign of wanting head tracking.

        Neither signal on its own is proof, and together they are the whole
        difference between advice and noise: a user with no tracker and no
        opentrack is not doing head tracking, and a `check` that nags them
        about it every run teaches them to skim the table.

        opentrack alone counts because it tracks a face through a webcam,
        with no NaturalPoint hardware anywhere.
        """
        return bool(self.trackers) or self.opentrack is not None

    @property
    def inaccessible(self) -> tuple[Tracker, ...]:
        return tuple(tracker for tracker in self.trackers if not tracker.accessible)


def detect_head_tracking(system: System, paths: TargetPaths) -> HeadTracking:
    """Read the machine's head-tracking state. Read-only, like every probe."""
    return HeadTracking(
        trackers=detect_trackers(system),
        udev_rule=find_udev_rule(system),
        opentrack=find_opentrack(system),
        flatpak=system.which("flatpak") is not None,
        wine_bridge=has_npclient_location(system.read_text(paths.user_reg)),
    )


def detect_trackers(system: System) -> tuple[Tracker, ...]:
    """Every connected NaturalPoint device, in bus order."""
    trackers = []
    for name in system.list_dir(USB_DEVICES):
        device = USB_DEVICES / name
        vendor = _attribute(system, device, "idVendor")
        # Interfaces (`1-2:1.0`) carry no idVendor, so they fall out here
        # without having to be recognised by the shape of their name.
        if vendor != NATURALPOINT_VENDOR:
            continue
        node = _device_node(system, device)
        if node is None:
            continue
        trackers.append(
            Tracker(
                name=_tracker_name(system, device),
                node=node,
                accessible=system.is_accessible(node),
            )
        )
    return tuple(sorted(trackers, key=lambda tracker: tracker.node))


def _tracker_name(system: System, device: Path) -> str:
    """What the device calls itself, or its ids.

    The ids are the fallback on purpose: a hard-coded product table would
    claim to recognise devices nobody here has ever seen, and get the names
    of half of them wrong.
    """
    product = _attribute(system, device, "product")
    if product:
        return product
    return f"{NATURALPOINT_VENDOR}:{_attribute(system, device, 'idProduct') or '????'}"


def _device_node(system: System, device: Path) -> Path | None:
    """The `/dev/bus/usb` node, which is the thing permissions apply to."""
    bus = _attribute(system, device, "busnum") or ""
    number = _attribute(system, device, "devnum") or ""
    if not bus.isdigit() or not number.isdigit():
        return None
    return Path(f"/dev/bus/usb/{int(bus):03d}/{int(number):03d}")


def _attribute(system: System, device: Path, name: str) -> str | None:
    text = system.read_text(device / name)
    return text.strip() if text else None


# `ATTR{idVendor}=="131d"` and the `ATTRS{...}` spelling both count, and so
# does an upper-case id — all three appear in rules found in the wild.
_VENDOR_MATCH = re.compile(rf'idVendor}}\s*==\s*"{NATURALPOINT_VENDOR}"', re.IGNORECASE)


def find_udev_rule(system: System) -> Path | None:
    """The first rules file that mentions the vendor, in udev's own order.

    Matched on the vendor id rather than on a filename, because the rule that
    matters may have arrived under any name — hand-written, or shipped by
    linuxtrack or an opentrack package.

    Finding one is not the same as it working: it says a rule exists, never
    that it grants access. `check` reads the device node for that.
    """
    for directory in UDEV_RULE_DIRS:
        for name in system.list_dir(directory):
            if not name.endswith(".rules"):
                continue
            text = system.read_text(directory / name) or ""
            # A commented-out rule is the shape of somebody who already tried
            # this and gave up. It is not an installed rule.
            if any(_VENDOR_MATCH.search(line) for line in _uncommented(text)):
                return directory / name
    return None


def _uncommented(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


def install_rule_command() -> str:
    """The exact command that installs the rule. Printed, never run."""
    return (
        f"printf '%s\\n' '{RULE_LINE}' | sudo tee {RULE_FILE} "
        f"&& sudo udevadm control --reload-rules && sudo udevadm trigger"
    )


def find_opentrack(system: System) -> str | None:
    """Where opentrack is, on PATH or as a flatpak."""
    on_path = system.which("opentrack")
    if on_path is not None:
        return on_path
    for directory in _flatpak_bin_dirs(system):
        if system.exists(directory / OPENTRACK_FLATPAK):
            return f"flatpak {OPENTRACK_FLATPAK}"
    return None


def _flatpak_bin_dirs(system: System) -> tuple[Path, ...]:
    return (
        Path("/var/lib/flatpak/exports/bin"),
        system.home() / ".local" / "share" / "flatpak" / "exports" / "bin",
    )


def has_npclient_location(user_reg: str | None) -> bool:
    """Whether the prefix tells DCS where to find NPClient.dll.

    DCS reaches a head tracker through NaturalPoint's client DLL, and finds
    that DLL through this registry key — so under Proton the key is the thing
    that connects opentrack to the game. Without it a perfectly configured
    opentrack moves nothing in the cockpit.
    """
    if user_reg is None:
        return False
    key = re.escape(NPCLIENT_KEY)
    return re.search(rf"^\[{key}\]", user_reg, re.MULTILINE | re.IGNORECASE) is not None
