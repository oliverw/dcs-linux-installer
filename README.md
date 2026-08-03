# dcs-linux-installer

Install [DCS World](https://www.digitalcombatsimulator.com/) **Standalone** on Linux, and keep it working.

> **Status: pre-alpha.** `dcs-linux check` works, including finding the DCS installs you already have; everything else is a stub.
> The design is settled and the work is broken down in [issue #1](https://github.com/oliverw/dcs-linux-installer/issues/1). There is no usable release yet. Everything below describes the intended tool.

---

## What this is

Getting DCS running on Linux is not one problem, it is a pile of small ones: which Proton build, which winetricks verbs, which DLL overrides, and a handful of fixes for bugs that only show up on Linux. The community has solved all of it, but the answers are scattered across forum threads, wiki pages and half-maintained gists — and they go stale.

Worse, DCS's own updater **overwrites those fixes every time it runs**. Getting the game working once is achievable. Keeping it working is the part that wears people down.

`dcs-linux-installer` collects the known-good setup into one tool, applies the fixes, notices when a DCS update has reverted them, and puts them back.

It is aimed at anyone running Linux, not just one distro. Immutable systems like Bazzite and SteamOS are first-class.

## What it will do

- **Install** DCS World Standalone into a Wine prefix built with a pinned GE-Proton via [umu-launcher](https://github.com/Open-Wine-Components/umu-launcher)
- **Check** your system and tell you exactly what is missing, with fixes that suit your distro
- **Find** DCS installs you already have — from Lutris, Heroic, Steam, or this tool
- **Patch** the known Linux-specific breakages, and re-apply them after DCS updates revert them
- **Verify** that DCS actually works, by reading its logs rather than assuming a process that started is a process that works
- **Report** a redacted diagnostics bundle you can paste into a bug report
- **Head tracking** — TrackIR and opentrack setup and permission checks

### Not in scope

HOTAS configuration, VR, and [SRS](http://dcssimpleradio.com/). Each is a large project in its own right. This tool installs the game and keeps it running.

## Two things worth knowing up front

### Your multiplayer access is protected by default

DCS has an **Integrity Check** that hashes game files. Servers running pure-client enforcement reject clients whose files have been modified — and some of the necessary Linux fixes modify exactly those files.

So every patch declares whether it carries that risk. Safe patches apply by default. Anything that could cost you multiplayer access requires an explicit opt-in and says so plainly before it writes anything. Every patch is fully revertible back to a state that passes Integrity Check.

No silent trade of your multiplayer access for a single-player bug fix.

### The prefix is disposable, the download is not

DCS is over 150 GB. Most setups put it *inside* the Wine prefix, which means Wine's most common repair — delete the prefix and rebuild it — also deletes the download.

Here the game lives on a path you choose, outside the prefix, mapped in. The prefix stays small enough to throw away and rebuild in seconds, and you can put the game on whichever drive has room.

## Planned usage

```sh
# One-shot, no install
uvx --from dcs-linux-installer dcs-linux check

# Or install the command
uv tool install dcs-linux-installer

dcs-linux check      # is this machine ready? what DCS installs exist?
dcs-linux install    # build the prefix, then hand off to the DCS updater
dcs-linux patch      # apply the Linux fixes (IC-safe ones by default)
dcs-linux verify     # launch DCS and confirm it actually works
dcs-linux report     # diagnostics bundle for a bug report
```

Requires [uv](https://docs.astral.sh/uv/). It installs to your home directory without root, so this works on immutable distros too.

### `dcs-linux check`

The one command that works today. It needs no DCS install — it is the first thing to run — and reports a pass/fail row per check, each failure carrying the command that fixes it, chosen for your distro:

```sh
dcs-linux check          # exits non-zero if anything blocking is wrong
dcs-linux --json check   # same results, machine-readable
```

It reports your distro and whether its base system is immutable, your GPU and driver version, umu-launcher and the available GE-Proton builds, missing external tools, free disk space against what a DCS install actually needs, and whether your filesystem supports reflink snapshots.

Once DCS is installed it also checks the things that break it in ways the logs never mention — DLSS upscaling, the missing Segoe fonts the AH-64D needs, `d3dcompiler_47`, and whether the game and your saved games really do live outside the disposable prefix.

Paths default to `~/dcs-linux` and `~/.cache/dcs-linux/toolchain`; override with `DCS_LINUX_ROOT` and `DCS_LINUX_TOOLCHAIN`.

#### The DCS installs you already have

`check` also lists every DCS install on the machine — from Lutris, Heroic, Steam, or this tool — with its game directory, prefix, Proton build, edition and DCS version:

```
2 DCS installs
┏━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃   ┃ ID       ┃ Install                                                     ┃
┡━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ → │ 778d8145 │ steam · Steam edition · DCS 2.9.28.26385 · GE-Proton9-20    │
│   │          │ game:   /mnt/games/SteamLibrary/steamapps/common/DCSWorld   │
│   │          │ prefix: /mnt/games/SteamLibrary/steamapps/compatdata/223750 │
│   │ 7976590f │ lutris · Standalone edition · DCS 2.9.30.1000 · ge-proton   │
│   │          │ game:   /games/DCS World                                    │
│   │          │ prefix: /games/prefixes/dcs                                 │
└───┴──────────┴─────────────────────────────────────────────────────────────┘
```

Nothing is written while discovering — your launchers can be running.

The **ID** is how you name one install to a command, and it is derived from the game directory, so it does not change when the prefix is rebuilt:

```sh
dcs-linux check --install 7976590f   # report on that install
dcs-linux check --install 7976       # any unambiguous prefix works
```

With one install found, or one of ours, it is used automatically.

## Design

| Area | Decision |
| --- | --- |
| Runtime | Pinned GE-Proton via umu-launcher |
| Layout | Game outside the prefix, mapped in; prefix small and disposable |
| Install flow | Prepare prefix → hand off to the DCS updater GUI → patch → verify |
| Discovery | Adopts existing Lutris / Heroic / Steam installs, not just its own |
| Patches | Backup plus state file, matched by content pattern rather than line number |
| Patch state | Stored in `~/.local/state/dcs-linux/`, outside the install, where `DCS_updater repair` cannot delete it |
| Multiplayer | Per-patch Integrity Check risk; risky patches opt-in only |
| Interface | CLI subcommands with rich output, `--json` and `--no-color`, non-interactive flags throughout |
| Language | Python, managed by uv |

Full reasoning is in [issue #1](https://github.com/oliverw/dcs-linux-installer/issues/1).

## Contributing

The tool targets many distros, GPUs and launcher layouts, but is developed on one machine. **Bug reports are how coverage happens** — once `dcs-linux report` exists, its output is the most useful thing you can attach.

Work is tracked as issues under [#1](https://github.com/oliverw/dcs-linux-installer/issues/1). Anything labelled `ready-for-agent` with no open blockers is available to pick up.

### Releasing

Push a version tag; that is the whole process.

```sh
git tag v0.1.0
git push origin v0.1.0
```

The version number comes from the tag itself, so the tag and the published package can never disagree. Uploads use [PyPI trusted publishing](https://docs.pypi.org/trusted-publishers/) — there is no API token in the repository or in CI. See [ADR-0005](docs/adr/0005-tag-driven-trusted-publishing.md).

## Credit

This project stands on work the DCS Linux community did first:

- [ChaosRifle/DCS-on-Linux](https://github.com/ChaosRifle/DCS-on-Linux)
- [budderpard/DCS_Standalone_on_linux](https://github.com/budderpard/DCS_Standalone_on_linux)
- [TheZoq2/dcs_on_linux](https://github.com/TheZoq2/dcs_on_linux)
- [Hoggit wiki — DCS on Linux](https://wiki.hoggitworld.com/view/DCS_on_linux)

## Licence

MIT

---

Not affiliated with or endorsed by Eagle Dynamics. DCS World is their trademark.
