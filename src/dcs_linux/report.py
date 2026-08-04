"""Rendering for the `check` report: a scannable table, or JSON."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text

from dcs_linux.checks import CheckResult, Status
from dcs_linux.installs import EDITION_LABELS, DcsInstall
from dcs_linux.patches import (
    SERVERS_REJECT,
    Cleared,
    Outcome,
    PatchState,
    PatchStatus,
    risky_in_place,
)
from dcs_linux.prefix import BuildResult, Step, StepStatus
from dcs_linux.probes import Environment
from dcs_linux.updater import HandoffResult
from dcs_linux.verify import Finding, Verification, findings_json

_MARKER = {
    Status.PASS: ("ok", "green"),
    Status.WARN: ("warn", "yellow"),
    Status.FAIL: ("FAIL", "red"),
    Status.SKIP: ("skip", "dim"),
}

_PATCH_MARKER = {
    PatchStatus.APPLIED: ("applied", "green"),
    PatchStatus.DRIFTED: ("DRIFTED", "red"),
    PatchStatus.NOT_APPLIED: ("not applied", "dim"),
    PatchStatus.UNKNOWN: ("unknown", "dim"),
}


def render_table(console: Console, results: list[CheckResult]) -> None:
    """One row per check, with the fix for every failure alongside it."""
    table = Table(show_header=True, header_style="bold", expand=False)
    table.add_column("")
    table.add_column("Check")
    table.add_column("Result")

    for result in results:
        label, style = _MARKER[result.status]
        detail = Text(result.detail)
        if result.remediation:
            detail.append(f"\n→ {result.remediation}", style="cyan")
        table.add_row(Text(label, style=style), result.name, detail)

    console.print(table)

    failures = [result for result in results if result.is_blocking]
    if failures:
        console.print(
            f"\n[red]{len(failures)} blocking problem"
            f"{'s' if len(failures) > 1 else ''}[/red] — fix the arrows above, "
            "then run [bold]dcs-linux check[/bold] again."
        )
    else:
        console.print("\n[green]No blocking problems.[/green]")


def render_installs(console: Console, environment: Environment) -> None:
    """Every DCS install found, with the id other commands take to target one."""
    installs = environment.installs
    if not installs:
        console.print(
            "\nNo DCS install found. "
            "[bold]dcs-linux install[/bold] creates one; existing Lutris, Heroic and "
            "Steam installs are adopted automatically."
        )
        return

    table = Table(show_header=True, header_style="bold", expand=False)
    table.add_column("")
    table.add_column("ID")
    # Paths are the point of this table, so they get a column of their own and
    # fold rather than being truncated away in a narrow terminal.
    table.add_column("Install", overflow="fold")

    for install in installs:
        targeted = install == environment.targeted
        table.add_row(
            Text("→" if targeted else "", style="cyan"),
            Text(install.install_id, style="bold" if targeted else ""),
            _install_cell(install),
        )

    console.print(f"\n[bold]{len(installs)} DCS install{'s' if len(installs) > 1 else ''}[/bold]")
    console.print(table)
    if environment.targeted is None:
        console.print(
            "[yellow]No install targeted[/yellow] — the rows above that describe an "
            "install were skipped. Pass [bold]--install ID[/bold] to choose one."
        )


def _install_cell(install: DcsInstall) -> Text:
    """What the install is, then where it is."""
    facts = " · ".join(
        (
            str(install.launcher),
            f"{EDITION_LABELS[install.edition]} edition",
            f"DCS {install.version}" if install.version else "DCS version unknown",
            install.runtime or "runtime unknown",
        )
    )
    cell = Text(facts)
    cell.append(f"\ngame:   {install.game}", style="dim")
    cell.append(f"\nprefix: {install.prefix or 'unknown'}", style="dim")
    return cell


def render_patches(console: Console, states: tuple[PatchState, ...]) -> None:
    """Every patch, whether it is in place, and what it would risk."""
    table = Table(show_header=True, header_style="bold", expand=False)
    table.add_column("")
    table.add_column("Patch")
    table.add_column("IC risk")
    table.add_column("Detail", overflow="fold")

    for state in states:
        label, style = _PATCH_MARKER[state.status]
        risk = (
            Text("⚠ IC-risky", style="bold yellow")
            if state.patch.ic_risk
            else Text("IC-safe", style="dim")
        )
        table.add_row(Text(label, style=style), state.patch.id, risk, state.patch.summary)

    console.print(table)

    risky = [state for state in states if state.patch.ic_risk]
    if risky:
        console.print(
            f"\n[yellow]⚠ {len(risky)} patch(es) edit files DCS hashes[/yellow] — apply one and "
            f"{SERVERS_REJECT}. They are never applied unless named together with "
            "[bold]--allow-ic-risk[/bold], and reverting one gives multiplayer back."
        )

    in_place = risky_in_place(states)
    if in_place:
        console.print(
            f"[red]This install is currently modified[/red] by "
            f"{', '.join(state.patch.id for state in in_place)}."
        )

    drifted = [state for state in states if state.is_drifted]
    if drifted:
        console.print(
            f"\n[red]{len(drifted)} patch(es) undone by a DCS update[/red] — "
            "run [bold]dcs-linux patch apply[/bold] to put them back."
        )
    if any(state.status is PatchStatus.UNKNOWN for state in states):
        console.print(
            "\n[yellow]No install targeted[/yellow], so no patch could be inspected. "
            "Pass [bold]--install ID[/bold] to choose one."
        )


def render_outcomes(console: Console, outcomes: list[Outcome]) -> None:
    """What apply or revert did to each patch, and what it refused to do."""
    for outcome in outcomes:
        if not outcome.ok:
            console.print(f"[red]FAIL[/red] {outcome.patch.id}: {outcome.detail}")
        elif outcome.changed:
            console.print(f"[green]ok[/green]   {outcome.patch.id}: {outcome.detail}")
        else:
            console.print(f"[dim]skip[/dim] {outcome.patch.id}: {outcome.detail}")


_STEP_MARKER = {
    StepStatus.DONE: ("ok", "green"),
    StepStatus.SKIPPED: ("skip", "dim"),
    StepStatus.FAILED: ("FAIL", "red"),
}


def _render_step_lines(console: Console, steps: Sequence[Step]) -> None:
    """One line per step. Both phases of `install` report in this shape."""
    for step in steps:
        label, style = _STEP_MARKER[step.status]
        console.print(f"[{style}]{label:<4}[/{style}] {step.name}: {step.detail}")


def render_steps(console: Console, result: BuildResult, *, dcs_next: bool = False) -> None:
    """What the install did, one line per step, then what to do next.

    `dcs_next` says the handoff to the updater follows in the same run, so the
    closing line is left to `render_handoff` — telling the user DCS is not
    installed immediately before installing it would read as a failure.
    """
    _render_step_lines(console, result.steps)

    if not result.ok:
        console.print("\n[red]The install stopped.[/red] Fix the failure above and run it again.")
        return
    if result.runtime is None or dcs_next:
        return
    console.print(
        f"\n[green]The runtime is ready.[/green] DCS itself is not installed yet — "
        f"it goes in [bold]{result.runtime.game}[/bold], mapped into the prefix as D:."
    )


def render_handoff(console: Console, result: HandoffResult) -> None:
    """What the updater handoff did, and what the user can do now.

    Deliberately ends on the patches: they are the difference between an
    install that starts and one that flies, and nothing else on screen says
    they exist.
    """
    _render_step_lines(console, result.steps)

    if not result.ok:
        console.print(
            "\n[red]DCS is not installed yet.[/red] Nothing is lost — "
            "run [bold]dcs-linux install[/bold] again to carry on."
        )
        return
    console.print(
        "\n[green]DCS is installed.[/green] Apply the Linux fixes with "
        "[bold]dcs-linux patch apply[/bold] — [bold]dcs-linux patch[/bold] lists them "
        "first, and the ones that cost multiplayer are never applied unless you name "
        "them. Re-run it after every DCS update."
    )


def install_json(result: BuildResult, handoff: HandoffResult | None = None) -> dict[str, Any]:
    """The same install, machine-readable.

    The prefix build and the DCS handoff are one command and one payload, but
    two objects: `dcs` is null when the handoff was not reached or not asked
    for, which is different from a handoff that ran and failed.
    """
    return {
        "command": "install",
        "ok": result.ok and (handoff is None or handoff.ok),
        "steps": [_step_json(step) for step in result.steps],
        "runtime": result.runtime.as_json() if result.runtime else None,
        "dcs": handoff_json(handoff) if handoff is not None else None,
    }


def handoff_json(result: HandoffResult) -> dict[str, Any]:
    """What the handoff did, for `--json`."""
    return {
        "ok": result.ok,
        "steps": [_step_json(step) for step in result.steps],
        "stage": result.progress.stage.value,
        "game": str(result.progress.game_root) if result.progress.game_root else None,
        "version": result.progress.version,
        "installer_sha256": result.installer.sha256 if result.installer else None,
    }


def _step_json(step: Step) -> dict[str, Any]:
    return {"name": step.name, "status": step.status.value, "detail": step.detail}


def render_findings(console: Console, result: Verification) -> None:
    """What the run was judged on, one row per finding, fix alongside.

    The same shape as `render_table`, on purpose: `check` and `verify` answer
    "should this work" and "did it work", and a user reading both should not
    have to learn two layouts to do it.
    """
    table = Table(show_header=True, header_style="bold", expand=False)
    table.add_column("")
    table.add_column("Finding")
    table.add_column("Result", overflow="fold")

    for finding in result.findings:
        label, style = _MARKER[finding.status]
        detail = Text(finding.detail)
        if finding.remediation:
            detail.append(f"\n→ {finding.remediation}", style="cyan")
        table.add_row(Text(label, style=style), finding.name, detail)

    console.print(table)

    failures = [finding for finding in result.findings if finding.failed]
    if not failures:
        console.print(
            "\n[green]DCS is working.[/green] Nothing in this run looks broken — "
            "re-run [bold]dcs-linux verify[/bold] after every DCS update."
        )
        return
    console.print(
        f"\n[red]{len(failures)} problem{'s' if len(failures) > 1 else ''}[/red] — "
        "fix the arrows above, then verify again."
    )
    _render_patch_hint(console, failures)


def _render_patch_hint(console: Console, failures: Sequence[Finding]) -> None:
    """Say when the fix is one command, because most of the time it is."""
    patches = [finding.patch for finding in failures if finding.patch]
    if not patches:
        return
    console.print(
        f"[cyan]{len(patches)} of these {'are' if len(patches) > 1 else 'is'} fixed by a "
        f"patch this tool ships[/cyan]: [bold]dcs-linux patch apply "
        f"{patches[0]}[/bold]"
    )


def verify_json(result: Verification) -> dict[str, Any]:
    """The same verification, machine-readable."""
    return {
        "command": "verify",
        "ok": result.ok,
        "log": str(result.log_path) if result.log_path else None,
        "findings": findings_json(result.findings),
    }


def render_cleared(console: Console, cleared: Cleared) -> None:
    """What the shader-cache clear deleted."""
    for directory in cleared.directories:
        console.print(f"[dim]removed[/dim] {directory}")
    console.print(cleared.detail)


def cleared_json(cleared: Cleared) -> dict[str, Any]:
    """What the shader-cache clear deleted, for `--json`."""
    return {
        "directories": [str(directory) for directory in cleared.directories],
        "detail": cleared.detail,
    }


def patches_json(states: tuple[PatchState, ...]) -> list[dict[str, Any]]:
    """Patch standings for `--json`. `id` is what `patch apply` takes."""
    return [
        {
            "id": state.patch.id,
            "summary": state.patch.summary,
            "ic_risk": state.patch.ic_risk,
            "status": state.status.value,
            "detail": state.detail,
        }
        for state in states
    ]


def outcomes_json(outcomes: list[Outcome]) -> list[dict[str, Any]]:
    """What apply or revert did, for `--json`.

    `changed` separates a real application from an already-applied no-op.
    """
    return [
        {
            "id": outcome.patch.id,
            "ok": outcome.ok,
            "changed": outcome.changed,
            "detail": outcome.detail,
        }
        for outcome in outcomes
    ]


def as_json_payload(environment: Environment, results: list[CheckResult]) -> dict[str, Any]:
    """The same results, machine-readable."""
    distro = environment.distro
    return {
        "command": "check",
        "ok": not any(result.is_blocking for result in results),
        "distro": {
            "id": distro.id,
            "name": distro.name,
            "version": distro.version,
            "family": distro.family.value,
            "immutability": distro.immutability.value,
            "immutable": distro.is_immutable,
        },
        "checks": [
            {
                "key": result.key,
                "name": result.name,
                "status": result.status.value,
                "detail": result.detail,
                "remediation": result.remediation,
            }
            for result in results
        ],
        "installs": [
            {
                "id": install.install_id,
                "launcher": install.launcher.value,
                "edition": install.edition.value,
                "version": install.version,
                "runtime": install.runtime,
                "game": str(install.game),
                "prefix": str(install.prefix) if install.prefix else None,
                "targeted": install == environment.targeted,
            }
            for install in environment.installs
        ],
    }
