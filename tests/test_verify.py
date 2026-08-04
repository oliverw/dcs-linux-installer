"""Judging a `dcs.log`, against the logs captured from real runs in #2.

The fixtures are the point of this file. Two of them come from the same
install minutes apart — one that flew and one that crashed entering a mission
— so a rule that cannot tell them apart is a rule that does not work.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from dcs_linux import verify
from dcs_linux.checks import UPSCALING, Status
from dcs_linux.dcslog import DcsLog
from dcs_linux.probes import Environment, InstallState
from dcs_linux.report import findings_json
from dcs_linux.runner import Completed
from dcs_linux.verify import (
    AUTHORIZATION,
    CRASH,
    FONTS,
    SESSION,
    SHADERS,
    Finding,
)
from tests.environments import LAYOUT, OWN_INSTALL, PATHS, healthy_environment
from tests.fakes import FakeRunner, FakeSystem

FIXTURES = Path(__file__).parent / "fixtures" / "dcs-logs"
HEALTHY = (FIXTURES / "dcs.log-healthy-33modules-fontpatched").read_text(errors="replace")
CRASHED = (FIXTURES / "apache-font-crash.log").read_text(errors="replace")
SHADER_RECOMPILE = (FIXTURES / "dcs.log-2.9.28.26385-geproton11-3").read_text(errors="replace")

LOG_PATH = LAYOUT.saved_games / "DCS" / "Logs" / "dcs.log"


def log(text: str) -> DcsLog:
    return DcsLog(path=LOG_PATH, text=text, excerpts=())


def judged(text: str, environment: Environment | None = None) -> dict[str, Finding]:
    """Every finding from one log, by name, so a test can name the one it means."""
    found = verify.judge(environment or healthy_environment(), log(text))
    return {finding.name: finding for finding in found}


class TestAHealthyLog:
    """The 33-module run that reached the success bar (CONTEXT.md)."""

    def test_nothing_is_reported_as_broken(self) -> None:
        findings = verify.judge(healthy_environment(), log(HEALTHY))
        assert findings
        assert not [finding for finding in findings if finding.status is Status.FAIL]

    def test_the_benign_noise_does_not_become_a_finding(self) -> None:
        """Several hundred ERROR lines, none of them a fault (CONTEXT.md)."""
        assert judged(HEALTHY)[FONTS].status is Status.PASS
        assert judged(HEALTHY)[CRASH].status is Status.PASS
        assert judged(HEALTHY)[SHADERS].status is Status.PASS

    def test_authorization_went_through(self) -> None:
        assert judged(HEALTHY)[AUTHORIZATION].status is Status.PASS

    def test_the_session_ended_cleanly(self) -> None:
        finding = judged(HEALTHY)[SESSION]
        assert finding.status is Status.PASS


class TestTheApacheFontCrash:
    """Starts, reaches the menu, dies entering a mission. The whole point."""

    def test_a_dcs_that_started_but_broke_is_a_failure(self) -> None:
        findings = verify.judge(healthy_environment(), log(CRASHED))
        assert [finding for finding in findings if finding.status is Status.FAIL]

    def test_the_missing_font_is_named_with_the_patch_that_fixes_it(self) -> None:
        finding = judged(CRASHED)[FONTS]
        assert finding.status is Status.FAIL
        assert finding.patch == "segoe-fonts"
        assert finding.remediation is not None
        assert "segoe-fonts" in finding.remediation

    def test_the_access_violation_is_reported_separately(self) -> None:
        """Two findings, not one: the crash is the symptom, the font the cause."""
        finding = judged(CRASHED)[CRASH]
        assert finding.status is Status.FAIL
        assert "CockpitBase" in finding.detail

    def test_a_crashed_session_did_not_end_cleanly(self) -> None:
        finding = judged(CRASHED)[SESSION]
        assert finding.status is Status.FAIL

    def test_the_benign_font_line_is_not_the_fatal_one(self) -> None:
        """Both logs carry `Cannot create font [<path>]`; only one is fatal."""
        assert judged(HEALTHY)[FONTS].status is Status.PASS


class TestShaderCompilation:
    def test_a_shader_recompile_is_a_warning_not_a_fault(self) -> None:
        """It recompiles and carries on, so it is worth saying and not failing."""
        finding = judged(SHADER_RECOMPILE)[SHADERS]
        assert finding.status is Status.WARN
        assert finding.remediation is not None
        assert "d3dcompiler_47" in finding.remediation


class TestAuthorization:
    def test_a_clock_drift_failure_is_named_as_one(self) -> None:
        text = (
            "=== Log opened UTC 2026-08-02 21:30:03\n"
            "2026-08-02 21:30:07.021 ERROR   ASYNCNET (504): Login failed\n"
            "2026-08-02 21:30:07.022 ERROR   ASYNCNET (504): SSL certificate problem: "
            "certificate is not yet valid\n"
        )
        finding = judged(text)[AUTHORIZATION]
        assert finding.status is Status.FAIL
        assert "clock" in finding.detail.lower()
        assert finding.remediation is not None
        assert "timedatectl" in finding.remediation

    def test_a_plain_login_failure_is_not_blamed_on_the_clock(self) -> None:
        text = (
            "=== Log opened UTC 2026-08-02 21:30:03\n"
            "2026-08-02 21:30:07.021 ERROR   ASYNCNET (504): Login failed\n"
        )
        finding = judged(text)[AUTHORIZATION]
        assert finding.status is Status.FAIL
        assert "clock" not in finding.detail.lower()

    def test_never_getting_that_far_is_not_an_authorization_failure(self) -> None:
        finding = judged("=== Log opened UTC 2026-08-02 21:30:03\n")[AUTHORIZATION]
        assert finding.status is Status.WARN


class TestWhatTheLogNeverSays:
    """DLSS flicker is invisible in dcs.log, so log evidence alone is not enough."""

    def test_dlss_fails_a_verification_of_an_otherwise_healthy_log(self) -> None:
        environment = healthy_environment(
            install_state=InstallState(prefix_exists=True, upscaling="DLSS")
        )
        finding = judged(HEALTHY, environment)[UPSCALING]
        assert finding.status is Status.FAIL
        assert "DLSS" in finding.detail

    def test_the_dlss_check_is_the_same_rule_check_applies(self) -> None:
        environment = healthy_environment(
            install_state=InstallState(prefix_exists=True, upscaling="OFF")
        )
        assert judged(HEALTHY, environment)[UPSCALING].status is Status.PASS


class TestNoLogAtAll:
    def test_a_missing_log_is_a_failure_with_nothing_else_guessed(self) -> None:
        findings = verify.judge(healthy_environment(), None)
        statuses = {finding.name: finding.status for finding in findings}
        assert statuses[verify.LOG] is Status.FAIL
        # Nothing may be reported as passing on the strength of a log that
        # does not exist.
        assert statuses[FONTS] is Status.SKIP
        assert statuses[CRASH] is Status.SKIP

    def test_dlss_is_still_judged_without_a_log(self) -> None:
        """It was never in the log, so a missing log costs nothing here."""
        environment = healthy_environment(
            install_state=InstallState(prefix_exists=True, upscaling="DLSS")
        )
        findings = {f.name: f for f in verify.judge(environment, None)}
        assert findings[UPSCALING].status is Status.FAIL


class TestTheOverallVerdict:
    def test_a_verification_is_ok_when_nothing_failed(self) -> None:
        result = verify.Verification(findings=verify.judge(healthy_environment(), log(HEALTHY)))
        assert result.ok

    def test_one_failure_is_enough_to_fail_the_whole_thing(self) -> None:
        result = verify.Verification(findings=verify.judge(healthy_environment(), log(CRASHED)))
        assert not result.ok

    def test_warnings_alone_do_not_fail_it(self) -> None:
        findings = verify.judge(healthy_environment(), log(SHADER_RECOMPILE))
        assert any(finding.status is Status.WARN for finding in findings)
        assert verify.Verification(findings=findings).ok


class TestLogOpened:
    def test_the_opening_stamp_identifies_one_run(self) -> None:
        assert verify.log_opened(HEALTHY) == "2026-08-02 21:30:03"
        assert verify.log_opened(CRASHED) == "2026-08-02 21:23:52"

    def test_a_log_without_a_header_has_no_stamp(self) -> None:
        assert verify.log_opened("nothing useful here") is None


class TestFindingsFeedReport:
    def test_a_finding_survives_being_made_machine_readable(self) -> None:
        finding = judged(CRASHED)[FONTS]
        payload = findings_json((finding,))[0]
        assert payload["name"] == FONTS
        assert payload["status"] == "fail"
        assert payload["patch"] == "segoe-fonts"


class TestLaunching:
    """Starting DCS, and what the tool does around the part it cannot automate."""

    def machine(self, text: str = HEALTHY) -> FakeSystem:
        return FakeSystem(
            files={
                str(OWN_INSTALL.game / "bin" / "DCS.exe"): "MZ",
                str(LAYOUT.saved_games / "DCS" / "Logs" / "dcs.log"): text,
            }
        )

    def test_dcs_is_launched_through_umu_with_the_launcher_suppressed(self) -> None:
        runner = FakeRunner()
        verify.verify_install(self.machine(), runner, healthy_environment(), LAYOUT)

        command, environment = runner.calls[0]
        assert command[0] == str(LAYOUT.umu_run)
        assert command[1] == str(OWN_INSTALL.game / "bin" / "DCS.exe")
        assert command[2] == verify.NO_LAUNCHER
        assert environment["WINEPREFIX"] == str(LAYOUT.prefix)
        # IC-safe by construction: in the process, never in a hashed game file.
        assert environment["WINEDLLOVERRIDES"] == "wbemprox=n"

    def test_the_whole_process_tree_can_be_stopped(self) -> None:
        """umu → Proton → wine → DCS. Killing only the first orphans the rest."""
        runner = FakeRunner()
        verify.verify_install(self.machine(), runner, healthy_environment(), LAYOUT)
        assert runner.sessions == [True]

    def test_a_timeout_is_a_failure_and_says_dcs_was_stopped(self) -> None:
        runner = FakeRunner(
            results={
                str(OWN_INSTALL.game / "bin" / "DCS.exe"): Completed(
                    returncode=None, detail="timed out after 14400s and was stopped"
                )
            }
        )
        result = verify.verify_install(self.machine(), runner, healthy_environment(), LAYOUT)
        launch = named(result, verify.LAUNCH)
        assert launch.status is Status.FAIL
        assert "stopped" in launch.detail
        assert not result.ok

    def test_no_launch_starts_nothing_and_still_judges_the_log(self) -> None:
        runner = FakeRunner()
        result = verify.verify_install(
            self.machine(), runner, healthy_environment(), LAYOUT, launch=False
        )
        assert runner.calls == []
        assert named(result, verify.LAUNCH).status is Status.SKIP
        assert named(result, SESSION).status is Status.PASS

    def test_somebody_elses_prefix_is_judged_but_never_started(self) -> None:
        """Another launcher's prefix has a Proton and an environment we did not choose."""
        environment = healthy_environment(
            paths=replace(PATHS, prefix=Path("/home/pilot/Games/lutris/dcs"))
        )
        runner = FakeRunner()
        result = verify.verify_install(self.machine(), runner, environment, LAYOUT)

        assert runner.calls == []
        assert named(result, verify.LAUNCH).status is Status.SKIP
        # It said it would judge the previous run, so it must not then fail the
        # log for being the previous run.
        assert named(result, verify.LOG).status is Status.PASS
        assert result.ok

    def test_an_unfinished_install_fails_before_anything_is_started(self) -> None:
        runner = FakeRunner()
        system = FakeSystem(files={str(LAYOUT.saved_games / "DCS" / "Logs" / "dcs.log"): HEALTHY})
        result = verify.verify_install(system, runner, healthy_environment(), LAYOUT)

        assert runner.calls == []
        assert named(result, verify.LAUNCH).status is Status.FAIL
        assert not result.ok

    def test_no_launch_does_not_fail_the_log_for_being_the_previous_run(self) -> None:
        result = verify.verify_install(
            self.machine(), FakeRunner(), healthy_environment(), LAYOUT, launch=False
        )
        assert named(result, verify.LOG).status is Status.PASS
        assert result.ok

    def test_a_log_the_launch_did_not_rewrite_is_not_this_run(self) -> None:
        """The one way a verification could report a healthy run that never happened."""
        result = verify.verify_install(self.machine(), FakeRunner(), healthy_environment(), LAYOUT)
        finding = named(result, verify.LOG)
        assert finding.status is Status.FAIL
        assert "previous run" in finding.detail

    def test_a_freshly_written_log_is_judged_as_this_run(self) -> None:
        system = self.machine(HEALTHY)
        log = LAYOUT.saved_games / "DCS" / "Logs" / "dcs.log"

        def relaunch() -> None:
            system.files[log] = HEALTHY.replace("21:30:03", "22:05:11", 1).encode()

        runner = FakeRunner(effects={str(OWN_INSTALL.game / "bin" / "DCS.exe"): relaunch})
        result = verify.verify_install(system, runner, healthy_environment(), LAYOUT)

        assert named(result, verify.LOG).status is Status.PASS
        assert result.ok

    def test_the_briefing_says_the_menu_is_not_enough(self) -> None:
        """Starting and running are different failures on Linux (CONTEXT.md)."""
        announced: list[str] = []
        verify.verify_install(
            self.machine(),
            FakeRunner(),
            healthy_environment(),
            LAYOUT,
            announce=announced.append,
        )
        assert "Instant Action" in announced[0]


def named(result: verify.Verification, name: str) -> Finding:
    return next(finding for finding in result.findings if finding.name == name)


def test_an_environment_with_no_install_state_still_judges() -> None:
    """`verify` on a machine mid-install must not raise on the way past."""
    environment = replace(healthy_environment(), install_state=InstallState())
    assert verify.judge(environment, None)
