"""dcs-linux command-line interface."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import typer

from dcs_linux import __version__
from dcs_linux.checks import blocking_preflight, has_blocking_failure, run_checks
from dcs_linux.dcslog import read_log
from dcs_linux.diagnostics import bundle
from dcs_linux.fetcher import RealFetcher
from dcs_linux.installs import AmbiguousInstall, DcsInstall, InstallNotFound
from dcs_linux.output import OutputOptions, console_for, emit_stub, output_options
from dcs_linux.patches import (
    MULTIPLAYER_WARNING,
    REGISTRY,
    Outcome,
    Patch,
    apply_patch,
    by_id,
    clear_shader_cache,
    revert_patch,
    safe_patches,
)
from dcs_linux.paths import Layout, normalise, resolve_layout
from dcs_linux.prefix import build, resolve_verbs
from dcs_linux.probes import Environment, patch_store_for, probe
from dcs_linux.redaction import redactor_for
from dcs_linux.report import (
    as_json_payload,
    cleared_json,
    install_json,
    outcomes_json,
    patches_json,
    render_cleared,
    render_installs,
    render_outcomes,
    render_patches,
    render_steps,
    render_table,
)
from dcs_linux.runner import RealRunner
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


GAME_DIR_OPTION = typer.Option(
    None,
    "--game-dir",
    metavar="PATH",
    help="Where DCS itself goes. Outside the prefix, on any drive. "
    "Defaults to the game directory in the layout (see DCS_LINUX_ROOT).",
)

VERB_OPTION = typer.Option(
    [], "--verb", help="An extra winetricks verb, on top of the defaults. Repeatable."
)


@app.command()
def install(
    ctx: typer.Context,
    game_dir: Path | None = GAME_DIR_OPTION,
    verb: list[str] = VERB_OPTION,
    rebuild: bool = typer.Option(
        False,
        "--rebuild",
        help="Delete the prefix and build it again. The game directory and saved games "
        "are untouched — that is what makes this the standard repair.",
    ),
) -> None:
    """Build the runtime DCS needs: toolchain, prefix, winetricks, mapping.

    Stops short of DCS itself, which the updater installs (handled separately).
    Safe to re-run: an up-to-date prefix is left alone, and the mapping that
    keeps the game and the login outside it is re-asserted every time.
    """
    options = output_options(ctx)
    system = RealSystem()
    layout = _install_layout(system, game_dir)

    verbs = resolve_verbs(verb)
    if verbs.refusal is not None:
        typer.echo(verbs.refusal, err=True)
        raise typer.Exit(code=2)

    _require_preflight(system, layout)
    result = build(
        system,
        RealWriter(),
        RealRunner(),
        RealFetcher(),
        layout,
        verbs=verbs.verbs,
        rebuild=rebuild,
    )

    if options.json_output:
        typer.echo(json.dumps(install_json(result), indent=2))
    else:
        render_steps(console_for(options), result)

    if not result.ok:
        raise typer.Exit(code=1)


def _install_layout(system: System, game_dir: Path | None) -> Layout:
    """This machine's layout, with the game directory the user chose.

    Normalised on the way in, because the path is later compared against the
    prefix to decide whether `--rebuild` would destroy the download.
    """
    layout = resolve_layout(system)
    return replace(layout, game_dir=normalise(system, game_dir)) if game_dir else layout


def _require_preflight(system: System, layout: Layout) -> None:
    """Refuse to build on a machine `check` says cannot run DCS.

    Only the failures an install cannot itself remove count here — no GPU, a
    missing tool, a full disk, a game directory inside the prefix. Everything
    else `check` calls blocking on a fresh machine is what this command is
    about to fix.
    """
    environment = probe(system, layout=layout)
    blocking = blocking_preflight(run_checks(environment))
    if not blocking:
        return
    for result in blocking:
        typer.echo(f"{result.name}: {result.detail}", err=True)
        if result.remediation:
            typer.echo(f"  → {result.remediation}", err=True)
    typer.echo("run `dcs-linux check` for the full picture", err=True)
    raise typer.Exit(code=1)


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
    # `--list` is the documented spelling; a bare `patch` does the same thing
    # because listing is the only safe default for a command that can write.
    del list_patches
    options = output_options(ctx)
    environment = _probe(RealSystem(), install)

    if options.json_output:
        _emit_patch_json("list", patches_json(environment.patches))
        return
    render_patches(console_for(options), environment.patches)


def _emit_patch_json(action: str, patches: list[dict[str, Any]]) -> None:
    """The one shape every `patch` subcommand emits under --json."""
    typer.echo(json.dumps({"command": "patch", "action": action, "patches": patches}, indent=2))


@patch_app.command("apply")
def patch_apply(
    ctx: typer.Context,
    patch_id: str | None = PATCH_ARGUMENT,
    install: str | None = INSTALL_OPTION,
    allow_ic_risk: bool = typer.Option(
        False,
        "--allow-ic-risk",
        help="Permit a patch that edits a hashed game file. Costs multiplayer access.",
    ),
) -> None:
    """Apply the patches, or put back the ones a DCS update undid.

    Applying an already-applied patch is a no-op, so this is the one command
    to run after every DCS update without having to know what it broke.
    """
    chosen = _to_apply(patch_id, allow_ic_risk=allow_ic_risk)
    system, writer = RealSystem(), RealWriter()
    environment = _probe(system, install)
    targeted = _targeted(environment)
    store = patch_store_for(environment.layout, targeted)
    _emit_outcomes(
        ctx,
        "apply",
        [
            apply_patch(system, writer, store, patch, environment.paths, targeted.version)
            for patch in chosen
        ],
    )


@patch_app.command("clear-shader-cache")
def patch_clear_shader_cache(
    ctx: typer.Context,
    install: str | None = INSTALL_OPTION,
) -> None:
    """Delete DCS's compiled shaders so the next launch rebuilds them.

    Maintenance rather than a patch: it writes nothing, needs no backup and
    cannot be reverted because there is nothing to put back — DCS regenerates
    the caches. Always integrity-check safe, so it takes no opt-in flag.
    """
    options = output_options(ctx)
    system, writer = RealSystem(), RealWriter()
    environment = _probe(system, install)
    _targeted(environment)
    cleared = clear_shader_cache(system, writer, environment.paths)

    if options.json_output:
        payload = {"command": "patch", "action": "clear-shader-cache", **cleared_json(cleared)}
        typer.echo(json.dumps(payload, indent=2))
        return
    render_cleared(console_for(options), cleared)


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
    store = patch_store_for(environment.layout, targeted)
    _emit_outcomes(
        ctx,
        "revert",
        [revert_patch(system, writer, store, patch) for patch in _to_revert(patch_id)],
    )


def _named(patch_id: str) -> Patch:
    """The patch with this id, or a usage error listing the ones there are."""
    patch = by_id(patch_id)
    if patch is None:
        known = ", ".join(known.id for known in REGISTRY)
        typer.echo(f"no patch called {patch_id!r}; known patches: {known}", err=True)
        raise typer.Exit(code=2)
    return patch


def _to_apply(patch_id: str | None, *, allow_ic_risk: bool) -> tuple[Patch, ...]:
    """The patches a bare or named `apply` may write (ADR-0004).

    An unnamed apply takes the IC-safe patches and only those — `--allow-ic-risk`
    does not widen it. The flag consents to *one patch the user named*, not to
    whatever the registry grows later, and a sweep is exactly how somebody
    loses multiplayer without having chosen to.
    """
    if patch_id is None:
        return safe_patches()
    patch = _named(patch_id)
    _confirm_ic_risk(patch, allow_ic_risk)
    return (patch,)


def _to_revert(patch_id: str | None) -> tuple[Patch, ...]:
    """The patches `revert` may undo — all of them, risky included.

    Reverting needs no gate: undoing a risky patch is what gives multiplayer
    back, so refusing to sweep here would strand the user in the very state
    the gate exists to protect them from.
    """
    return (_named(patch_id),) if patch_id else REGISTRY


def _confirm_ic_risk(patch: Patch, accepted: bool) -> None:
    """Warn about a hashed-file edit, and refuse one not consented to.

    ADR-0004 asks for opt-in *and* a multiplayer warning, so the warning is
    printed on both paths. Printing it only on the refusal would mean the one
    user who actually ends up with a modified install — the one who passed the
    flag — is the only one never told what it costs.
    """
    if not patch.ic_risk:
        return
    typer.echo(f"{patch.id} {MULTIPLAYER_WARNING}", err=True)
    if not accepted:
        typer.echo("re-run with --allow-ic-risk if that is what you want", err=True)
        raise typer.Exit(code=2)


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
