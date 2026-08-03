# ADR-0006: Remediation must be possible on the distro that reads it

**Status:** accepted

## Context

`check` (#5) reports a failure with the command that fixes it. Advice that
cannot succeed on the machine reading it is worse than no advice: the user
runs it, it fails or is silently undone, and they lose trust in every other
row of the report.

The audience is multi-distro from day one, and includes immutable systems
where the usual `sudo <package manager> install` either does not exist or does
not survive the next system update.

## Decision

Remediation is chosen by **packaging family** and, above that, by how the base
system resists writes. Three cases:

| Immutability | Advice |
| --- | --- |
| mutable | the family's package manager (`dnf` / `apt` / `pacman` / `zypper`) |
| ostree (Bazzite, Silverblue, Bluefin, …) | `rpm-ostree install <pkg>`, noting it takes effect after a reboot |
| read-only (SteamOS) | never a package manager — get the tool from a `distrobox` container |

An unrecognised distro gets generic prose, never an invented command.

Detection: `ID` in `/etc/os-release` for the family, `/run/ostree-booted` or a
read-only `/usr` mount for immutability, plus a small list of IDs known to be
read-only.

## Consequences

- A failing check without remediation is a bug. There is a test that asserts
  every blocking failure carries a fix, across mutable, ostree and read-only
  fixture machines.
- `steamos-readonly disable` is never suggested. It works, and the next SteamOS
  update reverts it, so it produces a fix that quietly stops being applied.
- Adding a new external dependency means adding its package name for all four
  families, not just the developer's own.
- Detection is tested against real `/etc/os-release` files captured from
  upstream container images (`tests/fixtures/os-release/`). Containers cannot
  validate the immutable cases — a container shares the host kernel, so the
  ostree marker and a read-only `/usr` reflect the host, not the image — so
  those two fixtures are hand-written and the immutability rules are unit-
  tested rather than verified in a container.
