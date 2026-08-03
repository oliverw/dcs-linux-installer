"""Fail the release if the built artefacts disagree with the pushed tag.

hatch-vcs derives the version from the tag, so a mismatch means something is
wrong with the checkout (usually missing tags from a shallow clone) rather
than with the version number. Publishing is irreversible on PyPI, so this
runs before the upload, not after.
"""

from __future__ import annotations

import sys
from pathlib import Path


def wheel_version(name: str) -> str:
    # dcs_linux_installer-1.2.3-py3-none-any.whl
    return name.split("-")[1]


def sdist_version(name: str) -> str:
    # dcs_linux_installer-1.2.3.tar.gz
    return name.removesuffix(".tar.gz").split("-")[1]


def main(tag: str, dist_dir: Path) -> int:
    expected = tag.removeprefix("refs/tags/").removeprefix("v")
    if not expected:
        print("No tag given; expected refs/tags/vX.Y.Z", file=sys.stderr)
        return 1

    built = {wheel_version(p.name) for p in dist_dir.glob("*.whl")}
    built |= {sdist_version(p.name) for p in dist_dir.glob("*.tar.gz")}
    if not built:
        print(f"No artefacts found in {dist_dir}", file=sys.stderr)
        return 1

    if built != {expected}:
        print(
            f"Tag/package mismatch: tag says {expected}, artefacts say {sorted(built)}",
            file=sys.stderr,
        )
        return 1

    print(f"Tag and artefacts agree on version {expected}")
    return 0


if __name__ == "__main__":
    tag_arg = sys.argv[1] if len(sys.argv) > 1 else ""
    dist_arg = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("dist")
    raise SystemExit(main(tag_arg, dist_arg))
