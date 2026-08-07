"""Launching DCS and judging what it wrote, which is the question this project exists for.

`check` reads the machine and answers *should this work*. It is fast, static,
and it stays that way. This is the other half: DCS is started, the user flies
it, and then `dcs.log` is read to answer *did it work* — which is a different
question with a different failure mode, because **DCS fails quietly**. It
reaches the main menu with no symbols in the Apache, or with garbage MFDs, or
having never authorized at all, and the process exits 0 through every one of
them. A launcher that judged the exit code would call all three a success.

Three things shape this module.

- **The log is the evidence, and most of it is noise.** A healthy run is 150 KB
  and several hundred ERROR lines (`CONTEXT.md`). So the rules here are written
  against logs captured from real runs and committed as fixtures — including
  two from the *same install* minutes apart, one that flew and one that crashed
  — and a rule that cannot tell those two apart is not a rule.
- **A clean log is not a clean run.** DLSS flicker never reaches `dcs.log` at
  all. Reporting "no problems" on log evidence alone would be wrong in exactly
  the case users most often mistake for a broken install, so the static rules
  that catch it are folded in here rather than left to `check`.
- **Every finding names its own fix.** A verification that says "something is
  wrong with fonts" has done half the job; the useful half is `dcs-linux patch
  apply segoe-fonts`, which is why a `Finding` carries the patch id.

The launch itself is human-in-the-loop, like the updater handoff: the tool
opens DCS and the user flies to the success bar (`CONTEXT.md`) and quits. What
the tool controls is the environment it starts in, the timeout, and making sure
nothing is left running afterwards.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dcs_linux.checks import CheckResult, Status, check_upscaling
from dcs_linux.dcslog import (
    ACCESS_VIOLATION,
    AUTH_FAILURE,
    AUTH_SUCCESS,
    CLOCK_DRIFT,
    FONT_CRASH,
    LOG_CLOSED,
    RENDER_THREAD_STOPPED,
    SHADER_RECOMPILE,
    DcsLog,
    read_log,
)
from dcs_linux.launch import launch_dcs
from dcs_linux.paths import Layout
from dcs_linux.probes import Environment
from dcs_linux.runner import Runner
from dcs_linux.system import System

# Finding names, bound once so the tests, the JSON and the bundle agree.
LAUNCH = "Launch"
LOG = "dcs.log"
AUTHORIZATION = "Authorization"
FONTS = "Fonts"
CRASH = "Crash"
SHADERS = "Shaders"
SESSION = "Session"
# There is deliberately no `UPSCALING` here. That finding is `check`'s own rule
# reported verbatim, name included (`_from_check`), and a second spelling of
# the name in this module is a second thing to keep in step.

# `=== Log opened UTC 2026-08-02 21:30:03`, the first line of every log. It
# identifies one run, which is how a launch that wrote nothing is told from one
# that did: without it, the previous run's log would be judged as this one's.
_LOG_OPENED = re.compile(r"^=== Log opened(?: UTC)? (.+?)\s*$", re.MULTILINE)

# The faulting module is logged just above the exception, as a path to the dll.
_MODULE_SEARCH_LINES = 4
_MODULE = re.compile(r"([^\\/]+\.dll)", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    """One thing that was checked about a run, and what to do about it.

    Deliberately shaped like `checks.CheckResult` — the two are read side by
    side and one rule (DLSS) is literally shared — plus `patch`, because "which
    patch fixes this" is the answer that makes a finding actionable.
    """

    name: str
    status: Status
    detail: str
    remediation: str | None = None
    patch: str | None = None

    @property
    def failed(self) -> bool:
        return self.status is Status.FAIL


@dataclass(frozen=True)
class Verification:
    """Everything a run was judged on, and where the evidence came from."""

    findings: tuple[Finding, ...]
    log_path: Path | None = None

    @property
    def ok(self) -> bool:
        """No failure. Warnings are things worth saying, not things that broke."""
        return not any(finding.failed for finding in self.findings)


def log_opened(text: str | None) -> str | None:
    """The `Log opened` stamp, which identifies one run of DCS."""
    if text is None:
        return None
    match = _LOG_OPENED.search(text)
    return match.group(1) if match else None


def judge(environment: Environment, log: DcsLog | None) -> tuple[Finding, ...]:
    """Every finding about one run: what the log says, and what it never says.

    Pure, so it is tested against the captured logs rather than against a DCS
    that has to be launched. With no log the log-borne rules skip rather than
    pass — a rule that reports "no crash" having read nothing is worse than one
    that says it does not know.
    """
    text = log.text if log is not None else None
    return (
        _log_found(log),
        _authorization(text),
        _fonts(text),
        _crash(text),
        _shaders(text),
        _session(text),
        # Never in the log at all, and the failure users most often mistake for
        # a broken install (CONTEXT.md), so log evidence alone must not clear a
        # run. The identical rule `check` applies, not a second copy of it.
        _from_check(check_upscaling(environment)),
    )


def verify_install(
    system: System,
    runner: Runner,
    environment: Environment,
    layout: Layout,
    *,
    launch: bool = True,
    announce: Callable[[str], None] = lambda _: None,
) -> Verification:
    """Launch DCS, wait for the user to be done with it, and judge the log.

    The launch is a step that can fail on its own terms, so it produces its own
    finding and never stops the judging: a DCS that would not start still has a
    log from last time, and reading it is more use than reporting nothing.
    """
    previous = read_log(system, environment.paths)
    before = log_opened(previous.text) if previous is not None else None
    launch_finding = Finding(LAUNCH, Status.SKIP, "not requested (--no-launch)")
    if launch:
        announce(briefing(environment))
        launch_finding = _launch(system, runner, environment, layout)

    log = read_log(system, environment.paths)
    findings = judge(environment, log)
    # Only a launch that actually ran DCS can be judged on whether the log is
    # fresh. A launch that was skipped — someone else's prefix, `--no-launch` —
    # said in so many words that it is reading the previous run, and failing it
    # for being the previous run would contradict its own finding.
    if launch_finding.status is Status.PASS:
        findings = _with_freshness(findings, before, log)
    return Verification(
        findings=(launch_finding, *findings),
        log_path=log.path if log is not None else None,
    )


def briefing(environment: Environment) -> str:
    """What the user has to do once the window is up.

    Starting and running are different failures on Linux: the known breakages
    surface in a mission, not at the menu (`CONTEXT.md`), so a user who quits
    from the main menu has verified almost nothing and needs telling why.
    """
    version = environment.targeted.version if environment.targeted else None
    return "\n".join(
        [
            f"DCS {version} is about to start." if version else "DCS is about to start.",
            "",
            "Reaching the main menu proves very little — the breakages this checks for",
            "surface in a mission. So:",
            "",
            "  1. Let it reach the main menu.",
            "  2. Instant Action → any free flight, in the aircraft you care about.",
            "  3. Fly for a minute or so, then quit DCS normally.",
            "",
            "The first launch precompiles shaders and can sit on a black screen for",
            "several minutes. When you quit, this reads the log and says what it found.",
        ]
    )


def _launch(system: System, runner: Runner, environment: Environment, layout: Layout) -> Finding:
    """Start DCS in the prefix that was built for it, and wait.

    Only *our* prefix is launched into. An install adopted from Lutris or
    Heroic has a Proton build and an environment this tool did not choose
    (ADR-0007), and starting it under ours would be running something other
    than the thing being verified — so that install is judged from its log
    instead.
    """
    targeted = environment.targeted
    if targeted is None:
        return Finding(LAUNCH, Status.SKIP, "no install targeted, so nothing was launched")
    if environment.paths.prefix != layout.prefix:
        return Finding(
            LAUNCH,
            Status.SKIP,
            f"the prefix at {environment.paths.prefix} was not built by this tool, so DCS "
            f"was not started; the last dcs.log is judged instead",
            remediation=f"start it from {targeted.launcher}, then run this again",
        )

    result = launch_dcs(system, runner, environment)
    if not result.ok:
        return Finding(LAUNCH, Status.FAIL, result.detail, remediation="dcs-linux install")
    # The process result is reported, never used as the verification verdict:
    # DCS exits non-zero on ordinary quits under wine, and the log is evidence.
    return Finding(LAUNCH, Status.PASS, result.detail)


def _with_freshness(
    findings: tuple[Finding, ...], before: str | None, log: DcsLog | None
) -> tuple[Finding, ...]:
    """Overrule the log finding when this launch did not write the log.

    Without this, a DCS that died before opening its log would be judged on the
    previous run's — which is the one way a verification could report a healthy
    run that never happened. It replaces the log finding rather than adding to
    it, because "the log is here" and "the log is last week's" are two answers
    to one question.
    """
    if log is None or log_opened(log.text) != before:
        return findings
    stale = Finding(
        LOG,
        Status.FAIL,
        f"{log.path} was not rewritten, so DCS never got as far as opening a log; "
        "everything below describes the previous run",
        remediation="check that DCS starts at all: dcs-linux check",
    )
    return tuple(stale if finding.name == LOG else finding for finding in findings)


def _log_found(log: DcsLog | None) -> Finding:
    if log is None:
        return Finding(
            LOG,
            Status.FAIL,
            "no dcs.log found, so there is nothing to judge this run on",
            remediation="run DCS once; if it never starts, dcs-linux check says why",
        )
    stamp = log_opened(log.text)
    opened = f", opened {stamp}" if stamp else ""
    return Finding(LOG, Status.PASS, f"{log.path}{opened}")


def _authorization(text: str | None) -> Finding:
    """Whether ED let this copy of DCS in, and if not, why not.

    Its own finding because it fails silently: DCS carries on to the main menu
    unauthorized, and the user finds out when multiplayer and modules are gone.
    """
    if text is None:
        return _no_log(AUTHORIZATION)
    if AUTH_SUCCESS.search(text):
        return Finding(AUTHORIZATION, Status.PASS, "ED authorization succeeded")
    if not AUTH_FAILURE.search(text):
        # No line either way: DCS stopped before it got there, which is
        # somebody else's failure to report.
        return Finding(
            AUTHORIZATION,
            Status.WARN,
            "the log says nothing about authorization; DCS stopped before it asked",
        )
    if CLOCK_DRIFT.search(text):
        return Finding(
            AUTHORIZATION,
            Status.FAIL,
            "authorization was refused over certificate validity dates, which is what a "
            "wrong system clock looks like — not a bad password",
            remediation="fix the clock, then try again: timedatectl set-ntp true "
            "(and `timedatectl status` to confirm it synchronised)",
        )
    return Finding(
        AUTHORIZATION,
        Status.FAIL,
        "ED authorization failed; DCS runs unauthorized, without multiplayer or modules",
        remediation="check the login in the DCS launcher, and that this machine has a "
        "working network and a correct clock (timedatectl status)",
    )


def _fonts(text: str | None) -> Finding:
    """The empty brackets are the whole distinction (`CONTEXT.md`).

    `Cannot create font [<path>] size 0` is on healthy runs. `Cannot create
    font [] size 30` is the missing Segoe font, and the access violation is on
    the next line.
    """
    if text is None:
        return _no_log(FONTS)
    if not FONT_CRASH.search(text):
        return Finding(FONTS, Status.PASS, "no font failed to load")
    return Finding(
        FONTS,
        Status.FAIL,
        "a font was requested by no name at all — the missing Segoe font. The AH-64D "
        "crashes on entering a mission without it",
        remediation="dcs-linux patch apply segoe-fonts",
        patch="segoe-fonts",
    )


def _crash(text: str | None) -> Finding:
    """DCS wrote a crash dump, and which module it was in."""
    if text is None:
        return _no_log(CRASH)
    lines = text.splitlines()
    index = next((n for n, line in enumerate(lines) if ACCESS_VIOLATION.search(line)), None)
    if index is None:
        return Finding(CRASH, Status.PASS, "no access violation in the log")
    module = _faulting_module(lines, index)
    where = f" in {module}" if module else ""
    return Finding(
        CRASH,
        Status.FAIL,
        f"DCS crashed with an access violation{where}",
        remediation="the finding above usually says why; if nothing else failed, "
        "attach `dcs-linux report` to a bug report",
    )


def _faulting_module(lines: list[str], index: int) -> str | None:
    """The dll named just above the exception in DCS's crash dump header."""
    start = max(0, index - _MODULE_SEARCH_LINES)
    for line in reversed(lines[start:index]):
        match = _MODULE.search(line)
        if match:
            return match.group(1)
    return None


def _shaders(text: str | None) -> Finding:
    """A shader that had to be rebuilt, which is slow but not broken."""
    if text is None:
        return _no_log(SHADERS)
    if not SHADER_RECOMPILE.search(text):
        return Finding(SHADERS, Status.PASS, "all shaders were found precompiled")
    return Finding(
        SHADERS,
        Status.WARN,
        "DCS could not find a precompiled shader and rebuilt it, which costs minutes "
        "on this launch",
        remediation="expected once after clearing the shader cache. If it happens every "
        "launch, the prefix is missing d3dcompiler_47 — dcs-linux install puts it back",
    )


def _session(text: str | None) -> Finding:
    """How the run ended, which is often the only thing that matters.

    A clean shutdown stops the render thread and then closes the log. A log
    that closes without the first is DCS unwinding after a crash; a log that
    never closes is a kill or a hang.
    """
    if text is None:
        return _no_log(SESSION)
    stopped = RENDER_THREAD_STOPPED in text
    closed = LOG_CLOSED in text
    if stopped and closed:
        return Finding(SESSION, Status.PASS, "DCS shut down cleanly")
    if closed:
        return Finding(
            SESSION,
            Status.FAIL,
            "DCS closed without stopping its render thread, which is what it does on the "
            "way down from a crash",
            remediation="the crash finding above names the module",
        )
    return Finding(
        SESSION,
        Status.FAIL,
        "the log never closed: DCS was killed, or it hung and never got to shut down",
        remediation="if DCS hung on a black screen, let the first launch finish "
        "precompiling shaders before quitting",
    )


def _from_check(result: CheckResult) -> Finding:
    """One of `check`'s rules, reported as a finding."""
    return Finding(
        name=result.name,
        status=result.status,
        detail=result.detail,
        remediation=result.remediation,
    )


def _no_log(name: str) -> Finding:
    """What a log-borne rule reports when there is no log: not a pass."""
    return Finding(name, Status.SKIP, "no dcs.log to read")
