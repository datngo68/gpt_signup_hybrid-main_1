# Requirements Document

## Introduction

Chuyển đổi toàn bộ hệ thống persistence của `gpt_signup_hybrid` từ JSON files + in-memory sang SQLite. Mục tiêu: đảm bảo toàn vẹn dữ liệu (không mất data khi stop service), cấu trúc lại logic xử lý dữ liệu với transaction safety, và cung cấp một data access layer thống nhất cho toàn bộ ứng dụng.

Hiện tại hệ thống có 5 điểm persistence:
1. **Outlook state** (`runtime/outlook_state/*.json`) — trạng thái combo (used_for_signup, refresh_token, last_error)
2. **Session results** (`runtime/sessions/*.json`) — kết quả signup (session_token, access_token, cookies)
3. **Pool files** (text format `email|password|refresh_token|client_id`) — nguồn combo input
4. **Web JobManager** (in-memory) — queue + trạng thái jobs, mất hoàn toàn khi restart
5. **Refresh token rotation** — OutlookMailProvider persist token mới ra JSON sau mỗi refresh

## Glossary

- **Database_Engine**: Module SQLite wrapper cung cấp connection pooling, WAL mode, và transaction management cho toàn bộ ứng dụng
- **Repository**: Data access layer trừu tượng hóa SQL queries, cung cấp interface CRUD cho từng entity
- **Combo**: Chuỗi thông tin xác thực email dạng `email|password|refresh_token|client_id` dùng để đăng ký tài khoản
- **Job**: Một đơn vị công việc signup/session/link trong web UI, có lifecycle: queued → running → success/error/cancelled
- **Outlook_State**: Trạng thái sử dụng của một combo (đã signup chưa, lỗi gì, refresh_token mới nhất)
- **Session_Result**: Kết quả thành công của một lần signup (session_token, access_token, cookies, 2FA secret)
- **Migration_Tool**: Utility chuyển dữ liệu từ JSON files hiện tại sang SQLite database mới

## Requirements

### Requirement 1: Database Engine khởi tạo và cấu hình

**User Story:** As a developer, I want a centralized SQLite engine that initializes automatically with proper settings, so that all modules share a single reliable database connection.

#### Acceptance Criteria

1. WHEN the application starts, THE Database_Engine SHALL create any missing intermediate directories and the SQLite database file at the configured path (default: `runtime/data.db`)
2. WHEN the Database_Engine initializes, THE Database_Engine SHALL enable WAL (Write-Ahead Logging) journal mode for concurrent read/write support
3. WHEN the Database_Engine initializes, THE Database_Engine SHALL set `PRAGMA busy_timeout = 5000` to handle lock contention
4. WHEN the Database_Engine initializes, THE Database_Engine SHALL set `PRAGMA foreign_keys = ON` to enforce referential integrity
5. WHEN the Database_Engine initializes, THE Database_Engine SHALL execute schema migrations within a single transaction to create all required tables if they do not exist
6. THE Database_Engine SHALL expose a `get_connection()` method that returns a thread-safe, context-managed connection with automatic commit on success and rollback on exception
7. IF the database file path is not writable, THEN THE Database_Engine SHALL raise a descriptive error at startup instead of failing silently at runtime
8. IF a schema migration fails, THEN THE Database_Engine SHALL roll back all migration changes within that transaction and raise an error indicating the failed migration step

### Requirement 2: Schema thiết kế cho Outlook State

**User Story:** As a developer, I want outlook combo state stored in SQLite with proper schema, so that refresh token rotations and usage tracking survive service restarts.

#### Acceptance Criteria

1. WHEN the application initializes, THE Database_Engine SHALL create table `outlook_combos` (if not exists) with columns: `email TEXT PRIMARY KEY`, `password TEXT NOT NULL`, `refresh_token TEXT NOT NULL`, `client_id TEXT NOT NULL`, `used_for_signup INTEGER NOT NULL DEFAULT 0`, `last_error TEXT`, `last_failed_at TEXT`, `used_at TEXT`, `last_refresh_at TEXT`, `created_at TEXT NOT NULL DEFAULT (datetime('now'))`
2. WHEN a refresh token is rotated, THE Repository SHALL update `refresh_token` and set `last_refresh_at` to the current UTC timestamp in ISO 8601 format within a single transaction
3. WHEN a combo is marked as signup success, THE Repository SHALL set `used_for_signup = 1`, set `used_at` to current UTC timestamp in ISO 8601 format, and set `last_error` to NULL within a single transaction
4. WHEN a combo is marked as signup failure, THE Repository SHALL set `last_error` to the provided error string and `last_failed_at` to current UTC timestamp in ISO 8601 format without modifying `used_for_signup`
5. WHEN picking an available combo, THE Repository SHALL return the first combo WHERE `used_for_signup = 0` AND (`last_error` IS NULL OR `last_error` does not contain any of the terminal error substrings: "registration_disallowed", "invalid_grant", "AADSTS50173", "AADSTS70008") ordered by `created_at ASC`
6. IF no combo satisfies the availability criteria in criterion 5, THEN THE Repository SHALL raise an error indicating pool exhaustion with the total combo count

### Requirement 3: Schema thiết kế cho Jobs

**User Story:** As a developer, I want all job state persisted in SQLite, so that running/queued jobs can be recovered after a service restart.

#### Acceptance Criteria

1. THE Database_Engine SHALL create table `jobs` with columns: `id TEXT PRIMARY KEY`, `email TEXT NOT NULL`, `combo TEXT NOT NULL`, `mail_mode TEXT NOT NULL DEFAULT 'outlook' CHECK(mail_mode IN ('outlook','worker','gmail_advanced'))`, `status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','success','error','cancelled'))`, `error TEXT`, `password TEXT`, `secret TEXT`, `first_code TEXT`, `user_id TEXT`, `session_path TEXT`, `payment_link TEXT`, `session_data TEXT`, `created_at REAL NOT NULL`, `started_at REAL`, `finished_at REAL`, `job_type TEXT NOT NULL DEFAULT 'signup'`
2. THE Database_Engine SHALL create table `job_logs` with columns: `id INTEGER PRIMARY KEY AUTOINCREMENT`, `job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE`, `line TEXT NOT NULL`, `created_at REAL NOT NULL DEFAULT (unixepoch('subsec'))`
3. WHEN a job status changes to `running`, THE Repository SHALL update the `jobs` row setting `status`, `started_at` to the current unix timestamp within a single transaction
4. WHEN a job status changes to `success`, `error`, or `cancelled`, THE Repository SHALL update the `jobs` row setting `status`, `error` (if applicable), and `finished_at` to the current unix timestamp within a single transaction
5. WHEN a log line is appended, THE Repository SHALL insert into `job_logs` table
6. WHEN the web server starts, THE Repository SHALL load all jobs with status `queued` or `running`, reset any `running` to `queued` (clearing `started_at`), and re-enqueue them ordered by `created_at` ASC
7. WHEN `clear_finished` is called, THE Repository SHALL delete jobs with status `success` or `error` along with their log entries (cascade)
8. IF a database write operation fails, THEN THE Repository SHALL raise an exception propagating the underlying error without silently discarding the write

### Requirement 4: Schema thiết kế cho Session Results

**User Story:** As a developer, I want signup results stored in SQLite instead of individual JSON files, so that results are queryable and not scattered across filesystem.

#### Acceptance Criteria

1. THE Database_Engine SHALL create table `session_results` with columns: `id INTEGER PRIMARY KEY AUTOINCREMENT`, `email TEXT NOT NULL`, `password TEXT`, `name TEXT`, `age INTEGER`, `user_id TEXT`, `account_id TEXT`, `session_token TEXT`, `access_token TEXT`, `cookies TEXT` (JSON), `two_factor TEXT` (JSON), `phase1_seconds REAL`, `phase2_seconds REAL`, `otp_seconds REAL`, `created_at TEXT NOT NULL DEFAULT (datetime('now'))`
2. WHEN a signup completes with `success = True`, THE Repository SHALL insert a row into `session_results` with columns mapped from SignupResult fields: `email` (NOT NULL), `password`, `name`, `age`, `user_id`, `account_id`, `session_token`, `access_token`, `cookies` (serialized as JSON array), `phase1_seconds`, `phase2_seconds`, `otp_seconds`
3. WHEN 2FA is enabled post-signup, THE Repository SHALL update the `two_factor` column of the most recent `session_results` row matching the given email (ordered by `created_at DESC`, limit 1)
4. IF the Repository attempts to update `two_factor` and no matching row exists for the given email, THEN THE Repository SHALL raise an error indicating no session result found for that email
5. THE Repository SHALL provide an `export_json()` method that returns a dictionary with all column values of a session result row, with `cookies` and `two_factor` deserialized from JSON strings back to their native types (list and dict respectively), matching the structure produced by `SignupResult.model_dump()`

### Requirement 5: Transaction safety và data integrity

**User Story:** As a developer, I want all write operations wrapped in transactions, so that partial writes never corrupt the database.

#### Acceptance Criteria

1. IF no transaction is already active on the connection, THEN THE Database_Engine SHALL wrap every write operation (INSERT, UPDATE, DELETE) in an explicit transaction; IF a transaction is already active, THEN THE Database_Engine SHALL execute the write within the existing transaction without opening a nested transaction
2. IF an exception occurs during a write transaction, THEN THE Database_Engine SHALL rollback the transaction and re-raise the original exception with its type preserved
3. WHEN multiple related writes occur (e.g., update job status + insert log line), THE Repository SHALL execute them within a single transaction
4. THE Database_Engine SHALL use `BEGIN IMMEDIATE` for write transactions to acquire locks early and prevent deadlocks under concurrent access
5. IF the database lock cannot be acquired within the configured `busy_timeout`, THEN THE Database_Engine SHALL raise an OperationalError, triggering a transaction rollback per criterion 2
6. WHEN the service is stopped (SIGINT/SIGTERM), THE Database_Engine SHALL wait up to 5 seconds for all in-flight transactions to either commit or rollback, THEN close the database connection before process exit. An in-flight transaction is detected by attempting to acquire the internal write lock (threading.RLock); if the lock is held, a transaction is in progress.
7. IF the 5-second shutdown grace period expires with transactions still in-flight (lock not acquired), THEN THE Database_Engine SHALL close the connection directly, which triggers SQLite's implicit rollback of any pending transaction

### Requirement 6: Migration từ JSON sang SQLite

**User Story:** As a developer, I want to migrate existing JSON state files to the new SQLite database, so that no historical data is lost during the transition.

#### Acceptance Criteria

1. WHEN the `migrate` CLI command is executed, THE Migration_Tool SHALL read all files in `runtime/outlook_state/*.json` and insert corresponding rows into `outlook_combos` table
2. WHEN the `migrate` CLI command is executed, THE Migration_Tool SHALL read all files matching `runtime/sessions/signup-*.json` (excluding `*.2fa.json` files) and insert corresponding rows into `session_results` table
3. IF a record already exists in the database (duplicate `email` for `outlook_combos`, or duplicate `email` + `created_at` timestamp for `session_results`), THEN THE Migration_Tool SHALL skip that record and log a warning indicating the filename and reason
4. WHEN migration completes, THE Migration_Tool SHALL print a summary per entity type containing: total files found, files successfully inserted, files skipped due to duplicate, and files skipped due to parse error
5. THE Migration_Tool SHALL wrap the migration in one separate transaction per entity type (`outlook_combos` and `session_results`): if an unrecoverable database error occurs mid-transaction, all inserts for that entity type SHALL be rolled back
6. IF a JSON file contains invalid JSON or is unreadable, THEN THE Migration_Tool SHALL skip that file, log an error containing the filename, and continue processing remaining files within the same entity type
7. IF a source directory (`runtime/outlook_state/` or `runtime/sessions/`) does not exist or contains zero matching files, THEN THE Migration_Tool SHALL report zero records for that entity type and complete without error

### Requirement 7: Repository layer cho data access

**User Story:** As a developer, I want a clean repository abstraction, so that business logic modules do not contain raw SQL and can be tested independently.

#### Acceptance Criteria

1. THE Repository SHALL expose separate classes: `ComboRepository`, `JobRepository`, `SessionResultRepository`
2. THE Repository SHALL accept a database connection/engine in its constructor (dependency injection) so that calling modules can supply a test double without touching real storage
3. THE ComboRepository SHALL provide methods: `get_by_email(email) → record | None`, `upsert(combo_data)`, `ensure_exists(combo_data)`, `mark_success(email)`, `mark_failure(email, error)`, `pick_available() → record` (raises `RepositoryError` on pool exhaustion), `update_refresh_token(email, token)`
4. THE JobRepository SHALL provide methods: `create(job_data) → job_id`, `update_status(job_id, status)`, `update_email(job_id, email)`, `append_log(job_id, line)`, `get_by_id(job_id) → record | None`, `list_all() → list`, `list_by_status(status) → list`, `list_completed() → list`, `delete(job_id)`, `delete_finished(job_type=None) → deleted_count`
5. THE SessionResultRepository SHALL provide methods: `create(result_data) → record_id`, `get_by_email(email) → record | None`, `update_2fa(email, mfa_data)`, `export_json(email) → dict | None`
6. THE Repository SHALL never expose raw SQL connections or query strings to calling modules
7. IF a repository read method receives an identifier that matches no record, THEN THE Repository SHALL return None rather than raising an exception
8. IF a repository write operation fails due to a storage error, THEN THE Repository SHALL raise a dedicated `RepositoryError` exception containing the operation name and cause, without exposing raw driver details

### Requirement 8: Pool file import vào database

**User Story:** As a developer, I want pool files imported into SQLite on startup or via CLI, so that combo management is unified through the database.

#### Acceptance Criteria

1. WHEN an `import-pool` CLI command is executed with a pool file path, THE Migration_Tool SHALL parse the pool file (format: `email|password|refresh_token|client_id`, skipping blank lines and lines starting with `#`) and upsert combos into `outlook_combos` table
2. WHEN importing a combo whose email already exists in the database, THE Migration_Tool SHALL preserve the existing values of `used_for_signup`, `used_at`, `last_error`, and `last_failed_at` columns
3. WHEN importing a combo whose email already exists in the database, THE Migration_Tool SHALL overwrite `password`, `refresh_token`, and `client_id` with the values from the pool file line
4. IF a combo line fails to parse (invalid format, missing fields, or invalid `refresh_token` prefix), THEN THE Migration_Tool SHALL print the error with line number to stderr and continue processing remaining lines
5. WHEN import completes, THE Migration_Tool SHALL print summary to stdout: total lines processed, inserted count, updated count, skipped count (parse errors)
6. IF the specified pool file path does not exist or is not readable, THEN THE Migration_Tool SHALL print an error message to stderr and exit with code 1 without modifying the database
7. WHEN processing all lines in the pool file, THE Migration_Tool SHALL commit all successful upserts in a single transaction after all parseable lines have been processed

### Requirement 9: Backward compatibility cho CLI output

**User Story:** As a developer, I want the CLI signup command to still produce JSON output files, so that external tools consuming session files continue to work.

#### Acceptance Criteria

1. WHEN the `signup` CLI command completes (success or failure), THE System SHALL write the SignupResult JSON file to the output path determined by the `--output` flag or the default path `runtime/sessions/signup-<timestamp>-<email>.json`, unless the `--no-file-output` flag is set
2. WHEN the `signup` CLI command completes with `result.success == True`, THE System SHALL insert a row into the `session_results` table with the fields defined in Requirement 4 schema (email, password, name, age, user_id, account_id, session_token, access_token, cookies, phase1_seconds, phase2_seconds, otp_seconds)
3. WHEN the `enable-2fa` CLI command completes successfully, THE System SHALL write the `.2fa.json` file to the output path (existing behavior) unless `--no-file-output` is set, AND update the `two_factor` column in the `session_results` row matched by `email`
4. THE System SHALL support a `--no-file-output` flag on both the `signup` and `enable-2fa` commands to skip JSON file creation and only persist to SQLite
5. IF the SQLite persist operation fails during CLI execution AND `--no-file-output` is not set, THEN THE System SHALL log a warning to stderr and continue (JSON file output still produced, CLI exit code unchanged)
6. IF the SQLite persist operation fails during CLI execution AND `--no-file-output` is set, THEN THE System SHALL exit with a non-zero exit code and print an error message indicating the persistence failure

### Requirement 10: Web UI job recovery sau restart

**User Story:** As a developer, I want the web server to recover job state from SQLite on startup, so that users see their previous jobs and results without data loss.

#### Acceptance Criteria

1. WHEN the web server starts, THE JobManager SHALL load all jobs from SQLite (including their associated log lines from the `job_logs` table) and populate the in-memory job list preserving original creation order
2. WHEN the web server starts, THE JobManager SHALL re-enqueue jobs that were `queued` or `running` (treating `running` as interrupted — reset status to `queued`, clear `started_at`) and start worker loops to process them
3. WHEN a job transitions state in memory, THE JobManager SHALL await the SQLite persist to complete successfully before broadcasting the SSE event
4. IF the SQLite persist fails during a job state transition, THEN THE JobManager SHALL preserve the job's previous in-memory state and NOT broadcast the SSE event
5. WHEN the web server receives SIGINT or SIGTERM, THE JobManager SHALL mark all `running` jobs as `queued` in SQLite (ready for next startup) before process exit
6. WHEN `clear_finished` is called via API, THE JobManager SHALL delete jobs with status `success` or `error` (and their associated log entries) from both memory and SQLite

### Requirement 11: Data retention và cleanup

**User Story:** As a developer, I want automatic cleanup of old log entries and session results, so that the database does not grow unbounded over time.

#### Acceptance Criteria

1. THE JobRepository SHALL provide a `cleanup_old_logs(max_age_days: int = 30)` method that deletes `job_logs` entries older than the specified number of days for jobs that are in terminal state (`success`, `error`, `cancelled`)
2. THE SessionResultRepository SHALL provide a `cleanup_old_results(max_age_days: int = 90)` method that deletes `session_results` rows older than the specified number of days
3. WHEN `cleanup_old_logs` is called, it SHALL only delete logs for jobs in terminal state — logs for `queued` or `running` jobs SHALL be preserved regardless of age
4. WHEN `cleanup_old_results` is called, it SHALL delete rows where `created_at` is older than `max_age_days` from current time
5. Both cleanup methods SHALL return the count of deleted rows
6. Both cleanup methods SHALL execute within a single transaction
7. THE application MAY call cleanup methods on startup or via CLI command — the trigger mechanism is left to the caller

