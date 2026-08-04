"""Building the runtime: toolchain, prefix, winetricks verbs and the mapping.

Written against a fixture machine, so the thing under test is the sequencing
and the postconditions — which is where the empirical surprises live (umu
exiting 1 on success, the prefix being disposable only if the mapping holds).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from dcs_linux.paths import Layout
from dcs_linux.prefix import (
    GAMEID,
    GE_PROTON_VERSION,
    LAUNCH_ENVIRONMENT,
    UMU_VERSION,
    WINETRICKS_VERBS,
    BuildResult,
    StepStatus,
    build,
    read_manifest,
    resolve_verbs,
)
from dcs_linux.runner import Completed
from tests.fakes import FakeFetcher, FakeRunner, FakeSystem, FakeWriter

LAYOUT = Layout(
    root=Path("/data/dcs"),
    toolchain=Path("/data/toolchain"),
    state=Path("/data/state"),
)

UMU_PAYLOAD = {"umu-launcher": {"umu/umu-run": "#!/usr/bin/env python"}}
PROTON_PAYLOAD = {"proton-ge-custom": {f"{GE_PROTON_VERSION}/proton": "#!/usr/bin/env python"}}
PAYLOADS = {**UMU_PAYLOAD, **PROTON_PAYLOAD}


def machine(files: dict[str, str] | None = None) -> FakeSystem:
    return FakeSystem(files=files)


def creates_prefix(system: FakeSystem, layout: Layout = LAYOUT) -> dict[str, Callable[[], None]]:
    """umu's side effect: a prefix appears, whatever the exit code says."""

    def effect() -> None:
        system.files[layout.prefix / "system.reg"] = b"WINE REGISTRY Version 2\n"

    return {"prefix": effect}


def install(
    system: FakeSystem,
    *,
    runner: FakeRunner | None = None,
    fetcher: FakeFetcher | None = None,
    layout: Layout = LAYOUT,
    verbs: tuple[str, ...] = WINETRICKS_VERBS,
    rebuild: bool = False,
) -> tuple[BuildResult, FakeRunner, FakeFetcher]:
    runner = runner or FakeRunner(effects=creates_prefix(system, layout))
    fetcher = fetcher or FakeFetcher(system, payloads=PAYLOADS)
    result = build(
        system, FakeWriter(system), runner, fetcher, layout, verbs=verbs, rebuild=rebuild
    )
    return result, runner, fetcher


def statuses(result: BuildResult) -> dict[str, StepStatus]:
    return {step.name: step.status for step in result.steps}


def test_bare_machine_gets_a_complete_runtime() -> None:
    system = machine()
    result, runner, fetcher = install(system)

    assert result.ok
    # Everything real is done; only the wipe, which was not asked for, is not.
    assert statuses(result) == {
        "prefix wipe": StepStatus.SKIPPED,
        "umu-launcher": StepStatus.DONE,
        "GE-Proton": StepStatus.DONE,
        "prefix": StepStatus.DONE,
        "winetricks": StepStatus.DONE,
        "mapping": StepStatus.DONE,
        "manifest": StepStatus.DONE,
    }
    assert system.exists(LAYOUT.umu_run)
    assert system.exists(LAYOUT.ge_proton_build(GE_PROTON_VERSION) / "proton")
    assert system.exists(LAYOUT.prefix / "system.reg")
    assert runner.commands() == ["prefix", "winetricks"]
    assert any(GE_PROTON_VERSION in url for url in fetcher.urls)


def test_the_toolchain_versions_are_pinned_not_resolved() -> None:
    """Nothing asks a release API what is newest — the URLs carry the pins."""
    system = machine()
    _, _, fetcher = install(system)

    assert any(f"/{UMU_VERSION}/" in url for url in fetcher.urls)
    assert all("api.github.com" not in url for url in fetcher.urls)
    assert all("latest" not in url for url in fetcher.urls)


def test_the_pinned_versions_are_recorded_in_the_prefix() -> None:
    system = machine()
    result, _, _ = install(system)

    recorded = read_manifest(system, LAYOUT)
    assert recorded is not None
    assert recorded.umu_version == UMU_VERSION
    assert recorded.ge_proton == GE_PROTON_VERSION
    assert recorded.gameid == GAMEID
    assert recorded.verbs == WINETRICKS_VERBS
    assert result.runtime == recorded


def test_the_prefix_is_built_with_the_pinned_proton() -> None:
    system = machine()
    _, runner, _ = install(system)

    command, environment = runner.calls[0]
    assert command == [str(LAYOUT.umu_run), ""]
    assert environment["WINEPREFIX"] == str(LAYOUT.prefix)
    assert environment["PROTONPATH"] == str(LAYOUT.ge_proton_build(GE_PROTON_VERSION))
    assert environment["GAMEID"] == GAMEID


def test_a_non_zero_umu_exit_is_not_a_failure() -> None:
    """`umu-run ""` exits 1 having built a perfectly good prefix (ADR-0003)."""
    system = machine()
    runner = FakeRunner(
        results={"prefix": Completed(returncode=1)},
        effects=creates_prefix(system),
    )
    result, _, _ = install(system, runner=runner)

    assert result.ok
    assert statuses(result)["prefix"] is StepStatus.DONE


def test_a_missing_system_reg_is_a_failure_whatever_the_exit_code() -> None:
    system = machine()
    runner = FakeRunner(results={"prefix": Completed(returncode=0)})
    result, _, _ = install(system, runner=runner)

    assert not result.ok
    assert statuses(result)["prefix"] is StepStatus.FAILED
    assert "system.reg" in _detail(result, "prefix")
    # Nothing downstream ran on a prefix that does not exist.
    assert "winetricks" not in statuses(result)


def test_winetricks_verbs_are_applied_to_the_umu_prefix() -> None:
    system = machine()
    _, runner, _ = install(system)

    command, environment = runner.calls[1]
    assert command == [str(LAYOUT.umu_run), "winetricks", *WINETRICKS_VERBS]
    assert environment["WINEPREFIX"] == str(LAYOUT.prefix)


def test_a_failing_winetricks_stops_the_install() -> None:
    system = machine()
    runner = FakeRunner(
        results={"winetricks": Completed(returncode=1)},
        effects=creates_prefix(system),
    )
    result, _, _ = install(system, runner=runner)

    assert not result.ok
    assert statuses(result)["winetricks"] is StepStatus.FAILED


def test_vcrun2019_is_refused_with_the_reason() -> None:
    refused = resolve_verbs(["vcrun2019"])
    assert refused.verbs == ()
    assert refused.refusal is not None
    assert "RAM leak" in refused.refusal
    assert "vcrun2015" in refused.refusal and "vcrun2022" in refused.refusal


def test_an_extra_verb_is_added_to_the_defaults_not_swapped_for_them() -> None:
    resolved = resolve_verbs(["vcrun2022"])
    assert resolved.refusal is None
    assert resolved.verbs == (*WINETRICKS_VERBS, "vcrun2022")


def test_the_dll_override_and_launch_variables_are_recorded() -> None:
    system = machine()
    result, _, _ = install(system)

    assert result.runtime is not None
    assert result.runtime.environment["WINEDLLOVERRIDES"] == "wbemprox=n"
    assert result.runtime.environment == LAUNCH_ENVIRONMENT


def test_the_durable_directories_are_created_and_mapped_out() -> None:
    system = machine()
    install(system)

    assert system.exists(LAYOUT.game)
    assert system.exists(LAYOUT.saved_games)
    assert system.is_symlink(LAYOUT.prefix_game_drive)
    assert system.resolve(LAYOUT.prefix_game_drive) == LAYOUT.game
    assert system.is_symlink(LAYOUT.prefix_saved_games)
    assert system.resolve(LAYOUT.prefix_saved_games) == LAYOUT.saved_games


def test_the_game_directory_can_be_somewhere_else_entirely() -> None:
    layout = Layout(
        root=LAYOUT.root,
        toolchain=LAYOUT.toolchain,
        state=LAYOUT.state,
        game_dir=Path("/mnt/big/DCS"),
    )
    system = machine()
    runner = FakeRunner(effects=creates_prefix(system, layout))
    result, _, _ = install(system, runner=runner, layout=layout)

    assert result.ok
    assert system.resolve(layout.prefix_game_drive) == Path("/mnt/big/DCS")
    assert result.runtime is not None
    assert result.runtime.game == Path("/mnt/big/DCS")


def test_re_running_on_a_finished_install_writes_nothing() -> None:
    system = machine()
    install(system)
    result, runner, fetcher = install(system)

    assert result.ok
    assert runner.calls == []
    assert fetcher.urls == []
    # Every step skipped, manifest included: a DONE that changed nothing would
    # make every other DONE unreadable.
    assert set(statuses(result).values()) == {StepStatus.SKIPPED}


def test_a_rebuild_keeps_the_game_directory_and_the_login() -> None:
    """The repair the whole architecture rests on (ADR-0001)."""
    system = machine()
    install(system)
    system.files[LAYOUT.game / "DCS World" / "bin" / "DCS.exe"] = b"MZ"
    system.files[LAYOUT.saved_games / "DCS" / "Config" / "authdata.bin"] = b"login"

    runner = FakeRunner(effects=creates_prefix(system))
    result, _, _ = install(system, runner=runner, rebuild=True)

    assert result.ok
    assert statuses(result)["prefix wipe"] is StepStatus.DONE
    assert system.exists(LAYOUT.game / "DCS World" / "bin" / "DCS.exe")
    assert system.read_bytes(LAYOUT.saved_games / "DCS" / "Config" / "authdata.bin") == b"login"
    # And the mapping is back, so the rebuilt prefix can see both again.
    assert system.resolve(LAYOUT.prefix_saved_games) == LAYOUT.saved_games


def test_a_rebuild_refuses_when_the_game_is_inside_the_prefix() -> None:
    """Wiping then would not be a repair; it would be the accident."""
    layout = Layout(
        root=LAYOUT.root,
        toolchain=LAYOUT.toolchain,
        state=LAYOUT.state,
        game_dir=LAYOUT.prefix / "drive_c" / "Program Files" / "DCS World",
    )
    system = machine(files={str(layout.game / "bin" / "DCS.exe"): "MZ"})
    result, runner, _ = install(system, layout=layout, rebuild=True)

    assert not result.ok
    assert statuses(result)["prefix wipe"] is StepStatus.FAILED
    assert "would be destroyed" in _detail(result, "prefix wipe")
    assert system.exists(layout.game / "bin" / "DCS.exe")
    assert runner.calls == []


def test_a_rebuild_refuses_when_the_game_reaches_inside_the_prefix_by_symlink() -> None:
    """The guard compares resolved paths, not spellings.

    A game directory that is a symlink into the prefix looks safe to a string
    comparison, and wiping it would take the download with it.
    """
    system = machine()
    system.links[LAYOUT.game] = LAYOUT.prefix / "drive_c" / "DCS"
    system.symlinks.add(LAYOUT.game)
    system.files[LAYOUT.prefix / "drive_c" / "DCS" / "bin" / "DCS.exe"] = b"MZ"

    result, runner, _ = install(system, rebuild=True)

    assert not result.ok
    assert statuses(result)["prefix wipe"] is StepStatus.FAILED
    assert system.exists(LAYOUT.prefix / "drive_c" / "DCS" / "bin" / "DCS.exe")
    assert runner.calls == []


def test_bumping_the_umu_pin_re_fetches_it() -> None:
    """An unversioned zipapp path cannot say which build it is (ADR-0008)."""
    system = machine()
    install(system)
    system.files[LAYOUT.umu_version_marker] = b"1.4.3\n"

    result, _, fetcher = install(system)

    assert result.ok
    assert statuses(result)["umu-launcher"] is StepStatus.DONE
    assert any(f"/{UMU_VERSION}/" in url for url in fetcher.urls)
    assert (system.read_text(LAYOUT.umu_version_marker) or "").strip() == UMU_VERSION


def test_a_stale_mapping_is_re_pointed_at_the_current_game_directory() -> None:
    system = machine()
    install(system)
    FakeWriter(system).symlink(LAYOUT.prefix_game_drive, Path("/somewhere/old"))

    result, _, _ = install(system)

    assert statuses(result)["mapping"] is StepStatus.DONE
    assert system.resolve(LAYOUT.prefix_game_drive) == LAYOUT.game


def test_a_failed_download_stops_before_the_prefix_is_touched() -> None:
    system = machine()
    fetcher = FakeFetcher(system, payloads=PAYLOADS, failures={"umu-launcher": "network is down"})
    result, runner, _ = install(system, fetcher=fetcher)

    assert not result.ok
    assert statuses(result)["umu-launcher"] is StepStatus.FAILED
    assert "network is down" in _detail(result, "umu-launcher")
    assert runner.calls == []


def test_an_archive_without_umu_run_in_it_is_a_failure() -> None:
    system = machine()
    fetcher = FakeFetcher(system, payloads=PROTON_PAYLOAD)
    result, _, _ = install(system, fetcher=fetcher)

    assert not result.ok
    assert "umu-run" in _detail(result, "umu-launcher")


def test_a_new_verb_re_runs_winetricks_on_the_existing_prefix() -> None:
    system = machine()
    install(system)
    verbs = resolve_verbs(["vcrun2022"]).verbs

    result, runner, _ = install(system, verbs=verbs)

    assert result.ok
    assert runner.commands() == ["winetricks"]
    assert statuses(result)["prefix"] is StepStatus.SKIPPED
    recorded = read_manifest(system, LAYOUT)
    assert recorded is not None and recorded.verbs == verbs


def test_a_damaged_manifest_reads_as_absent() -> None:
    system = machine()
    install(system)
    system.files[LAYOUT.manifest] = b"{not json"

    assert read_manifest(system, LAYOUT) is None
    # And the install rebuilds rather than trusting the wreckage.
    result, runner, _ = install(system)
    assert result.ok
    assert "winetricks" in runner.commands()


def test_the_manifest_is_json_a_bug_report_can_carry() -> None:
    system = machine()
    install(system)

    payload = json.loads(system.read_text(LAYOUT.manifest) or "")
    assert payload["ge_proton"] == GE_PROTON_VERSION
    assert payload["verbs"] == list(WINETRICKS_VERBS)
    assert payload["game"] == str(LAYOUT.game)


def _detail(result: BuildResult, name: str) -> str:
    return next(step.detail for step in result.steps if step.name == name)
