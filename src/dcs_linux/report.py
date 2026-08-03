"""Rendering for the `check` report: a scannable table, or JSON."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text

from dcs_linux.checks import CheckResult, Status
from dcs_linux.probes import Environment

_MARKER = {
    Status.PASS: ("ok", "green"),
    Status.WARN: ("warn", "yellow"),
    Status.FAIL: ("FAIL", "red"),
    Status.SKIP: ("skip", "dim"),
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
    }
