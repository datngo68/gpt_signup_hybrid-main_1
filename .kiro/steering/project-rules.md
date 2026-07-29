# Project Rules — gpt_signup_hybrid

## Verification & test execution

- Không bao giờ dùng inline `python3 -c "..."` / `node -e "..."` / `bash -c "..."` để verify hoặc debug.
- Mọi loại check (syntax, import, smoke...) đều phải nằm trong file `.py` thật ở thư mục `test/`.
- Đặt tên rõ ràng: `test/check_<scope>.py`, `test/smoke_<scope>.py`, `test/test_<scope>.py`.
- Chỉ chạy file vừa viết: `python3 test/<file>.py`. Không chạy script tạm.
- Nếu chỉ muốn parse syntax — vẫn viết script `test/syntax_check.py` rồi chạy file đó.

## File layout

- File test/debug → `test/`
- Tài liệu .md user yêu cầu → `docs/`
- Không tạo doc, test, file tạm khi user chưa yêu cầu.

## Code style

- Không hardcode default insecure (TLS verify off, CORS *, auth bypass) — phải opt-in qua flag/env.
- Fail-fast, không fallback che lỗi.
- Không để code chết sau `return`/`raise`.

## Settings Store (CRITICAL — không được bỏ qua)

Mọi runtime configuration PHẢI lưu vào SQLite Settings Store (`db/repositories.py` → `SettingsRepository`). Đây là nguồn duy nhất (single source of truth) cho cấu hình toàn hệ thống.

### Quy tắc bắt buộc

1. **Backend**: Mọi cấu hình runtime (timeout, concurrency, proxy, toggle, form values...) PHẢI đọc từ `SettingsRepository` khi startup (`apply_settings`) và ghi qua write-through khi user thay đổi.
2. **Frontend**: KHÔNG dùng `localStorage` cho runtime config. Chỉ dùng `Settings.get(key)` / `Settings.save(key, value, token)`. localStorage chỉ cho textarea drafts (`gpt_reg.input.*`) và auth token.
3. **Key mới**: Khi thêm feature cần cấu hình mới → thêm key vào `_EXACT_KEYS` whitelist + type constraint trong `_validate_type_constraint()` TRƯỚC khi dùng.
4. **Write-through**: Endpoint nào nhận config change từ user → PHẢI gọi `settings_repo.bulk_set(...)` cuối handler (wrap try/except, không break endpoint nếu DB fail).
5. **Hydration**: Manager/service nào dùng config → PHẢI có method `apply_settings(dict)` được gọi tại startup.
6. **Không file config**: KHÔNG tạo JSON/YAML config file riêng. Tất cả vào bảng `settings`.
7. **Namespace**: Key format `namespace.field` (dot-separated, lowercase). Ví dụ: `reg.headless`, `hotmail.concurrency`, `autoreg.password`.

### Flow chuẩn khi thêm setting mới

```
1. Thêm key vào _EXACT_KEYS (db/repositories.py)
2. Thêm type constraint vào _validate_type_constraint()
3. Backend: apply_settings() hydrate field từ DB key
4. Backend: write-through trong endpoint handler
5. Frontend: Settings.get('key') để đọc, Settings.save() hoặc endpoint write-through để ghi
```
