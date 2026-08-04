"""The handoff to the DCS updater: what is prepared, launched and picked up."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

from dcs_linux.prefix import PREFIX_MARKER, Step, StepStatus
from dcs_linux.registry import registered
from dcs_linux.updater import (
    DOWNLOAD_PAGE,
    WEB_INSTALLER_NAME,
    HandoffResult,
    Stage,
    briefing,
    find_installer,
    handoff,
    progress,
    verify_installer,
)
from tests.environments import LAYOUT
from tests.fakes import FakeRunner, FakeSystem, FakeWriter

GAME_ROOT = LAYOUT.game / "DCS World"
INSTALLER = LAYOUT.toolchain / WEB_INSTALLER_NAME
# A PE file starts "MZ", and the real web installer is a few megabytes.
INSTALLER_BYTES = b"MZ" + b"\0" * (4 * 1024 * 1024)
AUTOUPDATE = '{"version": "2.9.28.26385"}'


def machine(
    files: dict[str, str] | None = None, blobs: dict[str, bytes] | None = None
) -> FakeSystem:
    """A built, correctly mapped prefix — what `install` leaves behind."""
    return FakeSystem(
        files={str(LAYOUT.prefix / PREFIX_MARKER): "WINE REGISTRY", **(files or {})},
        blobs=blobs,
        directories={str(LAYOUT.game), str(LAYOUT.saved_games)},
        links={
            str(LAYOUT.prefix_game_drive): str(LAYOUT.game),
            str(LAYOUT.prefix_saved_games): str(LAYOUT.saved_games),
        },
    )


def installed(system: FakeSystem, *, complete: bool = True) -> None:
    """Put a DCS install under the game directory, as the updater would."""
    system.files[GAME_ROOT / "bin" / "DCS_updater.exe"] = b"MZ"
    system.files[GAME_ROOT / "autoupdate.cfg"] = AUTOUPDATE.encode()
    if complete:
        system.files[GAME_ROOT / "bin" / "DCS.exe"] = b"MZ"


def with_installer(system: FakeSystem) -> FakeSystem:
    system.files[INSTALLER] = INSTALLER_BYTES
    return system


def unmap(system: FakeSystem, link: Path) -> None:
    system.links.pop(link)
    system.symlinks.discard(link)


def step(result: HandoffResult, name: str) -> Step:
    steps = {found.name: found for found in result.steps}
    assert name in steps, f"no {name!r} step in {sorted(steps)}"
    return steps[name]


def run(
    system: FakeSystem,
    runner: FakeRunner,
    *,
    installer: Path | None = None,
    announce: Callable[[str], None] = lambda _: None,
) -> HandoffResult:
    return handoff(
        system, FakeWriter(system), runner, LAYOUT, installer=installer, announce=announce
    )


def finishes(system: FakeSystem, *, complete: bool = True) -> FakeRunner:
    """A runner whose GUI leaves an install behind, as the real one would."""

    def install() -> None:
        installed(system, complete=complete)

    return FakeRunner(
        effects={
            str(INSTALLER): install,
            str(GAME_ROOT / "bin" / "DCS_updater.exe"): install,
        }
    )


# --- What is on disk -------------------------------------------------------


def test_an_empty_game_directory_is_nothing_installed() -> None:
    assert progress(machine(), LAYOUT).stage is Stage.ABSENT


def test_an_install_with_the_game_binary_is_complete() -> None:
    system = machine()
    installed(system)
    found = progress(system, LAYOUT)

    assert found.stage is Stage.COMPLETE
    assert found.game_root == GAME_ROOT
    assert found.version == "2.9.28.26385"


def test_an_install_without_the_game_binary_is_only_partial() -> None:
    """Bootstrapped and downloading, or abandoned halfway. Not runnable."""
    system = machine()
    installed(system, complete=False)

    assert progress(system, LAYOUT).stage is Stage.PARTIAL


# --- The web installer -----------------------------------------------------


def test_the_installer_is_found_in_the_toolchain_directory() -> None:
    assert find_installer(with_installer(machine()), LAYOUT) == INSTALLER


def test_the_installer_filename_is_matched_case_insensitively() -> None:
    """It arrives by hand, and the real one is lowercase `web` (#2)."""
    odd = LAYOUT.toolchain / "dcs_world_WEB.exe"
    system = machine(files={str(odd): "MZ"})

    assert find_installer(system, LAYOUT) == odd


def test_a_named_installer_outranks_the_toolchain_copy() -> None:
    elsewhere = Path("/home/pilot/Downloads/DCS_World_web.exe")
    system = with_installer(machine(files={str(elsewhere): "MZ"}))

    assert find_installer(system, LAYOUT, elsewhere) == elsewhere


def test_a_named_installer_that_is_not_there_is_not_silently_replaced() -> None:
    system = with_installer(machine())

    assert find_installer(system, LAYOUT, Path("/nowhere/DCS_World_web.exe")) is None


def test_a_verified_installer_carries_the_hash_it_was_verified_by() -> None:
    system = with_installer(machine())
    verified = verify_installer(system, INSTALLER)

    assert verified.refusal is None
    assert verified.installer is not None
    assert verified.installer.sha256 == hashlib.sha256(INSTALLER_BYTES).hexdigest()
    assert verified.installer.size == len(INSTALLER_BYTES)


def test_something_that_is_not_a_windows_executable_is_refused() -> None:
    """A download page saved as HTML is the common way this goes wrong."""
    system = machine(files={str(INSTALLER): "<!doctype html>" + "x" * (4 * 1024 * 1024)})
    verified = verify_installer(system, INSTALLER)

    assert verified.installer is None
    assert verified.refusal is not None
    assert "Windows executable" in verified.refusal


def test_a_truncated_installer_is_refused() -> None:
    system = machine(blobs={str(INSTALLER): b"MZ" + b"\0" * 100})
    verified = verify_installer(system, INSTALLER)

    assert verified.installer is None
    assert verified.refusal is not None and "truncated" in verified.refusal


# --- The handoff -----------------------------------------------------------


def test_the_handoff_refuses_without_a_prefix_to_hand_off_into() -> None:
    result = run(FakeSystem(), FakeRunner())

    assert result.ok is False
    assert step(result, "prefix").status is StepStatus.FAILED
    assert "dcs-linux install" in step(result, "prefix").detail


def test_the_handoff_refuses_when_the_game_drive_is_not_mapped() -> None:
    """Unmapped, the updater would install into the prefix and lose it all."""
    system = with_installer(machine())
    unmap(system, LAYOUT.prefix_game_drive)
    result = run(system, FakeRunner())

    assert result.ok is False
    assert step(result, "mapping").status is StepStatus.FAILED


def test_an_unmapped_saved_games_is_a_warning_not_a_refusal() -> None:
    """It costs a re-login on the next rebuild, which is the user's to accept."""
    system = with_installer(machine())
    unmap(system, LAYOUT.prefix_saved_games)
    result = run(system, finishes(system))

    assert result.ok is True
    assert "log in again" in step(result, "mapping").detail


def test_a_bare_machine_is_bootstrapped_with_the_web_installer() -> None:
    system = with_installer(machine())
    runner = finishes(system)
    result = run(system, runner)

    assert result.ok is True, result.steps
    command, environment = runner.calls[0]
    assert command == [str(LAYOUT.umu_run), str(INSTALLER)]
    assert environment["WINEPREFIX"] == str(LAYOUT.prefix)


def test_a_bootstrapped_directory_is_updated_rather_than_re_bootstrapped() -> None:
    """`DCS_World_web.exe` refuses a directory it has already used (#2)."""
    system = with_installer(machine())
    installed(system)
    runner = FakeRunner()
    result = run(system, runner)

    assert result.ok is True
    command, _ = runner.calls[0]
    assert command == [str(LAYOUT.umu_run), str(GAME_ROOT / "bin" / "DCS_updater.exe"), "update"]


def test_a_missing_installer_says_where_to_get_it_and_where_to_put_it() -> None:
    result = run(machine(), FakeRunner())

    assert result.ok is False
    detail = step(result, "web installer").detail
    assert DOWNLOAD_PAGE in detail
    assert str(LAYOUT.toolchain) in detail


def test_an_abandoned_download_is_reported_as_such_with_the_way_to_retry() -> None:
    system = with_installer(machine())
    result = run(system, FakeRunner())

    assert result.ok is False
    detail = step(result, "DCS").detail
    assert "nothing was installed" in detail
    assert "again" in detail


def test_a_half_finished_download_is_resumable_not_broken() -> None:
    system = with_installer(machine())
    result = run(system, finishes(system, complete=False))

    assert result.ok is False
    assert "resumes" in step(result, "DCS").detail
    assert registered(system, LAYOUT) == ()


def test_a_finished_install_is_registered_so_check_finds_it() -> None:
    system = with_installer(machine())
    result = run(system, finishes(system))

    assert result.ok is True
    assert [entry.game for entry in registered(system, LAYOUT)] == [GAME_ROOT]
    assert step(result, "register").status is StepStatus.DONE


def test_the_user_is_told_what_to_do_in_the_gui_before_it_opens() -> None:
    said: list[str] = []
    system = with_installer(machine())
    run(system, finishes(system), announce=said.append)

    spoken = "\n".join(said)
    assert spoken, "the GUI opened with nothing said about it"
    assert "D:\\" in spoken
    assert "torrent" in spoken.lower()


def test_the_briefing_is_honest_that_the_download_rate_is_not_measured() -> None:
    text = briefing(LAYOUT, progress(machine(), LAYOUT))

    assert "not measured" in text


def test_the_briefing_for_a_resumed_install_says_it_resumes() -> None:
    system = machine()
    installed(system, complete=False)

    assert "resume" in briefing(LAYOUT, progress(system, LAYOUT)).lower()
