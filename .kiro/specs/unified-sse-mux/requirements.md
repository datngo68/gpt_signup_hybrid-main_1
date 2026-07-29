# Requirements Document

## Introduction

Unified SSE Multiplexer gộp 6 SSE endpoints riêng lẻ (reg, session, link, hotmail, hme_log, autoreg_log) thành 1 endpoint duy nhất với channel-based multiplexing. Mục tiêu: giải phóng HTTP/1.1 connection budget (từ 6 SSE connections xuống 1), loại bỏ tình trạng browser queue API requests do hết slot.

## Glossary

- **SseMux**: Singleton backend component quản lý multiplexed SSE connections, nhận events từ managers và fan-out tới tất cả subscribers
- **Subscriber**: Một SSE client connection được đại diện bởi một asyncio.Queue nhận mọi event từ tất cả 6 channels
- **Channel**: Một luồng event logic (reg, session, link, hotmail, hme_log, autoreg_log) được phân biệt bởi trường `channel` trong payload
- **SseBus**: Frontend JavaScript module quản lý single EventSource connection và route events theo channel tới handlers
- **Snapshot**: Trạng thái hiện tại đầy đủ của một channel, gửi cho client khi mới connect
- **Heartbeat**: SSE comment (`: ping`) gửi định kỳ để giữ connection alive và phát hiện disconnect
- **EventSource**: Browser Web API để nhận Server-Sent Events qua HTTP
- **Manager**: Backend component (JobManager, SessionJobManager, LinkJobManager, HotmailManager) phát sinh events cho channel tương ứng
- **LogBuffer**: Backend component lưu trữ log entries cho hme_log và autoreg_log channels

## Requirements

### Requirement 1: Single Unified SSE Endpoint

**User Story:** As a frontend developer, I want a single SSE endpoint that delivers events from all channels, so that the application uses only 1 HTTP connection for real-time updates.

#### Acceptance Criteria

1. THE SseMux SHALL expose a single `GET /api/sse` endpoint that accepts a `token` query parameter for authentication
2. WHEN a client connects to `GET /api/sse`, THE SseMux SHALL create a Subscriber and subscribe the Subscriber to all 6 channels (reg, session, link, hotmail, hme_log, autoreg_log)
3. WHEN a client connects to `GET /api/sse`, THE SseMux SHALL deliver snapshots for all 6 channels before streaming live events
4. THE SseMux SHALL include a `channel` field in every SSE data payload to identify the source channel

### Requirement 2: Legacy SSE Endpoint Removal

**User Story:** As a maintainer, I want the old per-module SSE endpoints removed, so that the codebase has a single unified pattern for real-time event delivery.

#### Acceptance Criteria

1. WHEN the unified endpoint is deployed, THE System SHALL remove the `GET /api/events` endpoint (reg channel)
2. WHEN the unified endpoint is deployed, THE System SHALL remove the `GET /api/session/events` endpoint (session channel)
3. WHEN the unified endpoint is deployed, THE System SHALL remove the `GET /api/link/events` endpoint (link channel)
4. WHEN the unified endpoint is deployed, THE System SHALL remove the `GET /api/hotmail/events` endpoint (hotmail channel)
5. WHEN the unified endpoint is deployed, THE System SHALL remove the `GET /api/icloud/run/log/stream` endpoint (hme_log channel)
6. WHEN the unified endpoint is deployed, THE System SHALL remove the `GET /api/icloud/autoreg/stream` endpoint (autoreg_log channel)

### Requirement 3: SseMux Singleton Backend

**User Story:** As a backend developer, I want a centralized SseMux component, so that all managers publish events through one fan-out mechanism.

#### Acceptance Criteria

1. THE SseMux SHALL be instantiated as a singleton accessible to all managers and LogBuffers
2. THE SseMux SHALL provide a `publish(channel, event)` method that enqueues the event into every active Subscriber queue
3. THE SseMux SHALL provide a `subscribe()` method that creates a new Subscriber with an asyncio.Queue and returns the subscriber_id and queue reference
4. THE SseMux SHALL provide an `unsubscribe(subscriber_id)` method that removes the Subscriber and releases the queue
5. WHEN a Manager or LogBuffer produces an event, THE Manager or LogBuffer SHALL call `SseMux.publish(channel, event)` to deliver the event to all connected subscribers

### Requirement 4: Subscriber Model

**User Story:** As a backend developer, I want a simplified Subscriber model that receives all events without channel filtering, so that the subscription logic remains minimal.

#### Acceptance Criteria

1. THE Subscriber SHALL contain an asyncio.Queue with a maximum size of 1000 entries
2. THE Subscriber SHALL receive events from all 6 channels without filtering
3. IF the Subscriber queue is full, THEN THE SseMux SHALL drop the event for that Subscriber without blocking other subscribers

### Requirement 5: Snapshot on Connect

**User Story:** As a frontend user, I want to receive the current state of all channels immediately upon connecting, so that the UI is fully hydrated without waiting for incremental updates.

#### Acceptance Criteria

1. WHEN a new Subscriber connects, THE SseMux SHALL generate and send snapshots for all 6 channels in a single batch
2. THE SseMux SHALL send snapshot events before any live events for the new Subscriber
3. THE SseMux SHALL wrap each snapshot event with the corresponding `channel` field

### Requirement 6: Heartbeat Mechanism

**User Story:** As a system operator, I want periodic heartbeat signals, so that idle connections remain open and disconnects are detected promptly.

#### Acceptance Criteria

1. WHILE a Subscriber is connected, THE SseMux SHALL send a heartbeat SSE comment (`: ping`) every 5 seconds when no data events are sent
2. WHEN the client disconnects, THE SseMux SHALL detect the disconnection and call `unsubscribe()` to clean up the Subscriber

### Requirement 7: Frontend SseBus Module

**User Story:** As a frontend developer, I want a single SseBus module that manages the EventSource connection and routes events to per-channel handlers, so that modules do not manage their own SSE connections.

#### Acceptance Criteria

1. THE SseBus SHALL provide a `connect()` method that opens a single EventSource to `GET /api/sse` with token authentication
2. THE SseBus SHALL provide an `on(channel, callback)` method that registers a handler for a specific channel
3. WHEN an SSE message is received, THE SseBus SHALL parse the JSON payload and invoke all registered callbacks for the event's `channel` field
4. IF the EventSource connection drops, THEN THE SseBus SHALL automatically reconnect after a 3-second delay

### Requirement 8: Legacy Frontend SSE Code Removal

**User Story:** As a frontend maintainer, I want per-module connectSSE/disconnectSSE functions removed, so that all modules use SseBus exclusively.

#### Acceptance Criteria

1. WHEN the SseBus is deployed, THE System SHALL remove `connectSSE()` and `disconnectSSE()` functions from app.js, session.js, link.js, hotmail.js, hme.js, and autoreg.js
2. WHEN the SseBus is deployed, THE System SHALL replace per-module EventSource usage with `SseBus.on(channel, handler)` calls

### Requirement 9: Token Authentication

**User Story:** As a security-minded developer, I want the unified SSE endpoint to require token authentication, so that unauthenticated clients cannot subscribe to events.

#### Acceptance Criteria

1. WHEN a client connects to `GET /api/sse` without a valid `token` query parameter, THE SseMux SHALL reject the connection with HTTP 401
2. THE SseMux SHALL validate the token using the same authentication mechanism as the existing API endpoints

### Requirement 10: Connection Budget Optimization

**User Story:** As an end user, I want the browser to have available HTTP connection slots for API calls, so that UI interactions (loading profiles, emails) are not blocked by SSE connections.

#### Acceptance Criteria

1. THE System SHALL use exactly 1 SSE connection per browser tab to the origin server
2. WHEN the unified SSE endpoint is active, THE System SHALL have at least 5 HTTP connection slots available for API requests (given HTTP/1.1 limit of 6 per origin)
