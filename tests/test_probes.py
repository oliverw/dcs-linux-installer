from pathlib import Path

from dcs_linux.paths import Layout, resolve_layout
from dcs_linux.probes import (
    InstallState,
    find_prefix_saved_games,
    has_dll_override,
    probe,
    probe_gpus,
    probe_install,
    probe_kernel,
    probe_missing_tools,
    probe_proton_builds,
    probe_umu,
    read_graphics_block,
    read_upscaling,
    target_paths,
)
from dcs_linux.system import CommandResult, DiskUsage, filesystem_type_from_mounts
from tests.fakes import FakeSystem

LAYOUT = Layout(root=Path("/data/dcs"), toolchain=Path("/data/toolchain"))


# What `winetricks d3dcompiler_47` actually writes into the prefix.
DLL_OVERRIDE = '[Software\\\\Wine\\\\DllOverrides] 1700000000\n"d3dcompiler_47"="native,builtin"\n'


def state_of(system: FakeSystem) -> InstallState:
    """The install state of our own layout, which is where these fixtures live."""
    return probe_install(system, target_paths(system, LAYOUT, None))


def gpu_files(vendor_id: str, driver: str) -> dict[str, str]:
    return {
        "/sys/class/drm/card1/device/vendor": f"{vendor_id}\n",
        "/sys/class/drm/card1/device/uevent": f"DRIVER={driver}\nPCI_SLOT_NAME=0000:01:00.0\n",
    }


class TestLayout:
    def test_defaults_live_under_the_home_directory(self) -> None:
        layout = resolve_layout(FakeSystem(home="/home/pilot"))
        assert layout.root == Path("/home/pilot/dcs-linux")
        assert layout.toolchain == Path("/home/pilot/.cache/dcs-linux/toolchain")

    def test_environment_overrides_win(self) -> None:
        system = FakeSystem(
            env={"DCS_LINUX_ROOT": "/mnt/big/dcs", "DCS_LINUX_TOOLCHAIN": "/mnt/big/tools"}
        )
        layout = resolve_layout(system)
        assert layout.root == Path("/mnt/big/dcs")
        assert layout.toolchain == Path("/mnt/big/tools")

    def test_the_three_lifetimes_are_siblings_not_nested(self) -> None:
        layout = resolve_layout(FakeSystem())
        assert layout.prefix not in layout.game.parents
        assert layout.prefix not in layout.saved_games.parents


class TestGpu:
    def test_nvidia_driver_version_comes_from_the_kernel_module(self) -> None:
        system = FakeSystem(
            files={
                **gpu_files("0x10de", "nvidia"),
                "/sys/module/nvidia/version": "610.43.03\n",
            }
        )
        (gpu,) = probe_gpus(system)
        assert gpu.vendor == "NVIDIA"
        assert gpu.kernel_driver == "nvidia"
        assert gpu.driver_version == "610.43.03"

    def test_nvidia_driver_version_falls_back_to_proc(self) -> None:
        system = FakeSystem(
            files={
                **gpu_files("0x10de", "nvidia"),
                "/proc/driver/nvidia/version": (
                    "NVRM version: NVIDIA UNIX x86_64 Kernel Module  580.82.09  Fri\n"
                ),
            }
        )
        (gpu,) = probe_gpus(system)
        assert gpu.driver_version == "580.82.09"

    def test_amd_reports_mesa_version_via_glxinfo(self) -> None:
        system = FakeSystem(
            files=gpu_files("0x1002", "amdgpu"),
            executables={"glxinfo": "/usr/bin/glxinfo"},
            commands={
                "glxinfo -B": CommandResult(
                    returncode=0,
                    stdout="OpenGL version string: 4.6 (Compatibility Profile) Mesa 25.1.4\n",
                )
            },
        )
        (gpu,) = probe_gpus(system)
        assert gpu.vendor == "AMD"
        assert gpu.kernel_driver == "amdgpu"
        assert gpu.driver_version == "25.1.4"

    def test_mesa_version_is_unknown_without_glxinfo(self) -> None:
        (gpu,) = probe_gpus(FakeSystem(files=gpu_files("0x8086", "i915")))
        assert gpu.vendor == "Intel"
        assert gpu.driver_version is None

    def test_connectors_are_not_mistaken_for_devices(self) -> None:
        files = gpu_files("0x10de", "nvidia")
        files["/sys/class/drm/card1-HDMI-A-1/status"] = "connected\n"
        assert len(probe_gpus(FakeSystem(files=files))) == 1

    def test_no_gpu_at_all(self) -> None:
        assert probe_gpus(FakeSystem()) == ()


class TestUmu:
    def test_zipapp_in_the_toolchain_is_preferred(self) -> None:
        system = FakeSystem(
            files={"/data/toolchain/umu/umu-run": ""},
            commands={"/data/toolchain/umu/umu-run --version": CommandResult(0, "1.4.4\n")},
        )
        umu = probe_umu(system, LAYOUT)
        assert umu.path == Path("/data/toolchain/umu/umu-run")
        assert umu.usable
        assert umu.version == "1.4.4"

    def test_falls_back_to_umu_run_on_path(self) -> None:
        system = FakeSystem(
            executables={"umu-run": "/usr/bin/umu-run"},
            commands={"/usr/bin/umu-run --version": CommandResult(0, "1.4.4\n")},
        )
        assert probe_umu(system, LAYOUT).path == Path("/usr/bin/umu-run")

    def test_present_but_unrunnable_is_not_usable(self) -> None:
        """Not executable, or the wrong architecture: the command never runs."""
        system = FakeSystem(files={"/data/toolchain/umu/umu-run": ""})
        umu = probe_umu(system, LAYOUT)
        assert umu.path is not None
        assert not umu.usable

    def test_a_build_that_does_not_answer_version_is_still_usable(self) -> None:
        system = FakeSystem(
            files={"/data/toolchain/umu/umu-run": ""},
            commands={"/data/toolchain/umu/umu-run --version": CommandResult(2, "")},
        )
        umu = probe_umu(system, LAYOUT)
        assert umu.usable
        assert umu.version is None

    def test_absent(self) -> None:
        umu = probe_umu(FakeSystem(), LAYOUT)
        assert umu.path is None
        assert not umu.usable


class TestGeProton:
    def test_lists_unpacked_builds_only(self) -> None:
        system = FakeSystem(
            files={
                "/data/toolchain/ge-proton/GE-Proton11-3/proton": "",
                "/data/toolchain/ge-proton/GE-Proton10-9/proton": "",
                "/data/toolchain/ge-proton/half-extracted/README": "",
            }
        )
        assert probe_proton_builds(system, LAYOUT) == ("GE-Proton10-9", "GE-Proton11-3")

    def test_steams_compatibilitytools_are_searched_too(self) -> None:
        """Bazzite and SteamOS ship Steam with builds already in place."""
        system = FakeSystem(
            files={
                "/home/pilot/.steam/root/compatibilitytools.d/GE-Proton11-3/proton": "",
                "/usr/share/steam/compatibilitytools.d/GE-Proton10-9/proton": "",
            }
        )
        assert probe_proton_builds(system, LAYOUT) == ("GE-Proton10-9", "GE-Proton11-3")

    def test_the_same_build_in_two_places_is_listed_once(self) -> None:
        system = FakeSystem(
            files={
                "/data/toolchain/ge-proton/GE-Proton11-3/proton": "",
                "/home/pilot/.steam/root/compatibilitytools.d/GE-Proton11-3/proton": "",
            }
        )
        assert probe_proton_builds(system, LAYOUT) == ("GE-Proton11-3",)

    def test_none_unpacked(self) -> None:
        assert probe_proton_builds(FakeSystem(), LAYOUT) == ()


class TestExternalTools:
    def test_reports_only_what_is_missing(self) -> None:
        system = FakeSystem(executables={"curl": "/usr/bin/curl", "tar": "/usr/bin/tar"})
        assert probe_missing_tools(system) == ("bwrap",)

    def test_nothing_missing(self) -> None:
        system = FakeSystem(
            executables={"curl": "/usr/bin/curl", "tar": "/usr/bin/tar", "bwrap": "/usr/bin/bwrap"}
        )
        assert probe_missing_tools(system) == ()


class TestInstallState:
    def test_bare_machine_has_nothing(self) -> None:
        state = state_of(FakeSystem())
        assert not state.prefix_exists
        assert state.upscaling is None

    def test_healthy_install_is_recognised(self) -> None:
        system = FakeSystem(
            files={
                "/data/dcs/prefix/system.reg": "",
                "/data/dcs/prefix/drive_c/windows/Fonts/segoeui.ttf": "",
                "/data/dcs/prefix/drive_c/windows/Fonts/seguisb.ttf": "",
                "/data/dcs/prefix/drive_c/windows/Fonts/seguisym.ttf": "",
                "/data/dcs/prefix/user.reg": DLL_OVERRIDE,
                "/data/dcs/game/DCS World/bin/DCS.exe": "",
                "/data/dcs/saved-games/DCS/Config/options.lua": '["Upscaling"] = "OFF",',
            },
            symlinks={"/data/dcs/prefix/drive_c/users/steamuser/Saved Games"},
        )
        state = state_of(system)
        assert state.prefix_exists
        assert state.missing_segoe_fonts == ()
        assert state.d3dcompiler_installed
        assert state.saved_games_mapped
        assert state.upscaling == "OFF"

    def test_corefonts_alone_leaves_every_segoe_font_missing(self) -> None:
        system = FakeSystem(
            files={
                "/data/dcs/prefix/system.reg": "",
                "/data/dcs/prefix/drive_c/windows/Fonts/arial.ttf": "",
                "/data/dcs/prefix/drive_c/windows/Fonts/times.ttf": "",
            }
        )
        state = state_of(system)
        assert state.missing_segoe_fonts == ("segoeui.ttf", "seguisb.ttf", "seguisym.ttf")

    def test_font_names_are_matched_case_insensitively(self) -> None:
        system = FakeSystem(
            files={
                "/data/dcs/prefix/system.reg": "",
                "/data/dcs/prefix/drive_c/windows/Fonts/SegoeUI.ttf": "",
                "/data/dcs/prefix/drive_c/windows/Fonts/seguisb.ttf": "",
                "/data/dcs/prefix/drive_c/windows/Fonts/SEGUISYM.TTF": "",
            }
        )
        assert state_of(system).missing_segoe_fonts == ()

    def test_saved_games_left_as_a_real_directory_is_not_mapped(self) -> None:
        system = FakeSystem(
            files={
                "/data/dcs/prefix/system.reg": "",
                "/data/dcs/prefix/drive_c/users/steamuser/Saved Games/DCS/Config/options.lua": "",
            }
        )
        assert not state_of(system).saved_games_mapped


class TestPrefixSavedGames:
    def test_a_prefix_that_names_the_user_directory_after_the_user(self) -> None:
        """Lutris and Heroic prefixes have no `steamuser` — umu creates that."""
        system = FakeSystem(
            files={"/games/prefix/drive_c/users/Public/Documents/keep": ""},
            symlinks={"/games/prefix/drive_c/users/pilot/Saved Games"},
        )
        found = find_prefix_saved_games(system, Path("/games/prefix"))
        assert found == Path("/games/prefix/drive_c/users/pilot/Saved Games")

    def test_steamuser_wins_when_both_exist(self) -> None:
        system = FakeSystem(
            directories={
                "/games/prefix/drive_c/users/pilot/Saved Games",
                "/games/prefix/drive_c/users/steamuser/Saved Games",
            }
        )
        found = find_prefix_saved_games(system, Path("/games/prefix"))
        assert found.parent.name == "steamuser"

    def test_falls_back_to_steamuser_before_the_prefix_exists(self) -> None:
        found = find_prefix_saved_games(FakeSystem(), Path("/games/prefix"))
        assert found == Path("/games/prefix/drive_c/users/steamuser/Saved Games")


class TestD3dcompiler:
    def test_the_dll_being_present_is_not_enough(self) -> None:
        """Wine's default prefix already ships the file as a builtin stub."""
        system = FakeSystem(
            files={
                "/data/dcs/prefix/system.reg": "",
                "/data/dcs/prefix/drive_c/windows/system32/d3dcompiler_47.dll": "",
                "/data/dcs/prefix/user.reg": "[Software\\\\Wine\\\\DllOverrides] 1700000000\n",
            }
        )
        assert not state_of(system).d3dcompiler_installed

    def test_the_native_override_is_what_counts(self) -> None:
        system = FakeSystem(
            files={"/data/dcs/prefix/system.reg": "", "/data/dcs/prefix/user.reg": DLL_OVERRIDE}
        )
        assert state_of(system).d3dcompiler_installed

    def test_a_builtin_override_does_not_count(self) -> None:
        reg = '[Software\\\\Wine\\\\DllOverrides] 1700000000\n"d3dcompiler_47"="builtin"\n'
        system = FakeSystem(
            files={"/data/dcs/prefix/system.reg": "", "/data/dcs/prefix/user.reg": reg}
        )
        assert not state_of(system).d3dcompiler_installed

    def test_no_user_reg_at_all(self) -> None:
        assert not has_dll_override(None, "d3dcompiler_47")


class TestUpscaling:
    def test_reads_the_dlss_setting(self) -> None:
        options = 'options = {["graphics"] = {["Upscaling"] = "DLSS", ["AA"] = "DLAA"}}'
        assert read_upscaling(options) == "DLSS"

    def test_tolerates_whitespace(self) -> None:
        assert read_upscaling('[ "Upscaling" ]   =   "OFF"') == "OFF"

    def test_absent_setting(self) -> None:
        assert read_upscaling('["AA"] = "DLAA"') is None

    def test_no_file(self) -> None:
        assert read_upscaling(None) is None

    def test_found_inside_the_prefix_when_saved_games_was_never_mapped_out(self) -> None:
        """The unmapped install is exactly the one the DLSS check exists for."""
        inside = "/data/dcs/prefix/drive_c/users/steamuser/Saved Games/DCS/Config/options.lua"
        system = FakeSystem(
            files={"/data/dcs/prefix/system.reg": "", inside: '["Upscaling"] = "DLSS"'}
        )
        assert state_of(system).upscaling == "DLSS"

    def test_the_prefixs_own_copy_wins_when_both_exist(self) -> None:
        """The setting that matters is the one the running prefix reads.

        Mapped out, the two are the same file. Unmapped — or targeting an
        install that is not ours — they are not, and our own saved games say
        nothing about that prefix.
        """
        inside = "/data/dcs/prefix/drive_c/users/steamuser/Saved Games/DCS/Config/options.lua"
        system = FakeSystem(
            files={
                "/data/dcs/prefix/system.reg": "",
                "/data/dcs/saved-games/DCS/Config/options.lua": '["Upscaling"] = "OFF"',
                inside: '["Upscaling"] = "DLSS"',
            }
        )
        assert state_of(system).upscaling == "DLSS"


class TestFilesystemType:
    MOUNTS = (
        "/dev/sda2 / ext4 rw,relatime 0 0\n"
        "/dev/sdb1 /run/media/pilot/Data btrfs rw,relatime 0 0\n"
        "tmpfs /tmp tmpfs rw 0 0\n"
    )

    def test_longest_matching_mount_point_wins(self) -> None:
        found = filesystem_type_from_mounts(self.MOUNTS, Path("/run/media/pilot/Data/dcs/game"))
        assert found == "btrfs"

    def test_falls_back_to_the_root_mount(self) -> None:
        assert filesystem_type_from_mounts(self.MOUNTS, Path("/home/pilot/dcs")) == "ext4"

    def test_mount_point_escapes_are_decoded(self) -> None:
        mounts = "/dev/sdb1 /mnt/My\\040Disk xfs rw 0 0\n"
        assert filesystem_type_from_mounts(mounts, Path("/mnt/My Disk/game")) == "xfs"

    def test_unknown_when_nothing_matches(self) -> None:
        assert filesystem_type_from_mounts("", Path("/data")) is None


def test_probe_assembles_the_whole_environment() -> None:
    system = FakeSystem(
        files={
            "/etc/os-release": 'ID=fedora\nPRETTY_NAME="Fedora Linux 44"\nVERSION_ID=44\n',
            **gpu_files("0x10de", "nvidia"),
            "/sys/module/nvidia/version": "610.43.03\n",
        },
        env={"DCS_LINUX_ROOT": "/data/dcs", "DCS_LINUX_TOOLCHAIN": "/data/toolchain"},
        executables={"curl": "/usr/bin/curl"},
        disk=DiskUsage(total=2000, free=1000),
        filesystem="btrfs",
    )
    environment = probe(system)
    assert environment.distro.id == "fedora"
    assert environment.gpus[0].vendor == "NVIDIA"
    assert environment.missing_tools == ("tar", "bwrap")
    assert environment.filesystem == "btrfs"
    assert environment.layout.game == Path("/data/dcs/game")
    assert not environment.install_state.prefix_exists


class TestKernel:
    def test_the_kernel_release_is_read(self) -> None:
        system = FakeSystem(files={"/proc/sys/kernel/osrelease": "6.17.4-202.fc44.x86_64\n"})
        assert probe_kernel(system) == "6.17.4-202.fc44.x86_64"

    def test_no_proc_is_not_a_crash(self) -> None:
        assert probe_kernel(FakeSystem()) is None


GRAPHICS_OPTIONS = """options = {
    ["graphics"] = {
        ["messagesFontScale"] = 1,
        ["Upscaling"] = "DLSS",
        ["shadows"] = 4,
        ["multiMonitorSetup"] = "1camera",
    },
    ["plugins"] = {
        ["secret"] = "keep out",
    },
}
"""


class TestGraphicsBlock:
    def test_the_graphics_table_is_extracted_whole(self) -> None:
        block = read_graphics_block(GRAPHICS_OPTIONS)
        assert block is not None
        assert '["Upscaling"] = "DLSS"' in block
        assert '["shadows"] = 4' in block

    def test_nothing_outside_graphics_comes_with_it(self) -> None:
        """Only the graphics table is cheap and non-sensitive; the rest is not."""
        block = read_graphics_block(GRAPHICS_OPTIONS)
        assert block is not None
        assert "keep out" not in block

    def test_a_nested_table_does_not_end_the_block_early(self) -> None:
        options = 'a = {["graphics"] = {["x"] = {["y"] = 1}, ["z"] = 2}, ["w"] = 3}'
        block = read_graphics_block(options)
        assert block is not None
        assert '["z"] = 2' in block
        assert '["w"] = 3' not in block

    def test_an_unterminated_table_is_not_guessed_at(self) -> None:
        assert read_graphics_block('["graphics"] = {["x"] = 1') is None

    def test_no_graphics_table(self) -> None:
        assert read_graphics_block('["plugins"] = {}') is None
        assert read_graphics_block(None) is None

    def test_the_probe_picks_it_up(self) -> None:
        inside = "/data/dcs/prefix/drive_c/users/steamuser/Saved Games/DCS/Config/options.lua"
        system = FakeSystem(files={"/data/dcs/prefix/system.reg": "", inside: GRAPHICS_OPTIONS})
        graphics = state_of(system).graphics_options
        assert graphics is not None and "Upscaling" in graphics
