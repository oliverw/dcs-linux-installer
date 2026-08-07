"""Head tracking detection, against fixture machines only.

No test here needs a TrackIR plugged in, which is the point: the hardware is
rare, the permission failure it produces is not.
"""

from pathlib import Path

from dcs_linux.headtracking import (
    NATURALPOINT_VENDOR,
    OPENTRACK_FLATPAK,
    RULE_FILE,
    detect_head_tracking,
    install_rule_command,
)
from dcs_linux.paths import Layout, TargetPaths
from tests.fakes import FakeSystem

LAYOUT = Layout(
    root=Path("/data/dcs"),
    toolchain=Path("/data/toolchain"),
    state=Path("/data/state"),
)

PATHS = TargetPaths(
    game=LAYOUT.game / "DCS World",
    prefix=LAYOUT.prefix,
    saved_games=LAYOUT.saved_games,
    prefix_saved_games=LAYOUT.prefix_saved_games,
)

# What opentrack's Wine output protocol writes into the prefix: the key every
# TrackIR-aware game reads to find NPClient.dll.
NPCLIENT_REG = (
    "[Software\\\\NaturalPoint\\\\NATURALPOINT\\\\NPClient Location] 1700000000\n"
    '"Path"="Z:\\\\home\\\\pilot\\\\.local\\\\lib\\\\opentrack"\n'
)


def usb_device(
    name: str = "1-2",
    *,
    vendor: str = NATURALPOINT_VENDOR,
    product_id: str = "0158",
    busnum: int = 1,
    devnum: int = 7,
    product: str | None = "TrackIR 5",
) -> dict[str, str]:
    """One USB device as the kernel exposes it under /sys."""
    base = f"/sys/bus/usb/devices/{name}"
    files = {
        f"{base}/idVendor": f"{vendor}\n",
        f"{base}/idProduct": f"{product_id}\n",
        f"{base}/busnum": f"{busnum}\n",
        f"{base}/devnum": f"{devnum}\n",
    }
    if product is not None:
        files[f"{base}/product"] = f"{product}\n"
    return files


class TestDeviceDetection:
    def test_a_naturalpoint_device_is_found_and_named(self) -> None:
        system = FakeSystem(files=usb_device())
        tracker = detect_head_tracking(system, PATHS).trackers[0]
        assert tracker.name == "TrackIR 5"
        assert tracker.node == Path("/dev/bus/usb/001/007")

    def test_a_nameless_device_falls_back_to_its_usb_ids(self) -> None:
        """No invented catalogue: an unknown NaturalPoint device says what it is."""
        system = FakeSystem(files=usb_device(product=None, product_id="0155"))
        assert detect_head_tracking(system, PATHS).trackers[0].name == "131d:0155"

    def test_nothing_connected_is_reported_as_nothing_not_as_an_error(self) -> None:
        assert detect_head_tracking(FakeSystem(), PATHS).trackers == ()

    def test_a_joystick_is_not_a_head_tracker(self) -> None:
        """Scope discipline: this is head tracking only, never HOTAS (#13)."""
        system = FakeSystem(files=usb_device("1-3", vendor="044f", product="Thrustmaster Warthog"))
        assert detect_head_tracking(system, PATHS).trackers == ()

    def test_usb_interfaces_and_root_hubs_are_not_devices(self) -> None:
        files = {**usb_device(), **usb_device("usb1", vendor="1d6b", product="xHCI Host")}
        files["/sys/bus/usb/devices/1-2:1.0/bInterfaceNumber"] = "0\n"
        assert len(detect_head_tracking(FakeSystem(files=files), PATHS).trackers) == 1

    def test_every_connected_tracker_is_reported(self) -> None:
        files = {**usb_device("1-2", devnum=7), **usb_device("2-1", busnum=2, devnum=3)}
        nodes = [
            tracker.node
            for tracker in detect_head_tracking(FakeSystem(files=files), PATHS).trackers
        ]
        assert nodes == [Path("/dev/bus/usb/001/007"), Path("/dev/bus/usb/002/003")]


class TestAccess:
    def test_an_accessible_device_says_so(self) -> None:
        system = FakeSystem(files=usb_device(), accessible={"/dev/bus/usb/001/007"})
        assert detect_head_tracking(system, PATHS).trackers[0].accessible

    def test_the_usual_failure_is_a_device_present_but_unreadable(self) -> None:
        """The mundane reason head tracking fails on Linux (#13)."""
        assert (
            not detect_head_tracking(FakeSystem(files=usb_device()), PATHS).trackers[0].accessible
        )


class TestUdevRule:
    def test_a_rule_naming_the_vendor_is_found(self) -> None:
        system = FakeSystem(
            files={
                **usb_device(),
                "/etc/udev/rules.d/99-trackir.rules": 'ATTRS{idVendor}=="131d", MODE="0660"\n',
            }
        )
        assert detect_head_tracking(system, PATHS).udev_rule == Path(
            "/etc/udev/rules.d/99-trackir.rules"
        )

    def test_a_rule_shipped_by_a_package_counts(self) -> None:
        """linuxtrack and opentrack packages drop theirs under /usr/lib."""
        system = FakeSystem(
            files={
                **usb_device(),
                "/usr/lib/udev/rules.d/99-TIR.rules": 'ATTR{idVendor}=="131D"\n',
            }
        )
        assert detect_head_tracking(system, PATHS).udev_rule == Path(
            "/usr/lib/udev/rules.d/99-TIR.rules"
        )

    def test_rules_about_other_hardware_do_not_count(self) -> None:
        system = FakeSystem(
            files={
                **usb_device(),
                "/etc/udev/rules.d/70-joystick.rules": 'ATTRS{idVendor}=="044f"\n',
            }
        )
        assert detect_head_tracking(system, PATHS).udev_rule is None

    def test_non_rule_files_in_the_rules_directory_are_ignored(self) -> None:
        system = FakeSystem(files={**usb_device(), "/etc/udev/rules.d/notes.txt": "131d\n"})
        assert detect_head_tracking(system, PATHS).udev_rule is None

    def test_the_emitted_command_writes_the_rule_and_reloads_udev(self) -> None:
        command = install_rule_command()
        assert str(RULE_FILE) in command
        assert NATURALPOINT_VENDOR in command
        assert "udevadm control --reload-rules" in command

    def test_a_commented_out_rule_is_not_an_installed_rule(self) -> None:
        """That shape is somebody who tried this once and gave up."""
        system = FakeSystem(
            files={
                **usb_device(),
                "/etc/udev/rules.d/99-trackir.rules": '# ATTRS{idVendor}=="131d"\n',
            }
        )
        assert detect_head_tracking(system, PATHS).udev_rule is None

    def test_the_rule_sorts_before_systemds_uaccess_rules(self) -> None:
        """A uaccess tag set after 73-seat-late.rules is a tag nothing reads."""
        assert int(RULE_FILE.name.split("-")[0]) < 73

    def test_detection_never_escalates_privileges(self) -> None:
        """The tool prints the sudo line; it never runs one (#13)."""
        system = FakeSystem(files=usb_device())
        detect_head_tracking(system, PATHS)
        assert system.runs == []


class TestOpentrack:
    def test_found_on_path(self) -> None:
        system = FakeSystem(executables={"opentrack": "/usr/bin/opentrack"})
        assert detect_head_tracking(system, PATHS).opentrack == "/usr/bin/opentrack"

    def test_found_as_a_system_flatpak(self) -> None:
        system = FakeSystem(files={f"/var/lib/flatpak/exports/bin/{OPENTRACK_FLATPAK}": ""})
        assert detect_head_tracking(system, PATHS).opentrack == f"flatpak {OPENTRACK_FLATPAK}"

    def test_found_as_a_user_flatpak(self) -> None:
        system = FakeSystem(
            files={f"/home/pilot/.local/share/flatpak/exports/bin/{OPENTRACK_FLATPAK}": ""},
            home="/home/pilot",
        )
        assert detect_head_tracking(system, PATHS).opentrack == f"flatpak {OPENTRACK_FLATPAK}"

    def test_absent_is_absent(self) -> None:
        assert detect_head_tracking(FakeSystem(), PATHS).opentrack is None

    def test_whether_flatpak_itself_is_available_is_recorded(self) -> None:
        """Advising a flatpak install is useless if flatpak is not installed."""
        assert not detect_head_tracking(FakeSystem(), PATHS).flatpak
        assert detect_head_tracking(
            FakeSystem(executables={"flatpak": "/usr/bin/flatpak"}), PATHS
        ).flatpak


class TestWineBridge:
    def test_the_npclient_registry_key_is_read_from_the_prefix(self) -> None:
        system = FakeSystem(files={str(PATHS.user_reg): NPCLIENT_REG})
        assert detect_head_tracking(system, PATHS).wine_bridge

    def test_a_prefix_without_the_key_has_no_bridge(self) -> None:
        system = FakeSystem(files={str(PATHS.user_reg): "[Software\\\\Wine] 1700000000\n"})
        assert not detect_head_tracking(system, PATHS).wine_bridge

    def test_no_prefix_at_all_has_no_bridge(self) -> None:
        assert not detect_head_tracking(FakeSystem(), PATHS).wine_bridge
