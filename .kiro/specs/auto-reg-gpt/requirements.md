# Requirements Document

## Introduction

Auto Reg GPT tự động hóa quy trình đăng ký tài khoản ChatGPT bằng cách poll email iCloud có status `created` từ bảng `icloud_emails`, chạy signup flow với `mail_mode='worker'` (icloud-cf-mail Worker), và lưu kết quả vào bảng `chatgpt_accounts` mới. Feature hoạt động như sub-tab "Auto Reg" trong HME tab, với toggle ON/OFF và UI 50/50 split hiển thị danh sách email + output realtime.

## Glossary

- **AutoRegRunner**: Singleton async runner class (pattern mirrors `HmeRunner`) điều phối vòng lặp tự động đăng ký ChatGPT. Chạy trong cùng event loop FastAPI.
- **System**: Hệ thống gpt_signup_hybrid (backend FastAPI + frontend vanilla JS).
- **Worker**: Cloudflare Worker `icloud-cf-mail` dùng để poll OTP email qua HTTP API.
- **icloud_emails**: Bảng SQLite chứa danh sách email iCloud HME đã tạo.
- **chatgpt_accounts**: Bảng SQLite mới (v9 migration) lưu tài khoản ChatGPT đã đăng ký thành công.
- **HME_Tab**: Tab "HME" trên web UI chứa sub-pages Profiles, Run Log, Emails, và sub-tab mới Auto Reg.
- **Auto_Reg_UI**: Sub-tab "Auto Reg" trong HME_Tab hiển thị panel điều khiển + output.
- **SSE**: Server-Sent Events — phương thức stream log realtime từ server tới browser.
- **Default_Password**: Mật khẩu mặc định áp dụng cho tất cả tài khoản ChatGPT được tạo trong session.

## Requirements

### Requirement 1: AutoRegRunner Singleton

**User Story:** As a user, I want an automated runner that continuously polls for available iCloud emails and registers ChatGPT accounts, so that I do not have to manually trigger each registration.

#### Acceptance Criteria

1. THE System SHALL provide an `AutoRegRunner` class as a module-level lazy singleton initialized on first API call, following the same pattern as `HmeRunner` in `web/icloud_routes.py`.
2. WHEN the AutoRegRunner is started, THE AutoRegRunner SHALL poll the `icloud_emails` table for rows with `status='created'` at a configurable interval.
3. WHILE the AutoRegRunner is running, THE AutoRegRunner SHALL process available emails using the existing signup flow with `mail_mode='worker'`.
4. THE AutoRegRunner SHALL support lifecycle methods: `start`, `stop` with the same semantics as `HmeRunner` (cancel event, non-blocking stop, state reset in finally).
5. IF the AutoRegRunner is already running and a start request is received, THEN THE System SHALL return an error response indicating the runner is already active.

### Requirement 2: Concurrency Configuration

**User Story:** As a user, I want to configure the number of concurrent registrations, so that I can balance speed against rate-limiting risks.

#### Acceptance Criteria

1. THE Auto_Reg_UI SHALL provide a numeric input field for concurrency with minimum value 1 and maximum value 5.
2. WHEN the AutoRegRunner is started, THE AutoRegRunner SHALL limit the number of simultaneous signup tasks to the configured concurrency value.
3. THE System SHALL default the concurrency value to 1 when no user input is provided.

### Requirement 3: Signup Flow Integration

**User Story:** As a user, I want the auto-registration to use the existing signup infrastructure with Worker-based OTP polling, so that no new mail integration code is needed.

#### Acceptance Criteria

1. WHEN processing an iCloud email, THE AutoRegRunner SHALL invoke the signup flow via `get_spec(mail_mode='worker')` → `spec.parse_line()` → `spec.build_request()` → `run_signup()` as demonstrated in `JobManager._run_worker()`.
2. THE AutoRegRunner SHALL pass `logs_url` and `api_key` from environment variables or UI configuration to the Worker mail provider.
3. THE AutoRegRunner SHALL use the Default_Password value from the UI input as the registration password for each signup attempt.
4. WHEN OTP verification is required, THE AutoRegRunner SHALL poll the icloud-cf-mail Worker endpoint using the target iCloud email address as the recipient filter.

### Requirement 4: Result Persistence

**User Story:** As a user, I want successful registrations stored in a dedicated table, so that I can retrieve credentials later.

#### Acceptance Criteria

1. WHEN a ChatGPT registration succeeds, THE System SHALL INSERT a row into the `chatgpt_accounts` table containing the email, password, and `secret_2fa` values.
2. WHEN a ChatGPT registration succeeds, THE System SHALL UPDATE the corresponding `icloud_emails` row setting `status='used_for_chatgpt'`.
3. THE System SHALL execute the INSERT and UPDATE within a single database transaction to maintain consistency.
4. IF the database transaction fails, THEN THE System SHALL log the error and continue processing the next available email.

### Requirement 5: Database Schema Migration

**User Story:** As a developer, I want the new `chatgpt_accounts` table created via the existing migration system, so that schema changes are tracked and reproducible.

#### Acceptance Criteria

1. THE System SHALL define a `chatgpt_accounts` table with columns: `id INTEGER PRIMARY KEY AUTOINCREMENT`, `email TEXT NOT NULL UNIQUE`, `password TEXT NOT NULL`, `secret_2fa TEXT`, `created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))`.
2. THE System SHALL register the new table creation as `MIGRATIONS[9]` in `db/schema.py` and increment `CURRENT_VERSION` to 9.
3. THE System SHALL add an index on `chatgpt_accounts(email)` for lookup performance.

### Requirement 6: Toggle Button UI

**User Story:** As a user, I want a toggle button in the HME tab header to start/stop auto-registration with a single click, so that control is immediate and visible.

#### Acceptance Criteria

1. THE Auto_Reg_UI SHALL display a toggle button labeled "AUTO REG GPT" in the HME tab header area.
2. WHEN the toggle is switched ON, THE System SHALL send a start request to the AutoRegRunner API endpoint.
3. WHEN the toggle is switched OFF, THE System SHALL send a stop request to the AutoRegRunner API endpoint.
4. WHILE the AutoRegRunner is running, THE toggle button SHALL display an active/ON visual state.
5. WHILE the AutoRegRunner is idle, THE toggle button SHALL display an inactive/OFF visual state.

### Requirement 7: Auto Reg Sub-Tab Layout

**User Story:** As a user, I want a dedicated sub-tab with a split view showing input emails and registration output side-by-side, so that I can monitor progress in realtime.

#### Acceptance Criteria

1. THE Auto_Reg_UI SHALL render as a sub-tab named "Auto Reg" inside the HME_Tab navigation.
2. THE Auto_Reg_UI SHALL use a 50/50 horizontal split layout with left panel and right panel.
3. THE left panel SHALL display a table of `icloud_emails` rows filtered to statuses `created` and `used_for_chatgpt`.
4. THE right panel SHALL display realtime output streamed via SSE showing `email|password|secret_2fa` for each completed registration.
5. THE Auto_Reg_UI SHALL include a "Default password" text input field for setting the registration password.
6. THE Auto_Reg_UI SHALL include a numeric input field for polling interval (seconds) with a minimum value of 10.
7. THE Auto_Reg_UI SHALL include the concurrency input field (1–5) as specified in Requirement 2.

### Requirement 8: Realtime Streaming

**User Story:** As a user, I want to see registration results appear immediately in the UI without refreshing, so that I can monitor the automation in realtime.

#### Acceptance Criteria

1. THE System SHALL provide an SSE endpoint `GET /api/icloud/autoreg/stream` that streams AutoRegRunner log events to connected clients.
2. WHEN a registration completes successfully, THE System SHALL push an SSE event containing `email`, `password`, and `secret_2fa` to all connected subscribers.
3. WHEN a registration fails, THE System SHALL push an SSE event with level `error` containing the email and error description.
4. THE System SHALL use a `LogBuffer` instance (same pattern as HmeRunner) to bridge async runner events to SSE transport.

### Requirement 9: API Endpoints

**User Story:** As a developer, I want RESTful API endpoints for controlling the AutoRegRunner, so that the frontend can manage the automation lifecycle.

#### Acceptance Criteria

1. THE System SHALL expose `POST /api/icloud/autoreg/start` accepting JSON body with fields: `concurrency` (int, 1–5), `poll_interval` (int, seconds), `default_password` (string), `logs_url` (string), `api_key` (string).
2. THE System SHALL expose `POST /api/icloud/autoreg/stop` to signal the AutoRegRunner to stop gracefully.
3. THE System SHALL expose `GET /api/icloud/autoreg/status` returning JSON with fields: `running` (bool), `processed` (int), `success` (int), `errors` (int), `current_cycle` (int).
4. THE System SHALL expose `GET /api/icloud/autoreg/stream` as the SSE endpoint for realtime log events.
5. THE System SHALL expose `GET /api/icloud/autoreg/accounts` returning paginated list of `chatgpt_accounts` rows.
6. WHEN an API endpoint is called without valid authentication token, THE System SHALL return HTTP 401.

### Requirement 10: Error Handling and Resilience

**User Story:** As a user, I want the auto-registration to continue processing remaining emails even when individual registrations fail, so that one failure does not stop the entire batch.

#### Acceptance Criteria

1. IF a single signup attempt raises an exception, THEN THE AutoRegRunner SHALL log the error, skip the failed email, and continue with the next available email.
2. IF no emails with `status='created'` are found during a poll cycle, THEN THE AutoRegRunner SHALL wait the configured poll interval before checking again.
3. IF the icloud-cf-mail Worker is unreachable during OTP polling, THEN THE AutoRegRunner SHALL retry up to 3 times with exponential backoff before marking the attempt as failed.
4. WHILE the AutoRegRunner is running, THE System SHALL track and expose cumulative statistics: total processed, successful, and failed counts.
