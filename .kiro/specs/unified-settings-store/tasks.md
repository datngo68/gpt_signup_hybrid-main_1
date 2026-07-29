# Implementation Plan: unified-settings-store

## Overview

Thống nhất runtime configuration vào bảng SQLite `settings` (flat KV, dot-namespaced key, JSON-encoded value). Triển khai tuần tự từ DB layer → Repository → HTTP API → Manager hydration → Write-through → Migration → Frontend integration.

## Tasks

- [x] 1. Schema migration v10
  - [x] 1.1 Thêm DDL bảng `settings` và index vào `db/schema.py`
    - Set `CURRENT_VERSION = 10`
    - Thêm `DDL_SETTINGS` và `DDL_SETTINGS_INDEXES` constants
    - Thêm `MIGRATIONS[10]` với CREATE TABLE + CREATE INDEX statements
    - Append vào `ALL_DDL` để fresh DB tạo sẵn bảng
    - _Requirements: R1.1, R1.2, R1.3, R1.4, R1.5_

- [x] 2. SettingsRepository + whitelist + validation
  - [x] 2.1 Khai báo whitelist constants và validation trong `db/repositories.py`
    - Thêm `_KEY_REGEX`, `_KEY_MAX_LEN = 128`
    - Thêm `_EXACT_KEYS: frozenset[str]` với toàn bộ key từ R8.1
    - Thêm `_SENSITIVE_KEYS: frozenset[str]` cho audit redaction (R10.5)
    - Thêm type constraint validators theo bảng design §3
    - _Requirements: R8.1, R8.2, R3.4, R3.6, R4.1_

  - [x] 2.2 Implement class `SettingsRepository` — CRUD methods
    - `__init__(self, engine: DatabaseEngine)`
    - `_validate_key(key)` — regex + length check
    - `_validate_whitelist(key, op)` — exact key check
    - `_validate_type(key, value)` — type constraint dispatch
    - `get(key) -> Any | None` — SELECT + json.loads, raise on corrupt (R3.3)
    - `set(key, value) -> None` — validate + UPSERT + audit log
    - `delete(key) -> bool` — validate + DELETE + audit log
    - `list(prefix=None) -> dict` — SELECT with optional prefix filter
    - `bulk_get(keys) -> dict` — SELECT WHERE key IN (...)
    - `bulk_set(items) -> None` — validate all → single transaction UPSERT + audit
    - _Requirements: R2.1, R2.2, R2.3, R2.4, R2.5, R2.6, R2.7, R2.8, R2.9, R3.1, R3.2, R3.3, R3.5, R4.2, R4.3, R4.5_

  - [x] 2.3 Implement `_with_retry` — atomic transaction + busy-lock retry
    - Retry max 3 lần với backoff `[50ms, 150ms, 400ms]`
    - Catch `sqlite3.OperationalError("locked")` → retry
    - Propagate `RepositoryError` sau max retries
    - _Requirements: R11.1, R11.3, R2.8_

  - [x] 2.4 Write property tests cho SettingsRepository (P1, P2, P3, P5, P6)
    - **Property P1: Round-trip consistency** — `∀ (k,v) ∈ whitelist × JSON-serializable: set(k,v); get(k) == v`
    - **Property P2: Whitelist rejection** — `∀ k ∉ whitelist: set(k, v) raises RepositoryError`
    - **Property P3: Regex rejection** — `∀ k not matching regex: set(k, v) raises RepositoryError`
    - **Property P5: Bulk atomicity** — `bulk_set fail at ki ⇒ DB unchanged for k1..kN`
    - **Property P6: Delete idempotency** — `delete(k) twice → 1st True, 2nd False`
    - **Validates: R2.9, R4.2, R3.4, R11.1, R2.3**

  - [x] 2.5 Write unit tests cho SettingsRepository
    - Test get/set/delete/list/bulk_get/bulk_set happy path
    - Test type validation failures (wrong type, out of range)
    - Test audit log entries created correctly
    - Test sensitive key redaction in audit
    - _Requirements: R2.1–R2.9, R3.1–R3.6, R10.1–R10.5_

- [x] 3. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Factory function và HTTP API
  - [x] 4.1 Thêm `get_settings_repo(engine)` vào `db/__init__.py`
    - Import `SettingsRepository` từ `db/repositories`
    - Factory function tạo instance với engine parameter
    - _Requirements: R2 (convenience)_

  - [x] 4.2 Implement 5 CRUD endpoints trong `web/server.py`
    - `GET /api/settings` — query param `prefix`, return `{"settings": {...}}`
    - `GET /api/settings/{key}` — return value hoặc 404
    - `PUT /api/settings/{key}` — body `{"value": ...}`, validate → 200 hoặc 422
    - `DELETE /api/settings/{key}` — return `{"deleted": true}` hoặc 404
    - `POST /api/settings/bulk` — body `{"items": {...}}`, atomic bulk_set → `{"updated": N}`
    - Tất cả gated bởi `require_token` middleware
    - Trả 422 khi whitelist/validation vi phạm
    - _Requirements: R5.1, R5.2, R5.3, R5.4, R5.5, R5.7, R5.8, R4.4_

  - [x] 4.3 Write unit tests cho Settings API endpoints
    - Test CRUD happy path (200 responses)
    - Test 404 cho key không tồn tại
    - Test 422 cho whitelist/validation violations
    - Test 401 cho missing/wrong token
    - **Property P7: list prefix filter** — `list(prefix="reg") ⊆ _EXACT_KEYS ∩ {k: k.startswith("reg.")}`
    - **Validates: R5.1–R5.8, R4.4, R2.4**

- [x] 5. Manager hydration at startup
  - [x] 5.1 Thêm `apply_settings(settings_dict)` methods vào managers
    - `web/manager.py` — `JobManager.apply_settings()`: reg.headless, reg.job_timeout, reg.debug, reg.mode, reg.max_concurrent, etc.
    - Tương tự cho `SessionManager`, `LinkManager` nếu có settings relevant
    - Mỗi manager chỉ set field nếu key tồn tại trong dict, otherwise giữ default
    - _Requirements: R9.2, R9.3_

  - [x] 5.2 Mở rộng startup hook trong `web/server.py`
    - Gọi `settings_repo.list()` 1 lần
    - Truyền dict vào `manager.apply_settings(all_settings)` cho mỗi manager
    - Wrap trong try/except: log warning nếu RepositoryError, dùng default
    - Chạy trước `manager.recover_jobs()`
    - _Requirements: R9.1, R9.4, R9.5_

- [x] 6. Write-through hooks trong existing endpoints
  - [x] 6.1 Thêm write-through vào `POST /api/config`
    - Sau khi apply in-memory, gọi `settings_repo.bulk_set({"reg.mode": ..., "reg.headless": ..., ...})`
    - Map tất cả field từ payload → namespace `reg.*` + `proxy.url`
    - Catch RepositoryError → log warning, vẫn return 200, thêm `settings_persist_error` vào response
    - _Requirements: R6.1, R6.2, R6.6, R6.7_

  - [x] 6.2 Thêm write-through vào `PUT /api/icloud/run/config`
    - Map sang `hme.runner.action`, `hme.runner.count_per_cycle`, `hme.runner.retry_interval`, `hme.runner.label`, `hme.runner.note`
    - Gọi `settings_repo.bulk_set(...)` trong cùng handler
    - _Requirements: R6.3_

  - [x] 6.3 Thêm write-through vào AutoReg + Hotmail endpoints
    - AutoReg start → `autoreg.concurrency`, `autoreg.poll_interval`, `autoreg.password`, `autoreg.logs_url`, `autoreg.api_key`
    - Hotmail start → `hotmail.target_count`, `hotmail.concurrency`, `hotmail.max_attempts`, `hotmail.domain`, `hotmail.delay_between`, `hotmail.headless`, `hotmail.captcha_methods`, `hotmail.captcha_key`
    - _Requirements: R6.4, R6.5_

  - [x] 6.4 Write integration test cho write-through
    - Test `POST /api/config` → verify key written to settings table
    - Test RepositoryError không break existing endpoint
    - **Property P8: Audit log count** — audit log row count ≥ write count
    - **Validates: R6.1–R6.7, R10.1–R10.3**

- [x] 7. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Migration endpoint
  - [x] 8.1 Implement `POST /api/settings/import-from-localstorage` trong `web/server.py`
    - Accept body `{"localstorage": {<key>: <raw_string>, ...}}`
    - Parse localStorage values theo key mapping table (design §7)
    - Đọc `runtime/icloud/runner_config.json` server-side nếu tồn tại
    - Chỉ ghi key chưa tồn tại trong DB (skip existing)
    - Toàn bộ ghi trong 1 Atomic_Transaction
    - Response: `{"imported": [...], "skipped": [...], "client_keys_to_remove": [...], "renamed_runner_config_to": "..."}`
    - Rename `runner_config.json` → `.bak` sau commit thành công
    - Handle corrupt runner_config.json → skip file, thêm `runner_config_error`
    - Gate bởi `require_token`
    - _Requirements: R7.1, R7.2, R7.3, R7.4, R7.5, R7.6, R7.7, R7.8, R7.10, R7.11, R11.2_

  - [x] 8.2 Write test cho migration endpoint
    - Test import fresh (all keys imported)
    - Test idempotency (2nd call → imported=[])
    - Test corrupt runner_config.json handling
    - Test atomic rollback on failure
    - **Property P4: Import idempotent** — 2 calls same input → 2nd returns imported=[]
    - **Validates: R7.4, R7.7, R7.8, R7.10**

- [x] 9. Frontend — settings.js module
  - [x] 9.1 Tạo `web/static/settings.js`
    - IIFE module expose `window.Settings`
    - `Settings.bootstrap(token)` — check migrated flag → call migration endpoint → load all
    - `Settings.load(token)` — GET /api/settings → cache
    - `Settings.get(key)` — return from cache
    - `Settings.save(key, value, token)` — PUT /api/settings/{key} cho UI-only keys
    - _Requirements: R12.1, R12.2, R12.4, R12.5_

  - [x] 9.2 Thêm `<script src="/static/settings.js">` vào `web/static/index.html`
    - Load TRƯỚC app.js
    - _Requirements: R12.2_

- [x] 10. Frontend integration — app.js
  - [x] 10.1 Sửa `web/static/app.js` — dùng `Settings.bootstrap(token)` thay `loadSettings()`
    - Gọi `await Settings.bootstrap(token)` khi init
    - Hydrate UI controls từ `Settings.get(...)` thay vì parse localStorage
    - Bỏ references tới `LS_SETTINGS`, `loadSettings()` function cũ
    - _Requirements: R12.2, R12.3_

- [x] 11. Frontend integration — bỏ localStorage cũ trong feature modules
  - [x] 11.1 Sửa `web/static/hme.js` — bỏ localStorage privacy mask
    - Thay `localStorage.getItem('hme.privacy.mask.v1')` bằng `Settings.get('hme.privacy_mask')`
    - Persist thay đổi qua `Settings.save('hme.privacy_mask', value, token)`
    - _Requirements: R12.4_

  - [x] 11.2 Sửa `web/static/autoreg.js` — bỏ LS_KEY `autoreg.config.v1`
    - Dùng `Settings.get('autoreg.concurrency')`, `Settings.get('autoreg.poll_interval')`, etc.
    - Bỏ code đọc/ghi localStorage `autoreg.config.v1`
    - _Requirements: R12.2, R12.3_

  - [x] 11.3 Sửa `web/static/hotmail.js` — dùng Settings cho initial form values
    - Dùng `Settings.get('hotmail.*')` cho initial form population
    - _Requirements: R12.2_

  - [x] 11.4 Sửa `web/static/link.js` — bỏ LS_LINK_MODE
    - Thay `localStorage.getItem('gpt_reg.link.mode')` bằng `Settings.get('ui.link_mode')`
    - Persist qua `Settings.save('ui.link_mode', value, token)`
    - _Requirements: R12.4_

- [x] 12. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties (P1–P8 từ design §9)
- Audit log redaction (R10.5) được cover trong task 2.1 (`_SENSITIVE_KEYS`) và 2.2 (logic redact trong `_do_set`)
- Atomic transaction (R11) được cover trong task 2.3 (`_with_retry`) và 2.2 (`bulk_set` dùng single transaction)
- Language: Python (backend), JavaScript (frontend)

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1"] },
    { "id": 2, "tasks": ["2.2", "2.3"] },
    { "id": 3, "tasks": ["2.4", "2.5", "4.1"] },
    { "id": 4, "tasks": ["4.2", "5.1"] },
    { "id": 5, "tasks": ["4.3", "5.2", "6.1", "6.2", "6.3"] },
    { "id": 6, "tasks": ["6.4", "8.1"] },
    { "id": 7, "tasks": ["8.2", "9.1"] },
    { "id": 8, "tasks": ["9.2", "10.1"] },
    { "id": 9, "tasks": ["11.1", "11.2", "11.3", "11.4"] }
  ]
}
```
