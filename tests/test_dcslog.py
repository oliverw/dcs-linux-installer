"""Finding dcs.log, and cutting it down to what a bug report needs."""

from pathlib import Path

from dcs_linux import dcslog
from dcs_linux.dcslog import (
    FAULTS,
    HEADER,
    MAX_FAULT_LINES,
    MAX_LINE_CHARS,
    MAX_TAIL_LINES,
    SIGNATURES,
    TAIL,
)
from dcs_linux.paths import Layout
from dcs_linux.probes import TargetPaths
from tests.fakes import FakeSystem

LAYOUT = Layout(root=Path("/data/dcs"), toolchain=Path("/data/toolchain"))
PREFIX_SAVED_GAMES = LAYOUT.prefix / "drive_c" / "users" / "steamuser" / "Saved Games"

FIXTURES = Path(__file__).parent / "fixtures" / "dcs-logs"
HEALTHY = (FIXTURES / "dcs.log-healthy-33modules-fontpatched").read_text(errors="replace")
CRASHED = (FIXTURES / "apache-font-crash.log").read_text(errors="replace")


def paths(**overrides: object) -> TargetPaths:
    base: dict[str, object] = {
        "game": LAYOUT.game,
        "prefix": LAYOUT.prefix,
        "saved_games": LAYOUT.saved_games,
        "prefix_saved_games": PREFIX_SAVED_GAMES,
    }
    return TargetPaths(**{**base, **overrides})  # type: ignore[arg-type]


def titled(excerpts: tuple[dcslog.Excerpt, ...], title: str) -> dcslog.Excerpt:
    return next(excerpt for excerpt in excerpts if excerpt.title == title)


class TestFindingTheLog:
    def test_the_log_inside_the_prefix_is_found(self) -> None:
        log = PREFIX_SAVED_GAMES / "DCS" / "Logs" / "dcs.log"
        system = FakeSystem(files={str(log): "=== Log opened"})
        assert dcslog.find_log(system, paths()) == log

    def test_an_openbeta_saved_games_directory_counts(self) -> None:
        log = PREFIX_SAVED_GAMES / "DCS.openbeta" / "Logs" / "dcs.log"
        system = FakeSystem(files={str(log): "=== Log opened"})
        assert dcslog.find_log(system, paths()) == log

    def test_our_durable_saved_games_is_searched_too(self) -> None:
        log = LAYOUT.saved_games / "DCS" / "Logs" / "dcs.log"
        system = FakeSystem(files={str(log): "=== Log opened"})
        assert dcslog.find_log(system, paths()) == log

    def test_no_log_is_not_an_error(self) -> None:
        """A machine that has never launched DCS is exactly when report is run."""
        assert dcslog.find_log(FakeSystem(), paths()) is None
        assert dcslog.read_log(FakeSystem(), paths()) is None

    def test_the_log_is_read_and_excerpted(self) -> None:
        log = PREFIX_SAVED_GAMES / "DCS" / "Logs" / "dcs.log"
        system = FakeSystem(files={str(log): HEALTHY})
        read = dcslog.read_log(system, paths())
        assert read is not None
        assert read.path == log
        assert read.excerpts


class TestExcerpts:
    def test_the_header_carries_the_versions(self) -> None:
        header = titled(dcslog.excerpt(HEALTHY), HEADER)
        text = "\n".join(header.lines)
        assert "DCS/2.9.28.26385" in text
        assert "--no-launcher" in text
        assert "Build number: 541" in text

    def test_the_header_stops_before_the_body(self) -> None:
        """Line 60 of the log is a dll load, not header material."""
        header = titled(dcslog.excerpt(HEALTHY), HEADER)
        assert not any("dx11backend.dll" in line for line in header.lines)

    def test_the_tail_is_the_end_of_the_log(self) -> None:
        tail = titled(dcslog.excerpt(CRASHED), TAIL)
        assert len(tail.lines) <= MAX_TAIL_LINES
        assert "Log closed" in tail.lines[-1]

    def test_a_healthy_log_reports_no_fatal_signature(self) -> None:
        assert not any(e.title == SIGNATURES for e in dcslog.excerpt(HEALTHY))

    def test_the_font_crash_is_called_out(self) -> None:
        signatures = titled(dcslog.excerpt(CRASHED), SIGNATURES)
        text = "\n".join(signatures.lines)
        assert "Cannot create font" in text
        assert "ACCESS_VIOLATION" in text

    def test_known_benign_noise_is_dropped(self) -> None:
        """CONTEXT.md records these as appearing on healthy runs."""
        text = "\n".join(titled(dcslog.excerpt(HEALTHY), FAULTS).lines)
        assert "mainDepthBuffer" not in text
        assert "KevinWakePattern" not in text
        assert "shaderErrors" not in text

    def test_real_errors_survive(self) -> None:
        text = "\n".join(titled(dcslog.excerpt(HEALTHY), FAULTS).lines)
        assert "already declared in shapes.txt" in text

    def test_faults_are_bounded_and_say_what_was_dropped(self) -> None:
        faults = titled(dcslog.excerpt(HEALTHY), FAULTS)
        assert len(faults.lines) <= MAX_FAULT_LINES
        assert faults.omitted > 0

    def test_repeats_are_collapsed_rather_than_repeated(self) -> None:
        log = "\n".join(
            f"2026-08-02 21:30:0{n} ERROR   EDCORE ({n}): Object BOOM already declared"
            for n in range(5)
        )
        faults = titled(dcslog.excerpt(log), FAULTS)
        assert len(faults.lines) == 1
        assert "×5" in faults.lines[0]

    def test_a_single_enormous_line_is_truncated(self) -> None:
        log = "2026-08-02 21:30:01 ERROR   EDCORE (1): " + "x" * 5000
        faults = titled(dcslog.excerpt(log), FAULTS)
        assert len(faults.lines[0]) <= MAX_LINE_CHARS

    def test_an_empty_log_produces_nothing_to_show(self) -> None:
        assert dcslog.excerpt("") == ()
