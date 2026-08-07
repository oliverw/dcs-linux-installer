"""The rules, exercised against fixture environments only."""

from dataclasses import replace

import pytest

from dcs_linux.checks import (
    GIB,
    HEAD_TRACKING_CHECKS,
    REQUIRED_FREE_BYTES,
    CheckResult,
    Status,
    check_d3dcompiler,
    check_dcs_head_tracking,
    check_disk_space,
    check_distro,
    check_external_tools,
    check_game_location,
    check_gpu,
    check_head_tracker,
    check_opentrack,
    check_proton_builds,
    check_reflink_filesystem,
    check_saved_games_mapping,
    check_segoe_fonts,
    check_umu,
    check_upscaling,
    has_blocking_failure,
    run_checks,
)
from dcs_linux.distro import Family
from dcs_linux.headtracking import (
    OPENTRACK_INSTALL,
    RULE_FILE,
    HeadTracking,
    install_rule_command,
)
from dcs_linux.probes import Gpu, InstallState, Umu
from dcs_linux.system import DiskUsage
from tests.environments import (
    BAZZITE,
    FEDORA,
    LAYOUT,
    OWN_INSTALL,
    STEAMOS,
    bare_environment,
    healthy_environment,
    with_tracker,
)


class TestDistroRow:
    def test_reports_name_and_that_the_filesystem_is_mutable(self) -> None:
        result = check_distro(healthy_environment())
        assert result.status is Status.PASS
        assert "Fedora Linux 44" in result.detail
        assert "mutable base system" in result.detail

    def test_reports_immutability_as_a_fact_not_a_fault(self) -> None:
        result = check_distro(healthy_environment(distro=BAZZITE))
        assert result.status is Status.PASS
        assert "immutable base system" in result.detail

    def test_unreadable_os_release_warns(self) -> None:
        unknown = replace(FEDORA, id="unknown", name="unknown", family=Family.UNKNOWN)
        assert check_distro(healthy_environment(distro=unknown)).status is Status.WARN


class TestGpuRow:
    def test_reports_vendor_and_driver_version(self) -> None:
        result = check_gpu(healthy_environment())
        assert result.status is Status.PASS
        assert "NVIDIA" in result.detail
        assert "610.43.03" in result.detail

    def test_no_gpu_blocks(self) -> None:
        assert check_gpu(healthy_environment(gpus=())).status is Status.FAIL

    def test_an_old_driver_is_reported_not_judged(self) -> None:
        """One verified driver version is not a threshold; report the number."""
        old = (Gpu(vendor="NVIDIA", kernel_driver="nvidia", driver_version="470.03"),)
        result = check_gpu(healthy_environment(gpus=old))
        assert result.status is Status.PASS
        assert "470.03" in result.detail

    def test_the_discrete_gpu_leads_and_the_igpu_is_still_named(self) -> None:
        hybrid = (
            Gpu(vendor="NVIDIA", kernel_driver="nvidia", driver_version="610.43.03"),
            Gpu(vendor="Intel", kernel_driver="i915", driver_version=None),
        )
        result = check_gpu(healthy_environment(gpus=hybrid))
        assert result.detail.startswith("NVIDIA")
        assert "also Intel" in result.detail

    def test_unknown_mesa_version_suggests_glxinfo_for_the_distro(self) -> None:
        amd = (Gpu(vendor="AMD", kernel_driver="amdgpu", driver_version=None),)
        result = check_gpu(healthy_environment(gpus=amd))
        assert result.status is Status.WARN
        assert result.remediation == "sudo dnf install glx-utils"

    def test_current_mesa_passes(self) -> None:
        amd = (Gpu(vendor="AMD", kernel_driver="amdgpu", driver_version="25.1.4"),)
        assert check_gpu(healthy_environment(gpus=amd)).status is Status.PASS


class TestToolchainRows:
    def test_missing_umu_blocks_and_never_suggests_pip(self) -> None:
        result = check_umu(bare_environment())
        assert result.status is Status.FAIL
        assert result.remediation is not None
        for impossible in ("pip install", "uv tool install", "pypi"):
            assert impossible not in result.remediation.lower()

    def test_broken_umu_blocks(self) -> None:
        broken = Umu(path=LAYOUT.umu_run, usable=False, version=None)
        assert check_umu(healthy_environment(umu=broken)).status is Status.FAIL

    def test_working_umu_reports_its_version(self) -> None:
        result = check_umu(healthy_environment())
        assert result.status is Status.PASS
        assert "1.4.4" in result.detail

    def test_missing_ge_proton_blocks(self) -> None:
        assert check_proton_builds(bare_environment()).status is Status.FAIL

    def test_available_proton_builds_are_listed(self) -> None:
        environment = healthy_environment(proton_builds=("GE-Proton10-9", "GE-Proton11-3"))
        result = check_proton_builds(environment)
        assert result.status is Status.PASS
        assert result.detail == "GE-Proton10-9, GE-Proton11-3"


class TestExternalToolsRow:
    def test_missing_tools_block_with_a_distro_specific_command(self) -> None:
        result = check_external_tools(healthy_environment(missing_tools=("bwrap", "curl")))
        assert result.status is Status.FAIL
        assert result.remediation == "sudo dnf install bubblewrap curl"

    def test_immutable_distro_gets_a_layering_command(self) -> None:
        environment = healthy_environment(distro=BAZZITE, missing_tools=("bwrap",))
        result = check_external_tools(environment)
        assert result.remediation is not None
        assert result.remediation.startswith("rpm-ostree install bubblewrap")

    def test_read_only_distro_is_never_told_to_run_a_package_manager(self) -> None:
        environment = healthy_environment(distro=STEAMOS, missing_tools=("bwrap",))
        result = check_external_tools(environment)
        assert result.remediation is not None
        assert "distrobox" in result.remediation
        assert "pacman" not in result.remediation

    def test_nothing_missing_passes(self) -> None:
        assert check_external_tools(healthy_environment()).status is Status.PASS


class TestDiskSpaceRow:
    def test_ample_space_passes(self) -> None:
        assert check_disk_space(healthy_environment()).status is Status.PASS

    def test_too_little_space_blocks(self) -> None:
        environment = healthy_environment(disk=DiskUsage(total=200 * GIB, free=40 * GIB))
        result = check_disk_space(environment)
        assert result.status is Status.FAIL
        assert "40 of 200 GiB free" in result.detail

    def test_marginal_headroom_warns(self) -> None:
        environment = healthy_environment(disk=DiskUsage(total=400 * GIB, free=150 * GIB))
        result = check_disk_space(environment)
        assert result.status is Status.WARN
        assert result.remediation is not None

    def test_exactly_the_requirement_is_not_a_failure(self) -> None:
        environment = healthy_environment(
            disk=DiskUsage(total=2000 * GIB, free=REQUIRED_FREE_BYTES)
        )
        assert check_disk_space(environment).status is not Status.FAIL

    def test_unreadable_free_space_warns(self) -> None:
        assert check_disk_space(healthy_environment(disk=None)).status is Status.WARN


class TestFilesystemRow:
    @pytest.mark.parametrize("filesystem", ["btrfs", "xfs"])
    def test_reflink_capable_filesystems_pass(self, filesystem: str) -> None:
        assert check_reflink_filesystem(healthy_environment(filesystem=filesystem)).status is (
            Status.PASS
        )

    def test_other_filesystems_warn_without_blocking(self) -> None:
        result = check_reflink_filesystem(healthy_environment(filesystem="ext4"))
        assert result.status is Status.WARN
        assert "reflink" in result.detail

    def test_unknown_filesystem_warns(self) -> None:
        assert check_reflink_filesystem(healthy_environment(filesystem=None)).status is Status.WARN


class TestUpscalingRow:
    def test_dlss_blocks_and_says_where_to_turn_it_off(self) -> None:
        environment = healthy_environment(install_state=replace(InstallState(), upscaling="DLSS"))
        result = check_upscaling(environment)
        assert result.status is Status.FAIL
        assert result.remediation is not None
        assert "Upscaling" in result.remediation

    def test_off_passes(self) -> None:
        assert check_upscaling(healthy_environment()).status is Status.PASS

    def test_skipped_before_options_lua_exists(self) -> None:
        assert check_upscaling(bare_environment()).status is Status.SKIP


class TestPrefixRows:
    def test_missing_segoe_fonts_block(self) -> None:
        install = replace(
            InstallState(prefix_exists=True), missing_segoe_fonts=("segoeui.ttf", "seguisb.ttf")
        )
        result = check_segoe_fonts(healthy_environment(install_state=install))
        assert result.status is Status.FAIL
        assert "AH-64D" in result.detail

    def test_segoe_check_is_skipped_with_no_prefix(self) -> None:
        assert check_segoe_fonts(bare_environment()).status is Status.SKIP

    def test_missing_d3dcompiler_blocks(self) -> None:
        environment = healthy_environment(install_state=InstallState(prefix_exists=True))
        result = check_d3dcompiler(environment)
        assert result.status is Status.FAIL
        assert result.remediation == "umu-run winetricks d3dcompiler_47"

    def test_d3dcompiler_check_is_skipped_with_no_prefix(self) -> None:
        assert check_d3dcompiler(bare_environment()).status is Status.SKIP


class TestLifetimeRows:
    def test_unmapped_saved_games_blocks(self) -> None:
        environment = healthy_environment(install_state=InstallState(prefix_exists=True))
        result = check_saved_games_mapping(environment)
        assert result.status is Status.FAIL
        assert result.remediation is not None
        assert str(LAYOUT.saved_games) in result.remediation

    def test_mapped_saved_games_passes(self) -> None:
        assert check_saved_games_mapping(healthy_environment()).status is Status.PASS

    def test_game_under_drive_c_blocks(self) -> None:
        inside = replace(OWN_INSTALL, game=LAYOUT.prefix / "drive_c" / "DCS World")
        result = check_game_location(healthy_environment(targeted=inside, installs=(inside,)))
        assert result.status is Status.FAIL
        assert str(inside.game) in result.detail

    def test_game_outside_the_prefix_passes(self) -> None:
        assert check_game_location(healthy_environment()).status is Status.PASS

    def test_skipped_with_no_install(self) -> None:
        result = check_game_location(bare_environment())
        assert result.status is Status.SKIP
        assert result.detail == "no DCS install found"

    def test_several_installs_and_none_chosen_says_so(self) -> None:
        """Picking between somebody's installs is their decision, not ours."""
        installs = (OWN_INSTALL, replace(OWN_INSTALL, game=LAYOUT.game / "DCS World 2"))
        result = check_game_location(bare_environment(installs=installs))
        assert result.status is Status.SKIP
        assert "--install" in result.detail


class TestWholeReport:
    def test_healthy_machine_has_no_blocking_failures(self) -> None:
        results = run_checks(healthy_environment())
        assert not has_blocking_failure(results)

    def test_bare_machine_reports_toolchain_gaps_and_skips_install_checks(self) -> None:
        results = {result.name: result for result in run_checks(bare_environment())}
        assert results["umu-launcher"].status is Status.FAIL
        assert results["Proton builds"].status is Status.FAIL
        assert results["Segoe fonts"].status is Status.SKIP
        assert results["Upscaling"].status is Status.SKIP
        assert results["Game location"].status is Status.SKIP

    def test_every_failure_carries_remediation(self) -> None:
        environments = [
            bare_environment(),
            bare_environment(distro=BAZZITE, missing_tools=("bwrap",), gpus=()),
            bare_environment(distro=STEAMOS, missing_tools=("curl", "tar")),
            healthy_environment(
                install_state=InstallState(prefix_exists=True, upscaling="DLSS"),
                disk=DiskUsage(total=100 * GIB, free=10 * GIB),
                filesystem="ext4",
            ),
        ]
        for environment in environments:
            for result in run_checks(environment):
                if result.is_blocking:
                    assert result.remediation, f"{result.name} fails with no way to fix it"

    def test_immutable_remediation_never_suggests_impossible_commands(self) -> None:
        environment = bare_environment(distro=STEAMOS, missing_tools=("curl", "bwrap"), gpus=())
        advice = " ".join(result.remediation or "" for result in run_checks(environment))
        for impossible in ("sudo pacman", "sudo dnf", "sudo apt", "steamos-readonly"):
            assert impossible not in advice

    def test_every_row_has_a_stable_json_key(self) -> None:
        keys = [result.key for result in run_checks(healthy_environment())]
        assert len(keys) == len(set(keys))
        assert "saved_games_mapping" in keys
        assert "d3dcompiler_47" in keys


def test_only_a_fail_blocks() -> None:
    for status in (Status.PASS, Status.WARN, Status.SKIP):
        assert not CheckResult(name="x", status=status, detail="").is_blocking
    assert CheckResult(name="x", status=Status.FAIL, detail="").is_blocking


class TestHeadTrackerRow:
    def test_no_device_is_reported_calmly_and_skipped(self) -> None:
        result = check_head_tracker(healthy_environment())
        assert result.status is Status.SKIP
        assert "no" in result.detail.lower()
        assert result.remediation is None

    def test_an_accessible_tracker_passes_and_is_named(self) -> None:
        result = check_head_tracker(healthy_environment(head_tracking=with_tracker()))
        assert result.status is Status.PASS
        assert "TrackIR 5" in result.detail
        assert "/dev/bus/usb/001/007" in result.detail

    def test_an_unreadable_tracker_warns_with_the_rule_to_install(self) -> None:
        """The mundane reason head tracking fails on Linux."""
        result = check_head_tracker(healthy_environment(head_tracking=with_tracker(access=False)))
        assert result.status is Status.WARN
        assert result.remediation is not None
        assert result.remediation.startswith(install_rule_command())
        assert "replug" in result.remediation

    def test_whether_the_udev_rule_is_installed_is_always_stated(self) -> None:
        without = check_head_tracker(healthy_environment(head_tracking=with_tracker()))
        with_rule = check_head_tracker(
            healthy_environment(head_tracking=with_tracker(rule=RULE_FILE))
        )
        assert "no udev rule" in without.detail
        assert str(RULE_FILE) in with_rule.detail

    def test_a_rule_that_is_installed_and_still_not_working_says_what_to_do_next(self) -> None:
        """Writing the rule a second time would not help; reconnecting might."""
        tracking = with_tracker(access=False, rule=RULE_FILE)
        result = check_head_tracker(healthy_environment(head_tracking=tracking))
        assert result.status is Status.WARN
        assert result.remediation is not None
        assert "udevadm trigger" in result.remediation
        assert "sudo tee" not in result.remediation

    def test_the_tool_only_ever_prints_the_privileged_command(self) -> None:
        assert install_rule_command().count("sudo") >= 1


class TestOpentrackRow:
    def test_installed_passes_and_says_where(self) -> None:
        tracking = replace(with_tracker(), opentrack="/usr/bin/opentrack")
        result = check_opentrack(healthy_environment(head_tracking=tracking))
        assert result.status is Status.PASS
        assert "/usr/bin/opentrack" in result.detail

    def test_absent_with_no_tracker_is_not_worth_nagging_about(self) -> None:
        assert check_opentrack(healthy_environment()).status is Status.SKIP

    def test_a_tracker_with_no_opentrack_warns_and_says_how_to_install_it(self) -> None:
        result = check_opentrack(healthy_environment(head_tracking=with_tracker()))
        assert result.status is Status.WARN
        assert result.remediation is not None
        assert OPENTRACK_INSTALL in result.remediation

    def test_flatpak_itself_is_installed_first_when_it_is_missing(self) -> None:
        tracking = replace(with_tracker(), flatpak=False)
        result = check_opentrack(healthy_environment(head_tracking=tracking))
        assert result.remediation is not None
        assert result.remediation.startswith("sudo dnf install flatpak")

    def test_an_immutable_base_is_never_told_to_run_a_package_manager(self) -> None:
        tracking = replace(with_tracker(), flatpak=False)
        result = check_opentrack(healthy_environment(distro=STEAMOS, head_tracking=tracking))
        assert result.remediation is not None
        assert "pacman" not in result.remediation


class TestDcsHeadTrackingRow:
    def test_skipped_when_nothing_suggests_head_tracking_is_wanted(self) -> None:
        assert check_dcs_head_tracking(healthy_environment()).status is Status.SKIP

    def test_skipped_before_there_is_a_prefix_to_read(self) -> None:
        environment = bare_environment(head_tracking=with_tracker())
        result = check_dcs_head_tracking(environment)
        assert result.status is Status.SKIP
        assert "no prefix yet" in result.detail

    def test_a_registered_wine_bridge_passes(self) -> None:
        tracking = replace(with_tracker(), wine_bridge=True)
        assert check_dcs_head_tracking(healthy_environment(head_tracking=tracking)).status is (
            Status.PASS
        )

    def test_a_tracker_dcs_cannot_see_warns_and_names_the_prefix(self) -> None:
        result = check_dcs_head_tracking(healthy_environment(head_tracking=with_tracker()))
        assert result.status is Status.WARN
        assert result.remediation is not None
        assert str(LAYOUT.prefix) in result.remediation

    def test_opentrack_alone_is_enough_to_engage_the_row(self) -> None:
        """opentrack tracks a face through a webcam, with no TrackIR anywhere."""
        tracking = HeadTracking(opentrack="/usr/bin/opentrack", flatpak=True)
        assert check_dcs_head_tracking(healthy_environment(head_tracking=tracking)).status is (
            Status.WARN
        )


def test_head_tracking_never_blocks_a_check() -> None:
    """A missing or unreadable tracker must never make `check` exit non-zero.

    Head tracking is a nice-to-have; DCS runs without it. A blocking failure
    here would stop `install` and tell a user with no tracker that their
    machine is not ready.
    """
    broken = with_tracker(access=False)
    for environment in (
        healthy_environment(head_tracking=broken),
        bare_environment(head_tracking=broken),
        healthy_environment(head_tracking=HeadTracking()),
    ):
        rows = [check(environment) for check in HEAD_TRACKING_CHECKS]
        assert not has_blocking_failure(rows)
