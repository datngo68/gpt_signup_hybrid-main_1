# Implementation Plan: SQLite Persistence Layer

## Overview

Chuyển persistence layer từ JSON files + in-memory sang SQLite. Triển khai theo thứ tự dependency: Engine → Schema → Repositories → Migration Tool → Tích hợp CLI/Web/MailProvider → Graceful Shutdown.

Ngôn ngữ: Python (stdlib `sqlite3`, async context manager đồng bộ trên calling thread).

## Tasks

- [x] 1. Khởi tạo module `db/` và Database Engine
  - [x] 1.1 Tạo package `db/` với `__init__.py`, `engine.py`, `schema.py`
    - Tạo thư mục `gpt_signup_hybrid/db/`
    - `__init__.py` export `get_engine()`, `get_repos()`
    - `schema.py` chứa DDL strings (tables, indexes) + `_schema_version` table + `CURRENT_VERSION = 2`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

  - [x] 1.2 Implement `DatabaseEngine` class trong `engine.py`
    - Constructor nhận `db_path: Path` (default `runtime/data.db`), tạo directories nếu thiếu
    - Raise `PermissionError` nếu path không writable
    - Enable WAL mode, `busy_timeout=5000`, `foreign_keys=ON`
    - Implement `get_connection()` context manager: `BEGIN IMMEDIATE`, auto-commit on success, rollback on exception, re-raise original exception type
    - Implement `get_connection_async()` — chạy lock/tx đồng bộ trên calling thread (threading.RLock phải release trên cùng thread; asyncio.to_thread dispatch sang thread khác gây RuntimeError)
    - Implement `raw_connection()` cho read-only operations (không BEGIN IMMEDIATE)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.6, 1.7, 5.1, 5.2, 5.4_

  - [x] 1.3 Implement schema migration trong `DatabaseEngine.__init__`
    - Đọc `_schema_version` table, so sánh với `CURRENT_VERSION`
    - Execute DDL trong single transaction, rollback toàn bộ nếu fail
    - Raise `SchemaError` (subclass `DatabaseError`) khi migration fail
    - _Requirements: 1.5, 1.8_

  - [x] 1.4 Write property test cho transaction safety (Property 2)
    - **Property 2: Connection context manager commit/rollback**
    - **Validates: Requirements 1.6, 5.1, 5.2**

- [x] 2. Implement Repository Layer
  - [x] 2.1 Tạo `db/repositories.py` — exception classes và `ComboRepository`
    - Define `DatabaseError`, `SchemaError`, `RepositoryError` exception hierarchy
    - Implement `ComboRepository` với methods: `get_by_email`, `upsert`, `mark_success`, `mark_failure`, `pick_available`, `update_refresh_token`, `list_all`
    - `pick_available()` filter: `used_for_signup=0` AND `last_error` không chứa terminal strings, ORDER BY `created_at ASC`
    - Raise `RepositoryError` nếu pool exhausted (từ `pick_available` trả None, caller xử lý)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 7.1, 7.2, 7.3, 7.6, 7.7, 7.8_

  - [x] 2.2 Write property tests cho ComboRepository (Properties 3, 4, 5)
    - **Property 3: Refresh token rotation round-trip**
    - **Property 4: Combo state mutation preserves invariants**
    - **Property 5: Pick available returns correct combo under filtering rules**
    - **Validates: Requirements 2.2, 2.3, 2.4, 2.5, 2.6**

  - [x] 2.3 Implement `JobRepository`
    - Methods: `create`, `update_status`, `append_log`, `get_by_id`, `list_all`, `list_by_status`, `delete`, `delete_finished`, `get_logs`, `recover_interrupted`
    - `update_status("running")` → set `started_at`; terminal status → set `finished_at`
    - `recover_interrupted()` → SELECT status IN ('queued','running'), reset running→queued, clear `started_at`, return ordered by `created_at ASC`
    - `delete_finished()` → DELETE WHERE status IN ('success','error') (CASCADE xoá job_logs)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 7.4, 7.7, 7.8_

  - [x] 2.4 Write property tests cho JobRepository (Properties 6, 7, 8, 9)
    - **Property 6: Job status transition updates correct fields**
    - **Property 7: Job log append round-trip**
    - **Property 8: Reset running to queued preserves other statuses**
    - **Property 9: Delete finished removes only success/error jobs**
    - **Validates: Requirements 3.3, 3.4, 3.5, 3.6, 3.7, 10.2, 10.6**

  - [x] 2.5 Implement `SessionResultRepository`
    - Methods: `create`, `get_by_email`, `update_2fa`, `export_json`, `list_all`
    - `create()` serialize `cookies` (list→JSON string), return auto-increment id
    - `update_2fa()` → UPDATE most recent row (ORDER BY `created_at DESC` LIMIT 1), raise `RepositoryError` nếu không tìm thấy row
    - `export_json()` → deserialize `cookies` (JSON→list), `two_factor` (JSON→dict)
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 7.5, 7.7, 7.8_

  - [x] 2.6 Write property tests cho SessionResultRepository (Properties 10, 11, 12)
    - **Property 10: Session result create round-trip**
    - **Property 11: Session result export_json deserialization**
    - **Property 12: 2FA update targets only the most recent record**
    - **Validates: Requirements 4.2, 4.3, 4.5**

  - [x] 2.7 Write property test cho read non-existent identifiers (Property 17)
    - **Property 17: Read non-existent identifiers return None**
    - **Validates: Requirements 7.7**

- [x] 3. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Implement Migration Tool
  - [x] 4.1 Tạo `db/migrate.py` — `MigrationTool`, `MigrationSummary`, `ImportSummary`
    - `MigrationTool.__init__` nhận `engine`, `combo_repo`, `session_repo` (dependency injection)
    - Implement `migrate_outlook_state(state_dir)`: đọc `runtime/outlook_state/*.json`, parse fields (email, refresh_token, client_id, password từ filename hoặc content), upsert vào `outlook_combos`, skip duplicate (log warning), wrap trong 1 transaction per entity type
    - Implement `migrate_sessions(sessions_dir)`: đọc `runtime/sessions/signup-*.json` (exclude `*.2fa.json`), insert vào `session_results`, skip duplicate email+created_at, wrap trong 1 transaction
    - Handle: invalid JSON → log error + skip, directory không tồn tại → report 0 records
    - Return `MigrationSummary` dataclass cho mỗi entity type
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7_

  - [x] 4.2 Write property tests cho Migration (Properties 13, 14, 16)
    - **Property 13: Migration preserves data from JSON files to SQLite**
    - **Property 14: Migration skips duplicates without error**
    - **Property 16: Parse resilience — valid items processed despite invalid siblings**
    - **Validates: Requirements 6.1, 6.2, 6.3, 6.6**

  - [x] 4.3 Implement `import_pool_file(pool_path)` trong `MigrationTool`
    - Parse pool file format `email|password|refresh_token|client_id` (skip blank, skip `#`)
    - Upsert logic: existing email → preserve `used_for_signup`, `used_at`, `last_error`, `last_failed_at`; overwrite `password`, `refresh_token`, `client_id`
    - Parse error → print to stderr với line number, continue
    - Commit tất cả successful upserts trong single transaction sau khi process hết
    - File không tồn tại → print error to stderr, raise SystemExit(1)
    - Return `ImportSummary` dataclass
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7_

  - [x] 4.4 Write property test cho pool import upsert semantics (Property 15)
    - **Property 15: Pool import upsert preserves state, updates credentials**
    - **Validates: Requirements 8.1, 8.2, 8.3**

- [x] 5. Tích hợp CLI — thêm commands `migrate` và `import-pool`
  - [x] 5.1 Thêm CLI command `migrate` trong `cli.py`
    - Khởi tạo `DatabaseEngine` + repos + `MigrationTool`
    - Gọi `migrate_outlook_state()` + `migrate_sessions()`
    - Print summary per entity type (total, inserted, skipped_duplicate, skipped_error)
    - _Requirements: 6.1, 6.2, 6.4_

  - [x] 5.2 Thêm CLI command `import-pool` trong `cli.py`
    - Nhận argument `pool_file: Path`
    - Khởi tạo engine + repos + MigrationTool
    - Gọi `import_pool_file()`, print summary
    - Exit code 1 nếu file không tồn tại
    - _Requirements: 8.1, 8.5, 8.6_

  - [x] 5.3 Tích hợp SQLite persist vào CLI `signup` command
    - Sau signup success: insert row vào `session_results` table
    - Sau signup (success hoặc fail): update combo state qua `ComboRepository` thay vì JSON file
    - Thêm flag `--no-file-output` để skip JSON file creation
    - Nếu SQLite persist fail + file output enabled → log warning, continue
    - Nếu SQLite persist fail + `--no-file-output` → exit code 1
    - _Requirements: 9.1, 9.2, 9.4, 9.5, 9.6_

  - [x] 5.4 Tích hợp SQLite persist vào CLI `enable-2fa` command
    - Sau 2FA success: update `two_factor` column qua `SessionResultRepository.update_2fa()`
    - Respect `--no-file-output` flag
    - _Requirements: 9.3, 9.4_

- [x] 6. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Tích hợp Web UI — JobManager với SQLite
  - [x] 7.1 Refactor `JobManager` inject `JobRepository`
    - Constructor nhận optional `job_repo: JobRepository | None`
    - `add_jobs()` → persist job vào SQLite trước khi broadcast SSE
    - `_job_log()` → persist log line vào SQLite
    - `update_status` transitions → persist trước khi broadcast
    - Nếu SQLite persist fail → giữ in-memory state cũ, KHÔNG broadcast SSE
    - _Requirements: 10.3, 10.4_

  - [x] 7.2 Implement job recovery on startup
    - Khi `get_manager()` được gọi lần đầu: load all jobs từ SQLite (kèm log lines)
    - Reset `running` → `queued` (clear `started_at`)
    - Re-enqueue recovered jobs theo `created_at ASC`
    - _Requirements: 10.1, 10.2_

  - [x] 7.3 Implement `clear_finished` với SQLite
    - Xoá jobs success/error từ cả memory và SQLite (cascade xoá job_logs)
    - _Requirements: 10.6_

  - [x] 7.4 Implement shutdown handler — mark running jobs as queued
    - Khi SIGINT/SIGTERM: mark all `running` jobs → `queued` trong SQLite
    - Đảm bảo jobs recoverable ở lần startup tiếp theo
    - _Requirements: 10.5_

  - [x] 7.5 Write property test cho job recovery (Property 18)
    - **Property 18: Job recovery loads all persisted jobs with logs**
    - **Validates: Requirements 10.1**

- [x] 8. Tích hợp OutlookMailProvider với ComboRepository
  - [x] 8.1 Refactor `OutlookMailProvider._persist_state` sử dụng `ComboRepository`
    - Inject optional `combo_repo: ComboRepository | None` vào constructor
    - Khi refresh token rotate: gọi `combo_repo.update_refresh_token()` thay vì write JSON
    - Fallback về JSON persist nếu `combo_repo` is None (backward compat)
    - _Requirements: 2.2_

  - [x] 8.2 Refactor `outlook_pool.py` functions sử dụng `ComboRepository`
    - `pick_first_available` → delegate sang `combo_repo.pick_available()` khi có repo
    - `mark_signup_success` → delegate sang `combo_repo.mark_success()`
    - `mark_signup_failure` → delegate sang `combo_repo.mark_failure()`
    - Giữ backward compat: fallback JSON nếu không có repo
    - _Requirements: 2.3, 2.4, 2.5_

- [x] 9. Implement Graceful Shutdown cho DatabaseEngine
  - [x] 9.1 Implement `DatabaseEngine.close(timeout=5.0)`
    - Đợi in-flight transactions commit/rollback trong `timeout` seconds
    - Sau timeout → force rollback + close connection
    - Set `is_closed = True` property
    - _Requirements: 5.6, 5.7_

  - [x] 9.2 Wire shutdown handler vào application lifecycle
    - Register SIGINT/SIGTERM handler gọi `engine.close()`
    - Web server: gọi trước uvicorn shutdown
    - CLI: gọi trong atexit hoặc signal handler
    - _Requirements: 5.6, 5.7_

- [x] 10. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties defined in design document
- Unit tests validate specific examples and edge cases
- Backward compatibility được duy trì qua fallback logic khi `combo_repo` is None
- Tất cả repository methods đều synchronous (get_connection_async chạy trên calling thread, không qua asyncio.to_thread)

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2"] },
    { "id": 2, "tasks": ["1.3"] },
    { "id": 3, "tasks": ["1.4", "2.1"] },
    { "id": 4, "tasks": ["2.2", "2.3", "2.5"] },
    { "id": 5, "tasks": ["2.4", "2.6", "2.7"] },
    { "id": 6, "tasks": ["4.1"] },
    { "id": 7, "tasks": ["4.2", "4.3"] },
    { "id": 8, "tasks": ["4.4", "5.1", "5.2"] },
    { "id": 9, "tasks": ["5.3", "5.4"] },
    { "id": 10, "tasks": ["7.1", "8.1", "8.2"] },
    { "id": 11, "tasks": ["7.2", "7.3", "7.4"] },
    { "id": 12, "tasks": ["7.5", "9.1"] },
    { "id": 13, "tasks": ["9.2"] }
  ]
}
```
