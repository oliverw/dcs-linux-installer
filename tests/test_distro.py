from pathlib import Path

import pytest

from dcs_linux.distro import Family, Immutability, detect_distro
from tests.fakes import FakeSystem

FIXTURES = Path(__file__).parent / "fixtures" / "os-release"


def os_release(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text()


def system_for(
    name: str,
    *,
    files: dict[str, str] | None = None,
    directories: set[str] | None = None,
) -> FakeSystem:
    return FakeSystem(
        files={"/etc/os-release": os_release(name), **(files or {})},
        directories=directories,
    )


@pytest.mark.parametrize(
    ("fixture", "distro_id", "family"),
    [
        ("fedora", "fedora", Family.FEDORA),
        ("bazzite", "bazzite", Family.FEDORA),
        ("steamos", "steamos", Family.ARCH),
        ("ubuntu", "ubuntu", Family.DEBIAN),
        ("arch", "arch", Family.ARCH),
        ("tumbleweed", "opensuse-tumbleweed", Family.SUSE),
    ],
)
def test_identifies_distro_and_packaging_family(
    fixture: str, distro_id: str, family: Family
) -> None:
    distro = detect_distro(system_for(fixture))
    assert distro.id == distro_id
    assert distro.family is family


def test_unknown_distro_when_os_release_is_missing() -> None:
    distro = detect_distro(FakeSystem())
    assert distro.id == "unknown"
    assert distro.family is Family.UNKNOWN
    assert not distro.is_immutable


def test_falls_back_to_usr_lib_os_release() -> None:
    system = FakeSystem(files={"/usr/lib/os-release": os_release("fedora")})
    assert detect_distro(system).id == "fedora"


def test_pretty_name_and_version_are_read() -> None:
    distro = detect_distro(system_for("ubuntu"))
    assert distro.name.startswith("Ubuntu 24.04")
    assert distro.version == "24.04"


def test_ordinary_distro_is_mutable() -> None:
    assert detect_distro(system_for("fedora")).immutability is Immutability.MUTABLE


def test_ostree_marker_makes_the_system_immutable() -> None:
    system = system_for("bazzite", directories={"/run/ostree-booted"})
    assert detect_distro(system).immutability is Immutability.OSTREE


def test_steamos_is_read_only_even_without_the_ostree_marker() -> None:
    assert detect_distro(system_for("steamos")).immutability is Immutability.READ_ONLY


def test_read_only_usr_mount_makes_the_system_immutable() -> None:
    mounts = "/dev/sda2 / ext4 rw,relatime 0 0\n/dev/sda3 /usr ext4 ro,relatime 0 0\n"
    system = system_for("arch", files={"/proc/self/mounts": mounts})
    assert detect_distro(system).immutability is Immutability.READ_ONLY


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("fedora", "sudo dnf install bubblewrap"),
        ("ubuntu", "sudo apt install bubblewrap"),
        ("arch", "sudo pacman -S bubblewrap"),
        ("tumbleweed", "sudo zypper install bubblewrap"),
    ],
)
def test_mutable_distros_get_their_own_package_manager(fixture: str, expected: str) -> None:
    assert detect_distro(system_for(fixture)).install_hint("bwrap") == expected


def test_ostree_distro_is_told_to_layer_rather_than_dnf_install() -> None:
    system = system_for("bazzite", directories={"/run/ostree-booted"})
    hint = detect_distro(system).install_hint("bwrap")
    assert hint.startswith("rpm-ostree install bubblewrap")
    assert "dnf" not in hint
    assert "sudo" not in hint


def test_read_only_distro_is_never_told_to_use_a_package_manager() -> None:
    hint = detect_distro(system_for("steamos")).install_hint("bwrap")
    assert "distrobox" in hint
    for impossible in ("pacman", "sudo", "rpm-ostree", "steamos-readonly"):
        assert impossible not in hint


def test_unknown_distro_gets_generic_advice_with_no_invented_command() -> None:
    hint = detect_distro(FakeSystem()).install_hint("curl")
    assert hint == "install curl with your distro's package manager"


def test_package_names_are_translated_per_family() -> None:
    assert detect_distro(system_for("fedora")).package_for("dejavu-fonts") == "dejavu-sans-fonts"
    assert detect_distro(system_for("ubuntu")).package_for("dejavu-fonts") == "fonts-dejavu-core"


def test_install_hint_covers_several_packages_at_once() -> None:
    hint = detect_distro(system_for("fedora")).install_hint("curl", "tar", "bwrap")
    assert hint == "sudo dnf install curl tar bubblewrap"


class TestImmutableDistrosWeMustNotGetWrong:
    """Bazzite is the case that matters: it is a target, not an edge case.

    Getting it wrong is not a cosmetic error — it hands an image-based system
    a `sudo dnf install`, which is the one thing ADR-0006 forbids.
    """

    def test_bazzite_is_ostree_from_its_name_alone(self) -> None:
        """The runtime marker is a bonus, not the thing we rely on."""
        assert detect_distro(system_for("bazzite")).immutability is Immutability.OSTREE

    def test_bazzite_is_told_to_layer_even_with_no_marker(self) -> None:
        hint = detect_distro(system_for("bazzite")).install_hint("bwrap")
        assert hint.startswith("rpm-ostree install bubblewrap")
        assert "dnf" not in hint

    def test_fedora_atomic_desktops_are_recognised_by_variant(self) -> None:
        """Silverblue and Kinoite keep ID=fedora and differ only in VARIANT_ID."""
        kinoite = 'ID=fedora\nPRETTY_NAME="Fedora Linux 44 (Kinoite)"\nVARIANT_ID=kinoite\n'
        system = FakeSystem(files={"/etc/os-release": kinoite})
        assert detect_distro(system).immutability is Immutability.OSTREE

    def test_rpm_ostree_on_path_implies_an_image_based_system(self) -> None:
        system = FakeSystem(
            files={"/etc/os-release": os_release("fedora")},
            executables={"rpm-ostree": "/usr/bin/rpm-ostree"},
        )
        assert detect_distro(system).immutability is Immutability.OSTREE

    def test_an_ostree_distro_we_cannot_place_gets_prose_not_rpm_ostree(self) -> None:
        """rpm-ostree is Fedora-side only; never invent it for an unknown ID."""
        system = FakeSystem(
            files={"/etc/os-release": 'ID=someatomic\nPRETTY_NAME="Some Atomic"\n'},
            directories={"/run/ostree-booted"},
        )
        hint = detect_distro(system).install_hint("bwrap")
        assert "rpm-ostree" not in hint
        assert "distrobox" in hint

    def test_an_unplaceable_distro_names_the_package_not_the_binary(self) -> None:
        hint = detect_distro(FakeSystem()).install_hint("bwrap")
        assert "bubblewrap" in hint
        assert "bwrap" not in hint
