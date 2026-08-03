# ADR-0005: Releases are tag-driven, version-from-git, trusted-published

**Status:** accepted (#4)

## Context

Patches ship bundled inside the package, so the release pipeline is also the
patch-update channel: publishing a version is how a fix reaches a user running
`uvx`. That makes it infrastructure, not a chore, and it needs to be right
before anything depends on it.

Two ways it goes wrong quietly:

- **A hand-maintained version string.** `pyproject.toml` says one number, the
  git tag says another, and the release that ships is not the one anyone
  reviewed. PyPI uploads are immutable, so the fix is always a new version.
- **A stored API token.** A long-lived PyPI token in repository secrets is a
  credential in CI that can publish under this project's name forever.

## Decision

- The version is **derived from the git tag** by `hatch-vcs`. `pyproject.toml`
  declares `dynamic = ["version"]` and carries no number at all — there is
  nothing to keep in sync, so nothing can drift.
- Publishing is triggered by **pushing a `v*` tag**, and by nothing else.
- Publishing uses **PyPI trusted publishing** (OIDC, `id-token: write`). No
  token exists in the repository or in CI secrets.
- Everything that can fail runs **before** the upload: lint, types, the test
  suite, `scripts/check_version.py` (which asserts the built artefacts and the
  tag agree), and a `uvx` smoke test of the built wheel — the same way a user
  gets it. A PyPI version can never be replaced, only superseded.

## Consequences

- Cutting a release is `git tag vX.Y.Z && git push --tags`. Nothing else.
- The checkout must be **unshallow** (`fetch-depth: 0`); without tags,
  `hatch-vcs` produces a `0.1.devN` version. The version check turns that from
  a bad upload into a failed job.
- A local build from an untagged tree is versioned `0.1.devN+g<sha>`. That is
  correct — it is not a release — and `dcs-linux --version` reports it.
- There is no credential to rotate, leak, or accidentally log.
