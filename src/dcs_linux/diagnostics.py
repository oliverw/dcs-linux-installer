"""The diagnostics bundle `report` prints.

This project is developed on one machine and shipped to every distro, GPU and
launcher layout there is. The bundle is how that gap gets covered: a good one
turns a bug reporter into a test environment we could not otherwise reach.

Two properties are not negotiable, and everything here is arranged around
them:

- **Safe to post in public.** Every path and every quoted line goes through a
  `Redactor`. The ED credential (`Saved Games/DCS/Config/authdata.bin`) is
  excluded by never being collected at all.
- **Bounded.** A DCS log is 150 KB with several hundred ERROR lines on a
  healthy run. Excerpts are capped per section, and the whole bundle is
  clamped as a last resort.

It renders markdown from an already-probed `Environment`, so it touches
neither the machine nor the clock and can be tested from fixtures.
"""

from __future__ import annotations

from dcs_linux.checks import CheckResult, Status, run_checks
from dcs_linux.dcslog import DcsLog
from dcs_linux.installs import EDITION_LABELS, DcsInstall
from dcs_linux.probes import Environment
from dcs_linux.redaction import Redactor
from dcs_linux.system import DiskUsage
from dcs_linux.verify import Finding, judge

GIB = 1024**3

# A last-resort clamp. The per-section caps in `dcslog` do the real work; this
# only exists so that no input at all can produce a bundle too long to paste.
MAX_BUNDLE_CHARS = 20_000

# The graphics table is small, but options.lua is hand-editable and this is a
# public paste, so it gets a ceiling of its own.
MAX_GRAPHICS_LINES = 60

CREDENTIAL_NOTE = (
    "Paths, user names and addresses are redacted. "
    "The ED credential (`Saved Games/DCS/Config/authdata.bin`) is never read."
)

_MARKER = {
    Status.PASS: "ok",
    Status.WARN: "warn",
    Status.FAIL: "FAIL",
    Status.SKIP: "skip",
}


def bundle(
    environment: Environment,
    log: DcsLog | None,
    redactor: Redactor,
    version: str,
) -> str:
    """The whole report, as markdown ready to paste into an issue."""
    parts = [
        f"# dcs-linux report\n\n_{CREDENTIAL_NOTE}_",
        _machine(environment, redactor, version),
        _checks(environment, redactor),
        _findings(environment, log, redactor),
        _installs(environment, redactor),
        _graphics(environment, redactor),
        _log(environment, log, redactor),
    ]
    return _clamp("\n\n".join(parts) + "\n")


def escape(text: str) -> str:
    """Make text safe to sit in a markdown table cell.

    Remediations are shell commands and check details run to several lines, so
    both a pipe and a newline happen in practice, and either one silently
    wrecks the table.
    """
    return text.replace("|", r"\|").replace("\n", "<br>")


def cell(redactor: Redactor, text: str) -> str:
    """One table cell: redacted, then escaped.

    Redaction lives here, at the single point every cell passes through,
    rather than at each call site. Values reach these tables from launcher
    config, directory names and the kernel — all user-controlled — so a table
    that scrubs most cells and forgets one is the failure mode to design out.
    """
    return escape(redactor.scrub(text))


def _machine(environment: Environment, redactor: Redactor, version: str) -> str:
    distro = environment.distro
    base = "immutable" if distro.is_immutable else "mutable"
    umu = environment.umu
    missing = ", ".join(environment.missing_tools)
    facts = [
        ("dcs-linux", version),
        ("Distro", f"{distro.name} ({base} base system)"),
        ("Kernel", environment.kernel or "unknown"),
        ("GPU", _gpus(environment)),
        ("umu", f"{umu.version or 'present'}" if umu.path else "not found"),
        ("Proton builds", ", ".join(environment.proton_builds) or "none found"),
        ("Game directory", _game_directory(environment, redactor)),
        ("External tools", f"missing: {missing}" if missing else "all present"),
    ]
    rows = "\n".join(f"| {name} | {cell(redactor, value)} |" for name, value in facts)
    return f"## Machine\n\n| | |\n| --- | --- |\n{rows}"


def _gpus(environment: Environment) -> str:
    if not environment.gpus:
        return "none found"
    return ", ".join(
        f"{gpu.vendor} ({gpu.kernel_driver or 'no kernel driver'}) "
        f"{gpu.driver_version or 'version unknown'}"
        for gpu in environment.gpus
    )


def _game_directory(environment: Environment, redactor: Redactor) -> str:
    """Where the game lives, and whether that drive can hold it."""
    where = redactor.path(environment.paths.game)
    details = [environment.filesystem or "filesystem unknown", _space(environment.disk)]
    return f"{where} ({', '.join(details)})"


def _space(disk: DiskUsage | None) -> str:
    if disk is None:
        return "free space unknown"
    return f"{disk.free / GIB:.0f} of {disk.total / GIB:.0f} GiB free"


def _checks(environment: Environment, redactor: Redactor) -> str:
    rows = "\n".join(_check_row(result, redactor) for result in run_checks(environment))
    return f"## Checks\n\n| | Check | Result | Fix |\n| --- | --- | --- | --- |\n{rows}"


def _check_row(result: CheckResult, redactor: Redactor) -> str:
    fix = cell(redactor, result.remediation) if result.remediation else ""
    return f"| {_MARKER[result.status]} | {result.name} | {cell(redactor, result.detail)} | {fix} |"


def _findings(environment: Environment, log: DcsLog | None, redactor: Redactor) -> str:
    """What `verify` makes of the last run, without launching anything.

    The same judgement `verify` reports, applied to the log already in hand, so
    a bug report says *what went wrong* and not only what the log contains. It
    launches nothing: `report` is what a user runs when DCS will not start.
    """
    findings = judge(environment, log)
    rows = "\n".join(_finding_row(finding, redactor) for finding in findings)
    header = "| | Finding | Result | Fix |\n| --- | --- | --- | --- |"
    return f"## Last run\n\n{header}\n{rows}"


def _finding_row(finding: Finding, redactor: Redactor) -> str:
    fix = cell(redactor, finding.remediation) if finding.remediation else ""
    marker = _MARKER[finding.status]
    return f"| {marker} | {finding.name} | {cell(redactor, finding.detail)} | {fix} |"


def _installs(environment: Environment, redactor: Redactor) -> str:
    if not environment.installs:
        return "## Installs\n\nNo DCS install found."
    header = (
        "| | ID | Launcher | Edition | Version | Runtime | Game | Prefix |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |"
    )
    rows = "\n".join(
        _install_row(install, redactor, targeted=install == environment.targeted)
        for install in environment.installs
    )
    return f"## Installs\n\n{header}\n{rows}{_nothing_targeted_note(environment)}"


def _nothing_targeted_note(environment: Environment) -> str:
    """Say when several installs were found and none was chosen.

    Without this the bundle reads as a report about the one install, when in
    fact every install-dependent row was skipped and the paths shown are this
    tool's own defaults rather than anything on the machine.
    """
    if environment.targeted is not None:
        return ""
    return (
        "\n\n**No install targeted** — the rows above that describe one install were "
        "skipped, and the paths in *Machine* are this tool's defaults, not a real "
        "install. Re-run with `--install ID` to report on one of these."
    )


def _install_row(install: DcsInstall, redactor: Redactor, *, targeted: bool) -> str:
    fields = (
        "->" if targeted else "",
        install.install_id,
        str(install.launcher),
        EDITION_LABELS[install.edition],
        install.version or "unknown",
        install.runtime or "unknown",
        str(install.game),
        str(install.prefix) if install.prefix else "unknown",
    )
    return "| " + " | ".join(cell(redactor, field) for field in fields) + " |"


def _graphics(environment: Environment, redactor: Redactor) -> str:
    """The graphics table from options.lua — where the DLSS flicker hides."""
    options = environment.install_state.graphics_options
    if options is None:
        return "## Graphics options\n\nNo options.lua yet."
    all_lines = redactor.scrub(options).splitlines()
    lines = all_lines[:MAX_GRAPHICS_LINES]
    cut = len(all_lines) - len(lines)
    note = f"\n\n_{cut} further line(s) omitted._" if cut else ""
    return "## Graphics options\n\n```lua\n" + "\n".join(lines) + f"\n```{note}"


def _log(environment: Environment, log: DcsLog | None, redactor: Redactor) -> str:
    if log is None:
        return f"## dcs.log\n\n{_no_log_reason(environment)}"
    blocks = [f"## dcs.log\n\n`{redactor.path(log.path)}`"]
    for excerpt in log.excerpts:
        body = "\n".join(redactor.scrub(line) for line in excerpt.lines)
        # Faults are deduplicated before they are capped, so what was dropped
        # is distinct faults, not lines — several thousand lines can hide
        # behind a handful of them.
        note = f"\n\n_{excerpt.omitted} further distinct entries omitted._"
        blocks.append(f"### {excerpt.title}\n\n```\n{body}\n```{note if excerpt.omitted else ''}")
    return "\n\n".join(blocks)


def _no_log_reason(environment: Environment) -> str:
    """Why there is no log — which is not always "DCS never ran"."""
    if environment.targeted is None and environment.installs:
        return "No install targeted, so no dcs.log was read. Re-run with `--install ID`."
    return "No dcs.log found — DCS has not written one for this install yet."


def _clamp(text: str) -> str:
    if len(text) <= MAX_BUNDLE_CHARS:
        return text
    cut = text[:MAX_BUNDLE_CHARS]
    # Close the fence only if the cut landed inside one; appending it blindly
    # would open a block that swallows the truncation note.
    fence = "\n```" if cut.count("```") % 2 else ""
    return f"{cut}{fence}\n\n_Bundle truncated at {MAX_BUNDLE_CHARS} chars._\n"
