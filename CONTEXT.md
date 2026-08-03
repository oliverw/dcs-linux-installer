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
  Steam's app manifest for appid `223750`, and `bin/DCS_updater.exe`, which the
  Steam edition does not ship. Neither present means `unknown`, never a guess.
- **install id** — the stable handle `--install` takes, derived from the game
  directory alone, so it survives a prefix rebuild. An install *is* its game
  directory (ADR-0007).
- **targeted install** — the one install a command acts on. Ours by default,
  or the only one found; with several and none named, install-dependent checks
  are skipped rather than guessing.

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

## Patching

- **IC risk** (integrity check) — DCS servers running pure-client enforcement
  reject modified game files. A patch is:
  - **IC-safe** — touches only environment, launch arguments, or the wine
    prefix. Never a hashed game file. Safe by default.
  - **IC-risky** — edits a file DCS hashes. Must be **opt-in**, with a
    multiplayer warning.

  Preferring env/prefix changes over file edits is not a style choice; it is
  what keeps users able to join servers.

- **patch** — a single named fix with a check / apply / revert triple, backed
  by a state file outside the install.

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

## Known failure signatures

Recorded so `check` (#5) and `verify` (#12) can tell noise from faults.

**Fatal**

| Signature | Meaning |
| --- | --- |
| `Cannot create font [] size 30` then `C0000005 ACCESS_VIOLATION` in `CockpitBase.dll` | missing Segoe font; AH-64D dies entering a mission |

**Benign — appear on healthy runs**

| Signature | Meaning |
| --- | --- |
| `DX11Renderer initialization (... shaderErrors:1)` | normal on a working start |
| `Cannot load font [...\dxgui\skins\fonts\]` | path with no filename; harmless |
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
