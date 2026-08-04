# ADR-0008: The toolchain is pinned, not resolved

**Status:** accepted — deviates deliberately from the #2 spike

## Context

The spike fetched **whatever GE-Proton was newest**, by asking the GitHub
releases API for `latest`. That is right for a spike: it was being run to find
out what works, on a machine where the answer changed between runs.

It is wrong for a shipped tool. Two users running `dcs-linux install` a week
apart would get different Proton builds, and neither would know it. When one
of them files a bug, "GE-Proton" is not an answer to "which build?".

## Decision

`dcs_linux.prefix` carries two constants — `UMU_VERSION` and
`GE_PROTON_VERSION` — and builds the download URLs from them. Nothing asks a
release API anything.

The pinned pair is the pair that was flown to the success bar: umu 1.4.4 and
GE-Proton11-3 (`spikes/runs/0015/versions.json`).

Both are recorded in a **runtime manifest** inside the prefix, together with
the winetricks verbs and the launch environment.

## Consequences

- Bumping either version is a deliberate change with a run journal behind it,
  not something that happens to a user overnight.
- A GE-Proton build unpacks into a directory named for its version, so the
  path carries the pin. The umu zipapp does not — it unpacks to one
  unversioned `umu/umu-run` — so the version it *is* is recorded beside it in
  `.dcs-linux-version`, and a bump re-fetches. Without that, bumping the pin
  would leave the old binary in place while the manifest claimed the new one.
- Pinning the version also pins the *asset name*, which removes the class of
  bug that landed the aarch64 build on an x86_64 machine (ADR-0003): the
  x86_64 asset is `<TAG>.tar.gz`, and with `<TAG>` known there is nothing left
  to pick between.
- The pins go stale. A GE-Proton release eventually stops fixing what DCS
  needs, and this project has to notice — a stale pin fails visibly on one
  machine rather than invisibly on everyone's.
- The manifest lives *in* the prefix, so wiping the prefix retracts the claim
  that anything was installed into it. It is also what lets a re-run tell an
  up-to-date prefix from one built with an older pin or a different verb set.
