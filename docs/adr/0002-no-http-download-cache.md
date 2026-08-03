# ADR-0002: No HTTP download cache; gold snapshots instead

**Status:** accepted — supersedes the assumption in #2's original plan

## Context

#2 assumed: *"The HTTP servers are plain `http://`, so the traffic is cacheable
with no TLS interception."* A caching proxy would have made re-testing a
500 GB+ install affordable.

A full nginx/podman cache plus an `/etc/hosts` redirect was built and verified
caching against the real CDN (MISS then HIT).

## Decision

**There is no HTTP cache, and there cannot be one.**

The premise is false. The host *offers* plain http, but the updater does not
use it — it downloads over **HTTPS to cdn77**. An HTTP proxy never sees the
traffic. Intercepting it would need a MITM certificate, which is out of scope.

Cheap iteration comes from **`gold/` + reflink** instead: `cp --reflink=always`
on btrfs/xfs. Measured 536 GB snapshot in 5.0 s and restore in 6.6 s, both
with zero additional disk.

## Consequences

- The harness was deleted (~216 lines), along with the machine state it needed
  (`net.ipv4.ip_unprivileged_port_start=80`, a system-wide `/etc/hosts` block).
- **Torrent/P2P stays enabled.** It was only ever disabled to force traffic
  through the cache; it is roughly an order of magnitude faster.
- Reflink is protocol-agnostic, so this survives any future change to how ED
  distributes content.
- Never redirect `www.digitalcombatsimulator.com`. It is the HTTPS-only
  API/auth host; pointing it at an http listener kills the updater outright
  (`replied HTTP -1`).

## Lesson

The original test proved the *host offered* http, not that the *updater used*
it. Verify the client's actual behaviour, not the server's capability.
