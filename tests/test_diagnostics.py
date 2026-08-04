"""The diagnostics bundle: what it says, and what it must never say."""

from dataclasses import replace
from pathlib import Path

from dcs_linux import dcslog
from dcs_linux.diagnostics import MAX_BUNDLE_CHARS, bundle, cell, escape
from dcs_linux.installs import DcsInstall, Edition, Launcher
from dcs_linux.probes import Environment, InstallState
from dcs_linux.redaction import Redactor
from tests.environments import LAYOUT, OWN_INSTALL, bare_environment, healthy_environment

FIXTURES = Path(__file__).parent / "fixtures" / "dcs-logs"
CRASHED = (FIXTURES / "apache-font-crash.log").read_text(errors="replace")

REDACTOR = Redactor(home=Path("/home/oliver"), user="oliver")

STEAM_INSTALL = DcsInstall(
    game=Path("/home/oliver/.steam/steamapps/common/DCSWorld"),
    launcher=Launcher.STEAM,
    prefix=Path("/home/oliver/.steam/steamapps/compatdata/223750/pfx"),
    runtime="GE-Proton11-3",
    edition=Edition.STEAM,
    version="2.9.28.26385",
)

CRASH_LOG = dcslog.DcsLog(
    path=Path("/home/oliver/dcs-linux/saved-games/DCS/Logs/dcs.log"),
    text=CRASHED,
    excerpts=dcslog.excerpt(CRASHED),
)


def built(environment: Environment | None = None, log: dcslog.DcsLog | None = None) -> str:
    return bundle(
        environment=environment or healthy_environment(),
        log=log,
        redactor=REDACTOR,
        version="1.2.3",
    )


class TestTheMachine:
    def test_the_tool_version_is_stated(self) -> None:
        assert "1.2.3" in built()

    def test_the_distro_gpu_and_toolchain_are_stated(self) -> None:
        text = built(healthy_environment(kernel="6.17.4-202.fc44.x86_64"))
        assert "Fedora Linux 44" in text
        assert "6.17.4-202.fc44.x86_64" in text
        assert "NVIDIA" in text and "610.43.03" in text
        assert "1.4.4" in text
        assert "GE-Proton11-3" in text

    def test_the_game_directory_reports_space_and_filesystem(self) -> None:
        text = built()
        assert "btrfs" in text
        assert "GiB free" in text

    def test_it_is_markdown_a_forum_will_render(self) -> None:
        text = built()
        assert text.startswith("# ")
        assert "| --- |" in text


class TestChecks:
    def test_every_check_appears_with_its_result(self) -> None:
        text = built()
        assert "Segoe fonts" in text
        assert "Upscaling" in text

    def test_a_failure_brings_its_fix_along(self) -> None:
        text = built(healthy_environment(install_state=InstallState(prefix_exists=True)))
        assert "FAIL" in text
        assert "umu-run winetricks d3dcompiler_47" in text

    def test_a_pipe_in_a_cell_cannot_break_the_table(self) -> None:
        """Remediations are shell commands, and shell commands contain pipes."""
        assert escape("dnf list | grep dcs") == r"dnf list \| grep dcs"

    def test_a_multi_line_cell_stays_on_one_row(self) -> None:
        assert escape("first\nsecond") == "first<br>second"

    def test_every_cell_is_redacted_on_its_way_in(self) -> None:
        """The single point every table value passes through."""
        assert cell(REDACTOR, "/home/oliver/game") == "~/game"


class TestInstalls:
    def test_every_install_is_listed(self) -> None:
        text = built(healthy_environment(installs=(OWN_INSTALL, STEAM_INSTALL)))
        assert OWN_INSTALL.install_id in text
        assert STEAM_INSTALL.install_id in text
        assert "steam" in text

    def test_no_install_is_said_plainly(self) -> None:
        assert "No DCS install found" in built(bare_environment())

    def test_several_installs_and_none_chosen_says_so(self) -> None:
        """Otherwise the bundle reads as a report on an install nobody picked."""
        text = built(healthy_environment(installs=(OWN_INSTALL, STEAM_INSTALL), targeted=None))
        assert "No install targeted" in text
        assert "--install ID" in text

    def test_an_untargeted_bundle_does_not_claim_dcs_never_ran(self) -> None:
        text = built(healthy_environment(installs=(OWN_INSTALL, STEAM_INSTALL), targeted=None))
        assert "has not written one" not in text


class TestRedaction:
    def test_no_home_path_survives(self) -> None:
        environment = healthy_environment(installs=(STEAM_INSTALL,), targeted=STEAM_INSTALL)
        assert "/home/oliver" not in built(environment)

    def test_launcher_supplied_values_are_scrubbed_too(self) -> None:
        """Runtime and Proton names are directory names the user controls."""
        nosy = replace(STEAM_INSTALL, runtime="proton-/home/oliver-build")
        environment = healthy_environment(
            installs=(nosy,),
            targeted=nosy,
            proton_builds=("GE-Proton11-3-/home/oliver",),
            kernel="6.17.4-oliver",
        )
        assert "/home/oliver" not in built(environment)

    def test_the_log_is_redacted_too(self) -> None:
        text = built(log=CRASH_LOG)
        assert "/home/oliver" not in text

    def test_the_ed_credential_is_named_as_excluded(self) -> None:
        """It is excluded by never being read; the bundle says so out loud."""
        assert "authdata.bin" in built()


class TestTheLog:
    def test_excerpts_are_fenced_and_titled(self) -> None:
        text = built(log=CRASH_LOG)
        assert "```" in text
        assert dcslog.HEADER in text
        assert "Cannot create font" in text

    def test_no_log_is_reported_rather_than_omitted(self) -> None:
        assert "No dcs.log" in built()

    def test_a_huge_log_cannot_swamp_the_bundle(self) -> None:
        text = built(log=CRASH_LOG)
        assert len(text) <= MAX_BUNDLE_CHARS

    def test_the_omitted_count_is_stated_rather_than_hidden(self) -> None:
        assert "omitted" in built(log=CRASH_LOG)


class TestGraphicsOptions:
    def test_the_graphics_table_is_quoted(self) -> None:
        options = '["graphics"] = {["Upscaling"] = "DLSS"}'
        environment = healthy_environment(
            install_state=replace(healthy_environment().install_state, graphics_options=options)
        )
        assert options in built(environment)


class TestWhatVerifyFound:
    """`verify`'s judgement, on the log already in hand. Nothing is launched."""

    def test_the_bundle_says_what_went_wrong_not_only_what_the_log_holds(self) -> None:
        text = built(log=CRASH_LOG)
        assert "## Last run" in text
        assert "missing Segoe font" in text
        assert "patch apply segoe-fonts" in text

    def test_a_machine_that_has_never_run_dcs_reports_no_verdict_as_a_verdict(self) -> None:
        text = built(bare_environment())
        assert "## Last run" in text
        assert "no dcs.log" in text


def test_a_broken_machine_still_produces_a_bundle() -> None:
    """The case report exists for: nothing works, nothing is installed."""
    environment = bare_environment(distro=replace(bare_environment().distro, id="unknown"))
    text = built(environment)
    assert text.startswith("# ")
    assert str(LAYOUT.game) in text
