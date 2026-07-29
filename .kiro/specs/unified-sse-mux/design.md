# Design Document: Unified SSE Multiplexer

## Overview

Gộp 6 SSE endpoints riêng lẻ thành 1 endpoint duy nhất `GET /api/sse` với channel-based multiplexing. Backend component `SseMux` (singleton) nhận events từ tất cả managers/LogBuffers và fan-out tới mọi subscriber. Frontend component `SseBus` quản lý single EventSource connection và route events theo `channel` field tới per-module handlers.

**Mục tiêu**: Giải phóng 5 HTTP/1.1 connection slots (từ 6 SSE → 1) để browser không bị queue API requests.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ Backend (Python / FastAPI / asyncio)                             │
│                                                                 │
│  ┌─────────────┐  ┌──────────────────┐  ┌──────────────────┐   │
│  │ JobManager  │  │SessionJobManager │  │ LinkJobManager   │   │
│  │ _broadcast()│  │  _broadcast()    │  │  _broadcast()    │   │
│  └──────┬──────┘  └────────┬─────────┘  └────────┬─────────┘   │
│         │                  │                      │             │
│  ┌──────┴──────┐  ┌───────┴──────┐  ┌────────────┴─────────┐   │
│  │HotmailMgr  │  │HME LogBuffer │  │AutoReg LogBuffer     │   │
│  │_broadcast() │  │   push()     │  │     push()           │   │
│  └──────┬──────┘  └───────┬──────┘  └────────────┬─────────┘   │
│         │                  │                      │             │
│         ▼                  ▼                      ▼             │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                    SseMux (singleton)                    │    │
│  │  publish(channel, event)  →  fan-out to ALL subscribers │    │
│  │  subscribe() → (sub_id, Queue)                          │    │
│  │  unsubscribe(sub_id)                                    │    │
│  └────────────────────────┬────────────────────────────────┘    │
│                           │                                     │
│              ┌────────────┼────────────────┐                    │
│              ▼            ▼                ▼                    │
│         [Queue_A]    [Queue_B]        [Queue_N]                │
│         Subscriber   Subscriber       Subscriber               │
│                                                                 │
│  GET /api/sse?token=...                                         │
│    → create Subscriber                                          │
│    → yield snapshots (6 channels)                               │
│    → loop: queue.get() / heartbeat 5s                           │
│    → finally: unsubscribe                                       │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ Frontend (vanilla JavaScript)                                   │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                   SseBus (module)                        │    │
│  │  connect()  →  new EventSource('/api/sse?token=...')     │    │
│  │  on(channel, callback)  →  register handler             │    │
│  │  onmessage → parse JSON → route by data.channel         │    │
│  │  onerror → close + reconnect after 3s                   │    │
│  └──────────────────────────┬──────────────────────────────┘    │
│                             │                                   │
│          ┌──────────────────┼──────────────────────┐            │
│          ▼                  ▼                      ▼            │
│    on('reg', ...)    on('session', ...)    on('hotmail', ...)   │
│    app.js handler    session.js handler   hotmail.js handler    │
│                                                                 │
│    on('link', ...)   on('hme_log', ...)   on('autoreg_log',..) │
│    link.js handler   hme.js handler       autoreg.js handler   │
└─────────────────────────────────────────────────────────────────┘
```

## Components and Interfaces

### Backend: `web/sse_mux.py`

#### Subscriber Dataclass

```python
@dataclass
class Subscriber:
    id: str                          # UUID4, unique per connection
    queue: asyncio.Queue[dict]       # maxsize=1000
    created_at: float                # time.monotonic() for diagnostics
```

#### SseMux Class

```python
class SseMux:
    """Singleton multiplexed SSE fan-out hub."""

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
        queue = asyncio.Queue(maxsize=self.QUEUE_MAXSIZE)
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
        CHANNEL_ORDER = ["reg", "session", "link", "hotmail", "hme_log", "autoreg_log"]
        events = []
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
        return len(self._subscribers)
```

### Backend: SSE Endpoint (`web/server.py`)

```python
@app.get("/api/sse")
async def unified_sse(request: Request) -> StreamingResponse:
    """Single unified SSE endpoint for all channels."""
    sub_id, queue = _sse_mux.subscribe()

    async def gen():
        try:
            # 1. Send snapshots for all 6 channels
            snapshots = _sse_mux.generate_snapshots()
            for snap in snapshots:
                yield f"data: {json.dumps(snap)}\n\n"

            # 2. Stream live events with heartbeat
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=5.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                except (asyncio.CancelledError, GeneratorExit):
                    break
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            _sse_mux.unsubscribe(sub_id)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
```

**Auth**: Xử lý bởi `auth_middleware` hiện có (gate `/api/*` routes). Token qua `?token=` query param (EventSource không set custom header được).

### Backend: Manager Integration

Mỗi manager thêm hook publish vào `_broadcast()`:

```python
# JobManager._broadcast (channel="reg")
def _broadcast(self, event: dict[str, Any]) -> None:
    for q in list(self._subscribers):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass
    # Hook: publish to SseMux
    if _sse_mux is not None:
        _sse_mux.publish("reg", event)
```

Tương tự cho:
- `SessionJobManager._broadcast` → `channel="session"`
- `LinkJobManager._broadcast` → `channel="link"`
- `HotmailJobManager._broadcast_event` → `channel="hotmail"`

Cho LogBuffer, hook vào `push()`:
```python
async def push(self, level: str, message: str, payload: dict[str, Any]) -> None:
    # ... existing logic (build event, append deque, broadcast to direct subscribers) ...
    # Hook: publish to SseMux
    if self._sse_mux is not None:
        self._sse_mux.publish(self._channel_name, event.model_dump())
```

### Backend: Snapshot Registration

Snapshot functions được register tại service init (on_startup):

```python
_sse_mux.register_snapshot("reg", lambda: [manager.build_snapshot()])
_sse_mux.register_snapshot("session", lambda: [sm.build_snapshot()])
_sse_mux.register_snapshot("link", lambda: [lm.build_snapshot()])
_sse_mux.register_snapshot("hotmail", lambda: [hm.build_snapshot()])
_sse_mux.register_snapshot("hme_log", lambda: [e.model_dump() for e in hme_buffer.snapshot()])
_sse_mux.register_snapshot("autoreg_log", lambda: [e.model_dump() for e in autoreg_buffer.snapshot()])
```

Snapshot format per channel:

| Channel | Snapshot Format |
|---------|----------------|
| `reg` | `{"channel":"reg","type":"snapshot","max_concurrent":N,"headless":bool,...,"jobs":[...]}` |
| `session` | `{"channel":"session","type":"snapshot","max_concurrent":N,"job_timeout":N,...,"jobs":[...]}` |
| `link` | `{"channel":"link","type":"snapshot","max_concurrent":N,...,"region":"VN","jobs":[...]}` |
| `hotmail` | `{"channel":"hotmail","type":"snapshot","jobs":[...],"config":{...},"stats":{...}}` |
| `hme_log` | Multiple `{"channel":"hme_log","ts":"...","level":"...","message":"...","seq":N}` |
| `autoreg_log` | Multiple `{"channel":"autoreg_log","ts":"...","level":"...","message":"...","seq":N}` |

### Frontend: `SseBus` Module (in `app.js`)

```javascript
const SseBus = (() => {
  let _es = null;
  let _reconnectTimer = null;
  const _handlers = new Map();  // channel -> [callback, ...]

  function connect() {
    if (_es && _es.readyState !== 2) return;
    _disconnect();
    const url = withTokenQuery('/api/sse');
    _es = new EventSource(url);

    _es.onmessage = (e) => {
      let data;
      try { data = JSON.parse(e.data); } catch (_) { return; }
      const channel = data.channel;
      if (!channel) return;
      const cbs = _handlers.get(channel);
      if (cbs) cbs.forEach(cb => cb(data));
    };

    _es.onerror = () => {
      _disconnect();
      _reconnectTimer = setTimeout(connect, 3000);
    };
  }

  function on(channel, callback) {
    if (!_handlers.has(channel)) _handlers.set(channel, []);
    _handlers.get(channel).push(callback);
  }

  function _disconnect() {
    if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }
    if (_es) { try { _es.close(); } catch (_) {} _es = null; }
  }

  return { connect, on };
})();
```

### Frontend: Per-Module Migration

Mỗi module thay thế `connectSSE()`/`disconnectSSE()` + local `EventSource` bằng `SseBus.on(channel, handler)`:

```javascript
// session.js — BEFORE
function connectSSE() { ... new EventSource('/api/session/events') ... }
function disconnectSSE() { ... }
document.addEventListener('gpt:tab', (e) => {
  if (e.detail.tab === 'session') connectSSE();
  else disconnectSSE();
});

// session.js — AFTER
SseBus.on('session', (data) => {
  // Same handler logic as current es.onmessage callback
  if (data.type === 'snapshot') { ... }
  else if (data.type === 'job') { ... }
  else if (data.type === 'log') { ... }
});
```

Áp dụng tương tự cho: `app.js` (reg), `link.js`, `hotmail.js`, `hme.js` (hme_log), `autoreg.js` (autoreg_log).

## Data Models

### SSE Wire Format

Mỗi event gửi qua unified endpoint có dạng:

```
data: {"channel":"<channel_name>", ...event_payload}\n\n
```

Heartbeat (SSE comment, không parse bởi EventSource.onmessage):
```
: ping\n\n
```

### Channel Names (enum)

```python
CHANNELS = frozenset({"reg", "session", "link", "hotmail", "hme_log", "autoreg_log"})
```

### Subscriber Lifecycle

```
Client GET /api/sse?token=xxx
    → auth_middleware validates token (401 if invalid)
    → subscribe() → (sub_id, queue)
    → yield snapshots (all 6 channels, ordered)
    → loop:
        queue.get(timeout=5s) → yield data event
        TimeoutError → yield ": ping\n\n"
        is_disconnected() → break
    → finally: unsubscribe(sub_id)
```

### SseMux Public API Interface

| Method | Signature | Description |
|--------|-----------|-------------|
| `subscribe()` | `() -> tuple[str, asyncio.Queue]` | Create subscriber, return (id, queue) |
| `unsubscribe(sub_id)` | `(str) -> None` | Remove subscriber by id |
| `publish(channel, event)` | `(str, dict) -> None` | Fan-out to all subscribers |
| `register_snapshot(channel, fn)` | `(str, Callable) -> None` | Register snapshot generator |
| `generate_snapshots()` | `() -> list[dict]` | Build all-channel snapshot batch |

### SseBus Public API

| Method | Signature | Description |
|--------|-----------|-------------|
| `connect()` | `() -> void` | Open EventSource to `/api/sse` |
| `on(channel, callback)` | `(string, function) -> void` | Register channel handler |

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Queue full (1000 items) | Drop event for that subscriber; log nothing (high-frequency). Other subscribers unaffected. |
| Client disconnect | `request.is_disconnected()` detected in loop → break → `finally` calls `unsubscribe()` |
| Invalid/missing token | `auth_middleware` returns 401 before endpoint handler runs |
| Snapshot function throws | Catch exception, skip that channel's snapshot, continue with remaining channels |
| EventSource connection drop (frontend) | `onerror` → close → reconnect after 3s delay |
| JSON parse failure (frontend) | `try/catch` in `onmessage` → silently ignore malformed event |

## Endpoints Removed

| Old Endpoint | Channel | Location |
|--------------|---------|----------|
| `GET /api/events` | reg | `web/server.py` |
| `GET /api/session/events` | session | `web/server.py` |
| `GET /api/link/events` | link | `web/server.py` |
| `GET /api/hotmail/events` | hotmail | `web/hotmail_routes.py` |
| `GET /api/icloud/run/log/stream` | hme_log | `web/icloud_routes.py` (via router) |
| `GET /api/icloud/autoreg/stream` | autoreg_log | `web/icloud_routes.py` (via router) |

## File Changes

| File | Changes |
|------|---------|
| `web/sse_mux.py` | **NEW**: SseMux class, Subscriber dataclass |
| `web/server.py` | Add `GET /api/sse`; remove `GET /api/events`, `GET /api/session/events`, `GET /api/link/events`; hook SseMux into managers; register snapshots at startup |
| `web/hotmail_routes.py` | Remove `GET /api/hotmail/events`; hook SseMux into HotmailJobManager._broadcast |
| `web/icloud_routes.py` | Remove `GET /api/icloud/run/log/stream`, `GET /api/icloud/autoreg/stream`; hook SseMux into LogBuffers |
| `web/manager.py` | Add SseMux hook in `_broadcast()` for JobManager, SessionJobManager, LinkJobManager |
| `icloud_hme/web/log_buffer.py` | Add optional `_sse_mux` + `_channel_name` fields; hook publish in `push()` |
| `web/static/app.js` | Add SseBus module; replace reg connectSSE/disconnectSSE with `SseBus.on('reg', ...)` |
| `web/static/session.js` | Remove connectSSE/disconnectSSE; use `SseBus.on('session', ...)` |
| `web/static/link.js` | Remove connectSSE/disconnectSSE; use `SseBus.on('link', ...)` |
| `web/static/hotmail.js` | Remove connectSSE/disconnectSSE; use `SseBus.on('hotmail', ...)` |
| `web/static/hme.js` | Remove connectLogStream/disconnectLogStream; use `SseBus.on('hme_log', ...)` |
| `web/static/autoreg.js` | Remove connectSSE/disconnectSSE; use `SseBus.on('autoreg_log', ...)` |

## Testing Strategy

### Property-Based Tests (Hypothesis)

- **SseMux fan-out, isolation, back-pressure**: Generate random subscriber counts (1–50), random events, random channel names from the 6 valid channels. Assert properties 1–5.
- **Channel tagging**: Generate arbitrary dict events + random channel strings. Assert the output always has the correct `channel` key.
- **Auth rejection**: Generate random invalid token strings (empty, whitespace, wrong values). Assert 401 response.

### Example-Based Tests

- Endpoint removal: Assert old SSE routes return 404 or are not registered.
- Snapshot ordering: Connect subscriber, verify first N events are snapshots before any live event.
- Heartbeat timing: Assert heartbeat comment emitted after 5s idle.
- Frontend SseBus: Verify `on(channel, cb)` only dispatches to matching channel handlers.

### Integration Tests

- Full flow: Start server, connect single EventSource, publish events from different managers, verify all arrive multiplexed.
- Reconnection: Drop connection, verify frontend SseBus reconnects after 3s and receives fresh snapshots.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Fan-out Delivery

*For any* set of active subscribers and *for any* event published to any of the 6 channels, every subscriber whose queue is not full SHALL receive that event in their queue with the correct `channel` field attached.

**Validates: Requirements 1.2, 3.2, 4.2**

### Property 2: Snapshot-first Ordering

*For any* newly created subscriber, the sequence of events delivered to its queue SHALL begin with snapshot events for all 6 registered channels before any live event is enqueued, and each snapshot event SHALL contain the corresponding `channel` field.

**Validates: Requirements 1.3, 5.1, 5.2, 5.3**

### Property 3: Channel Tagging

*For any* event published through `SseMux.publish(channel, event)`, the resulting dict placed into subscriber queues SHALL contain a `"channel"` key whose value equals the channel argument passed to `publish()`.

**Validates: Requirements 1.4, 5.3**

### Property 4: Unsubscribe Isolation

*For any* subscriber that has been unsubscribed via `unsubscribe(sub_id)`, subsequent calls to `publish(channel, event)` SHALL NOT place any event into that subscriber's queue, and the subscriber SHALL no longer appear in the internal subscribers registry.

**Validates: Requirements 3.4**

### Property 5: Back-pressure Isolation

*For any* set of subscribers where at least one subscriber's queue is full and at least one has available capacity, calling `publish(channel, event)` SHALL successfully enqueue the event for all non-full subscribers and SHALL NOT block or raise an exception, while the full subscriber's event is silently dropped.

**Validates: Requirements 4.3**

### Property 6: Channel Routing (Frontend)

*For any* SSE message with a `channel` field value `C`, the SseBus SHALL invoke all and only the callbacks registered via `on(C, callback)`, and SHALL NOT invoke callbacks registered for other channel values.

**Validates: Requirements 7.2, 7.3**

### Property 7: Auth Rejection

*For any* request to `GET /api/sse` that does not include a valid `token` query parameter, the server SHALL respond with HTTP status 401 and SHALL NOT create a Subscriber or stream any events.

**Validates: Requirements 9.1**
