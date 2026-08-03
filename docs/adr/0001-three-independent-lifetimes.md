# ADR-0001: Prefix, game directory and saved games are three independent lifetimes

**Status:** accepted — verified on hardware (#2)

## Context

A wine prefix is disposable; rebuilding one is the repair every other recovery
path rests on. But by default both the DCS install and `Saved Games/DCS` live
*inside* the prefix, so that repair destroys a 500 GB+ download and the user's
login and keybinds along with it.

## Decision

Three directories, three lifetimes, with the durable two mapped into the
disposable one:

- `prefix/` — wiped and rebuilt freely
- `game/` — mapped as a wine drive letter, `prefix/dosdevices/d:`
- `saved-games/` — symlinked to `drive_c/users/steamuser/Saved Games`

Install to `D:\`, never `C:\`.

## Consequences

- Rebuilding the prefix is cheap and safe. Verified: a 12 GB partial download
  survived a full wipe and the updater resumed rather than restarting.
- The ED login (`Saved Games/DCS/Config/authdata.bin`) survives a rebuild, so
  repair costs no re-login. This is a *consequence of the mapping*, not a
  property of DCS.
- `authdata.bin` is a credential. It must never be committed, and must be
  excluded from the diagnostics bundle (#7).
- Anything that creates a prefix must do the mapping, or it silently sets up
  the user to lose data on the first repair.
