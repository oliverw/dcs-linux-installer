"""Distro detection, and the remediation advice that depends on it.

Remediation is only useful if it can actually succeed on the machine reading
it, so every command this module produces is chosen for the detected distro
and, above all, for whether its filesystem is immutable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dcs_linux.system import System

OS_RELEASE = Path("/etc/os-release")
FALLBACK_OS_RELEASE = Path("/usr/lib/os-release")
OSTREE_MARKER = Path("/run/ostree-booted")


class Family(StrEnum):
    """The packaging family, which is what remediation actually turns on."""

    FEDORA = "fedora"
    DEBIAN = "debian"
    ARCH = "arch"
    SUSE = "suse"
    UNKNOWN = "unknown"


class Immutability(StrEnum):
    """How (and whether) the base system resists being written to."""

    MUTABLE = "mutable"
    # Image-based, but layering packages genuinely works: rpm-ostree install.
    OSTREE = "ostree"
    # Read-only base with no supported layering path (SteamOS): never suggest
    # a package manager here, it is undone by the next system update.
    READ_ONLY = "read-only"


# Distros whose base image is read-only with no supported layering.
_READ_ONLY_IDS = frozenset({"steamos"})

# Image-based distros, recognised by name rather than only by the runtime
# marker. Bazzite is the case that matters: if the marker were ever missed we
# would hand an ostree system a `sudo dnf install`, which is exactly the
# advice ADR-0006 exists to prevent.
_OSTREE_IDS = frozenset({"bazzite", "bluefin", "aurora", "fedora-coreos", "fedora-iot"})

# Fedora's atomic desktops keep ID=fedora and identify themselves here.
_OSTREE_VARIANT_IDS = frozenset(
    {"silverblue", "kinoite", "sericea", "onyx", "cosmic-atomic", "base-atomic", "iot"}
)

_FAMILY_BY_ID = {
    "fedora": Family.FEDORA,
    "rhel": Family.FEDORA,
    "centos": Family.FEDORA,
    "nobara": Family.FEDORA,
    "bazzite": Family.FEDORA,
    "bluefin": Family.FEDORA,
    "aurora": Family.FEDORA,
    "debian": Family.DEBIAN,
    "ubuntu": Family.DEBIAN,
    "pop": Family.DEBIAN,
    "linuxmint": Family.DEBIAN,
    "arch": Family.ARCH,
    "manjaro": Family.ARCH,
    "endeavouros": Family.ARCH,
    "steamos": Family.ARCH,
    "opensuse": Family.SUSE,
    "opensuse-tumbleweed": Family.SUSE,
    "opensuse-leap": Family.SUSE,
    "sles": Family.SUSE,
}

# Package names differ per family for exactly the things `check` looks for.
_PACKAGES: dict[str, dict[Family, str]] = {
    "curl": {
        Family.FEDORA: "curl",
        Family.DEBIAN: "curl",
        Family.ARCH: "curl",
        Family.SUSE: "curl",
    },
    "tar": {
        Family.FEDORA: "tar",
        Family.DEBIAN: "tar",
        Family.ARCH: "tar",
        Family.SUSE: "tar",
    },
    "bwrap": {
        Family.FEDORA: "bubblewrap",
        Family.DEBIAN: "bubblewrap",
        Family.ARCH: "bubblewrap",
        Family.SUSE: "bubblewrap",
    },
    "glxinfo": {
        Family.FEDORA: "glx-utils",
        Family.DEBIAN: "mesa-utils",
        Family.ARCH: "mesa-utils",
        Family.SUSE: "Mesa-demo-x",
    },
    "magick": {
        Family.FEDORA: "ImageMagick",
        Family.DEBIAN: "imagemagick",
        Family.ARCH: "imagemagick",
        Family.SUSE: "ImageMagick",
    },
    # Named the same everywhere, but routed through here anyway so an
    # immutable base still gets layering or container advice rather than a
    # `sudo dnf install` that the next system update undoes (ADR-0006).
    "flatpak": {
        Family.FEDORA: "flatpak",
        Family.DEBIAN: "flatpak",
        Family.ARCH: "flatpak",
        Family.SUSE: "flatpak",
    },
    "dejavu-fonts": {
        Family.FEDORA: "dejavu-sans-fonts",
        Family.DEBIAN: "fonts-dejavu-core",
        Family.ARCH: "ttf-dejavu",
        Family.SUSE: "dejavu-fonts",
    },
}

_DEFAULT_PACKAGE = {
    "curl": "curl",
    "tar": "tar",
    "bwrap": "bubblewrap",
    "glxinfo": "glxinfo",
    "magick": "ImageMagick",
    "flatpak": "flatpak",
    "dejavu-fonts": "the DejaVu fonts",
}

_INSTALL_COMMAND = {
    Family.FEDORA: "sudo dnf install",
    Family.DEBIAN: "sudo apt install",
    Family.ARCH: "sudo pacman -S",
    Family.SUSE: "sudo zypper install",
}


@dataclass(frozen=True)
class Distro:
    """What we know about the running distro."""

    id: str
    name: str
    version: str | None
    family: Family
    immutability: Immutability

    @property
    def is_immutable(self) -> bool:
        return self.immutability is not Immutability.MUTABLE

    def package_for(self, key: str) -> str:
        """The distro's name for one of the packages `check` may ask for.

        An unplaceable distro gets the most widely used name rather than the
        binary's name, which is rarely what the package is called.
        """
        return _PACKAGES[key].get(self.family) or _DEFAULT_PACKAGE[key]

    def install_hint(self, *keys: str) -> str:
        """A command that installs `keys`, or advice when no such command exists.

        On an immutable base this never names a package manager that cannot
        durably succeed there.
        """
        packages = " ".join(self.package_for(key) for key in keys)

        # rpm-ostree only exists on the Fedora side. An ostree system we cannot
        # place gets prose, never an invented command (ADR-0006).
        if self.immutability is Immutability.OSTREE and self.family is Family.FEDORA:
            return f"rpm-ostree install {packages}   # takes effect after a reboot"

        if self.immutability is Immutability.READ_ONLY or self.immutability is Immutability.OSTREE:
            return (
                f"the base system is image-based; get {packages} from a container "
                f"instead: distrobox create -n dcs && distrobox enter dcs"
            )

        command = _INSTALL_COMMAND.get(self.family)
        if command is None:
            return f"install {packages} with your distro's package manager"
        return f"{command} {packages}"


UNKNOWN_DISTRO = Distro(
    id="unknown",
    name="unknown",
    version=None,
    family=Family.UNKNOWN,
    immutability=Immutability.MUTABLE,
)


def detect_distro(system: System) -> Distro:
    """Read the distro identity and whether its filesystem is immutable."""
    text = system.read_text(OS_RELEASE) or system.read_text(FALLBACK_OS_RELEASE)
    if text is None:
        return UNKNOWN_DISTRO

    fields = parse_os_release(text)
    distro_id = fields.get("ID", "unknown")
    return Distro(
        id=distro_id,
        name=fields.get("PRETTY_NAME") or distro_id,
        version=fields.get("VERSION_ID"),
        family=_family_for(distro_id, fields.get("ID_LIKE", "")),
        immutability=_immutability(system, distro_id, fields.get("VARIANT_ID", "")),
    )


def parse_os_release(text: str) -> dict[str, str]:
    """Parse os-release's `KEY=value` / `KEY="value"` lines."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        fields[key.strip()] = value
    return fields


def _family_for(distro_id: str, id_like: str) -> Family:
    family = _FAMILY_BY_ID.get(distro_id)
    if family is not None:
        return family
    for candidate in id_like.split():
        family = _FAMILY_BY_ID.get(candidate)
        if family is not None:
            return family
    return Family.UNKNOWN


def _immutability(system: System, distro_id: str, variant_id: str) -> Immutability:
    if distro_id in _READ_ONLY_IDS:
        return Immutability.READ_ONLY
    if distro_id in _OSTREE_IDS or variant_id in _OSTREE_VARIANT_IDS:
        return Immutability.OSTREE
    if system.exists(OSTREE_MARKER) or system.which("rpm-ostree") is not None:
        return Immutability.OSTREE
    if _usr_is_read_only(system):
        return Immutability.READ_ONLY
    return Immutability.MUTABLE


def _usr_is_read_only(system: System) -> bool:
    mounts = system.read_text(Path("/proc/self/mounts"))
    if mounts is None:
        return False
    for line in mounts.splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[1] == "/usr":
            return "ro" in fields[3].split(",")
    return False
