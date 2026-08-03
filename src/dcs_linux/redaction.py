"""Making a diagnostics bundle safe to post in public.

`report` exists to be pasted into a GitHub issue or a forum thread, so
everything it emits goes through here first. The rule is shape-preserving
redaction: a bug report is only useful if the reader can still see that a
path is a home directory or that an install sits on another drive, so names
are replaced rather than paths dropped.

This module never reads the machine and never decides *what* to include —
`Saved Games/DCS/Config/authdata.bin` is the ED credential and is excluded by
never being collected, not by being scrubbed here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from dcs_linux.system import System

USER = "<user>"
EMAIL = "<email>"
IDENTIFIER = "<id>"
ADDRESS = "<ip>"
UNKNOWN = "unknown"

# Profile names that identify nobody. steamuser is what umu creates, and which
# name a prefix uses is a diagnostic in itself: it says which launcher built
# it. Public is wine's shared profile.
KEPT_USERS = frozenset({"steamuser", "public"})

# Below this, a username is too generic to pattern-match on: `\bed\b` would
# eat half a DCS log. Such a name is still removed from every path.
MIN_MATCHABLE_USER = 3

# Addresses that are the same on every machine, so they identify nobody and
# their presence in the log is worth seeing.
KEPT_ADDRESSES = frozenset({"0.0.0.0", "255.255.255.255"})

_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"

_EMAIL = re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
# Home roots as the distros spell them: /var/home is Bazzite and Silverblue.
_UNIX_HOME = re.compile(r"(/var/home|/home|/Users)/([^/\s\"'\\]+)")
_WINDOWS_PROFILE = re.compile(r"([A-Za-z]:[\\/]users[\\/])([^\\/\s\"']+)", re.IGNORECASE)
_UUID = re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", re.IGNORECASE)
_IPV4 = re.compile(rf"\b{_OCTET}(?:\.{_OCTET}){{3}}\b")


@dataclass(frozen=True)
class Redactor:
    """Removes identifying data from text destined for a public post.

    `enabled=False` is the `--no-redact` escape hatch: the same object, doing
    nothing, so no caller has to know whether redaction is on.
    """

    home: Path
    user: str
    enabled: bool = True

    def scrub(self, text: str) -> str:
        """The text with names, addresses and account identifiers removed."""
        if not self.enabled:
            return text
        text = _EMAIL.sub(EMAIL, text)
        # The user's own home first, so it reads as ~ rather than as some
        # anonymous home directory. The boundary keeps /home/olivia out of it
        # when the user is oliver.
        text = re.sub(rf"{re.escape(str(self.home))}\b", "~", text)
        text = _UNIX_HOME.sub(lambda match: f"{match.group(1)}/{_name(match.group(2))}", text)
        text = _WINDOWS_PROFILE.sub(lambda match: f"{match.group(1)}{_name(match.group(2))}", text)
        if len(self.user) >= MIN_MATCHABLE_USER and self.user.lower() not in KEPT_USERS:
            text = re.sub(rf"\b{re.escape(self.user)}\b", USER, text)
        text = _UUID.sub(IDENTIFIER, text)
        text = _IPV4.sub(_address, text)
        return text

    def path(self, path: Path | None) -> str:
        """A path, redacted, or `unknown` if there is none."""
        if path is None:
            return UNKNOWN
        return self.scrub(str(path))


def redactor_for(system: System, *, enabled: bool = True) -> Redactor:
    """The redactor for this machine, named after whoever is running it."""
    home = system.home()
    return Redactor(home=home, user=home.name, enabled=enabled)


def _name(name: str) -> str:
    return name if name.lower() in KEPT_USERS else USER


def _address(match: re.Match[str]) -> str:
    address = match.group(0)
    if address in KEPT_ADDRESSES or address.startswith("127."):
        return address
    return ADDRESS
