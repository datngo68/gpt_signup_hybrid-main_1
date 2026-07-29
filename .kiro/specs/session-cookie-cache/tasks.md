# Implementation Plan

## Overview

Triển khai cache session/cookie file-based, cô lập theo instance, và `Session_Provider` để 4 luồng (reg/get-session/get-link/upi) tái dùng session, tránh login lại. Thứ tự: config keys → store → provider → capture cookie → khởi tạo singleton → wiring 4 luồng → verify.

## Tasks

- [ ] 1. Nền tảng: config keys + gom DRY
- [ ] 1.1 Thêm config keys vào Settings Store
  - Thêm `session.reuse_enabled`, `session.revalidate_http`, `session.cookie_max_age_hours` vào `_EXACT_KEYS` (db/repositories.py)
  - Thêm ràng buộc type vào `_validate_type_constraint()` (bool/bool/int≥1)
  - _Requirements: 7.1, 7.2, 7.3_
- [ ] 1.2 Gom DRY `NON_RETRYABLE_PATTERNS` + `is_fatal_login_error()` về `session_phase.py` (cạnh `SessionError`)
  - Thay 2 bản trùng ở `web/manager.py:2874` (SessionJobManager) và `upi_runner.py:1737` bằng import dùng chung
  - _Requirements: 8.5, 13.4_

- [ ] 2. Tạo `session_store.py` (IO + isolation + lock) — LEAF module
- [ ] 2.1 `SessionStore.__init__(instance_id, runtime_dir)` (DI, không global); default factory resolve `instance_id` từ engine.db_path.stem (server inject) / `GSH_DB_PATH` stem; `_cache_dir` per-instance
  - Chuyển `_safe_email_slug` từ `web/manager.py` vào đây + định nghĩa `SESSION_TOKEN_COOKIE_NAMES` + `has_session_token()` (gom DRY); KHÔNG import web/*, db/*, session_phase (tránh cycle)
  - _Requirements: 1.1, 1.2, 1.4, 1.5_
- [ ] 2.2 `SessionStore.save/load/delete` atomic write + chmod 0600 + fail-soft parse
  - Tên file `<safe_slug>-<sha256(email)[:12]>.json` (collision-safe — 1355/1370 email chứa `+`); `load` guard `record["email"]==email` else miss (R12)
  - _Requirements: 2.2, 2.3, 2.4, 2.5, 8.1, 8.3, 8.4, 12.1, 12.2, 12.3_
- [ ] 2.3 `lock_for(email)` trả `asyncio.Lock` per-(instance,email)
  - _Requirements: 5.1, 5.3, 5.4_
- [ ] 2.4 Viết `test/check_session_store_isolation.py` + `test/check_session_store_atomic.py`
  - Thêm case collision: 2 email khác nhau cùng slug (vd `a+b@x` vs `a_b@x`) → 2 file khác nhau; `load` guard email-mismatch trả None
  - _Requirements: 1.3, 2.2, 12.1, 12.2, 12.3_

- [ ] 3. Tạo `session_provider.py`
- [ ] 3.1 `SessionRecord` dataclass (`from_session_json`, `from_signup_result`, `to_dict`) + `SessionProvider.acquire(email, proxy, login_fn, log, force_fresh=False, fatal_classifier=is_fatal_login_error)` — lock → reuse (nếu không force_fresh) → login → save → **trả FULL session (còn `__cookies`)**
  - `acquire` chỉ xử lý session-JSON; reg dùng `from_signup_result`+`store.save` (không qua acquire) → 1 shape, SRP/OCP sạch
  - `_read_cfg()` đọc 3 key từ settings_repo (default an toàn khi store thiếu)
  - `_strip_cookies()` là helper cho FLOW gọi ở biên persist/broadcast — KHÔNG gọi trong `acquire`
  - _Requirements: 2.1, 2.6, 3.1, 3.4, 3.8, 4.5, 4.6, 5.2, 7.4_
- [ ] 3.2 `_try_reuse()` — HTTP_Revalidate qua `fetch_session_via_http`, ưu tiên proxy record + fallback proxy runtime, scrub JWT khi log
  - Nhánh `revalidate_http=false`: dùng TTL `cookie_max_age_hours`
  - _Requirements: 3.2, 3.3, 3.5, 3.6, 3.7, 7.2, 7.3, 8.2_
- [ ] 3.3 `fatal_classifier` mặc định (match `NON_RETRYABLE_PATTERNS`) → `store.delete(email)` chỉ khi fatal, không khi transient
  - _Requirements: 8.5_
- [ ] 3.4 Viết `test/check_session_provider_reuse.py` + `test/check_session_provider_cfg.py` (mock fetch_session_via_http); test force_fresh bỏ reuse; test fatal vs transient delete
  - _Requirements: 3.3, 3.4, 8.5, 9.3_

- [ ] 4. Capture cookie từ luồng Get Session browser
- [ ] 4.1 Sửa `session_phase._get_session_browser` để gắn `session_data["__cookies"]` (đồng nhất với pure_request) từ `ctx.cookies()` đã lấy ở vòng readiness
  - Strip `__cookies` trước khi broadcast SSE / persist DB jobs để tránh leak
  - _Requirements: 4.1, 4.3, 4.4_
- [ ] 4.2 (đã có sẵn) Xác nhận `get_session_pure_request` export `__cookies` + UPI trả `session_cookies` + reg có `SignupResult.cookies` — chỉ viết adapter `_to_record` đọc đúng từng nguồn
  - _Requirements: 4.1, 4.2, 4.4_

- [ ] 5. Khởi tạo `SessionProvider` singleton + seed cache ở `web/server.py` startup
- [ ] 5.1 Tạo `SessionStore` chung + `SessionProvider` singleton; inject `settings_repo`
  - _Requirements: 6.1, 6.2, 6.3, 6.4_
- [ ] 5.2 `seed_from_session_results(store, session_repo)` — import row có cookie session-token, idempotent (không clobber record mới hơn), proxy=null, best-effort try/except
  - Viết `test/check_session_seed.py` — DB tạm có 1 row cookie hợp lệ + 1 row cookie `[]` → seed đúng 1, chạy lại không nhân đôi
  - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7_

- [ ] 6. Wiring 4 luồng dùng Session_Provider
- [ ] 6.1 SessionJobManager: bọc `_login_with_retry` trong `provider.acquire(login_fn=...)`
  - Giữ persist DB jobs như cũ; strip `__cookies` trước khi persist `jobs.session_data`/broadcast (GAP 7)
  - _Requirements: 6.1, 4.5_
- [ ] 6.2 LinkJobManager (combo): thay `get_session_pure_request` trực tiếp bằng `provider.acquire`
  - _Requirements: 6.2_
- [ ] 6.3 UpiJobManager + `run_upi_qr_probe`: thêm `login_fn` param vào runner (Step 1 gọi `login_fn` nếu có); manager truyền `login_fn=provider.acquire(...)`
  - Sau login_fn: fill `auth_sink` (`access_token`, `session_cookies` từ `session["__cookies"]`, `active_proxy`) như hiện tại — reuse-hit vẫn có cookie vì provider không strip
  - `_run_upi_cycles`: cycle re-login (`relogin_block_streak>0`) truyền `force_fresh=True` (GAP 2)
  - `check_plan`: khi `job._session_cookies is None` → `store.load(email)` lấy cookies trước `fetch_session_via_http` (GAP 6)
  - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_
- [ ] 6.4 JobManager (reg, save-only): sau `session_repo.create` thành công → `store.save(email, record)` từ SignupResult.cookies; KHÔNG reuse
  - Giữ nguyên ghi `session_results` DB
  - _Requirements: 6.4, 6.6, 3.8, 4.2_
- [ ] 6.5 Bỏ `_UPI_TOKEN_DIR`/`_export_upi_token` global; cập nhật 3 probe script (`probe_hosted_upi.py`, `probe_account_entitlement.py`, `probe_upi_next_action.py`) trỏ `session_cache/<Instance_Id>/`, đọc cookie chấp nhận `cookies`|`session_cookies`
  - _Requirements: 6.5, 10.1, 10.2, 10.3_

- [ ] 7. Verify tổng thể
  - `test/syntax_check.py` AST toàn bộ file đã sửa
  - `test/smoke_session_cache.py` — end-to-end: save → load → reuse-hit (mock revalidate) cho 1 email, 2 instance không đè nhau
  - _Requirements: 1.3, 2.1, 3.3, 6.5_

## Task Dependency Graph

```json
{
  "waves": [
    {"wave": 1, "tasks": ["1.1", "1.2", "2.1", "2.2", "2.3", "4.1", "4.2"], "rationale": "Config keys, gom DRY login patterns, store IO/lock/helpers, capture cookie — độc lập, chạy song song."},
    {"wave": 2, "tasks": ["2.4", "3.1", "3.2", "3.3"], "rationale": "Provider cần config (1) + store (2); test store sau khi store xong."},
    {"wave": 3, "tasks": ["3.4", "5.1", "5.2"], "rationale": "Test provider + khởi tạo singleton + seed cache cần provider/store (2,3)."},
    {"wave": 4, "tasks": ["6.1", "6.2", "6.3", "6.4", "6.5"], "rationale": "Wiring 4 luồng + probe scripts cần singleton (5); 6.1 cần 4.1, 6.4 cần 4.2."},
    {"wave": 5, "tasks": ["7"], "rationale": "Verify tổng thể sau khi mọi wiring xong."}
  ]
}
```

```
1 (config keys) ─┐
                 ├─> 3 (provider) ─> 5 (singleton) ─> 6.1/6.2/6.3/6.4 (wiring) ─> 7 (verify)
2 (store) ───────┘                        ▲
4 (capture cookie) ───────────────────────┘  (6.1 cần 4.1; 6.4 cần 4.2)
```

- Task 1 và 2 độc lập, chạy trước.
- Task 3 cần 1 (đọc cfg) + 2 (store).
- Task 4 độc lập với 1/2/3 nhưng là tiền đề cho wiring 6.1 (get-session) và 6.4 (reg).
- Task 5 cần 3.
- Task 6.x cần 5 + (4 cho 6.1/6.4).
- Task 7 cuối cùng.

## Notes

- Bám precedent `_export_upi_token` (atomic, 0600, `_safe_email_slug`) khi viết `session_store`.
- Test theo project-rules: file thật trong `test/`, in `[PASS]/[FAIL]` realtime `flush=True`, không inline `python -c`.
- `session_results` DB giữ nguyên — spec này chỉ thêm lớp file cache, không đụng schema DB.
- Mọi key config mới phải vào `_EXACT_KEYS` + `_validate_type_constraint()` trước khi sử dụng (task 1 chặn các task sau).
