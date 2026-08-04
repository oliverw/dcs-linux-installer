"""The patch engine, against a fixture machine.

The point of these tests is the awkward half of the lifecycle: not "does apply
write the file", but what happens after a DCS update has silently undone the
work, and whether revert really puts back what was there.
"""

from __future__ import annotations

from pathlib import Path

from dcs_linux.checks import PATCHES, Status, check_patches
from dcs_linux.patches import (
    REGISTRY,
    SEGOE_FONT_NAMES,
    SEGOE_FONT_PATCH,
    Outcome,
    Patch,
    PatchStatus,
    Plan,
    apply_patch,
    find_substitute_font,
    revert_patch,
    safe_patches,
    states,
)
from dcs_linux.patchstate import PatchStore, load
from dcs_linux.paths import TargetPaths
from dcs_linux.probes import probe_patches
from dcs_linux.system import CommandResult, System
from tests.environments import LAYOUT, OWN_INSTALL, PATHS, healthy_environment
from tests.fakes import FakeSystem, FakeWriter

DEJAVU = Path("/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf")
FONT_BYTES = b"\x00\x01\x00\x00DejaVu"
DCS_VERSION = "2.9.28.26385"

STORE = PatchStore(directory=LAYOUT.patch_store(OWN_INSTALL.install_id))


def machine(**overrides: object) -> FakeSystem:
    """A machine with a prefix and a substitute font available."""
    defaults: dict[str, object] = {
        "blobs": {str(DEJAVU): FONT_BYTES},
        "directories": {str(PATHS.prefix), str(PATHS.fonts)},
    }
    return FakeSystem(**{**defaults, **overrides})  # type: ignore[arg-type]


def plan_nothing(system: System, paths: TargetPaths) -> Plan:
    """A planner for registry tests that never reach the filesystem."""
    return Plan()


def apply(system: FakeSystem, writer: FakeWriter) -> Outcome:
    return apply_patch(system, writer, STORE, SEGOE_FONT_PATCH, PATHS, DCS_VERSION)


def status(system: FakeSystem) -> PatchStatus:
    return states(system, STORE)[0].status


def install_files(system: FakeSystem) -> dict[Path, bytes]:
    """Everything on the machine except the state store.

    Revert is a claim about the *install*, not about the machine: the store
    legitimately survives, holding an emptied record.
    """
    return {
        path: data
        for path, data in system.files.items()
        if not path.is_relative_to(STORE.directory)
    }


class TestApply:
    def test_writes_every_segoe_name_into_the_prefix(self) -> None:
        system = machine()
        outcome = apply(system, FakeWriter(system))

        assert outcome.ok and outcome.changed
        for name in SEGOE_FONT_NAMES:
            assert system.read_bytes(PATHS.fonts / name) == FONT_BYTES

    def test_state_records_the_patch_the_version_and_a_hash_per_file(self) -> None:
        system = machine()
        apply(system, FakeWriter(system))

        record = load(system, STORE)["segoe-fonts"]
        assert record.dcs_version == DCS_VERSION
        assert {file.path.name for file in record.files} == set(SEGOE_FONT_NAMES)
        assert all(len(file.sha256) == 64 for file in record.files)

    def test_state_and_backups_live_outside_the_install(self) -> None:
        """`DCS_updater repair` deletes anything ED's manifest does not list."""
        system = machine(files={str(PATHS.fonts / "segoeui.ttf"): "older"})
        apply(system, FakeWriter(system))

        written = [*system.files, *system.directories]
        under_store = [path for path in written if STORE.directory in path.parents]
        assert under_store, "nothing was written to the state store"
        for path in under_store:
            assert not path.is_relative_to(PATHS.prefix)
            assert not path.is_relative_to(PATHS.game)

    def test_applying_twice_is_a_no_op(self) -> None:
        system = machine()
        writer = FakeWriter(system)
        apply(system, writer)
        before = dict(system.files)

        second = apply(system, writer)

        assert second.ok and not second.changed
        assert second.detail == "already applied"
        assert system.files == before
        assert len(load(system, STORE)) == 1

    def test_a_pre_existing_file_is_backed_up_before_it_is_overwritten(self) -> None:
        system = machine(files={str(PATHS.fonts / "segoeui.ttf"): "the original"})
        apply(system, FakeWriter(system))

        record = load(system, STORE)["segoe-fonts"]
        backed_up = [file for file in record.files if file.backup is not None]
        assert [file.path.name for file in backed_up] == ["segoeui.ttf"]
        assert system.read_text(STORE.absolute(backed_up[0].backup or "")) == "the original"


class TestRefusal:
    def test_no_prefix_means_nothing_is_written(self) -> None:
        system = FakeSystem(blobs={str(DEJAVU): FONT_BYTES})
        before = dict(system.files)

        outcome = apply(system, FakeWriter(system))

        assert not outcome.ok
        assert "no wine prefix" in outcome.detail
        assert system.files == before

    def test_no_substitute_font_says_which_package_to_install(self) -> None:
        system = machine(blobs={})
        before = dict(system.files)

        outcome = apply(system, FakeWriter(system))

        assert not outcome.ok
        assert "dejavu" in outcome.detail
        assert system.files == before
        assert load(system, STORE) == {}


class TestRevert:
    def test_files_the_patch_created_are_deleted(self) -> None:
        system = machine()
        writer = FakeWriter(system)
        before = install_files(system)
        apply(system, writer)

        outcome = revert_patch(system, writer, STORE, SEGOE_FONT_PATCH)

        assert outcome.ok and outcome.changed
        assert install_files(system) == before
        assert load(system, STORE) == {}

    def test_files_the_patch_overwrote_come_back_byte_for_byte(self) -> None:
        original = b"\x00the original segoe"
        system = machine(
            blobs={str(DEJAVU): FONT_BYTES, str(PATHS.fonts / "segoeui.ttf"): original}
        )
        writer = FakeWriter(system)
        before = install_files(system)
        apply(system, writer)

        revert_patch(system, writer, STORE, SEGOE_FONT_PATCH)

        assert system.read_bytes(PATHS.fonts / "segoeui.ttf") == original
        assert install_files(system) == before

    def test_reverting_something_never_applied_is_not_an_error(self) -> None:
        system = machine()
        outcome = revert_patch(system, FakeWriter(system), STORE, SEGOE_FONT_PATCH)
        assert outcome.ok and not outcome.changed
        assert outcome.detail == "not applied"


class TestDrift:
    def test_a_rebuilt_prefix_reads_as_drifted_not_as_applied(self) -> None:
        """The prefix is disposable, so this is the common case, not the rare one."""
        system = machine()
        writer = FakeWriter(system)
        apply(system, writer)

        writer.remove_tree(PATHS.prefix)

        assert status(system) is PatchStatus.DRIFTED

    def test_an_updater_overwriting_one_file_is_enough(self) -> None:
        system = machine()
        writer = FakeWriter(system)
        apply(system, writer)

        writer.write_bytes(PATHS.fonts / "seguisym.ttf", b"replaced by DCS_updater")

        assert status(system) is PatchStatus.DRIFTED
        assert "seguisym.ttf" in states(system, STORE)[0].detail

    def test_drift_is_repaired_by_applying_again(self) -> None:
        system = machine()
        writer = FakeWriter(system)
        apply(system, writer)
        writer.remove(PATHS.fonts / "seguisb.ttf")

        outcome = apply(system, writer)

        assert outcome.ok and outcome.changed
        assert "re-applied" in outcome.detail
        assert status(system) is PatchStatus.APPLIED

    def test_revert_after_a_partial_drift_still_leaves_nothing_behind(self) -> None:
        """Re-applying must not adopt our own files as the pristine copy.

        An update replaces one font and leaves the other two alone, so on
        re-apply those two still hold what we wrote. Backing them up then
        would make revert restore the patch instead of undoing it.
        """
        system = machine()
        writer = FakeWriter(system)
        before = install_files(system)
        apply(system, writer)
        writer.remove(PATHS.fonts / "seguisym.ttf")
        apply(system, writer)

        revert_patch(system, writer, STORE, SEGOE_FONT_PATCH)

        assert install_files(system) == before

    def test_revert_after_an_update_restores_the_updated_file(self) -> None:
        """What DCS put there is what the user must get back — not our copy."""
        system = machine()
        writer = FakeWriter(system)
        apply(system, writer)
        writer.write_bytes(PATHS.fonts / "seguisym.ttf", b"shipped by DCS 2.9.29")
        apply(system, writer)

        revert_patch(system, writer, STORE, SEGOE_FONT_PATCH)

        assert system.read_bytes(PATHS.fonts / "seguisym.ttf") == b"shipped by DCS 2.9.29"
        assert system.read_bytes(PATHS.fonts / "segoeui.ttf") is None

    def test_check_reports_drift_with_a_one_command_fix(self) -> None:
        system = machine()
        writer = FakeWriter(system)
        apply(system, writer)
        writer.remove_tree(PATHS.fonts)

        result = check_patches(
            healthy_environment(patches=probe_patches(system, LAYOUT, OWN_INSTALL))
        )

        assert result.name == PATCHES
        assert result.status is Status.FAIL
        assert result.remediation == "dcs-linux patch apply"

    def test_check_passes_once_the_patch_is_back(self) -> None:
        system = machine()
        apply(system, FakeWriter(system))

        result = check_patches(
            healthy_environment(patches=probe_patches(system, LAYOUT, OWN_INSTALL))
        )

        assert result.status is Status.PASS
        assert "segoe-fonts" in result.detail


class TestPartialFailure:
    """A write that dies halfway must still leave a revertible install."""

    def failing_writer(self, system: FakeSystem, on: str) -> FakeWriter:
        writer = FakeWriter(system)
        write_bytes = writer.write_bytes

        def guarded(path: Path, data: bytes) -> None:
            if path.name == on:
                raise OSError("no space left on device")
            write_bytes(path, data)

        writer.write_bytes = guarded  # type: ignore[method-assign]
        return writer

    def test_what_landed_before_the_failure_is_still_reverted(self) -> None:
        system = machine()
        before = install_files(system)
        writer = self.failing_writer(system, on="seguisb.ttf")

        outcome = apply(system, writer)
        assert not outcome.ok

        revert_patch(system, FakeWriter(system), STORE, SEGOE_FONT_PATCH)
        assert install_files(system) == before

    def test_a_failed_re_apply_does_not_forget_files_it_never_reached(self) -> None:
        """Those files are still patched from last time, and still ours to undo."""
        system = machine()
        before = install_files(system)
        apply(system, FakeWriter(system))
        # An update replaces the first font; re-applying then dies on the second.
        FakeWriter(system).write_bytes(PATHS.fonts / "segoeui.ttf", b"shipped by DCS")

        apply(system, self.failing_writer(system, on="seguisb.ttf"))
        revert_patch(system, FakeWriter(system), STORE, SEGOE_FONT_PATCH)

        assert system.read_bytes(PATHS.fonts / "segoeui.ttf") == b"shipped by DCS"
        assert system.read_bytes(PATHS.fonts / "seguisym.ttf") is None
        assert system.read_bytes(PATHS.fonts / "seguisb.ttf") is None
        assert install_files(system) == {**before, PATHS.fonts / "segoeui.ttf": b"shipped by DCS"}


class TestIcRisk:
    """ADR-0004: a hashed-file edit is never applied unless it was asked for."""

    def test_the_registry_ships_only_safe_patches_today(self) -> None:
        assert safe_patches() == REGISTRY

    def test_a_risky_patch_is_left_out_of_an_unqualified_apply(self) -> None:
        risky = Patch(id="risky", summary="edits a hashed file", ic_risk=True, plan=plan_nothing)
        assert safe_patches((SEGOE_FONT_PATCH, risky)) == (SEGOE_FONT_PATCH,)


class TestUnreadableState:
    def test_a_corrupt_state_file_reads_as_nothing_applied(self) -> None:
        """Being read mid-write must not crash the diagnostics that explain it."""
        system = machine(files={str(STORE.state_file): "{not json"})
        assert load(system, STORE) == {}
        assert status(system) is PatchStatus.NOT_APPLIED


class TestFontSearch:
    def test_a_nested_distro_font_directory_is_found(self) -> None:
        assert find_substitute_font(machine()) == DEJAVU

    def test_the_preferred_name_wins_over_fontconfig(self) -> None:
        system = machine(
            executables={"fc-match": "/usr/bin/fc-match"},
            commands={
                "fc-match --format=%{file} sans": CommandResult(0, "/usr/share/fonts/other.ttf")
            },
        )
        assert find_substitute_font(system) == DEJAVU

    def test_fontconfig_is_the_fallback(self) -> None:
        system = FakeSystem(
            executables={"fc-match": "/usr/bin/fc-match"},
            commands={
                "fc-match --format=%{file} sans": CommandResult(0, "/usr/share/fonts/other.ttf\n")
            },
        )
        assert find_substitute_font(system) == Path("/usr/share/fonts/other.ttf")

    def test_a_machine_with_no_fonts_at_all_finds_nothing(self) -> None:
        assert find_substitute_font(FakeSystem()) is None
