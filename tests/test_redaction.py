"""Redaction, against fixtures that contain the things it must remove."""

from pathlib import Path

import pytest

from dcs_linux.redaction import Redactor, redactor_for
from tests.fakes import FakeSystem

REDACTOR = Redactor(home=Path("/home/oliver"), user="oliver")


class TestHomePaths:
    def test_the_users_own_home_becomes_a_tilde(self) -> None:
        assert REDACTOR.scrub("/home/oliver/dcs-linux/game") == "~/dcs-linux/game"

    def test_another_home_keeps_its_shape_without_the_name(self) -> None:
        assert REDACTOR.scrub("/home/jenny/Games/dcs") == "/home/<user>/Games/dcs"

    def test_an_atomic_distro_home_is_redacted_too(self) -> None:
        """Bazzite and Silverblue put homes under /var/home."""
        assert REDACTOR.scrub("/var/home/jenny/Games") == "/var/home/<user>/Games"

    def test_a_home_under_a_different_root_is_still_hidden(self) -> None:
        elsewhere = Redactor(home=Path("/mnt/users/oliver"), user="oliver")
        assert elsewhere.scrub("/mnt/users/oliver/game") == "~/game"

    def test_paths_are_redacted_as_paths(self) -> None:
        assert REDACTOR.path(Path("/home/oliver/dcs-linux/prefix")) == "~/dcs-linux/prefix"

    def test_an_absent_path_reads_as_unknown(self) -> None:
        assert REDACTOR.path(None) == "unknown"


class TestWindowsProfiles:
    def test_a_wine_profile_named_after_the_user_is_redacted(self) -> None:
        text = r"SymInit: Symbol-SearchPath: 'C:\users\oliver\Saved Games'"
        assert r"C:\users\<user>\Saved Games" in REDACTOR.scrub(text)

    def test_steamuser_is_kept_because_it_names_nobody(self) -> None:
        """Which profile a prefix uses says which launcher built it."""
        text = r"C:\users\steamuser\Saved Games\DCS\Logs\dcs.log"
        assert REDACTOR.scrub(text) == text

    def test_forward_slashes_are_redacted_too(self) -> None:
        assert REDACTOR.scrub("c:/users/jenny/Temp") == "c:/users/<user>/Temp"


class TestIdentifiers:
    def test_the_username_is_removed_wherever_it_appears(self) -> None:
        assert REDACTOR.scrub("UserName: 'oliver'") == "UserName: '<user>'"

    def test_a_longer_word_containing_the_username_survives(self) -> None:
        assert REDACTOR.scrub("oliverwood.dll") == "oliverwood.dll"

    def test_email_addresses_go(self) -> None:
        assert REDACTOR.scrub("login oliver@example.com ok") == "login <email> ok"

    def test_uuids_go(self) -> None:
        text = 'ID: "{0.0.0.00000000}.{f3884136-6970-469b-aac8-e2895969c000}"'
        assert "f3884136" not in REDACTOR.scrub(text)

    def test_a_routable_address_goes(self) -> None:
        assert REDACTOR.scrub("interface 0: 192.168.1.42") == "interface 0: <ip>"

    def test_loopback_and_broadcast_stay(self) -> None:
        text = "Adding LAN search interface 0: 127.0.0.1 and 255.255.255.255"
        assert REDACTOR.scrub(text) == text

    def test_a_version_number_is_not_an_address(self) -> None:
        assert REDACTOR.scrub("DCS/2.9.28.26385") == "DCS/2.9.28.26385"

    def test_a_very_short_username_is_not_pattern_matched(self) -> None:
        """`\\bed\\b` would eat half a DCS log. The paths still cover it."""
        short = Redactor(home=Path("/home/ed"), user="ed")
        assert short.scrub("ED sound driver ed ready") == "ED sound driver ed ready"
        assert short.scrub("/home/ed/game") == "~/game"


class TestDisabled:
    def test_nothing_is_touched_when_redaction_is_off(self) -> None:
        raw = Redactor(home=Path("/home/oliver"), user="oliver", enabled=False)
        assert raw.scrub("/home/oliver oliver@example.com") == "/home/oliver oliver@example.com"
        assert raw.path(Path("/home/oliver")) == "/home/oliver"


@pytest.mark.parametrize("home", ["/home/oliver", "/var/home/oliver"])
def test_the_redactor_is_built_from_the_machines_home(home: str) -> None:
    redactor = redactor_for(FakeSystem(home=home))
    assert redactor.scrub(f"{home}/dcs-linux") == "~/dcs-linux"
    assert redactor.scrub("run as oliver") == "run as <user>"
