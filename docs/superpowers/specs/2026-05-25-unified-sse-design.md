# Unified SSE Multiplexer

**Date**: 2026-05-25
**Status**: Draft
**Problem**: Browser HTTP/1.1 gioi han 6 connections/origin. Frontend mo 6 SSE EventSource dong thoi (reg + session + link + hotmail + hme_log + autoreg_log) => het slot => GET /profiles, GET /emails bi queue => "Loading..." mai.

## Solution

Gop tat ca SSE streams vao **1 endpoint duy nhat** voi channel-based multiplexing. Frontend chi mo 1 EventSource, subscribe channels can thiet, doi channels khi switch tab ma khong can reconnect.

## Architecture

### Backend: SseMux (singleton)

```
SseMux
  ├─ _subscribers: dict[str, Subscriber]    # subscriber_id -> Subscriber
  ├─ publish(channel, event)                 # fan-out to matching subscribers
  ├─ subscribe(channels) -> (sub_id, Queue)  # register new subscriber
  ├─ unsubscribe(sub_id)                     # cleanup
  └─ set_channels(sub_id, channels)          # dynamic channel switch
```

**Subscriber** dataclass:
```python
@dataclass
class Subscriber:
    queue: asyncio.Queue[dict]          # maxsize=1000
    channels: set[str]                  # current subscribed channels
    snapshot_fns: dict[str, Callable]   # channel -> snapshot generator (injected at init)
```

**Lifecycle**:
1. Client connects `GET /api/sse?channels=reg&token=...`
2. Backend creates Subscriber, generates snapshots for requested channels, yields them
3. Managers/LogBuffers call `SseMux.publish(channel, event)` khi co event moi
4. `publish()` chi put vao queue cua subscriber co channel do
5. Client switch tab -> `POST /api/sse/channels` -> backend update `subscriber.channels`, gui snapshots cho channels moi add
6. Client disconnect -> `finally` block goi `unsubscribe()`

### Channels

| Channel        | Source            | Snapshot logic                              |
|----------------|-------------------|---------------------------------------------|
| `reg`          | JobManager        | `{type: "snapshot", jobs: [...], ...config}` |
| `session`      | SessionJobManager | `{type: "snapshot", jobs: [...], ...config}` |
| `link`         | LinkJobManager    | `{type: "snapshot", jobs: [...], ...config}` |
| `hotmail`      | HotmailManager    | `{type: "snapshot", jobs: [...], config, stats}` |
| `hme_log`      | HME LogBuffer     | Replay history entries                       |
| `autoreg_log`  | AutoReg LogBuffer | Replay history entries                       |

### Event wire format

Moi event wrap them `channel` field de frontend demux:

```
data: {"channel":"reg","type":"snapshot","jobs":[...],"max_concurrent":2}

data: {"channel":"hme_log","ts":"2026-05-25T...","level":"info","message":"...","seq":42}

data: {"channel":"reg","type":"job","job":{"id":"abc","status":"running"}}
```

Heartbeat (khong co channel):
```
: ping
```

### Endpoints

#### `GET /api/sse?channels=reg,hme_log&token=...`

- Tao subscriber voi requested channels
- Yield snapshots cho moi channel (theo thu tu channels param)
- Loop: `queue.get()` -> yield, heartbeat moi 5s
- `request.is_disconnected()` check -> break
- `finally`: unsubscribe

#### `POST /api/sse/channels`

Request body:
```json
{
  "subscriber_id": "abc123",
  "channels": ["hme_log", "autoreg_log"]
}
```

Response:
```json
{"ok": true, "channels": ["hme_log", "autoreg_log"]}
```

Side effect: `SseMux.set_channels(sub_id, new_channels)`
- Channels bi remove: khong gui gi (frontend clear UI khi switch tab)
- Channels moi add: gui snapshot qua queue de frontend hydrate

**subscriber_id**: server generate UUID khi subscribe, gui trong snapshot dau tien:
```
data: {"channel":"_system","type":"connected","subscriber_id":"abc123"}
```

### Frontend changes

#### `app.js` — SseBus (central SSE manager)

```javascript
const SseBus = (() => {
  let _es = null;
  let _subId = null;
  const _handlers = new Map();  // channel -> [callback]

  function connect(channels) {
    const qs = channels.join(',');
    _es = new EventSource(withTokenQuery(`/api/sse?channels=${qs}`));
    _es.onmessage = (e) => {
      const data = JSON.parse(e.data);
      if (data.channel === '_system' && data.type === 'connected') {
        _subId = data.subscriber_id;
        return;
      }
      const cbs = _handlers.get(data.channel) || [];
      cbs.forEach(cb => cb(data));
    };
    _es.onerror = () => { _es.close(); setTimeout(() => connect(channels), 3000); };
  }

  function switchChannels(channels) {
    api('/api/sse/channels', {
      method: 'POST',
      body: JSON.stringify({ subscriber_id: _subId, channels }),
    });
  }

  function on(channel, callback) {
    if (!_handlers.has(channel)) _handlers.set(channel, []);
    _handlers.get(channel).push(callback);
  }

  return { connect, switchChannels, on };
})();
```

#### Per-module changes

Moi module (session.js, link.js, hotmail.js, hme.js, autoreg.js) doi tu:
- `new EventSource('/api/xxx/events')` -> `SseBus.on('channel', handler)`
- connectSSE/disconnectSSE -> removed (SseBus quan ly)

#### Tab switching

```javascript
function activateTab(tabId) {
  // ... existing CSS logic ...
  const channelMap = {
    reg: ['reg'],
    session: ['session'],
    link: ['link'],
    hme: ['hme_log', 'autoreg_log'],
    hotmail: ['hotmail'],
  };
  SseBus.switchChannels(channelMap[tabId] || []);
}
```

### Backend integration: Bridging existing managers

Moi manager giu nguyen `_subscribers` set va `_broadcast()` method hien co (backward compat cho old endpoints). Them 1 hook vao `_broadcast()`:

```python
# In JobManager._broadcast (server.py)
def _broadcast(self, event: dict):
    # Existing fan-out to direct subscribers
    for q in list(self._subscribers):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass
    # NEW: publish to SseMux
    if _sse_mux is not None:
        _sse_mux.publish("reg", event)
```

Tuong tu cho SessionJobManager ("session"), LinkJobManager ("link"), HotmailManager ("hotmail").

Cho LogBuffer: them hook vao `push()`:
```python
async def push(self, level, message, payload):
    # ... existing logic ...
    # NEW: publish to SseMux
    if self._sse_mux is not None:
        self._sse_mux.publish(self._channel_name, event.model_dump())
```

### Snapshot functions

SseMux can biet cach generate snapshot cho moi channel khi subscriber moi connect hoac switch channels:

```python
_snapshot_fns: dict[str, Callable[[], list[dict]]] = {
    "reg": lambda: [manager.build_snapshot()],
    "session": lambda: [sm.build_snapshot()],
    "link": lambda: [lm.build_snapshot()],
    "hotmail": lambda: [hm.build_snapshot()],
    "hme_log": lambda: [e.model_dump() for e in hme_buffer.snapshot()],
    "autoreg_log": lambda: [e.model_dump() for e in autoreg_buffer.snapshot()],
}
```

Register snapshot functions khi init services (lazy, giong pattern hien co).

### Error handling

- `queue.put_nowait()` full -> drop event (giong behavior hien co)
- `request.is_disconnected()` -> cleanup subscriber
- Invalid channel name in request -> ignore (khong subscribe)
- `subscriber_id` khong ton tai khi POST /channels -> 404

### Backward compatibility

- Giu nguyen 6 endpoints cu (khong xoa)
- Frontend chuyen hoan toan sang `/api/sse`
- Endpoints cu van hoat dong cho debugging / external clients

### File changes

| File | Changes |
|------|---------|
| `web/sse_mux.py` | NEW: SseMux class, Subscriber dataclass |
| `web/server.py` | Add `GET /api/sse`, `POST /api/sse/channels`; hook SseMux vao managers |
| `web/icloud_routes.py` | Hook SseMux vao LogBuffers |
| `web/hotmail_routes.py` | Hook SseMux vao HotmailManager |
| `web/static/app.js` | Add SseBus module; doi activateTab; remove reg connectSSE/disconnectSSE |
| `web/static/session.js` | Replace EventSource voi SseBus.on('session', handler) |
| `web/static/link.js` | Replace EventSource voi SseBus.on('link', handler) |
| `web/static/hotmail.js` | Replace EventSource voi SseBus.on('hotmail', handler) |
| `web/static/hme.js` | Replace connectLogStream voi SseBus.on('hme_log', handler) |
| `web/static/autoreg.js` | Replace connectSSE voi SseBus.on('autoreg_log', handler) |

### Connection budget

| Scenario | SSE connections | Available for API |
|----------|----------------|-------------------|
| Before (worst case) | 6 | 0 |
| After (always) | 1 | 5 |

### Testing

1. Mo web UI, verify chi co 1 SSE connection (DevTools Network tab)
2. Switch giua 5 tabs, verify snapshot moi duoc gui khi switch
3. Chay HME runner + AutoReg dong thoi, verify profiles/emails load binh thuong
4. Verify log stream hoat dong binh thuong tren HME tab
5. Verify reg/session/link jobs update realtime khi dang o tab tuong ung
6. Verify old endpoints van hoat dong (curl test)
