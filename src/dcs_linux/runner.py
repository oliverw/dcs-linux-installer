"""The seam through which this tool runs long-lived external commands.

`dcs_linux.system.System.run` exists for asking the machine a question — a
`--version`, a `glxinfo` — and its fifteen-second timeout says so. Building a
prefix is a different kind of call: it takes minutes, needs a curated
environment (`WINEPREFIX`, `GAMEID`, `PROTONPATH`) and its output belongs on
the user's terminal rather than in a captured string, because `winetricks`
downloading forty megabytes of fonts with nothing on screen looks like a hang.

So it is a separate protocol. Keeping it apart from `System` also keeps the
reading interface incapable of starting a process, which is what stops
`check` and discovery from ever doing so.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Protocol

# Fetching GE-Proton, unpacking it and letting winetricks install corefonts
# over a slow link all happen inside one command each. Generous on purpose:
# the failure mode of a short timeout here is killing a working install
# halfway through.
DEFAULT_TIMEOUT = 3600.0


@dataclass(frozen=True)
class Completed:
    """How an external command ended.

    `returncode` is `None` when the command could not be started or timed out
    — a distinction that matters here, because umu exits non-zero on a run
    that created a perfectly good prefix (ADR-0003).
    """

    returncode: int | None
    detail: str = ""

    @property
    def started(self) -> bool:
        return self.returncode is not None


class Runner(Protocol):
    """Runs a command with an environment, and waits for it."""

    def run(
        self, command: list[str], environment: dict[str, str], timeout: float = DEFAULT_TIMEOUT
    ) -> Completed:
        """Run `command` with `environment` layered on the ambient one."""


class RealRunner:
    """`Runner` backed by real processes."""

    def run(
        self, command: list[str], environment: dict[str, str], timeout: float = DEFAULT_TIMEOUT
    ) -> Completed:
        # Layered over the real environment rather than replacing it: umu needs
        # a working HOME, DISPLAY and XDG_RUNTIME_DIR to do anything at all,
        # interactive or not.
        merged = {**os.environ, **environment}
        try:
            completed = subprocess.run(command, env=merged, timeout=timeout, check=False)
        except FileNotFoundError:
            return Completed(returncode=None, detail=f"{command[0]} could not be executed")
        except subprocess.TimeoutExpired:
            return Completed(returncode=None, detail=f"timed out after {timeout:.0f}s")
        except OSError as error:
            return Completed(returncode=None, detail=str(error))
        return Completed(returncode=completed.returncode)
