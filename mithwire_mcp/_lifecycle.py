"""Process-lifecycle helpers: make sure no browser outlives the server (ELE-174).

Three independent layers keep Chrome from being orphaned, each covering what the
one before cannot:

1. :func:`finish_even_if_cancelled` -- request cancellation. MCP clients cancel
   requests (``notifications/cancelled``) and the SDK implements that with an
   anyio cancel scope, which -- unlike a plain ``Task.cancel()`` -- re-delivers
   the cancellation at *every* checkpoint until the scope exits. Any ``await``
   in an ``except``/``finally`` of a cancelled request is therefore liable to be
   interrupted again, which is how half-started browsers used to be abandoned.
2. :func:`terminate_browser_processes` + :func:`exit_when_browsers_gone` --
   termination signals. Both are synchronous and need no event loop, so they work from a
   signal handler even when the normal shutdown path is stuck (the SDK reads
   stdin on a worker thread that cannot be interrupted while it waits for the
   next line, so after SIGTERM the regular teardown can block forever).
3. The engine's exit guard (``mithwire.core._exit_guard``) -- a sidecar that
   reaps the browser when this process is killed outright (SIGKILL, OOM).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from collections.abc import Awaitable
from typing import Any

logger = logging.getLogger(__name__)

# Strong references: a fire-and-forget task nobody holds can be garbage
# collected -- and silently dropped -- before it finishes.
_pending: set[asyncio.Future] = set()


async def _log_failures(awaitable: Awaitable[Any], what: str) -> None:
    try:
        await awaitable
    except Exception:  # noqa: BLE001
        logger.warning("%s failed", what, exc_info=True)


async def finish_even_if_cancelled(awaitable: Awaitable[Any], *, what: str = "cleanup") -> None:
    """Run ``awaitable`` to completion in a task of its own.

    The caller waits for it, but being cancelled -- once or repeatedly -- only
    stops the *waiting*: the work carries on regardless. The caller's
    cancellation is re-raised, never swallowed. Failures of the work itself are
    logged rather than raised, so a cleanup error cannot mask the exception that
    triggered the cleanup.
    """
    task = asyncio.ensure_future(_log_failures(awaitable, what))
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    await asyncio.shield(task)


async def drain(timeout: float) -> None:
    """Wait (bounded) for cleanups started by :func:`finish_even_if_cancelled`."""
    loop = asyncio.get_running_loop()
    running = [t for t in _pending if not t.done() and t.get_loop() is loop]
    if running:
        await asyncio.wait(running, timeout=timeout)


def _live_browser_processes() -> list[Any]:
    """Subprocess handles of every browser the engine launched and has not reaped.

    ``returncode is None`` means the child has not been reaped yet, so its PID
    cannot have been recycled: signalling it can never hit an unrelated process.
    """
    try:
        from mithwire.core import util
    except Exception:  # noqa: BLE001
        return []
    processes = []
    for browser in list(util.get_registered_instances()):
        proc = getattr(browser, "_process", None)
        if proc is not None and getattr(proc, "returncode", None) is None:
            processes.append(proc)
    return processes


def terminate_browser_processes() -> int:
    """SIGTERM every live browser process. Synchronous; safe in a signal handler."""
    count = 0
    for proc in _live_browser_processes():
        try:
            proc.terminate()
            count += 1
        except (ProcessLookupError, OSError):
            pass
    return count


def kill_browser_processes() -> int:
    """SIGKILL every live browser process. Synchronous; safe in a signal handler."""
    count = 0
    for proc in _live_browser_processes():
        try:
            proc.kill()
            count += 1
        except (ProcessLookupError, OSError):
            pass
    return count


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # e.g. EPERM: it exists, it just is not ours to signal
        return True
    return True


def _force_exit(code: int) -> None:
    kill_browser_processes()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001
            pass
    os._exit(code)


def exit_when_browsers_gone(
    code: int,
    *,
    grace: float,
    poll: float = 0.1,
    settle: float = 0.25,
) -> threading.Thread:
    """Guarantee the process ends after a termination signal.

    Call this right after :func:`terminate_browser_processes`. A daemon thread
    waits for those browsers to go (at most ``grace`` seconds, after which the
    stragglers are killed) and then exits the process. This is what makes SIGTERM
    reliable: the graceful path -- unwinding the event loop so the lifespan
    teardown runs -- can block indefinitely, because the SDK's stdin reader
    thread cannot be interrupted while it waits for the next line, and an
    interpreter that cannot finish unwinding never exits.

    The engine's exit guard removes the browsers' ephemeral profiles once this
    process is gone, so a hard exit loses nothing.
    """
    pids = [proc.pid for proc in _live_browser_processes()]

    def watch() -> None:
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not any(_pid_running(pid) for pid in pids):
                time.sleep(settle)  # let an already-running teardown finish its logging
                break
            time.sleep(poll)
        _force_exit(code)

    thread = threading.Thread(target=watch, name="mithwire-mcp-exit-watchdog", daemon=True)
    thread.start()
    return thread
