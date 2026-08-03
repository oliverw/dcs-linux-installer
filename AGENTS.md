## Agent skills

### Issue tracker

Issues live in GitHub Issues (oliverw/dcs-linux-installer), via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five canonical labels (needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## Instruction routing

Before starting work, read every document whose trigger matches. Multiple rows commonly apply. Read only the routed documents that are relevant.

| Area | Read when | Guidance |
| --- | --- | --- |
| Repository workflows | Working with GitHub issues, tickets, sagas, user-stories or PRDs, triage, domain docs, or durable agent guidance | [`docs/agents/issue-tracker.md`](docs/agents/issue-tracker.md) |
| Domain vocabulary and hard-won facts | **Before touching any code or docs.** Names the three lifetimes, IC risk, gold/reflink, and the log signatures that look fatal but are not | [`CONTEXT.md`](CONTEXT.md) |
| Decisions already settled empirically | Working on the prefix, the toolchain, downloading/caching, or patching — these record what was tried, what failed on real hardware, and why | [`docs/adr/`](docs/adr/) |
| Reproducing the install by hand | Debugging an install, or checking what the tool should be automating | [`docs/manual-install.md`](docs/manual-install.md) |
