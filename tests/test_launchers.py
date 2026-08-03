"""Discovery against a synthetic directory tree for each launcher layout.

No real DCS install is involved anywhere here — CI has neither 150 GB nor a
Steam account.
"""

import json
from pathlib import Path

from dcs_linux.installs import Edition, Launcher
from dcs_linux.launchers import discover
from dcs_linux.paths import Layout
from tests.fakes import FakeSystem

LAYOUT = Layout(root=Path("/data/dcs"), toolchain=Path("/data/toolchain"))
HOME = "/home/pilot"

AUTOUPDATE = json.dumps({"version": "2.9.28.26385", "branch": "release", "lang": "EN"})


def dcs_files(game: str, *, updater: bool = True) -> dict[str, str]:
    """The files an installed DCS leaves behind, Standalone unless told otherwise."""
    files = {f"{game}/bin/DCS.exe": "", f"{game}/autoupdate.cfg": AUTOUPDATE}
    if updater:
        files[f"{game}/bin/DCS_updater.exe"] = ""
    return files


# --- Steam -----------------------------------------------------------------

STEAM_ROOT = f"{HOME}/.steam/root"
STEAM_LIBRARY = "/mnt/games/SteamLibrary"
STEAM_GAME = f"{STEAM_LIBRARY}/steamapps/common/DCSWorld"

LIBRARY_FOLDERS = f"""
"libraryfolders"
{{
    "0" {{ "path" "{HOME}/.local/share/Steam" }}
    "1" {{ "path" "{STEAM_LIBRARY}" }}
}}
"""

APP_MANIFEST = """
"AppState"
{
    "appid"        "223750"
    "name"        "DCS World Steam Edition"
    "installdir"        "DCSWorld"
    "StateFlags"        "4"
}
"""

COMPAT_TOOL_MAPPING = """
"InstallConfigStore"
{
    "Software"
    {
        "Valve"
        {
            "Steam"
            {
                "CompatToolMapping"
                {
                    "223750" { "name" "GE-Proton11-3" "config" "" "priority" "250" }
                }
            }
        }
    }
}
"""


def steam_files(*, mapping: bool = True) -> dict[str, str]:
    files = {
        f"{STEAM_ROOT}/steamapps/libraryfolders.vdf": LIBRARY_FOLDERS,
        f"{STEAM_LIBRARY}/steamapps/appmanifest_223750.acf": APP_MANIFEST,
        f"{STEAM_LIBRARY}/steamapps/compatdata/223750/pfx/system.reg": "",
        **dcs_files(STEAM_GAME, updater=False),
    }
    if mapping:
        files[f"{STEAM_ROOT}/config/config.vdf"] = COMPAT_TOOL_MAPPING
    return files


class TestSteam:
    def test_an_install_in_a_secondary_library_is_found(self) -> None:
        (found,) = discover(FakeSystem(files=steam_files(), home=HOME), LAYOUT)
        assert found.launcher is Launcher.STEAM
        assert found.game == Path(STEAM_GAME)
        assert found.prefix == Path(f"{STEAM_LIBRARY}/steamapps/compatdata/223750/pfx")
        assert found.runtime == "GE-Proton11-3"
        assert found.edition is Edition.STEAM
        assert found.version == "2.9.28.26385"

    def test_runtime_falls_back_to_the_prefix_version_file(self) -> None:
        """Steam only writes a CompatToolMapping when the tool was chosen by hand."""
        files = steam_files(mapping=False)
        files[f"{STEAM_LIBRARY}/steamapps/compatdata/223750/version"] = "GE-Proton9-20\n"
        (found,) = discover(FakeSystem(files=files, home=HOME), LAYOUT)
        assert found.runtime == "GE-Proton9-20"

    def test_an_unknown_runtime_is_not_invented(self) -> None:
        (found,) = discover(FakeSystem(files=steam_files(mapping=False), home=HOME), LAYOUT)
        assert found.runtime is None

    def test_an_install_in_the_steam_root_itself_is_found(self) -> None:
        game = f"{STEAM_ROOT}/steamapps/common/DCSWorld"
        files = {
            f"{STEAM_ROOT}/steamapps/appmanifest_223750.acf": APP_MANIFEST,
            **dcs_files(game, updater=False),
        }
        (found,) = discover(FakeSystem(files=files, home=HOME), LAYOUT)
        assert found.game == Path(game)

    def test_a_library_without_dcs_yields_nothing(self) -> None:
        files = {f"{STEAM_ROOT}/steamapps/libraryfolders.vdf": LIBRARY_FOLDERS}
        assert discover(FakeSystem(files=files, home=HOME), LAYOUT) == ()

    def test_the_flatpak_steam_layout_is_searched(self) -> None:
        root = f"{HOME}/.var/app/com.valvesoftware.Steam/data/Steam"
        game = f"{root}/steamapps/common/DCSWorld"
        files = {
            f"{root}/steamapps/appmanifest_223750.acf": APP_MANIFEST,
            **dcs_files(game, updater=False),
        }
        (found,) = discover(FakeSystem(files=files, home=HOME), LAYOUT)
        assert found.game == Path(game)


# --- Lutris ----------------------------------------------------------------

LUTRIS_GAME = "/games/DCS World"
LUTRIS_CONFIG = f"""
game:
  exe: {LUTRIS_GAME}/bin/DCS_updater.exe
  prefix: /games/prefixes/dcs
  arch: win64
system:
  env:
    WINEDLLOVERRIDES: wbemprox=n
wine:
  version: ge-proton-11-3
"""


class TestLutris:
    def test_a_configured_dcs_game_is_found(self) -> None:
        files = {
            f"{HOME}/.config/lutris/games/dcs-world-1712.yml": LUTRIS_CONFIG,
            **dcs_files(LUTRIS_GAME),
        }
        (found,) = discover(FakeSystem(files=files, home=HOME), LAYOUT)
        assert found.launcher is Launcher.LUTRIS
        assert found.game == Path(LUTRIS_GAME)
        assert found.prefix == Path("/games/prefixes/dcs")
        assert found.runtime == "ge-proton-11-3"
        assert found.edition is Edition.STANDALONE

    def test_other_games_are_ignored(self) -> None:
        config = "game:\n  exe: /games/Other/other.exe\n  prefix: /games/prefixes/other\n"
        files = {
            f"{HOME}/.config/lutris/games/other-1.yml": config,
            "/games/Other/other.exe": "",
        }
        assert discover(FakeSystem(files=files, home=HOME), LAYOUT) == ()

    def test_an_unparseable_config_is_skipped(self) -> None:
        files = {f"{HOME}/.config/lutris/games/broken.yml": "game: [unclosed\n"}
        assert discover(FakeSystem(files=files, home=HOME), LAYOUT) == ()

    def test_a_config_without_a_prefix_still_reports_the_game(self) -> None:
        config = f"game:\n  exe: {LUTRIS_GAME}/bin/DCS.exe\n"
        files = {
            f"{HOME}/.config/lutris/games/dcs.yml": config,
            **dcs_files(LUTRIS_GAME),
        }
        (found,) = discover(FakeSystem(files=files, home=HOME), LAYOUT)
        assert found.game == Path(LUTRIS_GAME)
        assert found.prefix is None

    def test_the_flatpak_lutris_layout_is_searched(self) -> None:
        config_dir = f"{HOME}/.var/app/net.lutris.Lutris/config/lutris/games"
        files = {f"{config_dir}/dcs.yml": LUTRIS_CONFIG, **dcs_files(LUTRIS_GAME)}
        (found,) = discover(FakeSystem(files=files, home=HOME), LAYOUT)
        assert found.launcher is Launcher.LUTRIS


# --- Heroic ----------------------------------------------------------------

HEROIC_GAME = "/games/heroic/DCS World"
HEROIC_APP = "dcs-world-standalone"
HEROIC_LIBRARY = json.dumps(
    {
        "games": [
            {
                "app_name": HEROIC_APP,
                "title": "DCS World",
                "install": {"executable": f"{HEROIC_GAME}/bin/DCS.exe", "platform": "Windows"},
            },
            {
                "app_name": "some-other-game",
                "title": "Other",
                "install": {"executable": "/games/Other/other.exe", "platform": "Windows"},
            },
        ]
    }
)
HEROIC_GAME_CONFIG = json.dumps(
    {
        HEROIC_APP: {
            "winePrefix": f"{HOME}/Games/Heroic/Prefixes/default/DCS World",
            "wineVersion": {"name": "Proton - GE-Proton9-20", "type": "proton"},
        }
    }
)


class TestHeroic:
    def test_a_sideloaded_dcs_is_found(self) -> None:
        files = {
            f"{HOME}/.config/heroic/sideload_apps/library.json": HEROIC_LIBRARY,
            f"{HOME}/.config/heroic/GamesConfig/{HEROIC_APP}.json": HEROIC_GAME_CONFIG,
            **dcs_files(HEROIC_GAME),
        }
        (found,) = discover(FakeSystem(files=files, home=HOME), LAYOUT)
        assert found.launcher is Launcher.HEROIC
        assert found.game == Path(HEROIC_GAME)
        assert found.prefix == Path(f"{HOME}/Games/Heroic/Prefixes/default/DCS World")
        assert found.runtime == "Proton - GE-Proton9-20"

    def test_a_game_without_its_config_file_still_reports_the_game(self) -> None:
        files = {
            f"{HOME}/.config/heroic/sideload_apps/library.json": HEROIC_LIBRARY,
            **dcs_files(HEROIC_GAME),
        }
        (found,) = discover(FakeSystem(files=files, home=HOME), LAYOUT)
        assert found.prefix is None
        assert found.runtime is None

    def test_the_flatpak_heroic_layout_is_searched(self) -> None:
        config = f"{HOME}/.var/app/com.heroicgameslauncher.hgl/config/heroic"
        files = {
            f"{config}/sideload_apps/library.json": HEROIC_LIBRARY,
            **dcs_files(HEROIC_GAME),
        }
        (found,) = discover(FakeSystem(files=files, home=HOME), LAYOUT)
        assert found.launcher is Launcher.HEROIC


# --- Our own ---------------------------------------------------------------


class TestOurOwn:
    def test_the_install_this_tool_builds_is_found(self) -> None:
        files = {
            "/data/dcs/prefix/system.reg": "",
            "/data/toolchain/ge-proton/GE-Proton11-3/proton": "",
            **dcs_files("/data/dcs/game/DCS World"),
        }
        (found,) = discover(FakeSystem(files=files, home=HOME), LAYOUT)
        assert found.launcher is Launcher.DCS_LINUX
        assert found.game == Path("/data/dcs/game/DCS World")
        assert found.prefix == Path("/data/dcs/prefix")
        assert found.runtime == "GE-Proton11-3"

    def test_a_game_installed_inside_the_prefix_is_found_and_flagged(self) -> None:
        """The trap ADR-0001 exists to prevent: a rebuild would delete it."""
        game = "/data/dcs/prefix/drive_c/Program Files/Eagle Dynamics/DCS World"
        (found,) = discover(FakeSystem(files=dcs_files(game), home=HOME), LAYOUT)
        assert found.game == Path(game)
        assert found.under_prefix


# --- Across launchers ------------------------------------------------------


class TestDiscovery:
    def test_no_installs_at_all(self) -> None:
        assert discover(FakeSystem(home=HOME), LAYOUT) == ()

    def test_several_installs_are_all_listed(self) -> None:
        files = {
            **steam_files(),
            f"{HOME}/.config/lutris/games/dcs.yml": LUTRIS_CONFIG,
            **dcs_files(LUTRIS_GAME),
            "/data/dcs/prefix/system.reg": "",
            **dcs_files("/data/dcs/game/DCS World"),
        }
        found = discover(FakeSystem(files=files, home=HOME), LAYOUT)
        assert [install.launcher for install in found] == [
            Launcher.DCS_LINUX,
            Launcher.STEAM,
            Launcher.LUTRIS,
        ]
        assert len({install.install_id for install in found}) == 3

    def test_the_same_game_seen_twice_is_listed_once(self) -> None:
        """`~/.steam/root` and `~/.local/share/Steam` are the same directory."""
        files = {
            **steam_files(),
            f"{HOME}/.config/lutris/games/dcs.yml": (
                f"game:\n  exe: {STEAM_GAME}/bin/DCS.exe\n  prefix: /games/prefixes/dcs\n"
            ),
        }
        found = discover(FakeSystem(files=files, home=HOME), LAYOUT)
        assert [install.launcher for install in found] == [Launcher.STEAM]

    def test_discovery_writes_nothing(self) -> None:
        system = FakeSystem(files={**steam_files(), **dcs_files(LUTRIS_GAME)}, home=HOME)
        before = dict(system.files)
        discover(system, LAYOUT)
        assert system.files == before
