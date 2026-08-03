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


def output_options(ctx: typer.Context) -> OutputOptions:
    """The global output flags for this invocation."""
    assert isinstance(ctx.obj, OutputOptions)
    return ctx.obj


def console_for(options: OutputOptions) -> Console:
    """A console honouring --no-color, and NO_COLOR when the flag is absent."""
    # Passing no_color=None (rather than False) lets Rich fall back to the
    # NO_COLOR env var when the --no-color flag itself wasn't given.
    return Console(no_color=options.no_color or None, highlight=False, soft_wrap=False)


def emit_stub(ctx: typer.Context) -> None:
    """Report that the invoked subcommand exists but does nothing yet."""
    options = output_options(ctx)
    assert ctx.command.name is not None
    command = ctx.command.name

    if options.json_output:
        typer.echo(json.dumps({"command": command, "status": "not_implemented"}))
        return

    console = console_for(options)
    console.print(f"[yellow]dcs-linux {command}[/yellow] is not implemented yet.")
