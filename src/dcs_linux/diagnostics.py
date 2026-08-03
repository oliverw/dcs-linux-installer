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
from dcs_linux.installs import DcsInstall, Edition
from dcs_linux.probes import Environment
from dcs_linux.redaction import Redactor
from dcs_linux.system import DiskUsage

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

_EDITION = {
    Edition.STANDALONE: "Standalone",
    Edition.STEAM: "Steam",
    Edition.UNKNOWN: "unknown",
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
        _installs(environment, redactor),
        _graphics(environment, redactor),
        _log(log, redactor),
    ]
    return _clamp("\n\n".join(parts) + "\n")


def cell(text: str) -> str:
    """One table cell: pipes escaped, line breaks folded into the row.

    Remediations are shell commands and paths, so both happen in practice and
    either one silently wrecks a markdown table.
    """
    return text.replace("|", r"\|").replace("\n", "<br>")


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
    rows = "\n".join(f"| {name} | {cell(value)} |" for name, value in facts)
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
    detail = cell(redactor.scrub(result.detail))
    fix = cell(redactor.scrub(result.remediation)) if result.remediation else ""
    return f"| {_MARKER[result.status]} | {result.name} | {detail} | {fix} |"


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
    return f"## Installs\n\n{header}\n{rows}"


def _install_row(install: DcsInstall, redactor: Redactor, *, targeted: bool) -> str:
    fields = (
        "->" if targeted else "",
        install.install_id,
        str(install.launcher),
        _EDITION[install.edition],
        install.version or "unknown",
        install.runtime or "unknown",
        redactor.path(install.game),
        redactor.path(install.prefix),
    )
    return "| " + " | ".join(cell(field) for field in fields) + " |"


def _graphics(environment: Environment, redactor: Redactor) -> str:
    """The graphics table from options.lua — where the DLSS flicker hides."""
    options = environment.install_state.graphics_options
    if options is None:
        return "## Graphics options\n\nNo options.lua yet."
    lines = redactor.scrub(options).splitlines()[:MAX_GRAPHICS_LINES]
    return "## Graphics options\n\n```lua\n" + "\n".join(lines) + "\n```"


def _log(log: DcsLog | None, redactor: Redactor) -> str:
    if log is None:
        return "## dcs.log\n\nNo dcs.log found — DCS has not written one for this install yet."
    blocks = [f"## dcs.log\n\n`{redactor.path(log.path)}`"]
    for excerpt in log.excerpts:
        body = "\n".join(redactor.scrub(line) for line in excerpt.lines)
        note = f"\n\n_{excerpt.omitted} further line(s) omitted._" if excerpt.omitted else ""
        blocks.append(f"### {excerpt.title}\n\n```\n{body}\n```{note}")
    return "\n\n".join(blocks)


def _clamp(text: str) -> str:
    if len(text) <= MAX_BUNDLE_CHARS:
        return text
    # Closing the fence keeps the truncation note readable rather than
    # swallowed by whatever code block the cut landed in.
    return text[:MAX_BUNDLE_CHARS] + f"\n```\n\n_Bundle truncated at {MAX_BUNDLE_CHARS} chars._\n"
