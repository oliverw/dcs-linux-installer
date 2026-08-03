"""dcs-linux command-line interface."""

from __future__ import annotations

import json

import typer

from dcs_linux import __version__
from dcs_linux.checks import has_blocking_failure, run_checks
from dcs_linux.output import OutputOptions, console_for, emit_stub, output_options
from dcs_linux.probes import probe
from dcs_linux.report import as_json_payload, render_table
from dcs_linux.system import RealSystem

app = typer.Typer(
    name="dcs-linux",
    help="Install DCS World Standalone on Linux, and keep it working.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(show_version: bool) -> None:
    if show_version:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def global_options(
    ctx: typer.Context,
    version: bool | None = typer.Option(
        None,
        "--version",
        help="Show the version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of formatted text."
    ),
    no_color: bool = typer.Option(False, "--no-color", help="Disable coloured output."),
) -> None:
    ctx.obj = OutputOptions(json_output=json_output, no_color=no_color)


@app.command()
def check(ctx: typer.Context) -> None:
    """Check whether this machine is ready to run DCS."""
    options = output_options(ctx)
    environment = probe(RealSystem())
    results = run_checks(environment)

    if options.json_output:
        typer.echo(json.dumps(as_json_payload(environment, results), indent=2))
    else:
        render_table(console_for(options), results)

    if has_blocking_failure(results):
        raise typer.Exit(code=1)


@app.command()
def install(ctx: typer.Context) -> None:
    """Build the prefix, then hand off to the DCS updater."""
    emit_stub(ctx)


@app.command()
def patch(ctx: typer.Context) -> None:
    """Apply the Linux fixes (integrity-check-safe ones by default)."""
    emit_stub(ctx)


@app.command()
def verify(ctx: typer.Context) -> None:
    """Launch DCS and confirm it actually works."""
    emit_stub(ctx)


@app.command()
def report(ctx: typer.Context) -> None:
    """Produce a diagnostics bundle for a bug report."""
    emit_stub(ctx)


def run() -> None:
    app()


if __name__ == "__main__":
    run()
