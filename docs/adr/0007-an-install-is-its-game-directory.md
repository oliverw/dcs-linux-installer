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

An install *is* its game directory. The identifier `--install` accepts is a
truncated SHA-256 of that path, and the game directory is also the dedup key
when two launchers report the same install — first source wins, ours first.

Discovery never writes. It reads other programs' configuration files while
those programs may be running, and it must be safe to run at any time.

Where a fact cannot be read, it is reported as unknown rather than guessed.
Edition is the case that matters: Steam's app manifest and the presence of
`bin/DCS_updater.exe` are trustworthy, nothing else is, and edition decides
whether `install` may hand off to the updater at all.

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
