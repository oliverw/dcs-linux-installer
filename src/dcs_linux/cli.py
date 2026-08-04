"""dcs-linux command-line interface."""

from __future__ import annotations

import json

import typer

from dcs_linux import __version__
from dcs_linux.checks import has_blocking_failure, run_checks
from dcs_linux.dcslog import read_log
from dcs_linux.diagnostics import bundle
from dcs_linux.installs import AmbiguousInstall, DcsInstall, InstallNotFound
from dcs_linux.output import OutputOptions, console_for, emit_stub, output_options
from dcs_linux.patches import REGISTRY, Outcome, Patch, apply_patch, by_id, revert_patch
from dcs_linux.probes import Environment, patch_store, probe
from dcs_linux.redaction import redactor_for
from dcs_linux.report import (
    as_json_payload,
    outcomes_json,
    patches_json,
    render_installs,
    render_outcomes,
    render_patches,
    render_table,
)
from dcs_linux.system import RealSystem, System
from dcs_linux.writer import RealWriter

app = typer.Typer(
    name="dcs-linux",
    help="Install DCS World Standalone on Linux, and keep it working.",
    no_args_is_help=True,
    add_completion=False,
)

INSTALL_OPTION = typer.Option(
    None,
    "--install",
    metavar="ID",
    help="Act on one discovered install, by id or game path.",
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
def check(ctx: typer.Context, install: str | None = INSTALL_OPTION) -> None:
    """Check whether this machine is ready to run DCS, and list the installs found."""
    options = output_options(ctx)
    environment = _probe(RealSystem(), install)
    results = run_checks(environment)

    if options.json_output:
        typer.echo(json.dumps(as_json_payload(environment, results), indent=2))
    else:
        console = console_for(options)
        render_table(console, results)
        render_installs(console, environment)

    if has_blocking_failure(results):
        raise typer.Exit(code=1)


def _probe(system: System, install: str | None) -> Environment:
    """The probed machine, turning a bad `--install` into a usage error."""
    try:
        return probe(system, install)
    except AmbiguousInstall:
        raise _bad_install(f"{install!r} matches more than one install") from None
    except InstallNotFound:
        raise _bad_install(f"no install matches {install!r}") from None


def _bad_install(message: str) -> typer.Exit:
    """A bad --install is the user's mistake, not a failed check (exit 2)."""
    typer.echo(f"{message}; run `dcs-linux check` to list them", err=True)
    return typer.Exit(code=2)


@app.command()
def install(ctx: typer.Context) -> None:
    """Build the prefix, then hand off to the DCS updater."""
    emit_stub(ctx)


patch_app = typer.Typer(
    help="Apply the Linux fixes (integrity-check-safe ones by default).",
    invoke_without_command=True,
    add_completion=False,
)
app.add_typer(patch_app, name="patch")

PATCH_ARGUMENT = typer.Argument(
    None,
    metavar="[PATCH]",
    help="One patch by id. Every applicable patch if omitted.",
)


@patch_app.callback(invoke_without_command=True)
def patch_main(
    ctx: typer.Context,
    list_patches: bool = typer.Option(
        False, "--list", help="Show the patches and whether each is applied."
    ),
    install: str | None = INSTALL_OPTION,
) -> None:
    """Apply the Linux fixes (integrity-check-safe ones by default).

    With no subcommand this lists the patches, so that `patch` on its own is
    the safe, read-only thing — nothing here changes an install unless the
    user asked for `apply` or `revert` by name.
    """
    if ctx.invoked_subcommand is not None:
        return
    del list_patches  # Listing is what a bare `patch` does either way.
    _list_patches(ctx, install)


@patch_app.command("list")
def patch_list(ctx: typer.Context, install: str | None = INSTALL_OPTION) -> None:
    """Show the patches and whether each is currently applied."""
    _list_patches(ctx, install)


def _list_patches(ctx: typer.Context, install: str | None) -> None:
    options = output_options(ctx)
    environment = _probe(RealSystem(), install)

    if options.json_output:
        payload = {
            "command": "patch",
            "action": "list",
            "patches": patches_json(environment.patches),
        }
        typer.echo(json.dumps(payload, indent=2))
        return
    render_patches(console_for(options), environment.patches)


@patch_app.command("apply")
def patch_apply(
    ctx: typer.Context,
    patch_id: str | None = PATCH_ARGUMENT,
    install: str | None = INSTALL_OPTION,
) -> None:
    """Apply the patches, or put back the ones a DCS update undid.

    Applying an already-applied patch is a no-op, so this is the one command
    to run after every DCS update without having to know what it broke.
    """
    system, writer = RealSystem(), RealWriter()
    environment = _probe(system, install)
    targeted = _targeted(environment)
    store = patch_store(environment.layout, targeted)
    _emit_outcomes(
        ctx,
        "apply",
        [
            apply_patch(system, writer, store, patch, environment.paths, targeted.version)
            for patch in _chosen(patch_id)
        ],
    )


@patch_app.command("revert")
def patch_revert(
    ctx: typer.Context,
    patch_id: str | None = PATCH_ARGUMENT,
    install: str | None = INSTALL_OPTION,
) -> None:
    """Put every file a patch touched back exactly as it was."""
    system, writer = RealSystem(), RealWriter()
    environment = _probe(system, install)
    targeted = _targeted(environment)
    store = patch_store(environment.layout, targeted)
    _emit_outcomes(
        ctx,
        "revert",
        [revert_patch(system, writer, store, patch) for patch in _chosen(patch_id)],
    )


def _chosen(patch_id: str | None) -> tuple[Patch, ...]:
    """The patches named, or all of them."""
    if patch_id is None:
        return REGISTRY
    patch = by_id(patch_id)
    if patch is None:
        known = ", ".join(known.id for known in REGISTRY)
        typer.echo(f"no patch called {patch_id!r}; known patches: {known}", err=True)
        raise typer.Exit(code=2)
    return (patch,)


def _targeted(environment: Environment) -> DcsInstall:
    """The install to write to, refusing to guess between several.

    Unlike `check`, this one writes, so an ambiguous target is a usage error
    rather than a skipped row: patching the wrong install is not something a
    user can be expected to notice.
    """
    if environment.targeted is None:
        raise _bad_install(_why_nothing_targeted(environment))
    return environment.targeted


def _why_nothing_targeted(environment: Environment) -> str:
    if not environment.installs:
        return "no DCS install found"
    return f"{len(environment.installs)} installs found; pass --install ID to choose one"


def _emit_outcomes(ctx: typer.Context, action: str, outcomes: list[Outcome]) -> None:
    options = output_options(ctx)
    if options.json_output:
        payload = {"command": "patch", "action": action, "patches": outcomes_json(outcomes)}
        typer.echo(json.dumps(payload, indent=2))
    else:
        render_outcomes(console_for(options), outcomes)

    if any(not outcome.ok for outcome in outcomes):
        raise typer.Exit(code=1)


@app.command()
def verify(ctx: typer.Context) -> None:
    """Launch DCS and confirm it actually works."""
    emit_stub(ctx)


@app.command()
def report(
    ctx: typer.Context,
    install: str | None = INSTALL_OPTION,
    no_redact: bool = typer.Option(
        False,
        "--no-redact",
        help="Keep user names, home paths and addresses. Do not post the result in public.",
    ),
) -> None:
    """Produce a diagnostics bundle for a bug report."""
    options = output_options(ctx)
    system = RealSystem()
    environment = _probe(system, install)
    markdown = bundle(
        environment=environment,
        log=read_log(system, environment.paths),
        redactor=redactor_for(system, enabled=not no_redact),
        version=__version__,
    )

    if options.json_output:
        # One field, not a second machine-readable contract: the bundle is a
        # single artefact meant for pasting, and `check --json` already serves
        # anything that wants the facts structured.
        payload = {"command": "report", "redacted": not no_redact, "markdown": markdown}
        typer.echo(json.dumps(payload))
        return

    # Deliberately not the rich console: this gets pasted verbatim, so nothing
    # may wrap it, colour it or reflow a log line.
    typer.echo(markdown)
    # A broken machine is exactly when report is needed, so blocking failures
    # are the subject of the report, not a reason to exit non-zero.


def run() -> None:
    app()


if __name__ == "__main__":
    run()
