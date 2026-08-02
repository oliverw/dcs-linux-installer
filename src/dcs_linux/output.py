"""Output formatting shared by every command."""

from __future__ import annotations

import json
from dataclasses import dataclass

import typer
from rich.console import Console


@dataclass(frozen=True)
class OutputOptions:
    """Global output flags, threaded through the Typer context."""

    json_output: bool
    no_color: bool


def emit_stub(ctx: typer.Context) -> None:
    """Report that the invoked subcommand exists but does nothing yet."""
    assert isinstance(ctx.obj, OutputOptions)
    options = ctx.obj
    assert ctx.command.name is not None
    command = ctx.command.name

    if options.json_output:
        typer.echo(json.dumps({"command": command, "status": "not_implemented"}))
        return

    # Passing no_color=None (rather than False) lets Rich fall back to the
    # NO_COLOR env var when the --no-color flag itself wasn't given.
    console = Console(no_color=options.no_color or None, highlight=False)
    console.print(f"[yellow]dcs-linux {command}[/yellow] is not implemented yet.")
