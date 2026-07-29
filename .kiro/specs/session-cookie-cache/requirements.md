# Requirements Document

## Introduction

`session-cookie-cache` lưu lại **toàn bộ session + cookie** của mọi tài khoản ChatGPT sau khi login/đăng ký thành công, rồi **tái dùng** ở các lần chạy sau để tránh login lại. Áp dụng cho cả 4 luồng dùng session: đăng ký (`JobManager`), Get Session (`SessionJobManager`), Get Link (`LinkJobManager`), và UPI (`UpiJobManager`).

Nguyên tắc cốt lõi (theo lifetime thực tế đã xác minh trong code):
- **Cookie** (`__Secure-next-auth.session-token`) bền (ngày/tuần) → là nguồn dữ liệu chính để cache.
- **`access_token`** (JWT) ngắn hạn (vài giờ) → không tái dùng lâu; mint lại tươi từ cookie qua HTTP khi cần.
- Khi cần dùng session: thử **mint token tươi từ cookie qua HTTP** (`fetch_session_via_http`, ~1s, không cần browser); chỉ **full login** khi cookie đã chết.

Lưu trữ:
- Dữ liệu session/cookie lưu **file trong `runtime/`** (không lưu DB), atomic write, quyền `0600`.
- Cache phải **cô lập theo instance** dựa trên DB stem (`GSH_DB_PATH`) để nhiều web chạy song song (`--port/--db` khác nhau) không đè dữ liệu của nhau.

Phạm vi:
- Module mới `session_store.py` (IO file + lock per-email per-instance).
- Lớp `Session_Provider` điều phối: load → revalidate → reuse, hoặc full login → save.
- Wiring 4 luồng để dùng `Session_Provider`.
- Hợp nhất `runtime/upi_tokens` (đang hardcode global, collision giữa instance) vào store mới.
- Settings keys mới cho cấu hình bật/tắt + revalidate (đi qua Settings Store theo project-rules).

Ngoài phạm vi:
- Bảng DB `session_results` giữ **nguyên** làm lịch sử + nguồn UI list; không xoá, không đổi schema.
- Mã hoá-at-rest cho file cache: chấp nhận plaintext + quyền `0600` (giống precedent `_export_upi_token`).
- Đồng bộ cookie giữa các instance khác nhau (mỗi instance độc lập theo thiết kế).

## Glossary

- **Session_Cache_Record**: Bản ghi JSON cho 1 email gồm `email`, `cookies` (list dict), `access_token`, `session_token`, `two_factor` (secret/factor để full re-login), `proxy` (proxy đã mint token), `created_at`, `last_validated_at`.
- **Session_Store**: Module IO (`session_store.py`) đọc/ghi `Session_Cache_Record` ra file, atomic, `0600`, có lock per-email.
- **Instance_Id**: `Path(GSH_DB_PATH).stem` (vd `db4444`, `db5555`, `data`) — định danh instance để cô lập cache.
- **Cache_Dir**: `<runtime_dir>/session_cache/<Instance_Id>/` — thư mục chứa file cache của 1 instance.
- **Email_Slug**: Tên file an toàn từ email (giữ `[A-Za-z0-9._-@]`, ký tự khác → `_`), khớp logic `_safe_email_slug` hiện có.
- **Session_Provider**: Lớp điều phối tái dùng/lưu session, dùng chung bởi 4 luồng job.
- **HTTP_Revalidate**: Gọi `session_phase.fetch_session_via_http(cookies, proxy)` để mint `access_token` tươi từ cookie mà không mở browser.
- **Full_Login**: Login đầy đủ qua luồng hiện có (`get_session_pure_request` / `get_session` browser / signup).
- **Reuse_Hit**: Trường hợp HTTP_Revalidate trả 200 + có `accessToken` → dùng lại session, bỏ qua Full_Login.
- **Atomic_Write**: Ghi file qua `<file>.tmp` rồi `os.replace` (reader không bao giờ thấy file dở), chmod `0600` trước replace.

## Requirements

### Requirement 1: Cô lập cache theo instance

**User Story:** Là người chạy nhiều web song song (`--port 4444 --db=db4444`, `--port 5555 --db=db5555`), tôi muốn cache session của mỗi instance tách biệt để chúng không đè dữ liệu của nhau.

#### Acceptance Criteria

1. THE Session_Store SHALL resolve Instance_Id bằng `Path(os.environ["GSH_DB_PATH"]).stem`, fallback `"data"` khi `GSH_DB_PATH` không set; nếu engine được khởi tạo bằng `db_path` tường minh khác env, Instance_Id SHALL ưu tiên derive từ db path thật của engine để khớp DB đang dùng (GAP 8).
2. THE Session_Store SHALL đặt mọi file cache dưới Cache_Dir `<runtime_dir>/session_cache/<Instance_Id>/`.
3. WHEN hai instance có Instance_Id khác nhau cùng ghi cache cho cùng một email, THE Session_Store SHALL ghi vào hai file ở hai Cache_Dir khác nhau, không đè nhau.
4. WHERE `RUNTIME_DIR` được dùng chung giữa các instance, THE Session_Store SHALL vẫn cô lập bằng tầng `<Instance_Id>` trong đường dẫn.
5. THE Session_Store SHALL tạo Cache_Dir (mkdir parents, exist_ok) trước lần ghi đầu tiên.

### Requirement 2: Lưu toàn bộ session + cookie

**User Story:** Là người dùng, tôi muốn sau mỗi lần login/đăng ký thành công, toàn bộ session và cookie được lưu lại để lần sau khỏi login.

#### Acceptance Criteria

1. WHEN một luồng login/đăng ký thành công, THE Session_Provider SHALL lưu Session_Cache_Record gồm đầy đủ `cookies`, `access_token`, `session_token`, `two_factor` (nếu có), `proxy`, `created_at`, `last_validated_at`.
2. THE Session_Store SHALL ghi file bằng Atomic_Write với quyền `0600`.
3. IF FS không hỗ trợ chmod (vd Windows), THEN THE Session_Store SHALL bỏ qua chmod mà không raise (best-effort, giống precedent).
4. WHEN lưu cho email đã có record, THE Session_Store SHALL ghi đè theo latest-wins (giữ 1 record mới nhất/email/instance).
5. THE Session_Cache_Record SHALL serialize bằng `json.dumps(..., ensure_ascii=False)`.
6. THE Session_Provider SHALL không bao giờ để `cookies` rỗng được lưu như một Reuse_Hit hợp lệ — record không có cookie chỉ dùng cho lịch sử token, không tính là tái dùng được.

### Requirement 3: Tái dùng session, tránh login lại

**User Story:** Là người dùng, tôi muốn mỗi lần chạy hệ thống kiểm tra cookie/session đã lưu còn dùng được không để dùng luôn, chỉ login lại khi thật sự cần.

#### Acceptance Criteria

1. WHEN một luồng cần session cho email, THE Session_Provider SHALL load Session_Cache_Record trước khi quyết định login.
2. IF record tồn tại và có `cookies`, THEN THE Session_Provider SHALL thực hiện HTTP_Revalidate với `cookies` + `proxy` đã lưu.
3. IF HTTP_Revalidate trả 200 và có `accessToken` không rỗng (Reuse_Hit), THEN THE Session_Provider SHALL trả session tái dùng, cập nhật `access_token` + `last_validated_at`, và KHÔNG chạy Full_Login.
4. IF record không tồn tại, không có cookie, hoặc HTTP_Revalidate thất bại, THEN THE Session_Provider SHALL chạy Full_Login rồi lưu record mới (R2).
5. WHEN HTTP_Revalidate thất bại, THE Session_Provider SHALL log lý do (status/exception) ở mức đủ để chẩn đoán mà KHÔNG ghi `access_token`/JWT vào log (scrub theo `_scrub_jwt`).
6. WHERE `proxy` được lưu trong record, THE Session_Provider SHALL dùng đúng proxy đó cho HTTP_Revalidate để tránh Cloudflare flag do đổi IP.
7. IF HTTP_Revalidate với proxy đã lưu thất bại do lỗi mạng/proxy (không phải 401/expired), THEN THE Session_Provider SHALL thử lại một lần với `proxy` runtime hiện tại trước khi kết luận miss (GAP 9 — proxy lưu có thể đã chết/xoay SID).
8. THE đăng ký flow (`JobManager`) SHALL chỉ **lưu** record sau signup, KHÔNG bao giờ chạy đường reuse — signup tạo account mới, không có khái niệm "tái dùng session để bỏ qua đăng ký" (GAP 11).

### Requirement 4: Capture cookie từ các luồng login

**User Story:** Là maintainer, tôi muốn mọi luồng login surface được cookie để cache, vì hiện luồng Get Session browser đang vứt cookie sau khi lấy session JSON.

#### Acceptance Criteria

1. THE Get Session flow SHALL trả về (hoặc cung cấp cho Session_Provider) `cookies` của context cùng với session JSON, thay vì chỉ session JSON.
2. THE đăng ký flow SHALL truyền `SignupResult.cookies` vào Session_Provider khi lưu record.
3. IF một luồng Full_Login không thể lấy được cookie, THEN THE Session_Provider SHALL vẫn lưu record (token + metadata) nhưng đánh dấu không-reuse-được (R2.6) và log cảnh báo.
4. THE thay đổi capture cookie SHALL không làm đổi public return type theo cách phá vỡ caller hiện có nếu tránh được; nếu buộc đổi, mọi call-site SHALL được cập nhật trong cùng spec.
5. THE key `__cookies` SHALL bị **strip ở MỌI điểm** session JSON được persist vào `jobs.session_data` hoặc broadcast qua SSE/`to_dict()` — không chỉ lúc capture mà cả đường reuse trả session JSON kèm `__cookies` (GAP 7).
6. THE `_to_record` SHALL trích `session_token` theo nguồn: từ `SignupResult.session_token` (reg); hoặc từ `__cookies` (tìm `__Secure-next-auth.session-token` / `.0`) cho session JSON — vì `/api/auth/session` JSON KHÔNG chứa `session_token` (GAP 4).

### Requirement 5: Đồng thời và khoá

**User Story:** Là người chạy nhiều job, tôi muốn nhiều job cùng email trong một instance không login song song gây lockout hoặc ghi đè cache lẫn nhau.

#### Acceptance Criteria

1. THE Session_Store SHALL cung cấp lock per-(Instance_Id, Email_Slug) trong tiến trình để tuần tự hoá read-modify-write trên cùng một record.
2. WHEN hai job trong cùng instance cùng yêu cầu session cho một email, THE Session_Provider SHALL để job thứ hai dùng kết quả/record do job thứ nhất vừa tạo nếu Reuse_Hit khả dụng, thay vì Full_Login lần nữa.
3. THE Atomic_Write SHALL đảm bảo reader đồng thời không bao giờ đọc file ghi dở.
4. WHERE hai instance khác Instance_Id, THE Session_Store SHALL không cần khoá liên tiến trình (đường dẫn đã tách).
5. THE lock per-email SHALL chỉ bao đoạn read-modify-write cache + quyết định reuse; nếu phải giữ qua Full_Login dài (browser), việc giữ lock là chấp nhận được (mục tiêu chống login-spam cùng email) nhưng SHALL được ghi chú rõ trong code (GAP 10).

### Requirement 6: Wiring 4 luồng + hợp nhất upi_tokens

**User Story:** Là maintainer, tôi muốn cả 4 luồng dùng chung Session_Provider và xoá điểm collision `runtime/upi_tokens` hardcode global.

#### Acceptance Criteria

1. THE SessionJobManager (Get Session) SHALL dùng Session_Provider cho đường lấy session (reuse trước, Full_Login sau).
2. THE LinkJobManager (Get Link, combo mode) SHALL dùng Session_Provider thay vì gọi thẳng `get_session_pure_request` mỗi lần.
3. THE UpiJobManager SHALL dùng Session_Provider cho việc lấy session/token và lưu qua Session_Store (thay `_export_upi_token` global).
4. THE JobManager (đăng ký) SHALL lưu session vào Session_Store sau khi signup thành công (ngoài việc giữ ghi `session_results` DB như cũ).
5. THE `_UPI_TOKEN_DIR` hardcode global SHALL bị thay bằng Cache_Dir per-instance; không còn đường ghi cache nào dùng đường dẫn global chia sẻ giữa instance.
6. THE bảng `session_results` (DB) SHALL giữ nguyên hành vi ghi/đọc hiện tại cho lịch sử + UI; spec này KHÔNG xoá/đổi schema bảng đó.

### Requirement 7: Cấu hình qua Settings Store

**User Story:** Là người dùng, tôi muốn bật/tắt việc tái dùng session và chỉnh cách revalidate mà không sửa code.

#### Acceptance Criteria

1. THE feature SHALL thêm key `session.reuse_enabled` (bool, default `true`) vào `_EXACT_KEYS` + `_validate_type_constraint()` trước khi dùng.
2. THE feature SHALL thêm key `session.revalidate_http` (bool, default `true`) — khi `true` thì revalidate bằng HTTP_Revalidate; khi `false` thì coi record còn hạn theo `session.cookie_max_age_hours` là Reuse_Hit (không gọi network).
3. THE feature SHALL thêm key `session.cookie_max_age_hours` (int ≥ 1, default `24`) làm ngưỡng tuổi tối đa của record trước khi buộc Full_Login (áp dụng cả khi `revalidate_http=false`).
4. WHEN `session.reuse_enabled=false`, THE Session_Provider SHALL luôn Full_Login và vẫn lưu record (tắt reuse, không tắt lưu).
5. THE config keys SHALL đọc per-instance từ Settings Store (đã per-instance vì Settings nằm trong DB của instance).

### Requirement 8: Bảo mật + dọn dẹp

**User Story:** Là maintainer, tôi muốn file cache an toàn và không phình vô hạn.

#### Acceptance Criteria

1. THE file cache chứa cookie/token/proxy creds SHALL có quyền `0600`.
2. THE access_token/JWT SHALL không bao giờ đi qua SSE, `to_dict()`, hay log (scrub `eyJ...`).
3. THE Cache_Dir SHALL nằm trong `runtime/` (đã `.gitignore`) — không commit.
4. THE Session_Store SHALL cung cấp `delete(email)` để xoá record (dùng khi account chết/login fail kiểu fatal).
5. WHEN Full_Login fail kiểu fatal (sai password/2FA/no-secret), THE Session_Provider SHALL xoá record cũ (nếu có) để tránh reuse cookie hỏng ở lần sau. Phân loại fatal SHALL dựa trên exception có type rõ (vd `FatalLoginError`) hoặc tập pattern non-retryable đã có (`NON_RETRYABLE_PATTERNS`) — KHÔNG xoá record khi lỗi chỉ là transient/mạng (GAP 5).

### Requirement 9: Tích hợp luồng UPI (login nằm trong runner)

**User Story:** Là người chạy UPI, tôi muốn UPI tái dùng session đã lưu để bỏ qua login (Step 1), nhưng vẫn giữ được cơ chế đổi IP khi bị block.

#### Acceptance Criteria

1. WHERE login của UPI nằm trong `run_upi_qr_probe` (Step 1/6) chứ không ở `UpiJobManager`, THE thiết kế SHALL chèn điểm reuse vào Step 1 bằng cách truyền `session_provider` (hoặc `login_fn`) vào `run_upi_qr_probe`, hoặc cho `UpiJobManager` pre-acquire session rồi truyền `access_token` + `proxy` + `cookies` vào runner để runner skip Step 1 (GAP 1).
2. THE các step Stripe sau (checkout/approve dùng `Bearer access_token`) SHALL nhận `access_token` từ Reuse_Hit y như từ Full_Login — không thay đổi logic Stripe.
3. WHEN một cycle của `_run_upi_cycles` bị break do block-streak và yêu cầu re-login để đổi IP (`relogin_block_streak > 0`), THE cycle re-login đó SHALL **bỏ qua cache (force Full_Login)** để lấy IP/proxy mới — không reuse cookie cũ (GAP 2).
4. THE cycle đầu tiên của một UPI job SHALL được phép Reuse_Hit bình thường.
5. WHEN `UpiJobManager.check_plan` chạy mà `job._session_cookies is None` (vd sau khi restart server), THE check_plan SHALL load cookie từ Session_Store theo email trước khi gọi `fetch_session_via_http`, để khả năng check plan không mất khi restart (GAP 6).

### Requirement 10: Tương thích ngược với probe scripts

**User Story:** Là maintainer, tôi muốn việc thay `_UPI_TOKEN_DIR` không làm hỏng các script đang đọc `runtime/upi_tokens`.

#### Acceptance Criteria

1. WHERE `test/probe_hosted_upi.py`, `test/probe_account_entitlement.py`, `test/probe_upi_next_action.py` hiện đọc `runtime/upi_tokens/<email>.json`, THE thay đổi store SHALL giữ payload tương thích (chứa `email`, `access_token`, `session_cookies`, `proxy`) và cập nhật 3 script trỏ sang Cache_Dir mới `<runtime_dir>/session_cache/<Instance_Id>/` (GAP 3).
2. THE Session_Cache_Record SHALL dùng key `session_cookies` (giữ tên cũ của `_export_upi_token`) HOẶC cung cấp lớp đọc chấp nhận cả `cookies` lẫn `session_cookies` để 3 probe script + `probe_account_entitlement --export-file` đọc được không cần sửa định dạng.
3. THE spec SHALL KHÔNG để lại đường ghi `runtime/upi_tokens` global song song sau khi chuyển (tránh tái lập collision đa-instance) — chỉ một nguồn ghi per-instance.

### Requirement 11: Import một lần từ session_results (seed cache)

**User Story:** Là người dùng đã có sẵn hàng trăm account với cookie trong `session_results`, tôi muốn hệ thống tái dùng được chúng ngay mà không phải login lại từ đầu.

#### Acceptance Criteria

1. WHEN server khởi động, THE init SHALL chạy import một lần: với mỗi email trong `session_results` của instance hiện tại, lấy row **mới nhất CÓ cookie chứa `__Secure-next-auth.session-token`** và ghi vào Session_Store.
2. THE import SHALL chỉ đọc `SessionResultRepository` của engine instance hiện tại và ghi vào Cache_Dir của instance đó (cô lập per-instance tự nhiên — data.db→cache instance "data", db4444.db→"db4444", ...).
3. THE import SHALL bỏ qua row có cookie rỗng `[]`/null hoặc không chứa `session-token` (không reuse được — 41% dữ liệu thực tế).
4. THE record import SHALL set `proxy=null`; HTTP_Revalidate sau đó dùng proxy runtime hoặc DIRECT (cookies-based auth không kén IP — đã xác minh ở `check_plan`).
5. THE import SHALL idempotent: KHÔNG ghi đè file cache đã tồn tại nếu file đó mới hơn (so `created_at`/`last_validated_at`) — tránh clobber record runtime tươi bằng row DB cũ; chạy lại nhiều lần an toàn.
6. THE import SHALL best-effort: lỗi đọc DB/ghi file KHÔNG làm fail startup (log warning).
7. THE record import SHALL lấy `access_token`/`session_token` từ cột tương ứng của `session_results`, `cookies` từ cột `cookies` (đã deserialize), `two_factor` nếu có.

### Requirement 12: Tên file cache không đụng nhau (collision-safe)

**User Story:** Là người dùng có hàng nghìn email iCloud HME dạng `base+tag@icloud.com`, tôi muốn mỗi account có file cache riêng, không bị account khác ghi đè nhầm.

#### Acceptance Criteria

1. WHERE `_safe_email_slug` map ký tự ngoài `[A-Za-z0-9._-@]` (gồm `+`) thành `_`, hai email khác nhau CÓ THỂ ra cùng slug (thực tế: 1355/1370 email chứa `+`, 471 chứa cả `+` lẫn `_`) — THE tên file cache SHALL **không** chỉ dựa trên slug.
2. THE Session_Store SHALL đặt tên file theo dạng `<safe_slug>-<hash>.json` với `<hash>` là tiền tố hash ổn định của **email đầy đủ** (vd `sha256(email)[:12]`) để đảm bảo hai email khác nhau luôn ra hai file khác nhau.
3. WHEN `load(email)` đọc một record, THE Session_Store SHALL kiểm tra `record["email"] == email`; nếu lệch (đề phòng va chạm/được sửa tay), THE Session_Store SHALL coi như miss (trả None) — không bao giờ trả session của account khác.
4. THE thay đổi scheme tên file SHALL không phá probe scripts vì chúng chọn file theo mtime + đọc `email` trong nội dung, KHÔNG parse email từ tên file (đã xác minh `_load_latest_token`).

### Requirement 13: Ranh giới module & DRY (SOLID, chống phân mảnh)

**User Story:** Là maintainer, tôi muốn code không phân mảnh, không trùng lặp, phụ thuộc một chiều rõ ràng.

#### Acceptance Criteria

1. THE `session_store.py` SHALL là leaf module: chỉ phụ thuộc stdlib + `config` (runtime_dir); KHÔNG import `web/*`, `db/*`, `session_phase` — để mọi caller (manager, server, upi_runner, probe scripts) import được mà không tạo circular import.
2. THE `SessionStore` SHALL nhận `instance_id` + `runtime_dir` qua constructor (dependency injection), KHÔNG resolve global ở import-time — để test inject giá trị tường minh; server truyền `engine.db_path.stem` + `settings.runtime_dir`.
3. THE `_safe_email_slug` SHALL tồn tại ở **một** nơi (`session_store`); `web/manager.py` import lại, KHÔNG giữ bản sao sau khi bỏ `_export_upi_token`.
4. THE `NON_RETRYABLE_PATTERNS` + phân loại fatal login SHALL tồn tại ở **một** nơi (`session_phase.py`); `web/manager.py`, `upi_runner.py`, và `SessionProvider` import dùng chung — xoá 2 bản trùng hiện có.
5. THE việc chuẩn hoá dữ liệu session → record SHALL tập trung trong `SessionRecord` (2 constructor: `from_session_json`, `from_signup_result`), KHÔNG rải logic trích cookie/token ở nhiều call-site.
