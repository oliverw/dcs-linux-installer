"""Running long-lived external commands, and stopping all of one.

These use real processes on purpose. The thing being tested is that a *tree*
goes away — launching DCS means umu, then Proton, then wine, then the game —
and a fake process has no children to leave behind.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from dcs_linux.runner import RealRunner

# A shell that spawns a child and then waits: the same shape as umu launching
# Proton, small enough to be a test.
TREE = "sleep 60 & echo $! > {marker}; wait"
EARLY_PARENT_EXIT = "sleep 60 & echo $! > {marker}"


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def child_of(marker: str) -> int:
    """The pid the shell wrote, once it has had time to write it."""
    for _ in range(100):
        try:
            with open(marker) as handle:
                text = handle.read().strip()
            if text:
                return int(text)
        except FileNotFoundError:
            pass
        time.sleep(0.05)
    raise AssertionError("the command never started its child")


def test_a_missing_command_is_reported_not_raised() -> None:
    completed = RealRunner().run(["/nonexistent/umu-run"], {})
    assert not completed.started
    assert "could not be executed" in completed.detail


def test_an_exit_code_comes_back() -> None:
    completed = RealRunner().run(["sh", "-c", "exit 3"], {})
    assert completed.returncode == 3


def test_the_environment_is_layered_over_the_real_one() -> None:
    """umu needs HOME, DISPLAY and XDG_RUNTIME_DIR to do anything at all."""
    completed = RealRunner().run(["sh", "-c", 'test -n "$PATH" && test "$GAMEID" = umu-223750'], {})
    assert completed.returncode != 0
    completed = RealRunner().run(
        ["sh", "-c", 'test -n "$PATH" && test "$GAMEID" = umu-223750'],
        {"GAMEID": "umu-223750"},
    )
    assert completed.returncode == 0


def test_a_timeout_takes_the_whole_tree_down(tmp_path: Path) -> None:
    marker = str(tmp_path / "child.pid")
    completed = RealRunner().run(
        ["sh", "-c", TREE.format(marker=marker)], {}, timeout=1.0, own_session=True
    )
    child = child_of(marker)

    assert not completed.started
    assert "stopped" in completed.detail
    for _ in range(50):
        if not alive(child):
            break
        time.sleep(0.05)
    assert not alive(child), "the grandchild outlived the command it was started by"


def test_a_parent_exit_cleans_up_the_rest_of_its_tree(tmp_path: Path) -> None:
    """If umu exits first, the command still owns the Proton/Wine descendants."""
    marker = str(tmp_path / "child.pid")

    completed = RealRunner().run(
        ["sh", "-c", EARLY_PARENT_EXIT.format(marker=marker)],
        {},
        own_session=True,
    )
    child = child_of(marker)

    assert completed.returncode == 0
    for _ in range(50):
        if not alive(child):
            break
        time.sleep(0.05)
    assert not alive(child), "the command returned while its process tree was still running"


def test_an_interrupt_takes_the_whole_tree_down(tmp_path: pytest.TempPathFactory) -> None:
    """Its own session means Ctrl-C no longer reaches the tree by itself."""
    marker = str(tmp_path / "child.pid")  # type: ignore[operator]
    real_wait = subprocess.Popen.wait

    def interrupt_once(self: subprocess.Popen[bytes], timeout: float | None = None) -> int:
        subprocess.Popen.wait = real_wait  # type: ignore[method-assign]
        child_of(marker)
        raise KeyboardInterrupt

    subprocess.Popen.wait = interrupt_once  # type: ignore[method-assign,assignment]
    try:
        with pytest.raises(KeyboardInterrupt):
            RealRunner().run(["sh", "-c", TREE.format(marker=marker)], {}, own_session=True)
    finally:
        subprocess.Popen.wait = real_wait  # type: ignore[method-assign]

    child = child_of(marker)
    for _ in range(50):
        if not alive(child):
            break
        time.sleep(0.05)
    assert not alive(child), "Ctrl-C left DCS running"


def test_a_command_in_its_own_session_is_not_in_our_process_group() -> None:
    """Which is why the interrupt above has to be forwarded by hand."""
    completed = RealRunner().run(
        ["sh", "-c", f'test "$(ps -o pgid= -p $$ | tr -d " ")" != {os.getpgrp()}'],
        {},
        own_session=True,
    )
    assert completed.returncode == 0
