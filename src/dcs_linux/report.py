"""Rendering for the `check` report: a scannable table, or JSON."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text

from dcs_linux.checks import CheckResult, Status
from dcs_linux.installs import EDITION_LABELS, DcsInstall
from dcs_linux.patches import Outcome, PatchState, PatchStatus
from dcs_linux.probes import Environment

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
        risk = Text("⚠ risky", style="yellow") if state.patch.ic_risk else Text("safe", style="dim")
        table.add_row(Text(label, style=style), state.patch.id, risk, state.patch.summary)

    console.print(table)

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
