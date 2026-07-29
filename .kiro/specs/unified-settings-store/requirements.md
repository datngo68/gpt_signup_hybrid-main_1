# Requirements Document

## Introduction

`unified-settings-store` thống nhất toàn bộ runtime configuration của `gpt_signup_hybrid` (signup mode/headless/debug, proxy, default password, job timeout, post-reg toggles, auto-retry, mail-mode selector, worker config, autoreg config, HME runner form, HME privacy mask, Hotmail batch config, active tab) vào một SQLite key-value store duy nhất, thay thế phân mảnh hiện tại giữa `localStorage` (frontend) và `runtime/icloud/runner_config.json` (backend).

Phạm vi:
- Server-side persist 1 nguồn duy nhất qua bảng `settings` (flat KV, dot-namespaced key, JSON-encoded value, schema migration v10).
- Repository CRUD + HTTP API + write-through từ các config endpoint hiện có.
- Một-lần migration `localStorage` (+ legacy `runner_config.json`) → DB qua endpoint dedicated, idempotent qua cờ client-side.
- Manager hydration tại boot từ DB (thay thế `loadSettings()` defaults phía frontend).

Ngoài phạm vi:
- Textarea drafts (`gpt_reg.input.reg`, `gpt_reg.input.session`, `gpt_reg.link.input.*`) tiếp tục giữ ở `localStorage` — nội dung tạm thời, không phải runtime config.
- Auth token (`gpt_reg.auth_token`) giữ nguyên cơ chế hiện tại.
- Encryption-at-rest cho secrets (proxy creds, captcha key, worker token): chấp nhận lưu plaintext theo quyết định clarify #3.

## Glossary

- **Settings_Store**: Bảng SQLite `settings` (flat KV) + index `idx_settings_key`, schema version 10.
- **Settings_Repository**: Lớp Python data-access (`db/repositories.py`) cung cấp CRUD `get/set/delete/list/bulk_get/bulk_set` cho `Settings_Store`.
- **Settings_API**: Tập HTTP endpoint dưới prefix `/api/settings` do FastAPI server (`web/server.py`) expose.
- **Settings_Manager**: Singleton in-process (FastAPI startup) đọc `Settings_Store` 1 lần khi boot và inject giá trị runtime cho `JobManager`/`SessionManager`/`LinkManager`/`HmeRunner`/`AutoRegRunner`/`HotmailJobManager`.
- **Setting_Key**: Chuỗi dot-namespaced ASCII trong whitelist (chi tiết R8). Match regex `^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$`, length ≤ 128.
- **Setting_Value**: Bất kỳ giá trị JSON-serializable nào (string / number / boolean / null / array / object), serialize bằng `json.dumps(..., ensure_ascii=False)` trước khi ghi cột `value TEXT`.
- **Settings_Migration_Endpoint**: `POST /api/settings/import-from-localstorage` — endpoint một lần chuyển snapshot `localStorage` từ client + đọc legacy `runtime/icloud/runner_config.json` server-side rồi ghi vào `Settings_Store`.
- **Migrated_Flag**: Khoá `localStorage` `gpt_reg.settings_migrated_v1` set ở client sau khi `Settings_Migration_Endpoint` trả 2xx; dùng cho idempotency client-side.
- **Legacy_Runner_Config_File**: File `<runtime_dir>/icloud/runner_config.json` do `RunnerConfigStore` (`web/runner_config_store.py`) tạo ra ở phiên bản trước.
- **Audit_Log**: Bảng `icloud_audit_log` (đã tồn tại) — ghi event_type, timestamp_iso, payload_json. `Settings_Store` mượn bảng này cho event type `settings.set` / `settings.delete` / `settings.bulk_set` / `settings.import`.
- **Atomic_Transaction**: Một SQLite transaction duy nhất (`BEGIN ... COMMIT`) đảm bảo all-or-nothing cho operations đa-row (`bulk_set`, `import-from-localstorage`).
- **Whitelist**: Tập key/prefix hợp lệ được nạp vào `Settings_Repository` lúc boot (R8). Mọi key ngoài whitelist phải bị reject ở cả Repository và HTTP API.

## Requirements

### Requirement 1: Schema migration v10

**User Story:** Là maintainer, tôi muốn một lần migration thêm bảng `settings` vào `db/schema.py` để mọi instance chạy upgrade tự động và không cần thao tác tay.

#### Acceptance Criteria

1. THE Settings_Store SHALL được khai báo bằng DDL chứa cột `id INTEGER PRIMARY KEY AUTOINCREMENT`, `key TEXT NOT NULL UNIQUE`, `value TEXT`, `updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))` và một index `idx_settings_key` trên cột `key`.
2. THE db.schema module SHALL set `CURRENT_VERSION = 10` và thêm entry `MIGRATIONS[10]` chứa các statement tạo bảng `settings` cùng index `idx_settings_key`.
3. WHEN engine khởi động trên DB ở version ≤ 9, THE migration runner SHALL chạy `MIGRATIONS[10]` đúng một lần và ghi version 10 vào `_schema_version`.
4. IF `MIGRATIONS[10]` đã chạy trước đó (DB version = 10), THEN THE migration runner SHALL không re-execute statement và không raise error (idempotent).
5. THE settings DDL SHALL được thêm vào `ALL_DDL` để fresh database tạo từ scratch có sẵn bảng `settings` và index `idx_settings_key`.

### Requirement 2: Repository CRUD

**User Story:** Là backend engineer, tôi muốn một `Settings_Repository` cung cấp API rõ ràng cho mọi tác vụ đọc/ghi settings để các module nghiệp vụ không cần biết SQL.

#### Acceptance Criteria

1. THE Settings_Repository SHALL expose method `get(key: str) -> Any | None` trả về giá trị Python đã JSON-decode, hoặc None nếu key không tồn tại.
2. THE Settings_Repository SHALL expose method `set(key: str, value: Any) -> None` chạy SQL `INSERT ... ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')`.
3. THE Settings_Repository SHALL expose method `delete(key: str) -> bool` trả về `True` nếu xoá đúng 1 row, `False` nếu key không tồn tại.
4. THE Settings_Repository SHALL expose method `list(prefix: str | None = None) -> dict[str, Any]` trả về dict `{key: decoded_value}`; khi `prefix` được cung cấp, kết quả chỉ chứa key thoả `key == prefix` hoặc bắt đầu bằng `prefix + "."`.
5. THE Settings_Repository SHALL expose method `bulk_get(keys: Sequence[str]) -> dict[str, Any]` chỉ chứa key tồn tại trong DB.
6. THE Settings_Repository SHALL expose method `bulk_set(items: Mapping[str, Any]) -> None` ghi toàn bộ items trong duy nhất một Atomic_Transaction.
7. WHEN bất kỳ method ghi nào của Settings_Repository (set/delete/bulk_set) hoàn tất thành công, THE Settings_Repository SHALL cập nhật cột `updated_at` về timestamp ISO 8601 UTC dạng `YYYY-MM-DDThh:mm:ss.fffZ`.
8. WHEN method ghi raise exception, THE Settings_Repository SHALL rollback transaction và re-raise dưới dạng `RepositoryError(operation=..., cause=...)` (giống pattern `ComboRepository`).
9. THE round-trip property SHALL hold: với mọi (key, value) hợp lệ trong whitelist và `value` là JSON-serializable, sau khi `set(key, value)` thì `get(key)` trả về object `==` `value` về mặt cấu trúc (số kiểu int/float, string, bool, None, list, dict — không bao gồm tuple — được round-trip qua JSON).

### Requirement 3: Value validation và JSON encoding

**User Story:** Là maintainer, tôi muốn repository từ chối giá trị không hợp lệ ngay tại biên thay vì để hỏng dữ liệu trong DB.

#### Acceptance Criteria

1. WHEN `Settings_Repository.set(key, value)` được gọi với `value` không serialize được bằng `json.dumps`, THE Settings_Repository SHALL raise `RepositoryError("set", TypeError(...))` và không thực hiện ghi.
2. THE Settings_Repository SHALL serialize value bằng `json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))` trước khi ghi để output deterministic.
3. WHEN `Settings_Repository.get(key)` đọc một row có `value` không decode được bằng `json.loads`, THE Settings_Repository SHALL raise `RepositoryError("get", json.JSONDecodeError(...))` (fail-fast — không silent fallback).
4. IF caller truyền key không match regex `^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$` hoặc length > 128, THEN THE Settings_Repository SHALL raise `RepositoryError("set", ValueError("invalid key: ..."))` và không ghi.
5. THE Settings_Repository SHALL áp dụng cùng validation regex cho mọi key xuất hiện trong `bulk_set`, `delete`, `list(prefix=...)` (validate prefix chỉ với regex; prefix rỗng là hợp lệ).
6. WHERE giá trị thuộc trường có ràng buộc kiểu chặt theo Whitelist (R8), THE Settings_Repository SHALL áp dụng type-check tương ứng trước khi ghi (ví dụ key `proxy.url` SHALL chấp nhận string hoặc null; key `reg.headless` SHALL chấp nhận boolean; key `reg.job_timeout` SHALL chấp nhận number trong khoảng [30, 600]).

### Requirement 4: Key whitelist enforcement

**User Story:** Là maintainer, tôi muốn whitelist khoá để khách lạ hoặc bug client không tạo bừa key rác trong DB.

#### Acceptance Criteria

1. THE Settings_Repository SHALL khởi tạo với một Whitelist gồm key chính xác hoặc namespace prefix (ví dụ `reg.*`, `proxy.*`, `hme.runner.*`, `hme.privacy_mask`, `autoreg.*`, `hotmail.*`, `ui.active_tab`, `mail_mode.*`).
2. WHEN caller truyền key không khớp Whitelist (cả qua `set` lẫn `bulk_set`), THE Settings_Repository SHALL raise `RepositoryError("set", ValueError("key not in whitelist: ..."))` và không thực hiện ghi.
3. WHEN caller gọi `delete(key)` với key ngoài Whitelist, THE Settings_Repository SHALL raise `RepositoryError("delete", ValueError("key not in whitelist: ..."))`.
4. WHEN Settings_API nhận PUT/POST/DELETE với key vi phạm Whitelist, THE Settings_API SHALL trả HTTP 422 với body `{"detail": "key not in whitelist: <key>"}` và không gọi repository.
5. THE whitelist enforcement SHALL áp dụng đồng nhất cho `set`, `bulk_set`, và `delete`; đối với `get` / `list` / `bulk_get`, key ngoài Whitelist được xử lý như "không tồn tại" (return None / không xuất hiện trong output) — không raise.

### Requirement 5: HTTP API endpoints

**User Story:** Là frontend developer, tôi muốn một bộ HTTP endpoint REST nhất quán để đọc/ghi settings từ UI.

#### Acceptance Criteria

1. THE Settings_API SHALL expose `GET /api/settings` trả về JSON `{"settings": {<key>: <decoded_value>, ...}}` với toàn bộ key đang lưu (decoded). Hỗ trợ query param `prefix=<str>` để lọc theo prefix dot-namespace (R2.4).
2. THE Settings_API SHALL expose `GET /api/settings/{key}` trả về JSON `{"key": "<key>", "value": <decoded_value>}` khi key tồn tại; HTTP 404 với body `{"detail": "key not found"}` khi không.
3. THE Settings_API SHALL expose `PUT /api/settings/{key}` với body `{"value": <any-json>}`. Khi thành công trả HTTP 200 `{"key": "<key>", "value": <decoded_value>}`.
4. THE Settings_API SHALL expose `DELETE /api/settings/{key}` trả HTTP 200 `{"deleted": true}` khi xoá thành công, HTTP 404 khi key không tồn tại.
5. THE Settings_API SHALL expose `POST /api/settings/bulk` với body `{"items": {<key>: <value>, ...}}` ghi toàn bộ trong một Atomic_Transaction và trả `{"updated": <int>}`.
6. THE Settings_API SHALL expose `POST /api/settings/import-from-localstorage` (chi tiết R7).
7. WHEN bất kỳ endpoint Settings_API nhận key vi phạm Whitelist hoặc value không validate được theo R3/R4, THE Settings_API SHALL trả HTTP 422 và body `{"detail": "<message>"}` mà không thực hiện ghi.
8. THE Settings_API SHALL được gate bởi token middleware hiện hữu (`require_token`) — yêu cầu header `X-API-Token` hoặc query param `token`; thiếu/sai token trả HTTP 401.

### Requirement 6: Write-through từ existing config endpoint

**User Story:** Là maintainer, tôi muốn các endpoint config hiện có tiếp tục hoạt động và đồng thời tự ghi giá trị mới sang `Settings_Store` để không phải sửa frontend ngay.

#### Acceptance Criteria

1. WHEN client gọi `POST /api/config` với field `mode|headless|debug|job_timeout|default_password|post_reg_get_session|post_reg_get_link|post_reg_link_region|auto_retry|auto_retry_max|auto_retry_delay|max_concurrent`, THE FastAPI server SHALL ghi giá trị vào key tương ứng trong namespace `reg.*` (ví dụ `reg.headless`, `reg.job_timeout`) qua `Settings_Repository.bulk_set` trong cùng request.
2. WHEN client gọi `POST /api/config` với field `proxy`, THE FastAPI server SHALL ghi value vào key `proxy.url` (string không rỗng) hoặc xoá key `proxy.url` (khi value rỗng "") qua repository.
3. WHEN client gọi `PUT /api/icloud/run/config`, THE FastAPI server SHALL ghi tương ứng các key `hme.runner.action`, `hme.runner.count_per_cycle`, `hme.runner.retry_interval`, `hme.runner.label`, `hme.runner.note` vào `Settings_Store` qua repository.
4. WHEN AutoRegRunner endpoint nhận config thay đổi (concurrency, poll_interval, password, logs_url, api_key), THE FastAPI server SHALL ghi các key `autoreg.concurrency`, `autoreg.poll_interval`, `autoreg.password`, `autoreg.logs_url`, `autoreg.api_key`.
5. WHEN HotmailJobManager nhận config thay đổi (target_count, concurrency, max_attempts, domain, delay_between, headless, captcha_methods, captcha_key), THE FastAPI server SHALL ghi các key `hotmail.target_count`, `hotmail.concurrency`, `hotmail.max_attempts`, `hotmail.domain`, `hotmail.delay_between`, `hotmail.headless`, `hotmail.captcha_methods`, `hotmail.captcha_key`.
6. IF write-through tới `Settings_Store` raise `RepositoryError`, THEN THE FastAPI server SHALL log error level cảnh báo nhưng vẫn áp dụng cấu hình in-memory cho manager và trả HTTP 200 (manager memory là source of truth runtime; persistence là eventual). Endpoint SHALL ghi field `settings_persist_error: <message>` vào response body để client biết.
7. WHEN write-through ghi cùng lúc nhiều key của một endpoint (ví dụ `POST /api/config` với 5 field), THE FastAPI server SHALL gom thành một lần `bulk_set` để đảm bảo atomic và giảm round-trip SQLite.

### Requirement 7: One-shot migration từ localStorage

**User Story:** Là user nâng cấp từ phiên bản cũ, tôi muốn settings localStorage hiện có (mode, headless, debug, proxy, post-reg toggle, worker config, mail mode, autoreg, HME, Hotmail) được tự động chuyển sang DB và không trùng lặp trên các lần load sau.

#### Acceptance Criteria

1. THE Settings_Migration_Endpoint SHALL ghi nhận các key client gửi tương ứng (sau ánh xạ): `gpt_reg.settings → reg.*`, `gpt_reg.proxy_url → proxy.url`, `gpt_reg.proxy_visible → ui.proxy_visible`, `gpt_reg.mail_mode → mail_mode.current`, `gpt_reg.worker_config → mail_mode.worker_config`, `gpt_reg.active_tab → ui.active_tab`, `autoreg.config.v1 → autoreg.*`, `hme.privacy.mask.v1 → hme.privacy_mask`, `gpt_reg.link.mode → ui.link_mode` (không bao gồm `gpt_reg.input.*` và `gpt_reg.auth_token`).
2. THE Settings_Migration_Endpoint SHALL chấp nhận body schema `{"localstorage": {<original_key>: <raw_value_string>, ...}}` trong đó mỗi value là string thô client lấy bằng `localStorage.getItem`.
3. WHEN Settings_Migration_Endpoint chạy, THE FastAPI server SHALL đọc thêm `Legacy_Runner_Config_File` từ `<runtime_dir>/icloud/runner_config.json` (nếu tồn tại) và ánh xạ sang `hme.runner.action`, `hme.runner.count_per_cycle`, `hme.runner.retry_interval`, `hme.runner.label`, `hme.runner.note`.
4. WHEN ghi vào `Settings_Store`, THE Settings_Migration_Endpoint SHALL chỉ ghi key chưa tồn tại trong DB; với key đã tồn tại endpoint SHALL bỏ qua (preserve giá trị hiện hành) và đưa key đó vào `skipped` trong response.
5. WHEN import thành công, THE Settings_Migration_Endpoint SHALL trả body `{"imported": [<key>, ...], "skipped": [<key>, ...], "client_keys_to_remove": [<original_localstorage_key>, ...], "renamed_runner_config_to": "<absolute_path_or_null>"}`.
6. WHEN `Legacy_Runner_Config_File` tồn tại và import thành công, THE Settings_Migration_Endpoint SHALL rename file thành `<runtime_dir>/icloud/runner_config.json.bak` (atomic via `os.replace`) sau khi commit transaction để tránh re-import lần sau.
7. IF `Legacy_Runner_Config_File` không parse được (corrupt JSON / schema sai theo `RunnerConfig.from_dict`), THEN THE Settings_Migration_Endpoint SHALL bỏ qua file (không rename), thêm field `runner_config_error: "<message>"` vào response và vẫn import thành công các key client.
8. THE Settings_Migration_Endpoint SHALL hoàn thành toàn bộ ghi DB trong một Atomic_Transaction (rollback khi bất kỳ ghi nào fail).
9. WHEN client nhận response 2xx từ Settings_Migration_Endpoint, THE frontend SHALL xoá những key trong `client_keys_to_remove` khỏi `localStorage` và set `Migrated_Flag` = `"1"` để skip migration ở lần load tiếp theo (idempotent client-side).
10. THE idempotency property SHALL hold: gọi Settings_Migration_Endpoint hai lần liên tiếp với cùng input (và DB ban đầu trống) thì lần 2 trả `imported = []`, không thay đổi giá trị DB từ lần 1, và không raise error.
11. THE Settings_Migration_Endpoint SHALL được gate bởi token middleware giống các endpoint khác (R5.8).

### Requirement 8: Whitelist namespaces

**User Story:** Là reviewer, tôi muốn whitelist được khai báo tập trung và rõ ràng để biết key nào được phép tồn tại.

#### Acceptance Criteria

1. THE Settings_Repository SHALL chấp nhận key thuộc một trong các namespace/khoá sau và reject mọi key khác:
   - `reg.mode`, `reg.headless`, `reg.debug`, `reg.default_password`, `reg.job_timeout`, `reg.post_reg_get_session`, `reg.post_reg_get_link`, `reg.post_reg_link_region`, `reg.auto_retry`, `reg.auto_retry_max`, `reg.auto_retry_delay`, `reg.max_concurrent`.
   - `proxy.url`, `proxy.visible`.
   - `mail_mode.current`, `mail_mode.worker_config` (giá trị JSON object `{logs_url, api_key}`).
   - `hme.runner.action`, `hme.runner.count_per_cycle`, `hme.runner.retry_interval`, `hme.runner.label`, `hme.runner.note`, `hme.privacy_mask`.
   - `autoreg.concurrency`, `autoreg.poll_interval`, `autoreg.password`, `autoreg.logs_url`, `autoreg.api_key`.
   - `hotmail.target_count`, `hotmail.concurrency`, `hotmail.max_attempts`, `hotmail.domain`, `hotmail.delay_between`, `hotmail.headless`, `hotmail.captcha_methods`, `hotmail.captcha_key`.
   - `ui.active_tab`, `ui.proxy_visible`, `ui.link_mode`.
2. THE whitelist SHALL được khai báo dưới dạng tập constants (ví dụ `_EXACT_KEYS: frozenset[str]` + `_PREFIXES: tuple[str, ...]`) trong cùng module với Settings_Repository để test có thể import trực tiếp.
3. WHERE caller cần thêm key mới, THE maintainer SHALL bổ sung key vào constants whitelist trước khi merge — repository không cho phép register key tại runtime.

### Requirement 9: Settings_Manager hydration tại boot

**User Story:** Là backend engineer, tôi muốn các manager singleton lấy state từ DB ngay khi boot thay vì chờ frontend gửi config lần đầu.

#### Acceptance Criteria

1. WHEN FastAPI startup hook chạy (`@app.on_event("startup")`), THE FastAPI server SHALL gọi `Settings_Repository.list()` đúng một lần và truyền dict kết quả vào factory của `JobManager`/`SessionManager`/`LinkManager`/`HmeRunner`/`AutoRegRunner`/`HotmailJobManager`.
2. WHEN một manager nhận hydration dict ở constructor và key tồn tại, THE manager SHALL khởi tạo field tương ứng từ DB (ví dụ `JobManager.headless = settings["reg.headless"]`).
3. WHEN một manager nhận hydration dict và key không tồn tại, THE manager SHALL dùng default hard-coded hiện hành (giữ behavior cũ — không đổi default).
4. WHEN Settings_Repository.list raise `RepositoryError` ở startup, THE FastAPI server SHALL log warning, dùng default cho mọi field, và tiếp tục startup (server không fail-fast vì lỗi cấu hình).
5. THE hydration SHALL chạy trước khi `manager.recover_jobs()` được gọi để job recovery dùng đúng job_timeout đã persist.

### Requirement 10: Audit log cho thao tác ghi

**User Story:** Là reviewer bảo mật, tôi muốn mọi thay đổi settings được ghi log để truy vết.

#### Acceptance Criteria

1. WHEN `Settings_Repository.set(key, value)` ghi thành công, THE Settings_Repository SHALL insert vào Audit_Log một row có `event_type = "settings.set"`, `apple_id = NULL`, `payload_json = '{"key":"<key>","old_present":<bool>,"new_value":<json>}'`, trong cùng Atomic_Transaction.
2. WHEN `Settings_Repository.delete(key)` xoá thành công, THE Settings_Repository SHALL insert vào Audit_Log một row có `event_type = "settings.delete"`, `payload_json = '{"key":"<key>"}'`.
3. WHEN `Settings_Repository.bulk_set(items)` chạy, THE Settings_Repository SHALL insert vào Audit_Log một row có `event_type = "settings.bulk_set"`, `payload_json` chứa danh sách key được ghi (giá trị mới — không bao gồm value cũ).
4. WHEN `Settings_Migration_Endpoint` chạy thành công, THE Settings_Migration_Endpoint SHALL insert vào Audit_Log một row `event_type = "settings.import"` với `payload_json` chứa danh sách `imported` và `skipped`.
5. WHERE key thuộc namespace nhạy cảm (`proxy.url`, `autoreg.api_key`, `hotmail.captcha_key`, `mail_mode.worker_config`), THE Settings_Repository SHALL ghi `payload_json` với value redact thành chuỗi `"***"` (không log plaintext credentials vào Audit_Log) trong khi cột `value` của bảng `settings` vẫn lưu plaintext (theo R3 clarify #3).

### Requirement 11: Atomic transaction guarantees

**User Story:** Là maintainer, tôi muốn các thao tác đa-row không để DB ở trạng thái nửa vời khi server crash giữa chừng.

#### Acceptance Criteria

1. WHEN `Settings_Repository.bulk_set(items)` chạy với N item, THE Settings_Repository SHALL ghi tất cả N row + N audit entry trong duy nhất 1 transaction; nếu bất kỳ ghi nào fail thì toàn bộ rollback.
2. WHEN `Settings_Migration_Endpoint` ghi nhiều key + audit entry, THE FastAPI server SHALL bao tất cả ghi DB trong duy nhất 1 transaction; nếu bất kỳ ghi nào fail thì toàn bộ rollback và file `runner_config.json` không bị rename.
3. IF SQLite raise `OperationalError` (database locked), THEN THE Settings_Repository SHALL retry tối đa 3 lần với backoff `[50ms, 150ms, 400ms]` trước khi propagate `RepositoryError`.

### Requirement 12: Frontend integration

**User Story:** Là frontend developer, tôi muốn một module client gọn gàng để load settings từ DB và đồng bộ với UI.

#### Acceptance Criteria

1. WHEN trang web load và `Migrated_Flag` chưa set trong `localStorage`, THE frontend SHALL gọi `POST /api/settings/import-from-localstorage` với snapshot 9 key localStorage liệt kê ở R7.1, sau đó áp dụng R7.9.
2. WHEN trang web load (sau migration hoặc đã migrated), THE frontend SHALL gọi `GET /api/settings` đúng một lần và dùng kết quả để hydrate state UI (mode/headless/debug/proxy input/active_tab/post-reg toggles/HME runner form/Hotmail form/autoreg form/HME privacy mask) thay cho `loadSettings()` cũ.
3. WHEN UI thay đổi giá trị (ví dụ user toggle headless), THE frontend SHALL tiếp tục gọi endpoint hiện hữu (`POST /api/config`, `PUT /api/icloud/run/config`, ...) — KHÔNG gọi trực tiếp `PUT /api/settings/{key}` để write-through (R6) vẫn là source duy nhất cho hot-path.
4. WHERE settings không có endpoint chuyên dụng (ví dụ `ui.active_tab`, `hme.privacy_mask`, `ui.proxy_visible`, `ui.link_mode`), THE frontend SHALL gọi `PUT /api/settings/{key}` trực tiếp để persist.
5. WHEN `Migrated_Flag` đã set, THE frontend SHALL không gọi Settings_Migration_Endpoint nữa (idempotent client-side).
