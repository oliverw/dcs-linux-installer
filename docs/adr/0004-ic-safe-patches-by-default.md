# ADR-0004: Prefer IC-safe patches; risky ones are opt-in

**Status:** accepted

## Context

DCS servers running pure-client enforcement reject modified game files. Several
widely-cited Linux fixes edit hashed files, so applying them silently would
lock users out of multiplayer without telling them why.

## Decision

Express a fix as environment, launch arguments, or a change **inside the wine
prefix** wherever possible. Only edit a hashed game file when there is no
alternative, and then only opt-in with a multiplayer warning.

## Consequences

Verified on 2.9.28.26385:

| Fix | Rating | Status |
| --- | --- | --- |
| `wbemprox=n`, `WINE_SIMULATE_WRITECOPY=1`, `--no-launcher` | safe (env/args) | applied by default |
| Segoe font for the AH-64D | safe (prefix only) | **required** |
| Voice chat `optionsDb.lua` | ⚠️ risky | **appears obsolete** — loads clean with no edit |
| MFD/sight texture conversion | ⚠️ risky | **appears unnecessary** — TADS renders correctly |

Both risky patches currently look droppable. Stated as "appears": one aircraft,
one machine, one DCS version.

### They are shipped anyway, and they refuse rather than pretend

"Appears unnecessary on one machine" is not grounds for withholding a fix from
somebody hitting the bug on another. Both are in the registry as `ic_risk`
patches (#9), and both **refuse** when they find nothing to change — no
voice-chat entry in `optionsDb.lua`, no loose MFD or sight textures — rather
than reporting a success that changed nothing. On a current install that
refusal is the expected answer, and it says so.

The gate is enforced in one place, `safe_patches`, so a registry entry cannot
opt itself in by omission: a bare `patch apply` sweeps up the IC-safe patches
only, and `--allow-ic-risk` widens nothing — it consents to a patch the user
named. The multiplayer warning prints on both paths, because the user who
passes the flag is the one who ends up with a modified install.

### Clearing the shader cache is not a patch

It deletes regenerable files in `Saved Games` and writes nothing, so it has no
backup, no state record and nothing to revert — and it cannot fail an
integrity check, because DCS hashes none of it. Modelling it as a patch would
have given it a meaningless "applied" status. It is a subcommand instead:
`dcs-linux patch clear-shader-cache`.

### The font patch is the model case

Without it the AH-64D dies entering a mission (`Cannot create font [] size 30`
→ `C0000005 ACCESS_VIOLATION` in `CockpitBase.dll`). `winetricks corefonts`
installs 42 fonts and **none are Segoe**.

The fix copies a locally installed font into the prefix as `seguisym.ttf`,
`seguisb.ttf`, `segoeui.ttf`. Microsoft's actual file is **not** required —
verified in-cockpit that EUFD, MFD, HMD and Keyboard Unit all render correctly
with a DejaVu substitute. So the fix is IC-safe *and* has no redistribution
problem: exactly the shape to aim for.
