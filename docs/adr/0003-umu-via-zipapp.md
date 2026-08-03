# ADR-0003: Install umu-launcher as a zipapp

**Status:** accepted — verified on hardware (#2)

## Context

The tool must not assume `umu-run` or `winetricks` on PATH, and must work on
immutable distros without root.

## Decision

Fetch the upstream **zipapp** (`umu-launcher-<ver>-zipapp.tar`) into the
toolchain directory and invoke it by path.

## Consequences

- **`umu-launcher` is not on PyPI.** `pip install` / `uv tool install` cannot
  work — this was discovered only by running it.
- Distro packages exist (including an official Fedora RPM) but need root.
- winetricks needs no separate install: umu takes a `winetricks` positional,
  so `umu-run winetricks <verbs>` applies verbs to the prefix it manages.
  This closes #2's stated "known gap".
- `umu-run ""` creates a prefix and then **exits 1** trying to ShellExecute the
  empty string. The exit code is not a success signal — check for
  `prefix/system.reg`.
- GE-Proton releases ship an aarch64 build alongside x86_64. Match the
  **unsuffixed** asset name; taking "the first `.tar.gz`" lands the wrong
  architecture silently.
- `GAMEID=umu-223750` is recognised as "DCS World" but applies no protonfix.
  Harmless, but do not expect it to fix anything.
- umu **refuses to run as root**, and mapping to a non-root uid inside a user
  namespace loses access to the user's own files. Sandboxing umu in a userns
  is a dead end.
