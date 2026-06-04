"""In-process pub/sub for dashboard events.

Producers (e.g. session lifecycle hooks, action runner) publish typed events;
each connected WebSocket subscriber gets its own bounded queue and receives a
copy of every event published after it subscribed. A slow subscriber is
dropped rather than allowed to back up the producer — the dashboard is a
view layer, never a back-pressure source.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Per-subscriber buffer. Sized to absorb a brief stall (e.g. a slow ws send)
# without losing events under normal traffic, but bounded so a stuck client
# can never trap the producer.
_DEFAULT_QUEUE_SIZE = 256


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _Subscriber:
    """One consumer's slot. Holds the queue and the loop that owns it.

    The loop reference matters: ``publish`` may be called from a thread that
    does not own the loop the queue was created on (notably ``starlette``'s
    ``TestClient``, which runs the app in a portal thread). ``put_nowait`` is
    thread-safe on a queue bound to a different loop only if we schedule it
    via ``loop.call_soon_threadsafe`` — doing the put directly can drop the
    item silently when the producer is on the wrong thread.
    """

    __slots__ = ("queue", "loop")

    def __init__(self, queue: asyncio.Queue[dict[str, Any]], loop: asyncio.AbstractEventLoop) -> None:
        self.queue = queue
        self.loop = loop


class DashboardEventBus:
    """Tiny multi-consumer broadcast bus for dashboard events.

    No ``asyncio.Lock`` here on purpose — ``set.add/discard``/copy are atomic
    under CPython's GIL, and avoiding the lock lets ``publish()`` be called
    safely from any loop or thread (so tests can drive events from a fixture
    while the WS handler runs in a portal thread).
    """

    def __init__(self, *, queue_size: int = _DEFAULT_QUEUE_SIZE) -> None:
        self._subscribers: set[_Subscriber] = set()
        self._queue_size = queue_size

    def publish_nowait(self, kind: str, payload: dict[str, Any] | None = None) -> None:
        """Sync flavour of ``publish`` for callers outside an async context.

        Useful from threads that don't have a running loop (test fixtures,
        signal handlers). Otherwise prefer ``publish``.
        """
        event = {
            "kind": kind,
            "ts": _utc_now_iso(),
            "data": payload or {},
        }
        # Snapshot so a concurrent unsubscribe doesn't mutate during iteration.
        for sub in list(self._subscribers):
            self._deliver(sub, event)

    async def publish(self, kind: str, payload: dict[str, Any] | None = None) -> None:
        """Fan an event out to every current subscriber."""
        self.publish_nowait(kind, payload)

    def _deliver(self, sub: _Subscriber, event: dict[str, Any]) -> None:
        # If we're already on the queue's loop, put_nowait is the cheap path.
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is sub.loop:
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.debug("Dashboard subscriber queue full; dropping subscriber")
                self._subscribers.discard(sub)
            return
        # Cross-loop / cross-thread: hop onto the consumer's loop.
        try:
            sub.loop.call_soon_threadsafe(self._safe_put, sub, event)
        except RuntimeError:
            # Loop is closed — subscriber is already gone.
            self._subscribers.discard(sub)

    def _safe_put(self, sub: _Subscriber, event: dict[str, Any]) -> None:
        try:
            sub.queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.debug("Dashboard subscriber queue full; dropping subscriber")
            self._subscribers.discard(sub)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        sub = _Subscriber(queue=queue, loop=asyncio.get_running_loop())
        self._subscribers.add(sub)
        try:
            yield queue
        finally:
            self._subscribers.discard(sub)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
