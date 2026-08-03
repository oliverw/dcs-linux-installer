"""Asking each launcher where it put DCS.

Every launcher answers in its own words — Valve KeyValues, Lutris YAML,
Heroic JSON — and this module turns all of them into the one shape the rest
of the tool understands, `DcsInstall`.

Two rules hold throughout. Nothing here writes: these files belong to other
programs, and discovery running against a live Steam or Lutris must be safe.
And nothing here fails: a missing, half-written or unreadable config means
"this launcher has no DCS", never an exception, because one broken config
must not hide the installs the other launchers found.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import yaml

from dcs_linux.installs import (
    DcsInstall,
    Launcher,
    detect_edition,
    find_install_root,
    install_root_for_exe,
    is_dcs_install,
    read_version,
)
from dcs_linux.paths import Layout
from dcs_linux.system import System
from dcs_linux.vdf import KeyValues, dig, parse

# DCS World Steam Edition. The standalone edition has no Steam app of its own.
STEAM_APP_ID = "223750"

# Where a user who installed DCS into our prefix would have put it. Only
# needed for our own layout: the other launchers record the executable path,
# so a game under their drive_c is found without guessing.
DRIVE_C_CANDIDATES = (
    "Program Files/Eagle Dynamics/DCS World",
    "DCS World",
    "Games/DCS World",
)


def discover(system: System, layout: Layout) -> tuple[DcsInstall, ...]:
    """Every DCS install on this machine, ours first.

    Two launchers can point at the same game directory — `~/.steam/root` and
    `~/.local/share/Steam` are usually the same place, and adopting an
    existing install leaves it managed by both. The game directory is the
    identity of an install, so the first launcher to claim one wins and the
    duplicate is dropped.
    """
    home = system.home()
    found: dict[Path, DcsInstall] = {}
    sources = (
        _own_installs(system, layout),
        _steam_installs(system, home),
        _lutris_installs(system, home),
        _heroic_installs(system, home),
    )
    for install in (install for source in sources for install in source):
        found.setdefault(install.game, install)
    return tuple(_describe(system, install) for install in found.values())


def _describe(system: System, install: DcsInstall) -> DcsInstall:
    """Fill in what the install itself knows, whichever launcher found it."""
    return replace(
        install,
        edition=detect_edition(system, install.game, install.launcher),
        version=read_version(system, install.game),
    )


# --- Our own ---------------------------------------------------------------


def _own_installs(system: System, layout: Layout) -> Iterator[DcsInstall]:
    runtime = _own_runtime(system, layout)
    root = find_install_root(system, layout.game)
    if root is not None:
        yield DcsInstall(
            game=root, launcher=Launcher.DCS_LINUX, prefix=layout.prefix, runtime=runtime
        )
    for candidate in DRIVE_C_CANDIDATES:
        inside = layout.prefix / "drive_c" / candidate
        if is_dcs_install(system, inside):
            yield DcsInstall(
                game=inside, launcher=Launcher.DCS_LINUX, prefix=layout.prefix, runtime=runtime
            )


def _own_runtime(system: System, layout: Layout) -> str | None:
    """The Proton build in our own toolchain, if one is unpacked."""
    for name in system.list_dir(layout.ge_proton):
        if system.exists(layout.ge_proton / name / "proton"):
            return name
    return None


# --- Steam -----------------------------------------------------------------


def _steam_roots(home: Path) -> tuple[Path, ...]:
    return (
        home / ".steam" / "root",
        home / ".steam" / "steam",
        home / ".local" / "share" / "Steam",
        home / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam",
    )


def _steam_installs(system: System, home: Path) -> Iterator[DcsInstall]:
    for root in _steam_roots(home):
        for library in _steam_libraries(system, root):
            manifest = _read_vdf(system, library / "steamapps" / f"appmanifest_{STEAM_APP_ID}.acf")
            directory = dig(manifest, "appstate", "installdir")
            if not directory:
                continue
            game = library / "steamapps" / "common" / directory
            if not is_dcs_install(system, game):
                continue
            compatdata = library / "steamapps" / "compatdata" / STEAM_APP_ID
            yield DcsInstall(
                game=game,
                launcher=Launcher.STEAM,
                prefix=compatdata / "pfx",
                runtime=_steam_runtime(system, root, compatdata),
            )


def _steam_libraries(system: System, root: Path) -> list[Path]:
    """The root itself plus every library folder registered with it."""
    libraries = [root]
    folders = _read_vdf(system, root / "steamapps" / "libraryfolders.vdf").get("libraryfolders")
    for entry in (folders or {}).values() if isinstance(folders, dict) else ():
        # Steam ≥ 2021 nests a block per library; older files map index to path.
        path = dig(entry, "path") if isinstance(entry, dict) else entry
        if path and Path(path) not in libraries:
            libraries.append(Path(path))
    return libraries


def _steam_runtime(system: System, root: Path, compatdata: Path) -> str | None:
    """Which Proton build Steam runs DCS with.

    The mapping only exists once a compatibility tool has been chosen by
    hand, so the prefix's own `version` file is the fallback — it records
    what actually built the prefix.
    """
    config = _read_vdf(system, root / "config" / "config.vdf")
    chosen = dig(
        config,
        "installconfigstore",
        "software",
        "valve",
        "steam",
        "compattoolmapping",
        STEAM_APP_ID,
        "name",
    )
    return chosen or _first_line(system.read_text(compatdata / "version"))


def _read_vdf(system: System, path: Path) -> KeyValues:
    text = system.read_text(path)
    return parse(text) if text is not None else {}


# --- Lutris ----------------------------------------------------------------


def _lutris_game_dirs(home: Path) -> tuple[Path, ...]:
    return (
        home / ".config" / "lutris" / "games",
        home / ".var" / "app" / "net.lutris.Lutris" / "config" / "lutris" / "games",
    )


def _lutris_installs(system: System, home: Path) -> Iterator[DcsInstall]:
    for directory in _lutris_game_dirs(home):
        for name in sorted(system.list_dir(directory)):
            if not name.endswith((".yml", ".yaml")):
                continue
            config = _read_yaml(system, directory / name)
            game = config.get("game")
            if not isinstance(game, dict):
                continue
            # Lutris records the binary to run, not the game that is
            # installed, so the executable is also how we tell this is DCS.
            root = _install_above(system, game.get("exe"))
            if root is None:
                continue
            yield DcsInstall(
                game=root,
                launcher=Launcher.LUTRIS,
                prefix=_as_path(game.get("prefix")),
                runtime=_as_text(_dig_mapping(config, "wine", "version")),
            )


def _read_yaml(system: System, path: Path) -> dict[str, object]:
    text = system.read_text(path)
    if text is None:
        return {}
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --- Heroic ----------------------------------------------------------------


def _heroic_dirs(home: Path) -> tuple[Path, ...]:
    return (
        home / ".config" / "heroic",
        home / ".var" / "app" / "com.heroicgameslauncher.hgl" / "config" / "heroic",
    )


def _heroic_installs(system: System, home: Path) -> Iterator[DcsInstall]:
    for directory in _heroic_dirs(home):
        # DCS is on no store Heroic supports, so it can only be a sideloaded
        # app there — added by the user, pointing at an existing install.
        library = _read_json(system, directory / "sideload_apps" / "library.json")
        games = library.get("games")
        for entry in games if isinstance(games, list) else ():
            if not isinstance(entry, dict):
                continue
            root = _install_above(system, _dig_mapping(entry, "install", "executable"))
            app_name = _as_text(entry.get("app_name"))
            if root is None or app_name is None:
                continue
            settings = _read_json(system, directory / "GamesConfig" / f"{app_name}.json")
            game_config = settings.get(app_name)
            game_config = game_config if isinstance(game_config, dict) else {}
            yield DcsInstall(
                game=root,
                launcher=Launcher.HEROIC,
                prefix=_as_path(game_config.get("winePrefix")),
                runtime=_as_text(_dig_mapping(game_config, "wineVersion", "name")),
            )


def _read_json(system: System, path: Path) -> dict[str, object]:
    text = system.read_text(path)
    if text is None:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --- Shared ----------------------------------------------------------------


def _install_above(system: System, executable: object) -> Path | None:
    """The DCS install holding a launcher's configured executable, if any."""
    path = _as_path(executable)
    if path is None or not path.is_absolute():
        return None
    return install_root_for_exe(system, path)


def _dig_mapping(mapping: dict[str, object], *keys: str) -> object:
    node: object = mapping
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _as_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _as_path(value: object) -> Path | None:
    text = _as_text(value)
    return Path(text) if text is not None else None


def _first_line(text: str | None) -> str | None:
    return _as_text(text.strip().splitlines()[0].strip()) if text and text.strip() else None
