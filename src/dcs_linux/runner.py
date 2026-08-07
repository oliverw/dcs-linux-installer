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
import time
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
        timeout: float | None = DEFAULT_TIMEOUT,
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
        timeout: float | None = DEFAULT_TIMEOUT,
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
        except subprocess.TimeoutExpired:
            return Completed(returncode=None, detail=f"timed out after {timeout:.0f}s")
        except OSError as error:
            return Completed(returncode=None, detail=str(error))
        return Completed(returncode=completed.returncode)

    def _run_in_session(
        self, command: list[str], environment: dict[str, str], timeout: float | None
    ) -> Completed:
        """Run a whole process tree, and take all of it down when we stop waiting.

        Its own session means Ctrl-C no longer reaches the tree — the terminal
        signals its foreground group, and this is deliberately not in it. So
        the interrupt has to be forwarded by hand, or the one thing a user does
        to abort a four-hour wait would leave DCS running.
        """
        started = time.monotonic()
        process = subprocess.Popen(command, env=environment, start_new_session=True)
        group = process.pid
        try:
            returncode = process.wait(timeout=timeout)
            remaining = (
                None if timeout is None else max(0.0, timeout - (time.monotonic() - started))
            )
            if not _wait_for_group(group, remaining):
                _stop_group(process, group)
                return Completed(
                    returncode=None,
                    detail=f"timed out after {timeout:.0f}s and was stopped",
                )
            return Completed(returncode=returncode)
        except subprocess.TimeoutExpired:
            _stop_group(process, group)
            return Completed(
                returncode=None,
                detail=f"timed out after {timeout:.0f}s and was stopped",
            )
        except KeyboardInterrupt:
            _stop_group(process, group)
            raise


def _wait_for_group(group: int, timeout: float | None) -> bool:
    """Wait until every process in `group` has exited."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        try:
            os.killpg(group, 0)
        except OSError:
            return True
        if deadline is not None and time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _stop_group(process: subprocess.Popen[bytes], group: int) -> None:
    """Ask the process group to stop, then insist.

    Best-effort throughout: every failure here means the group is already gone,
    which is the outcome being asked for.
    """
    for stop in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, stop)
        except OSError:
            return
        deadline = time.monotonic() + TERMINATION_GRACE
        while time.monotonic() < deadline:
            process.poll()
            try:
                os.killpg(group, 0)
            except OSError:
                return
            time.sleep(0.05)
