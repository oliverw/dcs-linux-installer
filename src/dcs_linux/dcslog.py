"""Reading `dcs.log`, and cutting it down to what a bug report needs.

A DCS log is 150 KB and several hundred ERROR lines on a **healthy** run, so
pasting one whole is worse than pasting nothing: the reader stops looking.
This module holds the knowledge that separates signal from noise — the
signatures in `CONTEXT.md`, established over 15 runs on real hardware — and
uses it to produce a bounded excerpt.

`dcs_linux.verify` judges a log; this module only quotes it. The signature
tables are the part both need, so they live here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from dcs_linux.paths import TargetPaths
from dcs_linux.system import System

LOG_NAME = "dcs.log"
LOGS_DIR = "Logs"
# `DCS`, but also `DCS.openbeta` and friends: the saved-games directory name
# is the branch, and a machine may have more than one.
SAVED_GAMES_PREFIX = "DCS"

# Excerpt titles, bound once so the report and its tests agree.
HEADER = "Header"
SIGNATURES = "Known-fatal signatures"
FAULTS = "Errors and warnings"
TAIL = "Tail"

# Enough to see the shape of a fault, short enough that one runaway line
# cannot swamp the bundle.
MAX_LINE_CHARS = 200
MAX_FAULT_LINES = 40
MAX_TAIL_LINES = 15
# The header lives at the top; scanning further would start quoting the body.
HEADER_SEARCH_LINES = 120

# One line each, in log order. Anything else in the first lines is CPU
# affinity masks and cache geometry, which no bug report has ever needed.
HEADER_MARKERS = (
    "=== Log opened",
    "APP (Main): Command line:",
    "APP (Main): DCS/",
    "Application revision:",
    "Renderer revision:",
    "Terrain revision:",
    "Build number:",
    "CPU cores:",
)

_SEVERITY = re.compile(r"\b(?:ERROR(?:_ONCE)?|WARNING)\b")

# The empty brackets are the whole distinction: a *named* font that cannot be
# created is the benign path-with-no-filename case below.
FONT_CRASH = re.compile(r"Cannot create font \[\]")
ACCESS_VIOLATION = re.compile(r"C0000005 ACCESS_VIOLATION")

# Fatal, and worth naming: these are what turned a run from "starts" into
# "dies in a mission" (CONTEXT.md). Named separately above because `verify`
# judges on the same two patterns, and two copies of them would be two rules.
FATAL_SIGNATURES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("missing Segoe font", FONT_CRASH),
    ("access violation", ACCESS_VIOLATION),
)

# Recoverable: DCS rebuilds the shader and carries on, costing minutes. Absent
# from both healthy captures and present on the run made before
# `d3dcompiler_47` was in the prefix, so it discriminates — but it is also what
# a deliberate shader-cache clear looks like on the next launch.
SHADER_RECOMPILE = re.compile(r"Can't find precompiled shader for effect")

# Authorization. The success line is on both captured logs, so its absence is a
# fact about the run rather than about the DCS version.
AUTH_SUCCESS = re.compile(r"Successfully got authorization data")
AUTH_FAILURE = re.compile(
    r"Login failed|failed to get auth|authorization failed|auth data.*failed", re.IGNORECASE
)

# **Unverified.** No clock-drift failure was captured on hardware, so unlike
# everything else in this file these patterns are reasoned rather than
# observed: a machine whose clock is wrong fails ED's TLS handshake, and TLS
# says so in terms of validity dates. They are only ever matched *alongside*
# `AUTH_FAILURE`, so at worst a real failure is described with the wrong cause
# — never a healthy run flagged.
CLOCK_DRIFT = re.compile(
    r"certificate (?:is )?not yet valid|certificate has expired|CERT_NOT_YET_VALID"
    r"|CERT_HAS_EXPIRED|certificate verify failed",
    re.IGNORECASE,
)

# Written on every clean shutdown, in this order. A log that reaches the second
# without the first is DCS unwinding after a crash.
RENDER_THREAD_STOPPED = "render thread has stopped"
LOG_CLOSED = "=== Log closed."

# Appear on healthy runs. Quoting them sends readers chasing faults that are
# not there, so the excerpt drops them.
BENIGN_SIGNATURES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"DX11Renderer initialization",
        r"shaderErrors:",
        # A bracketed path with no filename on the end. The fatal case has
        # nothing between the brackets at all.
        r"Cannot (?:load|create) font \[[^\]]+\]",
        r"texture '(?:TEDAC_LCD_AH64|MFD_LCD_AH64)",
        r"texture 'KevinWakePattern",
        r"lightPalette\.tif",
        r"RoughMet",
        r"render target '(?:mainDepthBuffer|uiTargetColor|uiTargetDepth)' not found",
    )
)

_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?\s+")
_THREAD_ID = re.compile(r"\(\d+\)")


@dataclass(frozen=True)
class Excerpt:
    """One titled block of log lines, already bounded."""

    title: str
    lines: tuple[str, ...]
    omitted: int = 0


@dataclass(frozen=True)
class DcsLog:
    """The log that was found, the whole of it, and the parts worth quoting.

    `text` is carried because `verify` judges the log while `report` quotes it,
    and reading a 150 KB file twice to do both would let the two disagree about
    which run they are describing.
    """

    path: Path
    text: str
    excerpts: tuple[Excerpt, ...]


def find_log(system: System, paths: TargetPaths) -> Path | None:
    """The targeted install's `dcs.log`, wherever its saved games live.

    Mapped out, the in-prefix path and the durable one are the same file; both
    are searched because an unmapped install only has the first, and someone
    else's install only has whatever they mapped.
    """
    roots = [paths.prefix_saved_games]
    if paths.saved_games is not None:
        roots.append(paths.saved_games)
    for root in roots:
        for name in system.list_dir(root):
            if not name.startswith(SAVED_GAMES_PREFIX):
                continue
            candidate = root / name / LOGS_DIR / LOG_NAME
            if system.exists(candidate):
                return candidate
    return None


def read_log(system: System, paths: TargetPaths) -> DcsLog | None:
    """The excerpted log, or None if DCS has never written one here."""
    path = find_log(system, paths)
    if path is None:
        return None
    text = system.read_text(path)
    if text is None:
        return None
    return DcsLog(path=path, text=text, excerpts=excerpt(text))


def excerpt(text: str) -> tuple[Excerpt, ...]:
    """The parts of a log a bug report needs, and nothing else."""
    lines = text.splitlines()
    if not lines:
        return ()
    found = (_header(lines), _signatures(lines), _faults(lines), _tail(lines))
    return tuple(block for block in found if block.lines)


def _header(lines: list[str]) -> Excerpt:
    head = lines[:HEADER_SEARCH_LINES]
    picked = []
    for marker in HEADER_MARKERS:
        match = next((line for line in head if marker in line), None)
        if match is not None:
            picked.append(_clip(match))
    return Excerpt(title=HEADER, lines=tuple(picked))


def _signatures(lines: list[str]) -> Excerpt:
    """Every known-fatal signature in the log, named."""
    picked = []
    for name, pattern in FATAL_SIGNATURES:
        match = next((line for line in lines if pattern.search(line)), None)
        if match is not None:
            picked.append(f"{_clip(match)}   <- {name}")
    return Excerpt(title=SIGNATURES, lines=tuple(picked))


def _faults(lines: list[str]) -> Excerpt:
    """Errors and warnings, minus the noise, deduplicated and capped."""
    counts: dict[str, int] = {}
    first: dict[str, str] = {}
    for line in lines:
        if not _SEVERITY.search(line) or _is_benign(line):
            continue
        key = _THREAD_ID.sub("(*)", _TIMESTAMP.sub("", line))
        counts[key] = counts.get(key, 0) + 1
        first.setdefault(key, line)

    kept = list(first)[:MAX_FAULT_LINES]
    rendered = tuple(_clip(first[key]) + _repeat_suffix(counts[key]) for key in kept)
    return Excerpt(title=FAULTS, lines=rendered, omitted=len(first) - len(kept))


def _tail(lines: list[str]) -> Excerpt:
    """How the run ended, which is often the only thing that matters."""
    return Excerpt(title=TAIL, lines=tuple(_clip(line) for line in lines[-MAX_TAIL_LINES:]))


def _is_benign(line: str) -> bool:
    return any(pattern.search(line) for pattern in BENIGN_SIGNATURES)


def _repeat_suffix(count: int) -> str:
    return f"   (×{count})" if count > 1 else ""


def _clip(line: str) -> str:
    """One line, short enough to read. Trailing \\r is DCS writing CRLF."""
    line = line.rstrip("\r")
    if len(line) <= MAX_LINE_CHARS:
        return line
    return line[: MAX_LINE_CHARS - 1] + "…"
