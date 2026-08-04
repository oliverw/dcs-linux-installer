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
import signal
import subprocess
from dataclasses import dataclass
from typing import Protocol

# Fetching GE-Proton, unpacking it and letting winetricks install corefonts
# over a slow link all happen inside one command each. Generous on purpose:
# the failure mode of a short timeout here is killing a working install
# halfway through.
DEFAULT_TIMEOUT = 3600.0

# How long a process group gets to shut down after being asked politely,
# before it is killed outright. Long enough for wine to unwind its server,
# short enough that a wedged process cannot hold the command open.
TERMINATION_GRACE = 10.0


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
        self,
        command: list[str],
        environment: dict[str, str],
        timeout: float = DEFAULT_TIMEOUT,
        *,
        own_session: bool = False,
    ) -> Completed:
        """Run `command` with `environment` layered on the ambient one.

        `own_session` puts the command in a process group of its own so that a
        timeout can stop the *whole tree*. Launching DCS means launching umu,
        which launches Proton, which launches wine, which launches the game:
        killing only the process started here leaves the rest running against a
        prefix the next command assumes is idle.
        """


class RealRunner:
    """`Runner` backed by real processes."""

    def run(
        self,
        command: list[str],
        environment: dict[str, str],
        timeout: float = DEFAULT_TIMEOUT,
        *,
        own_session: bool = False,
    ) -> Completed:
        # Layered over the real environment rather than replacing it: umu needs
        # a working HOME, DISPLAY and XDG_RUNTIME_DIR to do anything at all,
        # interactive or not.
        merged = {**os.environ, **environment}
        try:
            if own_session:
                return self._run_in_session(command, merged, timeout)
            completed = subprocess.run(command, env=merged, timeout=timeout, check=False)
        except FileNotFoundError:
            return Completed(returncode=None, detail=f"{command[0]} could not be executed")
        except OSError as error:
            return Completed(returncode=None, detail=str(error))
        except subprocess.TimeoutExpired:
            return Completed(returncode=None, detail=f"timed out after {timeout:.0f}s")
        return Completed(returncode=completed.returncode)

    def _run_in_session(
        self, command: list[str], environment: dict[str, str], timeout: float
    ) -> Completed:
        """Run a whole process tree, and take all of it down on a timeout."""
        process = subprocess.Popen(command, env=environment, start_new_session=True)
        try:
            return Completed(returncode=process.wait(timeout=timeout))
        except subprocess.TimeoutExpired:
            _stop_group(process)
            return Completed(
                returncode=None,
                detail=f"timed out after {timeout:.0f}s and was stopped",
            )


def _stop_group(process: subprocess.Popen[bytes]) -> None:
    """Ask the process group to stop, then insist.

    Best-effort throughout: every failure here means the group is already gone,
    which is the outcome being asked for.
    """
    try:
        group = os.getpgid(process.pid)
    except OSError:
        return
    for stop in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, stop)
        except OSError:
            return
        try:
            process.wait(timeout=TERMINATION_GRACE)
            return
        except subprocess.TimeoutExpired:
            continue
