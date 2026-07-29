# Implementation Plan: Unified SSE Multiplexer

## Overview

Gộp 6 SSE endpoints riêng lẻ thành 1 endpoint `GET /api/sse` với channel-based multiplexing. Backend SseMux singleton fan-out events tới subscribers, frontend SseBus module route events theo channel. Triển khai theo thứ tự: core module → endpoint → manager hooks → remove old → frontend SseBus → migrate modules → cleanup → PBT.

## Tasks

- [x] 1. Implement SseMux core module
  - [x] 1.1 Create `web/sse_mux.py` with Subscriber dataclass and SseMux class
    - Define `Subscriber` dataclass with `id` (str), `queue` (asyncio.Queue), `created_at` (float)
    - Implement `SseMux` class with `_subscribers` dict and `_snapshot_fns` dict
    - Implement `subscribe()` → returns `(sub_id, queue)` with `maxsize=1000`
    - Implement `unsubscribe(sub_id)` → removes subscriber from registry
    - Implement `publish(channel, event)` → wraps event with `channel` field, fan-out via `put_nowait`, drops on `QueueFull`
    - Implement `register_snapshot(channel, fn)` → stores snapshot callable
    - Implement `generate_snapshots()` → calls all registered snapshot fns in channel order, wraps with `channel` field
    - Implement `subscriber_count` property
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 4.1, 4.2, 4.3, 5.1, 5.3_

- [x] 2. Implement unified SSE endpoint and snapshot registration
  - [x] 2.1 Add `GET /api/sse` endpoint in `web/server.py`
    - Create `StreamingResponse` with `text/event-stream` media type
    - On connect: call `subscribe()`, yield snapshots via `generate_snapshots()`, then loop `queue.get(timeout=5)` for live events
    - Emit `: ping\n\n` heartbeat on `asyncio.TimeoutError`
    - Detect disconnect via `request.is_disconnected()`, call `unsubscribe()` in `finally` block
    - Set headers: `Cache-Control: no-cache`, `X-Accel-Buffering: no`, `Connection: keep-alive`
    - Token auth handled by existing `auth_middleware` (gate on `/api/*`)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 5.1, 5.2, 6.1, 6.2, 9.1, 9.2_

  - [x] 2.2 Register snapshot functions at server startup
    - Instantiate `SseMux` singleton (module-level or in `on_startup`)
    - Register 6 snapshot functions: `reg`, `session`, `link`, `hotmail`, `hme_log`, `autoreg_log`
    - Each fn returns list of dicts matching the snapshot format in design
    - _Requirements: 5.1, 5.2, 5.3_

- [x] 3. Integrate manager publish hooks
  - [x] 3.1 Hook SseMux into `JobManager._broadcast()` in `web/manager.py`
    - Add `_sse_mux.publish("reg", event)` after existing fan-out logic
    - _Requirements: 3.5_

  - [x] 3.2 Hook SseMux into `SessionJobManager._broadcast()` in `web/manager.py`
    - Add `_sse_mux.publish("session", event)` after existing fan-out logic
    - _Requirements: 3.5_

  - [x] 3.3 Hook SseMux into `LinkJobManager._broadcast()` in `web/manager.py`
    - Add `_sse_mux.publish("link", event)` after existing fan-out logic
    - _Requirements: 3.5_

  - [x] 3.4 Hook SseMux into `HotmailJobManager._broadcast_event()` in `hotmail_pool/job_manager.py`
    - Add `_sse_mux.publish("hotmail", event)` after existing fan-out logic
    - _Requirements: 3.5_

  - [x] 3.5 Hook SseMux into LogBuffer `push()` in `icloud_hme/web/log_buffer.py`
    - Add optional `_sse_mux` and `_channel_name` fields to LogBuffer
    - In `push()`, call `_sse_mux.publish(self._channel_name, event.model_dump())` when mux is set
    - Wire both `hme_log` and `autoreg_log` buffers to SseMux at startup
    - _Requirements: 3.5_

- [x] 4. Checkpoint - Verify backend integration
  - Backend hoàn tất: SseMux core, unified endpoint, manager hooks, snapshot registration all working.

- [x] 5. Remove legacy backend SSE endpoints
  - [x] 5.1 Remove `GET /api/events` (reg) from `web/server.py`
    - _Requirements: 2.1_

  - [x] 5.2 Remove `GET /api/session/events` (session) from `web/server.py`
    - _Requirements: 2.2_

  - [x] 5.3 Remove `GET /api/link/events` (link) from `web/server.py`
    - _Requirements: 2.3_

  - [x] 5.4 Remove `GET /api/hotmail/events` (hotmail) from `web/hotmail_routes.py`
    - _Requirements: 2.4_

  - [x] 5.5 Remove `GET /api/icloud/run/log/stream` (hme_log) from `web/icloud_routes.py`
    - _Requirements: 2.5_

  - [x] 5.6 Remove `GET /api/icloud/autoreg/stream` (autoreg_log) from `web/icloud_routes.py`
    - _Requirements: 2.6_

- [x] 6. Implement frontend SseBus module
  - [x] 6.1 Create SseBus IIFE module in `web/static/app.js`
    - Implement `connect()` → opens `EventSource` to `/api/sse?token=...` via `withTokenQuery()`
    - Implement `on(channel, callback)` → registers handler in `_handlers` Map
    - Implement `onmessage` → parse JSON, route by `data.channel` to registered callbacks
    - Implement `onerror` → close + reconnect after 3s via `setTimeout`
    - Guard against double-connect (check `readyState`)
    - _Requirements: 7.1, 7.2, 7.3, 7.4_

- [x] 7. Migrate frontend modules to SseBus
  - [x] 7.1 Migrate `web/static/app.js` (reg channel)
    - Replace `connectSSE()`/`disconnectSSE()` with `SseBus.on('reg', handler)`
    - Move existing `es.onmessage` logic into SseBus handler callback
    - Call `SseBus.connect()` on page load
    - _Requirements: 8.1, 8.2_

  - [x] 7.2 Migrate `web/static/session.js` (session channel)
    - Remove `connectSSE()`/`disconnectSSE()` functions
    - Replace with `SseBus.on('session', handler)` using existing handler logic
    - Remove tab-based connect/disconnect event listeners
    - **CRITICAL**: Currently BROKEN — still points to removed `/api/session/events`
    - _Requirements: 8.1, 8.2_

  - [x] 7.3 Migrate `web/static/link.js` (link channel)
    - Remove `connectSSE()`/`disconnectSSE()` functions
    - Replace with `SseBus.on('link', handler)` using existing handler logic
    - _Requirements: 8.1, 8.2_

  - [x] 7.4 Migrate `web/static/hotmail.js` (hotmail channel)
    - Remove `connectSSE()`/`disconnectSSE()` functions
    - Replace with `SseBus.on('hotmail', handler)` using existing handler logic
    - _Requirements: 8.1, 8.2_

  - [x] 7.5 Migrate `web/static/hme.js` (hme_log channel)
    - Remove `connectLogStream()`/`disconnectLogStream()` functions
    - Replace with `SseBus.on('hme_log', handler)` using existing handler logic
    - _Requirements: 8.1, 8.2_

  - [x] 7.6 Migrate `web/static/autoreg.js` (autoreg_log channel)
    - Remove `connectSSE()`/`disconnectSSE()` functions
    - Replace with `SseBus.on('autoreg_log', handler)` using existing handler logic
    - _Requirements: 8.1, 8.2_

- [x] 8. Checkpoint - Verify full integration
  - Ensure all tabs receive real-time updates via unified SSE.
  - Verify only 1 SSE connection in DevTools Network tab.

- [x] 9. Property-based tests with Hypothesis
  - [x]* 9.1 Write property test for fan-out delivery
    - **Property 1: Fan-out Delivery**
    - Generate random subscriber counts (1–50) and random events for any of 6 channels
    - Assert every non-full subscriber receives the event with correct `channel` field
    - **Validates: Requirements 1.2, 3.2, 4.2**

  - [x]* 9.2 Write property test for snapshot-first ordering
    - **Property 2: Snapshot-first Ordering**
    - Create subscriber, register snapshot fns, generate snapshots then publish live events
    - Assert snapshot events precede live events and each has correct `channel` field
    - **Validates: Requirements 1.3, 5.1, 5.2, 5.3**

  - [x]* 9.3 Write property test for channel tagging
    - **Property 3: Channel Tagging**
    - Generate arbitrary dict events + random channel from 6 valid channels
    - Assert output dict always has `"channel"` key matching the publish argument
    - **Validates: Requirements 1.4, 5.3**

  - [x]* 9.4 Write property test for unsubscribe isolation
    - **Property 4: Unsubscribe Isolation**
    - Subscribe then unsubscribe, publish events after
    - Assert unsubscribed queue receives nothing and sub_id absent from registry
    - **Validates: Requirements 3.4**

  - [x]* 9.5 Write property test for back-pressure isolation
    - **Property 5: Back-pressure Isolation**
    - Fill one subscriber's queue to capacity, leave others with space
    - Publish event, assert non-full subscribers get it, no exception raised
    - **Validates: Requirements 4.3**

  - [x]* 9.6 Write property test for channel routing (frontend logic)
    - **Property 6: Channel Routing**
    - Generate messages with random channel values and multiple registered handlers
    - Assert only handlers matching the event's channel are invoked
    - **Validates: Requirements 7.2, 7.3**

  - [x]* 9.7 Write property test for auth rejection
    - **Property 7: Auth Rejection**
    - Generate random invalid token strings (empty, whitespace, wrong values)
    - Assert 401 response and no subscriber created
    - **Validates: Requirements 9.1**

- [x] 10. Final checkpoint
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Backend uses Python (FastAPI + asyncio), frontend uses vanilla JavaScript
- SseMux is a singleton — no dynamic channel switching, subscribers receive all 6 channels always
- No `POST /api/sse/channels` endpoint needed
- Auth handled by existing `auth_middleware` gating `/api/*` routes
- Property tests use Hypothesis library (already in project)
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- **⚠️ CRITICAL**: Backend đã xóa 5 legacy endpoints nhưng frontend (session/link/hotmail/hme/autoreg) vẫn đang connect tới chúng → app BROKEN cho 5 tabs này. Tasks 7.2–7.6 là urgent.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"], "status": "done" },
    { "id": 1, "tasks": ["2.1", "2.2"], "status": "done" },
    { "id": 2, "tasks": ["3.1", "3.2", "3.3", "3.4", "3.5"], "status": "done" },
    { "id": 3, "tasks": ["5.1", "5.2", "5.3", "5.4", "5.5", "5.6", "6.1"], "status": "done" },
    { "id": 4, "tasks": ["7.1", "7.2", "7.3", "7.4", "7.5", "7.6"], "status": "in_progress", "note": "7.1 done, 7.2-7.6 pending (BROKEN)" },
    { "id": 5, "tasks": ["9.1", "9.2", "9.3", "9.4", "9.5", "9.6", "9.7"], "status": "pending" }
  ]
}
```
