"""Unified SSE Multiplexer — fan-out hub for all real-time channels."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Subscriber:
    """Represents a single SSE client connection."""

    id: str
    queue: asyncio.Queue[dict]
    created_at: float


class SseMux:
    """Singleton multiplexed SSE fan-out hub.

    All managers/LogBuffers publish events through this hub,
    and all connected SSE clients receive them via their subscriber queues.
    """

    QUEUE_MAXSIZE: int = 1000

    def __init__(self) -> None:
        self._subscribers: dict[str, Subscriber] = {}
        self._snapshot_fns: dict[str, Callable[[], list[dict]]] = {}

    def register_snapshot(self, channel: str, fn: Callable[[], list[dict]]) -> None:
        """Register snapshot generator for a channel (called at service init)."""
        self._snapshot_fns[channel] = fn

    def subscribe(self) -> tuple[str, asyncio.Queue[dict]]:
        """Create new Subscriber. Returns (subscriber_id, queue)."""
        sub_id = str(uuid.uuid4())
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=self.QUEUE_MAXSIZE)
        self._subscribers[sub_id] = Subscriber(
            id=sub_id, queue=queue, created_at=time.monotonic()
        )
        return sub_id, queue

    def unsubscribe(self, sub_id: str) -> None:
        """Remove Subscriber, release queue reference."""
        self._subscribers.pop(sub_id, None)

    def publish(self, channel: str, event: dict) -> None:
        """Fan-out event to ALL active subscribers (non-blocking).

        Wraps event with channel field. Drops on full queue.
        """
        wrapped = {**event, "channel": channel}
        for sub in list(self._subscribers.values()):
            try:
                sub.queue.put_nowait(wrapped)
            except asyncio.QueueFull:
                pass  # Drop for slow subscriber, don't block others

    def generate_snapshots(self) -> list[dict]:
        """Generate snapshot events for all 6 channels (ordered).

        Returns list of dicts, each with 'channel' field.
        """
        CHANNEL_ORDER = ["reg", "session", "link", "upi", "hme_log", "autoreg_log"]
        events: list[dict] = []
        for ch in CHANNEL_ORDER:
            fn = self._snapshot_fns.get(ch)
            if fn is None:
                continue
            try:
                snapshot_data = fn()
                for item in snapshot_data:
                    events.append({**item, "channel": ch})
            except Exception:
                pass  # Best-effort snapshot, don't crash connection
        return events

    @property
    def subscriber_count(self) -> int:
        """Number of currently active subscribers."""
        return len(self._subscribers)
