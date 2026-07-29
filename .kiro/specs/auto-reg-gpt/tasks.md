# Implementation Plan: Auto Reg GPT

## Overview

Tự động hóa đăng ký ChatGPT từ email iCloud HME đã tạo. Module mới `autoreg/` chứa `AutoRegRunner` (singleton async runner), mount endpoints vào `build_icloud_router()`, UI sub-tab "Auto Reg" trong HME tab với layout 50/50 + SSE realtime output.

## Tasks

- [x] 1. Database migration v9 + ChatGptAccountRepository
  - [x] 1.1 Add MIGRATIONS[9] to db/schema.py
    - Add `chatgpt_accounts` table DDL (id, email UNIQUE, password, secret_2fa, created_at)
    - Add index `idx_chatgpt_accounts_email`
    - Increment `CURRENT_VERSION` to 9
    - _Requirements: 5.1, 5.2, 5.3_

  - [x] 1.2 Implement ChatGptAccountRepository in db/repositories.py
    - `persist_success(email, password, secret_2fa)` — atomic INSERT account + UPDATE icloud_emails status in single transaction
    - `list_accounts(page, page_size)` — paginated SELECT with total count
    - `get_created_emails(limit)` — SELECT email FROM icloud_emails WHERE status='created'
    - Follow existing repository patterns (RepositoryError, `self._engine`, context manager)
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 1.2_

- [x] 2. AutoRegRunner module (autoreg/)
  - [x] 2.1 Create autoreg/__init__.py and autoreg/runner.py with AutoRegRunner class
    - Implement `__init__` with LogCallback, stats, cancel_event, semaphore
    - Implement `start(config: AutoRegStartRequest)` — validate not already running, spawn poll loop task
    - Implement `stop()` — set cancel_event, non-blocking
    - Implement `_poll_loop()` — query created emails, process batch, interruptible sleep
    - Implement `_process_email(email)` — get_spec('worker') → parse_line → build_request → run_signup → persist
    - Implement `_interruptible_sleep(seconds)` — asyncio.wait_for on cancel_event
    - Implement `_resolve_worker_config(config)` — UI input > env var > hardcoded default
    - AutoRegStats dataclass (processed, success, errors — monotonically non-decreasing)
    - Single-instance guard: raise RuntimeError if already running
    - Concurrency bound via asyncio.Semaphore(config.concurrency)
    - Error resilience: single failure does not stop batch (gather with return_exceptions=True)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.2, 2.3, 3.1, 3.2, 3.3, 3.4, 10.1, 10.2, 10.3, 10.4_

  - [ ]* 2.2 Write property test for single-instance guard
    - **Property 1: Single-instance guard**
    - **Validates: Requirements 1.5**

  - [ ]* 2.3 Write property test for concurrency bound invariant
    - **Property 2: Concurrency bound invariant**
    - **Validates: Requirements 2.2**

  - [ ]* 2.4 Write property test for poll filter correctness
    - **Property 3: Poll filter correctness**
    - **Validates: Requirements 1.2, 7.3**

  - [ ]* 2.5 Write property test for password propagation
    - **Property 4: Password propagation**
    - **Validates: Requirements 3.3**

  - [ ]* 2.6 Write property test for atomic persistence
    - **Property 5: Atomic persistence on success**
    - **Validates: Requirements 4.1, 4.2, 4.3**

  - [ ]* 2.7 Write property test for stats monotonicity
    - **Property 6: Stats monotonicity**
    - **Validates: Requirements 10.4**

  - [ ]* 2.8 Write property test for resilience (single failure does not stop batch)
    - **Property 7: Resilience — single failure does not stop batch**
    - **Validates: Requirements 10.1**

- [x] 3. Checkpoint — Ensure core logic tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. API endpoints in web/icloud_routes.py
  - [x] 4.1 Add Pydantic models (AutoRegStartRequest, AutoRegStatusResponse, ChatGptAccountRow)
    - AutoRegStartRequest: concurrency (1–5), poll_interval (≥10), default_password, logs_url, api_key
    - AutoRegStatusResponse: running, processed, success, errors, current_cycle
    - _Requirements: 9.1, 9.3_

  - [x] 4.2 Add lazy singleton init + autoreg endpoints to build_icloud_router()
    - `_init_autoreg()` — lazy init AutoRegRunner + LogBuffer (same pattern as HmeRunner)
    - `POST /autoreg/start` — validate, call runner.start(), return 409 if already running
    - `POST /autoreg/stop` — call runner.stop()
    - `GET /autoreg/status` — return AutoRegStatusResponse from runner state
    - `GET /autoreg/stream` — SSE StreamingResponse iterating LogBuffer.subscribe()
    - `GET /autoreg/accounts` — paginated list from ChatGptAccountRepository
    - All endpoints gated by existing auth middleware (SSE via ?token= query param)
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 8.1, 8.4_

  - [ ]* 4.3 Write property test for SSE event completeness
    - **Property 8: SSE event completeness on success**
    - **Validates: Requirements 8.2, 7.4**

  - [ ]* 4.4 Write property test for status response completeness
    - **Property 9: Status response completeness**
    - **Validates: Requirements 9.3**

- [x] 5. Frontend — HTML sub-tab + autoreg.js + CSS
  - [x] 5.1 Add Auto Reg sub-tab HTML to web/static/index.html
    - Sub-tab navigation entry "Auto Reg" in HME tab
    - Section with card layout: header (toggle button + status badge), config row (password, concurrency, poll interval), 50/50 split (email table + output pane), stats bar
    - _Requirements: 6.1, 6.4, 6.5, 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7_

  - [x] 5.2 Create web/static/autoreg.js
    - Toggle button logic: POST /start or /stop based on state
    - SSE connection: EventSource to /api/icloud/autoreg/stream?token=...
    - Status polling: GET /status every 3s when running, update badge + stats
    - Left panel: fetch icloud_emails (status=created,used_for_chatgpt), render table
    - Right panel: append SSE events as formatted lines (email|password|secret_2fa)
    - Config inputs: read password, concurrency, poll_interval, pass to start request
    - _Requirements: 6.2, 6.3, 6.4, 6.5, 8.1, 8.2, 8.3_

  - [x] 5.3 Add CSS styles for autoreg layout
    - `.autoreg-config` row styling
    - `.autoreg-split` 50/50 flex layout
    - `.autoreg-panel-left` / `.autoreg-panel-right` panels
    - `.autoreg-stats` footer bar
    - Reuse existing `.card`, `.btn`, `.badge`, `.log-pane` classes
    - _Requirements: 7.2_

- [x] 6. Checkpoint — Ensure frontend integration works
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Integration wiring (startup/shutdown)
  - [x] 7.1 Wire AutoRegRunner lifecycle into server startup/shutdown
    - Import and init autoreg module in appropriate startup hook
    - Ensure graceful shutdown stops AutoRegRunner if running (cancel_event.set())
    - Verify router endpoints accessible after server start
    - _Requirements: 1.1, 1.4_

- [x] 8. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from design document
- AutoRegRunner follows the same singleton + LogBuffer + SSE pattern as existing HmeRunner
- DB migration v9 is self-contained (CREATE TABLE IF NOT EXISTS + index)
- Worker config resolution: UI input > env HYBRID_WORKER_LOGS_URL/API_KEY > hardcoded default
- Frontend reuses existing HME tab CSS classes and patterns

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2"] },
    { "id": 2, "tasks": ["2.1", "4.1"] },
    { "id": 3, "tasks": ["2.2", "2.3", "2.4", "2.5", "2.6", "2.7", "2.8", "4.2"] },
    { "id": 4, "tasks": ["4.3", "4.4", "5.1"] },
    { "id": 5, "tasks": ["5.2", "5.3"] },
    { "id": 6, "tasks": ["7.1"] }
  ]
}
```
