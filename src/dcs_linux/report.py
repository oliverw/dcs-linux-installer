"""Rendering for the `check` report: a scannable table, or JSON."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text

from dcs_linux.checks import CheckResult, Status
from dcs_linux.installs import DcsInstall, Edition
from dcs_linux.probes import Environment

_MARKER = {
    Status.PASS: ("ok", "green"),
    Status.WARN: ("warn", "yellow"),
    Status.FAIL: ("FAIL", "red"),
    Status.SKIP: ("skip", "dim"),
}

_EDITION = {
    Edition.STANDALONE: "Standalone",
    Edition.STEAM: "Steam",
    Edition.UNKNOWN: "unknown",
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
            f"{_EDITION[install.edition]} edition",
            f"DCS {install.version}" if install.version else "DCS version unknown",
            install.runtime or "runtime unknown",
        )
    )
    cell = Text(facts)
    cell.append(f"\ngame:   {install.game}", style="dim")
    cell.append(f"\nprefix: {install.prefix or 'unknown'}", style="dim")
    return cell


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
