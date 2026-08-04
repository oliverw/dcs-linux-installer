# Context

Shared vocabulary for this repo. Terms here mean exactly what this file says
they mean — use them, don't drift to synonyms.

Everything below was established empirically on real hardware during #2 (15
runs, Fedora 44 / RTX 4080, DCS 2.9.28.26385). Where a claim is unverified or
holds for only one machine, it says so.

---

## The three lifetimes

The central architectural idea. Three directories with **independent
lifetimes**, and confusing them is the main way this project can lose a user's
data.

| Term | Lifetime | Holds |
| --- | --- | --- |
| **prefix** | disposable — wiped and rebuilt freely | umu + pinned GE-Proton, wine's `drive_c` |
| **game directory** | durable — the expensive thing | the DCS install (536 GB with 33 modules) |
| **saved games** | durable — the irreplaceable thing | ED login, keybinds, config, `Logs/dcs.log` |

"Delete the prefix and rebuild it" is the **repair** that every other recovery
path rests on. It is only safe because the other two live outside the prefix.
By default DCS puts `Saved Games` *inside* the prefix, so an unmapped install
loses the user's login and keybinds on every repair.

- **mapping** — making a durable directory visible inside the disposable
  prefix. The game directory is mapped as a **drive letter**
  (`prefix/dosdevices/d:` → `game/`); saved games is symlinked into the user
  profile (`drive_c/users/steamuser/Saved Games`).
- **auth state** — `Saved Games/DCS/Config/authdata.bin`. A **credential**:
  never commit it, never include it in a diagnostics bundle. It survives a
  prefix wipe *because of* the mapping, not by any property of DCS.

## Discovery

- **discovery** — finding the DCS installs already on the machine, whoever put
  them there. Read-only, always: it reads Lutris, Heroic and Steam
  configuration that may be being written while we look (ADR-0007).
- **launcher** — who manages an install: `lutris`, `heroic`, `steam`, or
  `dcs-linux` for one of ours. Not a property of the install itself; the same
  directory can be claimed by two launchers.
- **edition** — **Standalone** or **Steam**. Only two signals are trusted:
  Steam's app manifest for appid `223750` (proof), and `bin/DCS_updater.exe`,
  which the Steam edition is believed not to ship because Steam does its
  updating — *unverified*, no Steam copy of DCS has been inspected. Neither
  present means `unknown`, never a guess.
- **install id** — the stable handle `--install` takes, derived from the game
  directory alone, so it survives a prefix rebuild. An install *is* its game
  directory (ADR-0007).
- **register** — `~/.local/state/dcs-linux/installs.json`: the game
  directories this tool installed into. Our own installs are the only ones no
  launcher config records — `--game-dir /mnt/big` is a choice nothing on the
  machine remembers — so `install` writes one entry when a download finishes.
  It says *where to look*, never *what is there*: every entry is re-verified
  against the disk before it becomes an install, so a deleted directory simply
  stops being reported.
- **targeted install** — the one install a command acts on. Ours by default,
  or the only one found; with several and none named, install-dependent checks
  are skipped rather than guessing.

## Diagnostics

- **bundle** — what `report` prints: one markdown document, meant to be pasted
  whole into an issue or a forum thread. It is the project's answer to being
  written on one machine and run on every distro there is, so a bug reporter
  becomes a test environment.
- **redaction** — removing identifying data from a bundle while keeping its
  shape: home paths become `~`, user names and addresses become placeholders,
  but a path still reads as a path. `steamuser` is deliberately **kept** — it
  identifies nobody, and which profile name a prefix uses says which launcher
  built it. The **auth state** is excluded by never being read, not by being
  scrubbed.
- **excerpt** — the bounded quotation of `dcs.log` a bundle carries: header,
  known-fatal signatures, errors with the benign signatures below filtered
  out, and the tail. A healthy run's log is 150 KB with several hundred ERROR
  lines, so quoting one whole is worse than quoting none: the reader stops
  looking.

## Iteration

- **gold** — a known-good snapshot of the game directory, taken with
  `cp --reflink=always`. On btrfs/xfs this shares extents: measured at
  **536 GB in 5.0 s with zero additional disk**.
- **cold run** — restore the game directory from gold, then rebuild. Measured
  **6.6 s** to repair a deliberately corrupted 536 GB install, with no network.
- **warm run** — wipe and rebuild the prefix only; the game directory stays.
- **run journal** — `spikes/runs/NNNN/`: pinned versions, every command and its
  output, the captured `dcs.log`, and the human verdict. `commands.log` is
  gitignored because it captures the interactive login.
- **success bar** — main menu → Instant Action free flight → ~60 s flyable,
  with a human verdict. Starting and running are different failures on Linux:
  the known breakages surface in a mission, not at the menu.

## Verification

- **verification** — what `verify` does: launch DCS, let the user fly it, then
  read `dcs.log` and say whether it *worked*. The deep counterpart to `check`,
  which stays fast and static and launches nothing. Both exist because they
  answer different questions: `check` asks *should this work*, `verify` asks
  *did it*.
- **finding** — one thing a run was judged on, with the fix beside it and, where
  one applies, the **patch** id that is the fix. Deliberately the same shape as
  a `check` row, because the two are read together.
- **quiet failure** — the reason `verify` exists. DCS reaches the main menu with
  no symbols in the Apache, with garbage MFDs, or having never authorized, and
  exits 0 through every one of them. Anything judging the exit code calls all
  three a success. A DCS that started and is broken is a **failure**.
- **freshness** — whether the log being judged is the one this launch wrote,
  told by the `=== Log opened` stamp. The only way a verification could report
  a healthy run that never happened is by judging the previous run's log.
- **clock drift** — a system clock wrong enough that ED's TLS handshake fails,
  so authorization is refused for reasons that look nothing like a bad
  password. Reported as itself, with `timedatectl set-ntp true` as the fix.
  **Unverified**: no clock-drift log was captured on hardware, so the
  certificate-validity signatures in `dcs_linux.verify.CLOCK_DRIFT` are
  reasoned rather than observed, and are matched only alongside an
  authorization failure that has already been established.

## Patching

- **IC risk** (integrity check) — DCS servers running pure-client enforcement
  reject modified game files. A patch is:
  - **IC-safe** — touches only environment, launch arguments, or the wine
    prefix. Never a hashed game file. Safe by default.
  - **IC-risky** — edits a file DCS hashes. Must be **opt-in**, with a
    multiplayer warning.

  Preferring env/prefix changes over file edits is not a style choice; it is
  what keeps users able to join servers.

  The two risky patches shipped are `voice-chat` (comments the voice-chat
  entries out of `optionsDb.lua`) and `mfd-textures` (re-encodes the AH-64D
  MFD and sight textures with ImageMagick). Neither symptom was reproduced on
  2.9.28.26385, so on a current install both **refuse** — that refusal is the
  expected outcome, not a failure of the tool. `check` reports whether an
  install currently carries a risky patch, because otherwise the first sign is
  a server refusing to let the user in, weeks after the fix was applied.

- **patch** — a single named fix with a check / apply / revert triple, backed
  by a state file outside the install.

- **maintenance** — a named action that deletes regenerable files instead of
  writing any, so it has no backup, no state record and no revert. Clearing
  the **shader cache** (`Saved Games/DCS/fxo`, `metashaders2`) is the only one:
  DCS rebuilds it on the next launch, which then takes several minutes.
  Deliberately not a patch — "applied" would mean nothing about it.

- **plan** — what a patch would write, assembled in full before anything is
  written. A patch that cannot assemble one returns a **refusal** instead and
  the engine touches nothing, so no install is ever left half-fixed.

- **drift** — a patch that was applied and is no longer in place, because
  `DCS_updater` overwrote the files or the prefix was rebuilt. Detected by
  hashing each patched file against what the state store says was written, so
  it is a fact about the files, never a stale flag. This is the normal way a
  working install stops working, and `check` reports it as a failure with
  `dcs-linux patch apply` as the one-command fix.

- **pristine backup** — the copy of a file taken the moment before a patch
  overwrote it, kept in the **patch store**
  (`~/.local/state/dcs-linux/<install id>/`). Outside the install because
  `DCS_updater repair` deletes files ED's manifest does not list. On a
  re-apply after partial drift, files still holding what the patch wrote are
  *ours* and keep their original backup — backing them up again would make
  revert restore the patch instead of undoing it.

- **patch-update channel** — the PyPI release pipeline. Patches ship bundled
  inside the package and `uvx` resolves the latest version on every
  invocation, so **publishing a release is how a fix reaches users**. There is
  no second delivery path. See ADR-0005.

## Toolchain

- **umu** — `umu-launcher`, the runtime that drives Proton. **Not on PyPI**;
  installed as a **zipapp** (no root, works on immutable distros).
- **GE-Proton** — the pinned Proton build. Release assets include an aarch64
  variant; the **unsuffixed** `.tar.gz` is x86_64.
- **GAMEID** — umu's protonfix key. `umu-223750` (DCS World Steam Edition) is
  *recognised* as "DCS World" but ProtonFixes applies **nothing** to it.
- **updater** — `DCS_updater.exe` inside the install. Distinct from the
  **web installer** (`DCS_World_web.exe`), which refuses to reuse a directory
  it has already bootstrapped.

- **handoff** — the second half of `install`: opening the updater GUI so the
  user can log in and choose modules, then reading the disk to find out what
  happened. It cannot be automated — the login is a browser-session affair —
  so what the tool controls is everything *around* it: the mapping is checked
  before the window opens, because `D:\` pointing at the wrong place is how
  150 GB lands inside the disposable prefix. The web installer bootstraps;
  every run after that goes through the installed updater with `update`.

- **stage** — how far the game directory has got: **absent**, **partial** or
  **complete**, judged by `bin/DCS.exe`. `autoupdate.cfg` and the updater land
  early, so neither says the game can be run. Partial and abandoned look
  identical on disk, which is why re-running `install` resumes rather than
  asking.

- **pin** — the exact umu and GE-Proton versions `install` fetches, held as
  constants in `dcs_linux.prefix`. Never resolved from a release API, so two
  users a week apart get the same runtime and a bug report names a
  reproducible pair (ADR-0008).

- **runtime manifest** — `.dcs-linux.json` inside the prefix: the pins, the
  `GAMEID`, the winetricks verbs and the launch environment. Written by
  `install`, and what a re-run reads to tell an up-to-date prefix from one
  built with an older pin. Inside the prefix on purpose — deleting the prefix
  must delete the claim that anything was installed into it.

- **launch environment** — `WINEDLLOVERRIDES=wbemprox=n` (DCS hangs querying
  WMI under wine) and `WINE_SIMULATE_WRITECOPY=1`, applied when DCS itself
  runs. IC-safe by construction: it lives in the process, so no hashed game
  file is involved. Distinct from the **prefix environment**
  (`WINEPREFIX`, `GAMEID`, `PROTONPATH`), which says *which* prefix and
  *which* Proton and is needed by winetricks and the updater too.

- **rebuild** — `dcs-linux install --rebuild`: delete the prefix and build it
  again. The repair every other recovery path rests on, and safe only because
  the other two lifetimes are outside. It **refuses** if the game directory or
  saved games is inside the prefix — there, wiping is not a repair.

## Known failure signatures

Recorded so `check` (#5), `report` (#7) and `verify` (#12) can tell noise from
faults. The tables live in code as `dcs_linux.dcslog.FATAL_SIGNATURES` and
`BENIGN_SIGNATURES`.

The two font lines below differ only in the brackets, and that is the whole
distinction: **empty brackets are fatal**, a bracketed path is not. Both say
"Cannot create font", and both appear in the same run.

**Fatal**

| Signature | Meaning |
| --- | --- |
| `Cannot create font [] size 30` then `C0000005 ACCESS_VIOLATION` in `CockpitBase.dll` | missing Segoe font; AH-64D dies entering a mission |

**Recoverable — slow, not broken**

| Signature | Meaning |
| --- | --- |
| `Can't find precompiled shader for effect ...` | DCS rebuilds it and carries on, costing minutes. Normal once after a shader-cache clear; every launch means the prefix is missing `d3dcompiler_47`. Absent from both healthy captures |

**Benign — appear on healthy runs**

| Signature | Meaning |
| --- | --- |
| `DX11Renderer initialization (... shaderErrors:1)` | normal on a working start |
| `Cannot create font [D:\DCS World\dxgui\skins\fonts\] size 0` | a *named* path with no filename on the end; harmless |
| `texture 'TEDAC_LCD_AH64' / 'MFD_LCD_AH64_*' not found` | runtime render-target names, not shipped files |
| `render target 'mainDepthBuffer' / 'uiTargetColor' not found` | same |
| `texture 'KevinWakePattern...' / 'lightPalette.tif' / livery `RoughMet`` | cosmetic |

**Not in the log at all**

- **DLSS flicker.** `["Upscaling"] = "DLSS"` in `Config/options.lua` causes
  violent flicker under Proton from launch. Nothing is logged. It has nothing
  to do with wine, Proton or this installer, but presents exactly like a
  broken install. Detectable **statically** by reading `options.lua`.
- **Shader-compile flicker.** Enabling TADS flickers for a few seconds, then
  settles. Distinguish from DLSS flicker: this one resolves itself, that one
  never does.
