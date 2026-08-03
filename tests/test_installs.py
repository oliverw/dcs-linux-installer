"""Identity of a DCS install: what one is, what to call it, which one to use."""

from pathlib import Path

import pytest

from dcs_linux.installs import (
    AmbiguousInstall,
    DcsInstall,
    Edition,
    InstallNotFound,
    Launcher,
    default_install,
    detect_edition,
    find_install_root,
    install_root_for_exe,
    is_dcs_install,
    read_version,
    select,
)
from tests.fakes import FakeSystem

AUTOUPDATE = '{"version": "2.9.28.26385", "branch": "release", "lang": "EN"}'


def install(game: str, launcher: Launcher = Launcher.LUTRIS, **overrides: object) -> DcsInstall:
    return DcsInstall(game=Path(game), launcher=launcher, **overrides)  # type: ignore[arg-type]


class TestIsDcsInstall:
    def test_the_game_binary_marks_an_install(self) -> None:
        system = FakeSystem(files={"/games/DCS World/bin/DCS.exe": ""})
        assert is_dcs_install(system, Path("/games/DCS World"))

    def test_autoupdate_cfg_alone_counts(self) -> None:
        """A download interrupted before the first binary landed is still an install."""
        system = FakeSystem(files={"/games/DCS World/autoupdate.cfg": AUTOUPDATE})
        assert is_dcs_install(system, Path("/games/DCS World"))

    def test_an_unrelated_directory_does_not(self) -> None:
        system = FakeSystem(files={"/games/Other/bin/Other.exe": ""})
        assert not is_dcs_install(system, Path("/games/Other"))


class TestFindInstallRoot:
    def test_the_directory_itself(self) -> None:
        system = FakeSystem(files={"/data/dcs/game/bin/DCS.exe": ""})
        assert find_install_root(system, Path("/data/dcs/game")) == Path("/data/dcs/game")

    def test_one_level_down(self) -> None:
        """The updater creates `<chosen directory>/DCS World`, not the install itself."""
        system = FakeSystem(files={"/data/dcs/game/DCS World/bin/DCS.exe": ""})
        found = find_install_root(system, Path("/data/dcs/game"))
        assert found == Path("/data/dcs/game/DCS World")

    def test_nothing_there(self) -> None:
        assert find_install_root(FakeSystem(), Path("/data/dcs/game")) is None


class TestInstallRootForExe:
    def test_walks_up_from_the_launcher_configured_binary(self) -> None:
        system = FakeSystem(files={"/games/DCS World/bin/DCS_updater.exe": ""})
        found = install_root_for_exe(system, Path("/games/DCS World/bin/DCS_updater.exe"))
        assert found == Path("/games/DCS World")

    def test_an_exe_from_another_game_is_not_adopted(self) -> None:
        system = FakeSystem(files={"/games/Other/bin/Other.exe": ""})
        assert install_root_for_exe(system, Path("/games/Other/bin/Other.exe")) is None


class TestVersion:
    def test_read_from_autoupdate_cfg(self) -> None:
        system = FakeSystem(files={"/games/DCS/autoupdate.cfg": AUTOUPDATE})
        assert read_version(system, Path("/games/DCS")) == "2.9.28.26385"

    def test_unparseable_config_is_an_unknown_version(self) -> None:
        system = FakeSystem(files={"/games/DCS/autoupdate.cfg": "{ truncated"})
        assert read_version(system, Path("/games/DCS")) is None

    def test_no_config_at_all(self) -> None:
        assert read_version(FakeSystem(), Path("/games/DCS")) is None


class TestEdition:
    def test_steam_owns_its_own_app(self) -> None:
        edition = detect_edition(FakeSystem(), Path("/games/DCSWorld"), Launcher.STEAM)
        assert edition is Edition.STEAM

    def test_the_updater_marks_the_standalone_edition(self) -> None:
        """The Steam edition has no `DCS_updater.exe`; Steam does the updating."""
        system = FakeSystem(files={"/games/DCS/bin/DCS_updater.exe": ""})
        assert detect_edition(system, Path("/games/DCS"), Launcher.LUTRIS) is Edition.STANDALONE

    def test_neither_signal_stays_unknown(self) -> None:
        system = FakeSystem(files={"/games/DCS/bin/DCS.exe": ""})
        assert detect_edition(system, Path("/games/DCS"), Launcher.LUTRIS) is Edition.UNKNOWN


class TestInstallId:
    def test_is_stable_for_the_same_game_directory(self) -> None:
        assert install("/games/DCS").install_id == install("/games/DCS").install_id

    def test_differs_between_installs(self) -> None:
        assert install("/games/DCS").install_id != install("/games/DCS2").install_id

    def test_does_not_change_with_the_launcher_that_found_it(self) -> None:
        """The identity of an install is where the game lives, nothing else."""
        by_lutris = install("/games/DCS", Launcher.LUTRIS)
        by_steam = install("/games/DCS", Launcher.STEAM)
        assert by_lutris.install_id == by_steam.install_id

    def test_is_short_enough_to_retype(self) -> None:
        assert len(install("/games/DCS").install_id) == 8


class TestUnderPrefix:
    def test_a_game_inside_drive_c_is_flagged(self) -> None:
        found = install(
            "/data/dcs/prefix/drive_c/Program Files/Eagle Dynamics/DCS World",
            prefix=Path("/data/dcs/prefix"),
        )
        assert found.under_prefix

    def test_a_game_outside_the_prefix_is_not(self) -> None:
        assert not install("/data/dcs/game", prefix=Path("/data/dcs/prefix")).under_prefix

    def test_no_prefix_at_all(self) -> None:
        assert not install("/data/dcs/game").under_prefix


class TestSelect:
    INSTALLS = (install("/games/DCS", Launcher.LUTRIS), install("/games/DCS2", Launcher.STEAM))

    def test_by_full_id(self) -> None:
        wanted = self.INSTALLS[1]
        assert select(self.INSTALLS, wanted.install_id) is wanted

    def test_by_id_prefix(self) -> None:
        wanted = self.INSTALLS[0]
        assert select(self.INSTALLS, wanted.install_id[:4]) is wanted

    def test_by_game_path(self) -> None:
        assert select(self.INSTALLS, "/games/DCS2") is self.INSTALLS[1]

    def test_case_is_ignored(self) -> None:
        wanted = self.INSTALLS[0]
        assert select(self.INSTALLS, wanted.install_id.upper()) is wanted

    def test_an_unknown_identifier_is_reported(self) -> None:
        with pytest.raises(InstallNotFound):
            select(self.INSTALLS, "zzzz")

    def test_an_empty_identifier_selects_nothing(self) -> None:
        with pytest.raises(InstallNotFound):
            select(self.INSTALLS, "  ")

    def test_a_prefix_matching_two_installs_is_ambiguous(self) -> None:
        first, second = _two_installs_sharing_an_id_prefix()
        with pytest.raises(AmbiguousInstall):
            select((first, second), first.install_id[:1])


class TestDefaultInstall:
    def test_our_own_install_wins(self) -> None:
        installs = (install("/games/DCS", Launcher.STEAM), install("/d/game", Launcher.DCS_LINUX))
        assert default_install(installs) is installs[1]

    def test_a_single_install_is_the_default(self) -> None:
        installs = (install("/games/DCS", Launcher.HEROIC),)
        assert default_install(installs) is installs[0]

    def test_several_foreign_installs_need_choosing(self) -> None:
        installs = (install("/games/DCS"), install("/games/DCS2"))
        assert default_install(installs) is None

    def test_nothing_installed(self) -> None:
        assert default_install(()) is None


def _two_installs_sharing_an_id_prefix() -> tuple[DcsInstall, DcsInstall]:
    """Two installs whose ids start with the same character.

    Searched for rather than hard-coded, so the test still means what it says
    if the id ever changes shape.
    """
    seen: dict[str, DcsInstall] = {}
    for index in range(200):
        found = install(f"/games/DCS{index}")
        first_character = found.install_id[0]
        if first_character in seen:
            return seen[first_character], found
        seen[first_character] = found
    raise AssertionError("no two install ids shared a first character")
