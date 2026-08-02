# Installing DCS World Standalone on Linux

Generated from the winning run of the `spikes/dcs_install.py` iteration loop
(issue #2). Every command here was executed on the machine described under
[Verified on](#verified-on) — nothing is transcribed from a guide.

This is the manual procedure. The `dcs-linux` tool automates it.

---

## Layout: three independent lifetimes

The single most important decision. Keep these three apart:

| Directory | Wiped? | Holds |
| --- | --- | --- |
| `prefix/` | **yes**, every rebuild | umu + pinned GE-Proton |
| `game/` | no | the install (536 GB with 33 modules) |
| `saved-games/` | no | ED login, config, keybinds, `Logs/dcs.log` |

`Saved Games/DCS` lives inside the prefix by default. Since "delete the prefix
and rebuild" is the repair everything else rests on, leaving it there means
that repair silently destroys your login and keybinds. Map it out.

Verified: a 12 GB partial download in `game/` survived a full prefix wipe and
rebuild, and the updater resumed rather than restarting.

**Your ED login lives in `Saved Games/DCS/Config/authdata.bin`.** Because the
step below maps `Saved Games` out of the prefix, that file sits in
`saved-games/` and survives a rebuild — no re-login. Leave it in the default
location inside the prefix and every prefix rebuild logs you out.

> `authdata.bin` is a credential. Never commit it, and never include it in a
> diagnostics bundle or bug report.

Pick a drive with room. This guide uses `$DATA`:

```bash
export DATA=/run/media/oliver/Data          # adjust
export PREFIX="$DATA/dcs-linux-spike/prefix"
export GAME="$DATA/dcs-linux-spike/game"
export SAVED="$DATA/dcs-linux-spike/saved-games"
export TOOLCHAIN="$DATA/.cache/dcs-linux/toolchain"
mkdir -p "$GAME" "$SAVED" "$TOOLCHAIN"
```

**Use btrfs or xfs** if you can. Reflink snapshots make re-testing nearly free
(see [Cheap iteration](#cheap-iteration)).

---

## 1. Toolchain

Neither `umu-run` nor `winetricks` needs to be on your PATH.

### umu-launcher

**`umu-launcher` is not on PyPI** — `pip install` / `uv tool install` cannot
work. Upstream ships distro packages and a distro-agnostic zipapp. The zipapp
needs no root and works on immutable distros:

```bash
UMU_VERSION=1.4.4
curl -L -o /tmp/umu.tar \
  "https://github.com/Open-Wine-Components/umu-launcher/releases/download/${UMU_VERSION}/umu-launcher-${UMU_VERSION}-zipapp.tar"
tar xf /tmp/umu.tar -C "$TOOLCHAIN"     # yields $TOOLCHAIN/umu/umu-run
chmod +x "$TOOLCHAIN/umu/umu-run"
export UMU="$TOOLCHAIN/umu/umu-run"
"$UMU" --version
```

Fedora users can instead use the official RPM asset
(`umu-launcher-<ver>.fc44.x86_64.rpm`), but that needs root.

### GE-Proton

Take the **x86_64** asset. The release also contains an `-aarch64` build, and
grabbing "the first `.tar.gz`" silently lands the wrong architecture:

```bash
mkdir -p "$TOOLCHAIN/ge-proton"
TAG=$(curl -s https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases/latest | grep -oP '"tag_name": "\K[^"]+')
curl -L -o /tmp/ge.tar.gz \
  "https://github.com/GloriousEggroll/proton-ge-custom/releases/download/${TAG}/${TAG}.tar.gz"   # note: NO -aarch64 suffix
tar xf /tmp/ge.tar.gz -C "$TOOLCHAIN/ge-proton"
export PROTONPATH="$TOOLCHAIN/ge-proton/$TAG"
```

---

## 2. Create the prefix

```bash
export WINEPREFIX="$PREFIX"
export GAMEID=umu-223750
"$UMU" ""
```

Two things to know:

- **The exit code is not a success signal.** `umu-run ""` builds the prefix,
  then exits **1** trying to `ShellExecute` the empty string
  (`Application could not be started`). This is expected. Check for
  `$PREFIX/system.reg` instead.
- **`GAMEID=umu-223750`** (DCS World Steam Edition) makes ProtonFixes
  recognise the game as "DCS World", but it reports
  *"No main stage global protonfix found"* — so it currently applies
  **nothing**. Harmless, but don't expect it to fix anything.

---

## 3. winetricks

umu takes a `winetricks` subcommand, so winetricks does not need installing
or pointing at the prefix:

```bash
"$UMU" winetricks corefonts xact d3dcompiler_47
```

`d3dcompiler_47` is what avoids the `fx_5_0` shader compilation failures.

> **Do not install `vcrun2019`** — it causes a system RAM leak. Use 2015 or
> 2022 if a `vcrun` turns out to be needed at all.

### 3b. Segoe fonts — required for the AH-64D

`corefonts` installs 42 fonts and **none of them are Segoe**. Without a Segoe
stand-in, entering a mission in the Apache crashes every time:

```
UIBASERENDERER: Cannot create font [] size 30!
# C0000005 ACCESS_VIOLATION in CockpitBase.dll, frame (ah64d)
```

The cockpit asks for a Segoe font, gets an empty name back, and dereferences
null. Copy any locally installed sans font in under the Segoe names:

```bash
SRC=/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf     # or liberation-sans
FONTS="$PREFIX/drive_c/windows/Fonts"
for n in seguisym.ttf seguisb.ttf segoeui.ttf; do cp "$SRC" "$FONTS/$n"; done
```

**Integrity-check safe** — this writes only into the wine prefix. No hashed
game file is touched, so pure-client multiplayer servers are unaffected.

Microsoft's real `seguisym.ttf` is **not required**: verified in-cockpit that
the EUFD, MFD labels, HMD heading tape and Keyboard Unit all render correctly
with a DejaVu substitute.

---

## 4. Map game/ and Saved Games out of the prefix

```bash
# game/ as a drive letter
mkdir -p "$PREFIX/dosdevices"
ln -sfn "$GAME" "$PREFIX/dosdevices/d:"

# Saved Games into the user profile
USERDIR="$PREFIX/drive_c/users/steamuser"
mkdir -p "$USERDIR"
rm -rf "$USERDIR/Saved Games"          # wine creates a real directory here
ln -sfn "$SAVED" "$USERDIR/Saved Games"
```

`game/` is now `D:\` inside the prefix.

---

## 5. Install DCS

Download **`DCS_World_web.exe`** from
<https://www.digitalcombatsimulator.com/en/downloads/world/> into
`$TOOLCHAIN`. This step cannot be scripted — the page needs a browser session.
Note the filename is lowercase `web`.

```bash
"$UMU" "$TOOLCHAIN/DCS_World_web.exe"
```

In the installer:

- **Set the install path to `D:\`** — this is `game/`. Installing to `C:\`
  puts the whole install inside the prefix, where the next rebuild destroys it.
- **Leave torrent/P2P enabled.** It is dramatically faster; throughput went
  from ~12 GB to ~100 GB in the same span once re-enabled.

Then log in and pick modules.

### Resuming, or adding modules later

Once bootstrapped, `DCS_World_web.exe` **refuses to reuse the directory**.
Use the installed updater instead:

```bash
"$UMU" "$GAME/DCS World/bin/DCS_updater.exe" update
```

---

## 6. Launch

```bash
WINEDLLOVERRIDES="wbemprox=n" \
WINE_SIMULATE_WRITECOPY=1 \
"$UMU" "$GAME/DCS World/bin/DCS.exe" --no-launcher
```

| Setting | Why |
| --- | --- |
| `wbemprox=n` | 2.9.12.5336+ hangs querying WMI under wine |
| `WINE_SIMULATE_WRITECOPY=1` | wine write-copy semantics |
| `--no-launcher` | skips the black-screen launcher |

All three are **integrity-check safe** — environment and arguments only, no
edits to hashed game files.

First launch compiles shaders and takes several minutes. Logs land in
`$SAVED/DCS/Logs/dcs.log`.

---

## Cheap iteration

Testing means rebuilding the prefix repeatedly. Never re-download to do it.

**Snapshot the finished install** (btrfs/xfs, seconds, no extra disk — the
extents are shared):

```bash
cp -a --reflink=always "$GAME" "$DATA/.cache/dcs-linux/gold"
```

Measured: **536 GB in 5.0 seconds**, with disk usage unchanged. Restoring
is the same shape -- a deliberately corrupted install came back in **6.6 s**.

Restore it the same way. Rebuilding the prefix from scratch then costs a few
minutes, not a 250 GB download.

> There is **no HTTP proxy cache** in this design, and there cannot be one.
> The updater downloads over **HTTPS** to cdn77, so an HTTP cache never sees
> the traffic; intercepting it would need a MITM certificate. Reflink
> snapshots replace it entirely and do not care about the protocol.

---

## Known log signatures

From a verified-good startup — these are normal, not faults:

| Line | Meaning |
| --- | --- |
| `DX11Renderer initialization (... shaderErrors:1)` | present on a working start |
| `DX11ShaderBinaries::loadCache done. Loaded 2630/2630` | shader cache healthy |
| `texture 'KevinWakePattern812x1024.dds' not found` | cosmetic, water wake |
| `UIBASERENDERER: Cannot load font [...\dxgui\skins\fonts\]!` | path resolves to a directory with no filename; seen on a good start |
| `texture 'MFD_LCD_AH64_{LEFT,RIGHT}_{PLT,CPG}' not found` | runtime render-target names, not shipped files; MFDs render correctly regardless |

Distinguish these from the **fatal** form, which is a different line entirely:

```
UIBASERENDERER: Cannot create font [] size 30!     <- empty name: crash follows
```

`VoiceChat.dll` loads cleanly on **2.9.28.26385** with no `optionsDb.lua`
edit, so the widely-cited voice-chat fix appears unnecessary on current
versions — worth confirming before applying it, since that edit touches a
hashed file and carries integrity-check risk.

**Not tested:** the TADS/PNVS sight. Reports of MFD/sight texture corruption
concern sight views specifically, and this run never slewed the TADS. Treat
the texture-conversion workaround as unverified rather than unnecessary.

---

## Verified on

| | |
| --- | --- |
| Distro / kernel | Fedora 44, 7.1.4-202.fc44.x86_64 |
| GPU / driver | NVIDIA RTX 4080, 610.43.03 |
| CPU / RAM | i7-12700F, 64 GB |
| umu-launcher | 1.4.4 (zipapp) |
| GE-Proton | GE-Proton11-3 |
| DCS | 2.9.28.26385, 33 modules (AH-64D, FA-18C, A-10C, F-15E, AV-8B, Su-33; Caucasus, Syria, Nevada, Persian Gulf, Sinai, Marianas) |
| Filesystem | btrfs on LUKS |

**Result:** main menu → AH-64D Instant Action → airborne and flyable for
60 s+ with no crash.

Multi-distro and multi-GPU paths are **untested** — this is one machine.
