"""LogBuffer — capped FIFO + asyncio pub-sub cho SSE subscribers (Task 5.1).

Requirements: 10.4, 10.5, 10.6, 10.7.

Design notes:
    - ``LogEvent`` được định nghĩa ở ``icloud_hme/web/schemas.py`` (Pydantic
      v2, frozen) làm single source of truth — module này chỉ re-export để
      backward-compatible với consumer cũ (task 5.2 refactor).
    - ``clear()`` reset seq về ``0`` — gọi mỗi lần Runner ``start()`` mới
      (R10.6).
    - ``push()`` non-blocking: ``q.put_nowait()`` bắt ``asyncio.QueueFull``
      để drop event cho subscriber chậm (R10.7); KHÔNG block Runner và
      KHÔNG block các subscriber khác.
    - ``subscribe()`` replay history từ deque tới queue mới trước khi add
      vào set ``_subscribers`` → subscriber muộn vẫn nhận đủ log đã có.
    - ``make_web_log_callback`` wrap ``buffer.push`` thành ``LogCallback``
      để HmeRunner gọi mà không depend trực tiếp ``LogBuffer``.
"""

from __future__ import annotations

import asyncio
import collections
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, AsyncIterator

from .schemas import LogEvent

if TYPE_CHECKING:  # pragma: no cover — type-only import, tránh cycle runtime.
    from runner import LogCallback

    from web.sse_mux import SseMux


# ── LogBuffer ────────────────────────────────────────────────────────────────
class LogBuffer:
    """Capped FIFO buffer + asyncio pub-sub cho SSE subscribers.

    Single-instance per process (DI ở Web layer). Async-safe trong asyncio
    single-threaded loop: mọi method không await trên shared state ngoại trừ
    ``subscribe()`` (chỉ await ở ``q.get()`` trong vòng yield).
    """

    MAX_ENTRIES: int = 10_000
    SUBSCRIBER_QUEUE_MAXSIZE: int = 1_000

    def __init__(
        self,
        sse_mux: "SseMux | None" = None,
        channel_name: str = "",
    ) -> None:
        self._entries: collections.deque[LogEvent] = collections.deque(
            maxlen=self.MAX_ENTRIES
        )
        self._subscribers: set[asyncio.Queue[LogEvent]] = set()
        self._seq: int = 0
        self._sse_mux: "SseMux | None" = sse_mux
        self._channel_name: str = channel_name

    # ── SseMux wiring ──────────────────────────────────────────────────
    def set_sse_mux(self, mux: "SseMux", channel_name: str) -> None:
        """Attach SseMux after construction (lazy-init buffers use this)."""
        self._sse_mux = mux
        self._channel_name = channel_name

    # ── Read-only inspection ────────────────────────────────────────────
    @property
    def seq(self) -> int:
        """Counter cuối cùng đã phát; ``0`` ngay sau ``clear()``."""
        return self._seq

    def __len__(self) -> int:
        return len(self._entries)

    def snapshot(self) -> list[LogEvent]:
        """Trả list bản sao các entry hiện có (an toàn để iterate ngoài)."""
        return list(self._entries)

    # ── Mutators ────────────────────────────────────────────────────────
    def clear(self) -> None:
        """Xoá toàn bộ entry và reset seq về ``0`` (R10.6).

        Không đóng subscriber đang lắng nghe — họ vẫn nhận event mới push
        sau ``clear()``. Web layer gọi method này mỗi khi Runner ``start()``
        chuyển ``is_running`` từ ``False`` sang ``True``.
        """
        self._entries.clear()
        self._seq = 0

    async def push(
        self, level: str, message: str, payload: dict[str, Any]
    ) -> None:
        """Append event vào buffer + broadcast non-blocking sang subscribers.

        Steps:
            1. Tăng ``_seq`` đơn điệu.
            2. Build ``LogEvent`` với ``ts`` ISO 8601 UTC + defensive-copy payload.
            3. Append vào deque (auto-evict entry cũ nhất khi vượt MAX_ENTRIES — R10.5).
            4. Broadcast: ``q.put_nowait(event)`` cho từng subscriber; bắt
               ``asyncio.QueueFull`` → drop event cho subscriber đó (R10.7).

        Signature khớp ``LogCallback`` (``async (level, message, payload)``)
        nên có thể dùng trực tiếp qua ``make_web_log_callback``.
        """
        self._seq += 1
        event = LogEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            level=level,
            message=message,
            payload=dict(payload),  # defensive copy — caller có thể mutate
            seq=self._seq,
        )
        self._entries.append(event)
        # Iterate over snapshot vì subscriber có thể discard chính nó (race
        # với ``finally`` trong subscribe loop). ``list(...)`` là sync, atomic
        # trong event loop trước bất kỳ await nào.
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Subscriber chậm — drop event cho subscriber đó (R10.7).
                # Không re-raise để KHÔNG block Runner và subscriber khác.
                continue

        # Hook: publish to SseMux (unified SSE multiplexer)
        if self._sse_mux is not None:
            self._sse_mux.publish(self._channel_name, event.model_dump())

    # ── Subscriber API ──────────────────────────────────────────────────
    async def subscribe(self) -> AsyncIterator[LogEvent]:
        """Async iterator yield event cho 1 subscriber.

        Behavior:
            - Tạo ``asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAXSIZE)``.
            - Replay history từ deque (snapshot tại thời điểm subscribe) vào
              queue trước khi add vào set ``_subscribers`` → subscriber muộn
              vẫn nhận log đã có. Nếu history > queue maxsize, drop entry cũ
              nhất qua ``put_nowait`` (fail-fast, không block subscribe).
            - Yield event qua ``q.get()`` cho tới khi consumer cancel /
              ``aclose()``.
            - ``finally`` luôn discard queue khỏi set để không leak (R9 risk).
        """
        q: asyncio.Queue[LogEvent] = asyncio.Queue(
            maxsize=self.SUBSCRIBER_QUEUE_MAXSIZE
        )
        # Replay history TRƯỚC khi add vào ``_subscribers`` để event mới
        # ``push()`` không bị double-deliver (vào q qua replay + qua broadcast).
        for event in self.snapshot():
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # History lớn hơn queue maxsize — fail-fast, dừng replay.
                # Subscriber sẽ chỉ thấy phần đầu lịch sử + event mới.
                break
        self._subscribers.add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subscribers.discard(q)


# ── Helper factory ───────────────────────────────────────────────────────────
def make_web_log_callback(buffer: "LogBuffer") -> "LogCallback":
    """Wrap ``buffer.push`` thành ``LogCallback`` cho HmeRunner.

    Giúp Runner không depend trực tiếp ``LogBuffer`` — chỉ thấy callback
    signature ``(level, message, payload) -> Awaitable[None]``.
    """

    async def _cb(level: str, message: str, payload: dict[str, Any]) -> None:
        await buffer.push(level, message, payload)

    return _cb
