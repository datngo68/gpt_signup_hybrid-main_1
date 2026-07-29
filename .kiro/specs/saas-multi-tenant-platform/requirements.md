# Requirements Document

## Introduction

Chuyển `gpt_signup_hybrid` từ **local single-operator tool** thành **dịch vụ multi-tenant** cho tối đa ~100 user online đồng thời, chạy trên **một máy cá nhân**, expose ra ngoài qua tunnel.

Mỗi user trả phí (ngoài hệ thống) để được cấp tài khoản có **thời hạn sử dụng theo ngày**. Hết hạn thì bị khoá. User dùng tool ở chế độ **HTTP-only** (cấm browser automation), và **tự mang tài nguyên** (proxy, Outlook combo, worker mail key) vào tài khoản của mình. Dữ liệu, cấu hình, job giữa các user **độc lập và cách ly hoàn toàn**. Admin **quản lý và giám sát toàn bộ**.

### Ràng buộc chốt (scope cứng)

| Hạng mục | Quyết định |
|---|---|
| Quy mô | ~100 user online; số **item/job chạy đồng thời bị giới hạn** (cap toàn cục + per-user) |
| Hạ tầng | 1 máy cá nhân, expose qua tunnel (vd Cloudflare Tunnel) |
| Chế độ tool | **HTTP-only** — user không được trigger Camoufox/Playwright |
| Tool cấp cho user | **Reg (request mode), Get Session, UPI QR, Get Link** |
| Loại bỏ khỏi user | **iCloud HME, AutoReg** (phụ thuộc browser/HME), mọi browser-mode |
| Tài nguyên | User **tự mang**: proxy pool, Outlook combo, worker mail key (per-user) |
| License | **Hết hạn theo ngày**; hết hạn = khoá đăng nhập + chặn submit job |
| Thanh toán | **Ngoài hệ thống**; admin cấp/gia hạn/khoá thủ công qua admin panel |
| Cách ly | Mỗi user chỉ thấy job/setting/pool/kết quả của chính mình; admin xem tất cả |
| Toàn vẹn dữ liệu | Migrate dữ liệu cũ an toàn, không mất logic nghiệp vụ hiện có |

### Hiện trạng kỹ thuật cần khắc phục (đã khảo sát code)

- `web/auth.py`: chỉ có **1 token tĩnh toàn cục** (`web.auth_token`), không có user/role/password/expiry.
- `db/schema.py` (v11): **không bảng nào có `tenant_id`** (`jobs`, `session_results`, `chatgpt_accounts`, `outlook_combos`, `settings`...). `settings` là KV global, key `UNIQUE`.
- `web/manager.py`: 4 manager là **singleton module-level, state in-memory** (`JobManager`, `SessionJobManager`, `LinkJobManager`, `UpiJobManager`); `web/server.py` cảnh báo không chạy >1 worker.
- `web/server.py`: surface API rất lớn, **tất cả global, không phân chủ sở hữu**; `/api/gopay-check/*` bỏ qua auth; **không có rate limit**.
- Secret (password, access_token, session JSON, refresh_token) lưu **plaintext** trên SQLite + filesystem `runtime/` dùng chung.
- **Hai kênh giao hàng song song**: (a) Web tool Python FastAPI; (b) `rust_upi_bot` — Telegram bot viết bằng Rust, **tự re-implement toàn bộ flow UPI HTTP** (login OpenAI + Stripe + UPI QR), độc lập, không gọi `/api/*` của Python. Telegram notify trong Python (`notify_upi_qr`) hiện cấu hình **global**, chưa per-tenant. → Scope kênh cần chốt (xem câu hỏi mở #6).

---

## Glossary

- **Tenant / User**: một tài khoản người dùng cuối đã được admin cấp, có thời hạn.
- **Admin**: chủ hệ thống, có toàn quyền quản trị và giám sát.
- **License / Hạn dùng**: mốc thời gian `expires_at`; quá mốc → tài khoản `expired`.
- **Job / Item**: một đơn vị công việc HTTP-only (1 dòng combo) thuộc 1 trong 4 loại tool.
- **Per-user resource**: proxy/combo/mail-key do chính user nạp, chỉ dùng cho job của user đó.
- **Global concurrency cap**: trần tổng số job chạy đồng thời toàn hệ thống.
- **Per-user concurrency cap**: trần số job chạy đồng thời của một user.

---

## Requirements

### R1 — Quản lý danh tính & tài khoản người dùng (Identity)

**User Story:** Là admin, tôi muốn tạo/cấp tài khoản cho user với thông tin đăng nhập riêng, để mỗi người dùng có danh tính độc lập.

#### Acceptance Criteria

1. WHEN admin tạo một user mới với username/email và password THEN hệ thống SHALL lưu user với password được **hash** (argon2id hoặc bcrypt), KHÔNG lưu plaintext.
2. THE hệ thống SHALL gán mỗi user một `tenant_id` định danh duy nhất, bất biến (immutable) suốt vòng đời.
3. THE hệ thống SHALL gán mỗi user một `role` thuộc tập `{admin, user}`.
4. WHEN admin tạo user THEN hệ thống SHALL cho phép đặt `expires_at` (mốc hết hạn theo ngày) và `status` khởi tạo (`active`).
5. IF username/email đã tồn tại THEN hệ thống SHALL từ chối tạo và trả lỗi rõ ràng (fail-fast), KHÔNG ghi đè user cũ.
6. THE hệ thống SHALL hỗ trợ trạng thái user thuộc tập `{active, suspended, expired}`.
7. WHEN hệ thống khởi động lần đầu mà chưa có admin nào THEN hệ thống SHALL bảo đảm có cơ chế tạo admin gốc (bootstrap) an toàn, không để hệ thống mở không auth.

### R2 — Xác thực & phiên đăng nhập (Authentication)

**User Story:** Là user, tôi muốn đăng nhập bằng tài khoản riêng để truy cập an toàn không dùng chung token với người khác.

#### Acceptance Criteria

1. WHEN user gửi đúng credential hợp lệ AND tài khoản `active` AND chưa hết hạn THEN hệ thống SHALL cấp một **session/token gắn với `tenant_id` và `role`**, có thời hạn (expiry).
2. WHEN user gửi sai credential THEN hệ thống SHALL từ chối với phản hồi không tiết lộ user tồn tại hay không, và so sánh bằng constant-time.
3. WHILE một request gọi `/api/*` (trừ endpoint đăng nhập/health công khai) THE hệ thống SHALL yêu cầu token hợp lệ và resolve ra `tenant_id` + `role` của request đó.
4. IF token thiếu/sai/hết hạn THEN hệ thống SHALL trả 401.
5. WHEN token hết hạn THEN hệ thống SHALL có cơ chế refresh hoặc buộc đăng nhập lại, KHÔNG cho dùng token vô thời hạn.
6. THE hệ thống SHALL thay thế hoàn toàn cơ chế single static token (`web/auth.py` hiện tại) bằng auth đa người dùng; token tĩnh chỉ còn (nếu giữ) cho kênh nội bộ admin/loopback và phải tách biệt rõ.
7. THE token SHALL KHÔNG được phép truyền qua query string ở các endpoint ghi log của reverse proxy trừ SSE (và SSE phải có biện pháp giảm thiểu leak — xem R12).

### R3 — License & vòng đời hết hạn (Licensing)

**User Story:** Là admin, tôi muốn cấp thời hạn dùng cho từng user và hệ thống tự khoá khi hết hạn, để vận hành mô hình trả phí theo ngày.

#### Acceptance Criteria

1. THE mỗi user SHALL có trường `expires_at` (timestamp UTC).
2. WHEN thời điểm hiện tại vượt `expires_at` THEN hệ thống SHALL coi user là `expired` và TỪ CHỐI mọi thao tác đăng nhập mới và mọi submit job mới (trả lỗi rõ ràng nêu lý do hết hạn).
3. WHEN user `expired` còn job đang chạy THEN hệ thống SHALL cho phép job đang chạy hoàn tất HOẶC dừng theo policy cấu hình, nhưng KHÔNG cho enqueue job mới.
4. WHEN admin gia hạn (`extend`) một user THEN hệ thống SHALL cập nhật `expires_at` và khôi phục khả năng dùng nếu user về trạng thái `active`.
5. WHEN admin `suspend` một user THEN hệ thống SHALL chặn đăng nhập + submit ngay lập tức bất kể `expires_at`.
6. THE hệ thống SHALL hiển thị cho user thời hạn còn lại (số ngày/giờ tới `expires_at`).
7. THE việc đánh giá hết hạn SHALL dựa trên đồng hồ server (UTC), không phụ thuộc client.

### R4 — Cách ly dữ liệu đa người dùng (Tenant Isolation)

**User Story:** Là user, tôi muốn dữ liệu/job/cấu hình của mình hoàn toàn riêng tư, không ai khác đọc/sửa được.

#### Acceptance Criteria

1. THE mọi bảng chứa dữ liệu thuộc về người dùng (`jobs`, `job_logs`, `session_results`, `chatgpt_accounts`, `outlook_combos`, dữ liệu UPI/link, cấu hình per-user) SHALL có cột `tenant_id` NOT NULL và index theo `tenant_id`.
2. WHEN một user truy vấn/thao tác bất kỳ tài nguyên job/kết quả/cấu hình THEN hệ thống SHALL ràng buộc truy vấn theo `tenant_id` của người gọi, KHÔNG cho phép truy cập tài nguyên của tenant khác.
3. IF user A yêu cầu một `job_id`/resource thuộc tenant B THEN hệ thống SHALL trả 404 (không tiết lộ tồn tại), KHÔNG trả dữ liệu.
4. THE Settings Store SHALL trở thành **per-tenant**: khoá định danh là `(tenant_id, key)` thay vì `key` global; cấu hình toàn cục của hệ thống lưu dưới một tenant hệ thống riêng (vd `tenant_id = system`).
5. THE artifact trên filesystem (session JSON, QR PNG, file kết quả) SHALL được namespace theo tenant (vd `runtime/<tenant_id>/...`) và user KHÔNG truy cập được path của tenant khác.
6. WHEN admin truy cập THEN hệ thống SHALL cho phép admin xem dữ liệu mọi tenant một cách tường minh (có kiểm soát qua role).
7. THE việc enforce isolation SHALL ở **tầng server/repository** (không chỉ ở UI); mọi repository method nhận dữ liệu người dùng phải nhận `tenant_id` làm tham số bắt buộc.

### R5 — Tài nguyên do user tự mang (Per-User Resources)

**User Story:** Là user, tôi muốn nạp proxy/combo/mail-key của riêng tôi để chạy job bằng tài nguyên của mình.

#### Acceptance Criteria

1. THE mỗi user SHALL có **proxy pool riêng** (lưu per-tenant), tách biệt pool của user khác; rotation mode riêng.
2. THE mỗi user SHALL có **Outlook combo / mail credential riêng** (per-tenant), và job của user chỉ dùng combo của chính họ.
3. THE mỗi user SHALL có **worker mail key / mail-mode config riêng** (per-tenant).
4. WHEN user lưu proxy/combo/mail-key THEN hệ thống SHALL validate định dạng (fail-fast) và lưu qua Settings Store/repository **per-tenant**.
5. THE giá trị nhạy cảm per-tenant (proxy credential, refresh_token, worker key) SHALL được **redact trong log/audit** và không trả nguyên giá trị ra response không cần thiết.
6. IF user chưa cấu hình tài nguyên bắt buộc cho một loại job THEN hệ thống SHALL từ chối submit với thông báo rõ thiếu gì, KHÔNG fallback sang tài nguyên người khác hay tài nguyên global.
7. THE hệ thống SHALL KHÔNG chia sẻ proxy/combo/mail-key giữa các tenant trong bất kỳ trường hợp nào.

### R6 — Tool HTTP-only cho user (Capability Restriction)

**User Story:** Là admin, tôi muốn user chỉ chạy được chế độ HTTP-only để bảo vệ tài nguyên máy cá nhân.

#### Acceptance Criteria

1. THE user SHALL chỉ được dùng các tool: **Reg (request/pure-HTTP mode), Get Session (pure-request), UPI QR (HTTP), Get Link (HTTP)**.
2. WHEN một submit job từ user có cấu hình yêu cầu browser/Camoufox/Playwright THEN hệ thống SHALL từ chối (fail-fast), bất kể client gửi gì.
3. THE hệ thống SHALL ép `reg.mode` (và tham số tương đương) của user về chế độ HTTP-only ở tầng server, KHÔNG tin tham số client.
4. THE endpoint/tool thuộc iCloud HME và AutoReg SHALL KHÔNG khả dụng với role `user` (ẩn và chặn ở server).
5. IF user cố gọi endpoint browser-mode hoặc HME/AutoReg THEN hệ thống SHALL trả 403.
6. THE các tính năng browser-only (nếu cần cho admin) SHALL chỉ truy cập được bởi role `admin`.

### R7 — Hàng đợi job & giới hạn đồng thời (Concurrency & Quota)

**User Story:** Là admin, tôi muốn giới hạn tổng số job chạy cùng lúc và số job mỗi user, để máy cá nhân không quá tải.

#### Acceptance Criteria

1. THE hệ thống SHALL áp một **global concurrency cap** cho tổng số job đang chạy đồng thời toàn hệ thống (cấu hình được, mặc định an toàn).
2. THE hệ thống SHALL áp một **per-user concurrency cap** (số job chạy đồng thời tối đa của một user, cấu hình được, mặc định nhỏ vd 2).
3. WHEN số job đang chạy đạt cap (global hoặc per-user) THEN hệ thống SHALL **xếp job mới vào hàng đợi** thay vì chạy ngay, và phản hồi trạng thái `queued` cho user.
4. THE hàng đợi SHALL phân phối **công bằng giữa các tenant** (một user không được chiếm hết slot khiến user khác chờ vô hạn).
5. WHEN một slot rảnh THEN hệ thống SHALL chọn job kế tiếp theo policy fair-share (vd round-robin theo tenant) trong giới hạn cap.
6. THE trạng thái job (`queued/running/success/error/cancelled`) và vị trí hàng đợi SHALL hiển thị realtime cho user.
7. THE giá trị cap SHALL được lưu trong cấu hình hệ thống (Settings Store của tenant hệ thống) và admin chỉnh được qua admin panel.
8. THE việc enforce cap SHALL ở tầng server (không phụ thuộc client), kể cả khi nhiều request submit đồng thời (atomic, không race vượt cap).

### R8 — Job state bền vững & phục hồi (Durable Job State)

**User Story:** Là user, tôi muốn job của mình không mất khi server restart, và được phục hồi đúng chủ sở hữu.

#### Acceptance Criteria

1. THE trạng thái job SHALL được persist (gắn `tenant_id`) **trước khi** mutate state in-memory và broadcast SSE.
2. WHEN server restart THEN hệ thống SHALL phục hồi job đang dở (`queued/running`) đúng theo `tenant_id`, không trộn lẫn giữa các tenant.
3. THE kiến trúc job manager SHALL được refactor để **không phụ thuộc singleton in-memory làm nguồn sự thật**; nguồn sự thật là DB.
4. IF DB ghi thất bại THEN hệ thống SHALL fail-fast/log rõ, KHÔNG để state in-memory và DB phân kỳ âm thầm.
5. THE UPI QR job (hiện in-memory, không persist) SHALL được đánh giá: hoặc persist tối thiểu (tenant + status + qr_path) hoặc tài liệu hoá rõ là ephemeral per-tenant và cleanup theo tenant.

### R9 — Admin panel & giám sát (Administration)

**User Story:** Là admin, tôi muốn một bảng điều khiển để quản lý user, license, và giám sát mọi hoạt động.

#### Acceptance Criteria

1. THE admin SHALL xem được danh sách toàn bộ user kèm `status`, `expires_at`, ngày tạo, hoạt động gần nhất.
2. THE admin SHALL **tạo / sửa / gia hạn / suspend / xoá** user.
3. THE admin SHALL reset password user (đặt password mới đã hash).
4. THE admin SHALL xem được job của mọi tenant (lọc theo tenant, status, loại tool, khoảng thời gian).
5. THE admin SHALL xem được **audit log** các hành động quan trọng: đăng nhập, submit/cancel job, thay đổi cấu hình, thao tác admin trên user.
6. THE admin SHALL chỉnh được cấu hình hệ thống: global cap, per-user cap mặc định, thời hạn mặc định.
7. THE admin SHALL xem được chỉ số tải hiện thời: số job running/queued toàn hệ thống và theo tenant.
8. WHEN admin xoá user THEN hệ thống SHALL xử lý dữ liệu liên quan theo policy rõ ràng (xoá kèm hoặc ẩn danh), không để mồ côi (orphan) tham chiếu.

### R10 — Audit & truy vết (Auditability)

**User Story:** Là admin, tôi muốn truy vết được ai làm gì khi nào, để kiểm soát và xử lý sự cố.

#### Acceptance Criteria

1. WHEN xảy ra sự kiện quan trọng (login success/fail, submit job, cancel, config change, admin action, license change) THEN hệ thống SHALL ghi một bản ghi audit gồm `tenant_id`/actor, loại sự kiện, timestamp UTC, metadata.
2. THE audit log SHALL **redact** dữ liệu nhạy cảm (password, token, refresh_token, proxy credential).
3. THE audit log SHALL truy vấn được theo tenant, loại sự kiện, khoảng thời gian.
4. THE audit log SHALL không cho user thường đọc; chỉ admin.
5. THE hệ thống SHALL có policy retention/cleanup cho audit log để không phình DB vô hạn.

### R11 — Toàn vẹn dữ liệu & di trú (Data Integrity & Migration)

**User Story:** Là chủ hệ thống, tôi muốn giữ nguyên dữ liệu và logic nghiệp vụ hiện có khi nâng cấp lên multi-tenant.

#### Acceptance Criteria

1. THE quá trình migrate schema SHALL theo cơ chế versioned migration hiện có (`db/schema.py`, `CURRENT_VERSION`), idempotent, có rollback-safety ở mức transaction.
2. WHEN migrate dữ liệu cũ (single-tenant) THEN hệ thống SHALL gán dữ liệu hiện hữu cho một tenant mặc định (vd admin/tenant hệ thống), KHÔNG mất dữ liệu.
3. THE logic nghiệp vụ HTTP hiện có (signup request-mode, get session pure-request, UPI HTTP, payment link) SHALL được giữ nguyên hành vi, chỉ bổ sung ràng buộc tenant + isolation.
4. WHEN migration chạy THEN hệ thống SHALL sao lưu DB trước khi áp đổi schema phá vỡ (breaking) và ghi log kết quả.
5. THE mọi setting key mới (auth, license, tenant resource, cap) SHALL được thêm vào whitelist `_EXACT_KEYS` + `_validate_type_constraint()` trước khi sử dụng (tuân thủ project rule Settings Store).
6. IF migration thất bại giữa chừng THEN hệ thống SHALL không khởi động ở trạng thái nửa vời mà fail-fast với thông báo rõ.

### R12 — Bảo mật & phơi nhiễm mạng (Security & Exposure)

**User Story:** Là chủ hệ thống, tôi muốn expose dịch vụ ra ngoài an toàn từ máy cá nhân.

#### Acceptance Criteria

1. THE secret tại rest (password đã hash là bắt buộc; access_token/refresh_token/session JSON nên được mã hoá hoặc tối thiểu giới hạn quyền + tài liệu hoá rủi ro) SHALL được xử lý theo policy bảo mật rõ ràng, KHÔNG để mọi secret plaintext mặc định.
2. THE hệ thống SHALL có **rate limiting** cho các endpoint nhạy cảm (đăng nhập, submit job) theo per-IP và/hoặc per-tenant.
3. THE endpoint `/api/gopay-check/*` (hiện bypass auth) SHALL được đánh giá lại: hoặc đưa vào auth, hoặc bảo vệ bằng secret riêng + rate limit; KHÔNG để mở hoàn toàn khi expose ra ngoài.
4. WHEN dịch vụ bind ra ngoài loopback THEN hệ thống SHALL bắt buộc opt-in tường minh và bắt buộc auth (không có chế độ mở không token).
5. THE token SHALL không bị log nguyên văn ở proxy/access log; SSE dùng token qua query phải có biện pháp giảm thiểu (cookie HttpOnly nếu khả thi, hoặc cấu hình proxy bỏ query khi log).
6. THE CORS SHALL không đặt `*` khi có auth đa người dùng; cấu hình theo origin tin cậy.
7. THE hệ thống SHALL không transmit code/secret/dữ liệu user ra endpoint bên thứ ba ngoài các provider mà chính user cấu hình (OpenAI/Stripe/mail provider/proxy của họ).

### R13 — Frontend đa người dùng (Frontend / UX)

**User Story:** Là user, tôi muốn giao diện đăng nhập và bảng làm việc riêng, chỉ thấy dữ liệu của tôi.

#### Acceptance Criteria

1. THE frontend SHALL có màn hình đăng nhập/đăng xuất; sau đăng nhập chỉ hiển thị dữ liệu của tenant hiện tại.
2. THE frontend SHALL hiển thị thời hạn còn lại của tài khoản và cảnh báo khi gần hết hạn.
3. THE frontend SHALL ẩn các tab/tool không khả dụng với role `user` (HME, AutoReg, browser-mode).
4. THE frontend SHALL dùng `Settings.get/save` per-tenant (qua API), KHÔNG dùng `localStorage` cho runtime config (tuân thủ Settings Store rule); `localStorage` chỉ cho token phiên + draft textarea.
5. WHEN tài khoản hết hạn/suspend trong lúc đang dùng THEN frontend SHALL phản hồi rõ ràng (buộc đăng nhập lại / thông báo khoá) khi nhận 401/403.
6. THE admin SHALL có giao diện quản trị riêng (quản lý user, license, giám sát job, audit).

### R14 — Vận hành trên máy cá nhân (Operability)

**User Story:** Là chủ hệ thống, tôi muốn vận hành ổn định trên một máy cá nhân với rủi ro được kiểm soát.

#### Acceptance Criteria

1. THE hệ thống SHALL chạy ổn định ở cấu hình một tiến trình (single process) với cap đồng thời phù hợp tài nguyên máy (cấu hình được).
2. THE hệ thống SHALL có cơ chế **backup DB** định kỳ (tài liệu hoá + script/lệnh), vì mất DB = mất dữ liệu của mọi user.
3. THE hệ thống SHALL có **policy cleanup** artifact/profile/log per-tenant để kiểm soát dung lượng đĩa.
4. THE hệ thống SHALL ghi log vận hành đủ để chẩn đoán (tải, lỗi job, lỗi auth) mà không lộ secret.
5. THE việc expose ra ngoài SHALL qua tunnel/proxy có TLS; tài liệu vận hành nêu rõ giới hạn (uptime phụ thuộc máy cá nhân, không SLA).
6. WHEN máy/tiến trình khởi động lại THEN hệ thống SHALL tự phục hồi trạng thái job + cấu hình từ DB (tuân R8) trong thời gian hợp lý.

---

## Ngoài phạm vi (Out of Scope)

- Tích hợp cổng thanh toán tự động (Stripe/VNPay/MoMo...) — thanh toán xử lý **ngoài hệ thống**, admin cấp/gia hạn thủ công.
- iCloud Hide My Email và AutoReg cho user (chỉ admin nếu cần; không thuộc gói user).
- Browser-mode (Camoufox/Playwright) cho user.
- Scale ngang nhiều máy/nhiều worker process, hạ tầng cloud-native, K8s.
- Mã hoá end-to-end nâng cao / HSM cho secret (chỉ đặt policy nền tảng, không triển khai HSM).

## Giả định (Assumptions)

- Có sẵn proxy chất lượng do **user tự mang**; mạng nhà 1 IP không tự chịu tải 100 user gọi OpenAI/Stripe nếu thiếu proxy.
- Máy cá nhân đủ RAM/CPU cho cap đồng thời được cấu hình (sẽ tinh chỉnh theo spec máy thực tế; mặc định khởi điểm an toàn).
- Thanh toán và quan hệ pháp lý với user do chủ hệ thống tự chịu trách nhiệm (rủi ro TOS OpenAI nằm ngoài phạm vi kỹ thuật).
- Dịch vụ phù hợp mô hình **private/closed group**, không cam kết SLA công khai.

## Câu hỏi mở (cần chốt ở giai đoạn Design)

1. **Spec máy thật** (RAM/CPU/băng thông) để chốt con số `global_cap` và `per_user_cap` mặc định.
2. **Chọn DB**: giữ SQLite (WAL, có rủi ro write-contention với ~100 user) hay chuyển Postgres? (Ảnh hưởng lớn tới design tenancy & concurrency.)
3. **Cơ chế token**: JWT stateless hay session token lưu DB (dễ revoke khi suspend/expire)?
4. **Mức mã hoá secret tại rest**: chấp nhận plaintext + giới hạn quyền (như hiện tại) hay mã hoá tối thiểu các token giá trị cao?
5. **UPI QR job**: persist per-tenant hay giữ ephemeral?
6. **Scope kênh giao hàng**: SaaS multi-tenant áp dụng cho (a) chỉ Web tool Python, (b) cả Web + `rust_upi_bot` Telegram, hay (c) chỉ Telegram bot? Hiện requirements viết cho hướng (a). Nếu chọn (b)/(c) phải bổ sung license-check + nhận diện user phía Telegram bot Rust.
7. **Telegram notify per-tenant**: mỗi user cấu hình bot token/chat_id riêng để nhận QR, hay dùng kênh notify chung của admin?
