"""The record of installs this tool created, and what discovery does with it."""

from __future__ import annotations

import json
from pathlib import Path

from dcs_linux.installs import Launcher
from dcs_linux.launchers import discover
from dcs_linux.registry import register, registered
from tests.environments import LAYOUT
from tests.fakes import FakeSystem, FakeWriter

ELSEWHERE = Path("/mnt/big/DCS World")


def test_nothing_is_registered_on_a_fresh_machine() -> None:
    assert registered(FakeSystem(), LAYOUT) == ()


def test_registering_records_the_game_directory_and_its_prefix() -> None:
    system = FakeSystem()
    changed = register(system, FakeWriter(system), LAYOUT, game=ELSEWHERE, prefix=LAYOUT.prefix)

    assert changed is True
    entries = registered(system, LAYOUT)
    assert [entry.game for entry in entries] == [ELSEWHERE]
    assert entries[0].prefix == LAYOUT.prefix


def test_registering_the_same_install_twice_writes_nothing_new() -> None:
    system = FakeSystem()
    writer = FakeWriter(system)
    register(system, writer, LAYOUT, game=ELSEWHERE, prefix=LAYOUT.prefix)
    changed = register(system, writer, LAYOUT, game=ELSEWHERE, prefix=LAYOUT.prefix)

    assert changed is False
    assert len(registered(system, LAYOUT)) == 1


def test_a_moved_prefix_updates_the_entry_rather_than_adding_one() -> None:
    """The game directory is the identity (ADR-0007); the prefix is a detail."""
    system = FakeSystem()
    writer = FakeWriter(system)
    register(system, writer, LAYOUT, game=ELSEWHERE, prefix=Path("/old/prefix"))
    changed = register(system, writer, LAYOUT, game=ELSEWHERE, prefix=LAYOUT.prefix)

    assert changed is True
    assert [entry.prefix for entry in registered(system, LAYOUT)] == [LAYOUT.prefix]


def test_a_damaged_register_reads_as_empty_rather_than_raising() -> None:
    system = FakeSystem(files={str(LAYOUT.installs_register): "{ truncated"})

    assert registered(system, LAYOUT) == ()


def test_a_registered_install_is_found_without_being_searched_for() -> None:
    """The whole point: `--game-dir /mnt/big` is not somewhere discovery looks."""
    system = FakeSystem(files={str(ELSEWHERE / "bin" / "DCS.exe"): "MZ"})
    register(system, FakeWriter(system), LAYOUT, game=ELSEWHERE, prefix=LAYOUT.prefix)

    installs = discover(system, LAYOUT)

    assert [install.game for install in installs] == [ELSEWHERE]
    assert installs[0].launcher is Launcher.DCS_LINUX
    assert installs[0].prefix == LAYOUT.prefix


def test_a_registered_install_that_has_been_deleted_is_not_reported() -> None:
    system = FakeSystem()
    register(system, FakeWriter(system), LAYOUT, game=ELSEWHERE, prefix=LAYOUT.prefix)

    assert discover(system, LAYOUT) == ()


def test_the_register_is_json_a_human_can_read() -> None:
    system = FakeSystem()
    register(system, FakeWriter(system), LAYOUT, game=ELSEWHERE, prefix=LAYOUT.prefix)

    payload = json.loads(system.read_text(LAYOUT.installs_register) or "")

    assert payload["installs"] == [{"game": str(ELSEWHERE), "prefix": str(LAYOUT.prefix)}]
