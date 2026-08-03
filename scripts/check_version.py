"""Fail the release if the built artefacts disagree with the pushed tag.

hatch-vcs derives the version from the tag, so a mismatch means something is
wrong with the checkout (usually missing tags from a shallow clone) rather
than with the version number. Publishing is irreversible on PyPI, so this
runs before the upload, not after.
"""

from __future__ import annotations

import sys
from pathlib import Path


def version_from_artefact(name: str) -> str:
    """Pull the version out of a wheel or sdist filename.

    Both are `<name>-<version><suffix>`, and neither the distribution name nor
    a PEP 440 version may contain a hyphen, so one split does both:

        dcs_linux_installer-1.2.3-py3-none-any.whl
        dcs_linux_installer-1.2.3.tar.gz
    """
    return name.removesuffix(".tar.gz").split("-")[1]


def normalise(version: str) -> str:
    """Normalise enough of PEP 440 to compare a tag against an artefact.

    `packaging` is not in the standard library and this runs without the
    project's dependencies, so this stays deliberately minimal: it folds only
    the separators a human puts in a tag but hatch-vcs strips, so the tag
    `v1.2.3-rc1` matches the artefact version `1.2.3rc1`.
    """
    return version.lower().replace("-", "").replace("_", "")


def main(tag: str, dist_dir: Path) -> int:
    expected = normalise(tag.removeprefix("refs/tags/").removeprefix("v"))
    if not expected:
        print("No tag given; expected refs/tags/vX.Y.Z", file=sys.stderr)
        return 1

    artefacts = [*dist_dir.glob("*.whl"), *dist_dir.glob("*.tar.gz")]
    if not artefacts:
        print(f"No artefacts found in {dist_dir}", file=sys.stderr)
        return 1

    built = {normalise(version_from_artefact(p.name)) for p in artefacts}
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
