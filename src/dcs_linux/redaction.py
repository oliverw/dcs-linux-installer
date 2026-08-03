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

# Below this, a username is too generic to pattern-match on its own: `\bed\b`
# would eat half a DCS log. Such a name is still removed from the user-named
# directories below, which is where it actually appears.
MIN_MATCHABLE_USER = 3

# Addresses that are the same on every machine, so they identify nobody and
# their presence in the log is worth seeing.
KEPT_ADDRESSES = frozenset({"0.0.0.0", "255.255.255.255"})

_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"

# Every directory whose next component is a user name. /var/home is Bazzite
# and Silverblue; /run/media and /media are where udisks mounts the drive a
# second DCS install so often lives on.
_USER_DIRS = r"(?:var[\\/]home|home|Users|run[\\/]media|media)"
# Both separators, because DCS logs wine paths — `Z:\home\jenny\...` is the
# same directory the shell spells `/home/jenny`.
_SEPARATOR = r"[\\/]"

_EMAIL = re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_USER_DIR = re.compile(rf"({_SEPARATOR}{_USER_DIRS}{_SEPARATOR})([^\\/\s\"']+)", re.IGNORECASE)
_WINDOWS_PROFILE = re.compile(rf"([A-Za-z]:{_SEPARATOR}users{_SEPARATOR})([^\\/\s\"']+)", re.I)
_UUID = re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", re.IGNORECASE)
# A SteamID64 names one Steam account. They all start with this prefix, so it
# can be matched without touching the numbers around it.
_STEAM_ID = re.compile(r"\b7656119\d{10}\b")
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
        # anonymous home directory. The lookahead means the whole component
        # has to match: /home/oliver-old is somebody else's directory.
        text = re.sub(rf"{re.escape(str(self.home))}(?=[/\\\s\"']|$)", "~", text)
        text = _USER_DIR.sub(_replace_name, text)
        text = _WINDOWS_PROFILE.sub(_replace_name, text)
        if len(self.user) >= MIN_MATCHABLE_USER and self.user.lower() not in KEPT_USERS:
            text = re.sub(rf"\b{re.escape(self.user)}\b", USER, text)
        text = _UUID.sub(IDENTIFIER, text)
        text = _STEAM_ID.sub(IDENTIFIER, text)
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


def _replace_name(match: re.Match[str]) -> str:
    """Keep the directory, replace the name in it — separators and all."""
    directory, name = match.group(1), match.group(2)
    return directory + (name if name.lower() in KEPT_USERS else USER)


def _address(match: re.Match[str]) -> str:
    address = match.group(0)
    if address in KEPT_ADDRESSES or address.startswith("127."):
        return address
    return ADDRESS
