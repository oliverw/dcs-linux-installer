# ADR-0007: An install is its game directory

**Status:** accepted

## Context

Most people who want this tool already have DCS, installed through Lutris,
Heroic or Steam, and will not re-download 150 GB. So discovery has to adopt
installs this tool did not create, several at once, and later commands need a
way to say *which* one they mean.

Every obvious handle is wrong in a way that costs the user data:

- The **prefix** is disposable and rebuilt freely (ADR-0001). An id derived
  from it changes on the repair the whole design rests on.
- The **launcher** is not stable either — adopting a Lutris install into this
  tool would silently rename it, and two launchers routinely point at the same
  directory (`~/.steam/root` and `~/.local/share/Steam` are the same place).
- An **index** ("install 2") is not stable across runs at all.

## Decision

An install *is* its game directory, with symlinks followed. The identifier
`--install` accepts is a truncated SHA-256 of that resolved path, and the same
path is the dedup key when two searches report one install — first source
wins, ours first.

Following symlinks is not a detail. On this machine `~/.steam/root` and
`~/.steam/steam` are both links to `~/.local/share/Steam`, and all three are
searched, so comparing the spellings reports one physical Steam install three
times under three different ids.

Discovery never writes. It reads other programs' configuration files while
those programs may be running, and it must be safe to run at any time.

Where a fact cannot be read, it is reported as unknown rather than guessed.
Edition is the case that matters: Steam's app manifest is proof, and
`bin/DCS_updater.exe` is the only other static signal — *unverified*, since no
Steam copy of DCS has been inspected. Edition decides whether `install` may
hand off to the updater at all, so a third signal is worth finding.

The same rule sank the obvious source for the Proton build behind a Steam
prefix. `compatdata/<appid>/version` looks like the answer but holds a bare
`11.0-100` for official Proton; only GE writes its build name there. The name
comes from line 2 of `config_info`, which is a path through the build's own
directory. Checked against seven prefixes on the development machine.

## Consequences

- The id survives a prefix wipe, a change of launcher, and re-running
  discovery — it changes only if the user moves the game, which is genuinely a
  different install to the tool.
- Two installs sharing a game directory are impossible by construction.
- A launcher's config being missing, half-written or unparseable yields "this
  launcher has no DCS", never an exception, so one broken config cannot hide
  the installs the other launchers found.
- `check` reports on one install at a time. With several found and none named,
  the install-dependent rows are skipped rather than reporting on an arbitrary
  choice.
