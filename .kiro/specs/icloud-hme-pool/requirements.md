# Requirements Document

_iCloud HME Pool_

## Introduction

Feature mở rộng module `icloud_hme/` thành một **HME Pool đầy đủ vòng đời**:

- **Phase MVP (Phần 1–4)**: bổ sung khả năng _record_ thao tác Camoufox/Playwright + HAR cho phân tích, đổi chiến lược pool sang _round-robin_, bổ sung state _limited có TTL retry_ (khác `disabled` permanent đang có), audit trail mỗi sự kiện tạo email, idempotency khi crash, profile management (check + delete).
- **Phase sau MVP (Phần 5–6)**: xóa email HME (single + bulk) và tích hợp web (REST API + UI).

Module hiện tại đã có: `bootstrap.py`, `checker.py`, `client.py`, `generator.py`, `repository.py`, `session.py` cùng schema DB v5 (`icloud_accounts`, `icloud_emails`). Feature này sửa/mở rộng các thành phần đó, không viết lại từ đầu.

Tham chiếu pattern: `outlook_pool.py` (combo pool, terminal error filter, mark_success/mark_failure, status_summary).

### Chiến lược "open-profile-each-run" (login-once + cookie fresh mỗi run)

Toàn bộ flow tự động (tạo HME, check profile, revoke email) được thiết kế **không cần login lại sau bootstrap** và **không cache Session_Bundle qua các lần chạy CLI**:

- **Bootstrap_Flow** (manual, headed) chạy 1 lần đầu cho mỗi Apple_ID — user login + 2FA tay → cookies + tokens được Camoufox flush vào `profile_dir`. Đây là entry point duy nhất chạm UI Apple ID.
- **Mọi command runtime** (create / check / revoke) launch Camoufox headless ngắn với `profile_dir`, navigate `https://www.icloud.com/` (root) chỉ để Apple webapp gọi `/setup/ws/1/validate` flush cookies vào BrowserContext, **trích xuất Session_Bundle** (cookies `X-APPLE-WEBAUTH-*`) từ `BrowserContext.cookies()`, đóng Camoufox, rồi gọi HME_API_Endpoint qua `httpx` thuần với cookies vừa lấy. Không thao tác UI, không nhập 2FA, không phụ thuộc Page object hay `window.webAuth` global (Apple đã gỡ).
- **Trong cùng 1 process run**, Session_Bundle được cache in-memory và tái dùng cho mọi request httpx trong cùng batch trên cùng profile (Requirement 12.8) — tạo 5 email cho 1 Apple_ID chỉ mở Camoufox 1 lần đầu batch. Camoufox đóng ngay sau extract, KHÔNG giữ chạy nền chờ thao tác.
- **KHÔNG persist Session_Bundle xuống disk, KHÔNG cache cross-run** — mỗi process run mới phải mở Camoufox lại để extract cookie fresh từ `profile_dir`. Đây là khác biệt với Option 1 (cache cookie xuống DB và reuse cross-run, rủi ro stale) và Option 3 (hybrid: ưu tiên cookie cache, fallback re-extract).
- **Khi Session_Bundle hết hạn** giữa batch (API trả `HmeAuthError`) → tool invalidate Session_Bundle in-memory, mark profile `session_expired` và DỪNG, không tự động mở Camoufox re-login. User phải chủ động chạy `bootstrap` mới đưa profile về `active`.

Phần API contract (endpoint, params, error classification) tham chiếu reverse-engineering từ project open-source `rtunazzz/hidemyemail-generator` (MIT license). Project upstream dùng cookie string + aiohttp; project này dùng Camoufox + Playwright `context.request` để giữ fingerprint khớp browser, giảm risk Apple flag bot.

## Glossary

- **iCloud_Profile**: thư mục Camoufox profile dir gắn 1–1 với 1 Apple ID đã login, đóng vai trò _credential store_ persistent sau bootstrap. Lưu trong `runtime/icloud_profiles/<apple_id>/`. Chứa cookies + IndexedDB + localStorage để các session sau trích xuất Session_Bundle mà không cần login lại.
- **Apple_ID**: email Apple đã hoàn tất bootstrap (login + 2FA), key chính trong `icloud_accounts.apple_id`.
- **HME_Email**: email Hide My Email do Apple sinh, format `<random>@icloud.com`, lưu trong `icloud_emails.email`.
- **HME_Candidate**: chuỗi email Apple trả ở bước `generate` nhưng CHƯA được reserve. Chỉ tồn tại trong response, không reserve thì không tốn slot trong account.
- **HME_Reserve**: bước thứ 2 chốt HME_Candidate thành HME_Email thật trong account, mới tốn slot quota.
- **HME_API_Endpoint**: 3 endpoint Apple HME API trên host `p68-maildomainws.icloud.com`: `POST /v1/hme/generate` (sinh candidate), `POST /v1/hme/reserve` (chốt candidate), `GET /v2/hme/list` (liệt kê email đã tạo). Host hardcode `p68` cho mọi profile — Apple HME API không phục vụ trên partition host khác.
- **HME_API_Params**: 4 query param trên mọi request HME_API_Endpoint: `clientBuildNumber`, `clientMasteringNumber`, `clientId`, `dsid`. Trong đó `clientBuildNumber` + `clientMasteringNumber` là client identifier (hardcode value từ webapp Apple hiện hành); `clientId` + `dsid` để chuỗi rỗng — Apple HME API KHÔNG enforce auth qua các query param này, auth thực qua cookie `X-APPLE-WEBAUTH-*`.
- **Session_Bundle**: tập credential trích xuất từ iCloud_Profile sau bootstrap, đủ để gọi HME_API_Endpoint mà không cần Camoufox/Page mở. Cấu trúc (refactor B — cookies-only): `{apple_id: str, cookies: dict[str,str], extracted_at: iso_timestamp}`. Cookies dict chứa các X-APPLE-WEBAUTH-* (USER, TOKEN, PCS-Mail, HSA-TRUST, VALIDATE...) — Validate yêu cầu non-empty + có ÍT NHẤT 1 cookie thuộc tập marker login `{X-APPLE-WEBAUTH-USER, X-APPLE-WEBAUTH-TOKEN, X-APPLE-WEBAUTH-PCS-Mail}`. **Scope: process-lifetime, không persist disk, không cache cross-run** — mỗi process run mới SHALL launch Camoufox lại để extract Session_Bundle fresh từ `profile_dir`, không đọc từ run trước.
- **Bootstrap_Flow**: flow manual headed chạy 1 lần đầu cho mỗi Apple_ID — user login + 2FA tay trong Camoufox, profile_dir được flush. Là entry point DUY NHẤT chạm UI login Apple ID. Cũng là cách duy nhất để recover profile đang ở `session_expired`.
- **Pool_Manager**: thành phần quản lý tập iCloud_Profile, chịu trách nhiệm pick profile + chuyển trạng thái.
- **HME_Generator**: thành phần tạo HME_Email. Mở Camoufox headless ngắn với profile_dir → trích xuất Session_Bundle → đóng browser → gọi `HmeClient.reserve_hme` qua HTTP với Session_Bundle.
- **HME_Manager**: thành phần quản lý vòng đời HME_Email Apple-side (deactivate / reactivate / delete / update-meta / list-sync) + update DB tương ứng. Phase sau MVP. Dùng cùng cơ chế Session_Bundle như HME_Generator, không thao tác UI.
- **Profile_Checker**: thành phần verify session 1 profile bằng API read-only (list HME) qua cùng cơ chế Session_Bundle, không thao tác UI.
- **Recorder**: thành phần ghi Playwright action log + HAR khi user thao tác manual trong Camoufox headed.
- **Recording_Session**: 1 lượt user mở Camoufox manual để record. Mỗi session sinh 1 thư mục `runtime/icloud_recordings/<session_id>/` chứa `actions.jsonl` + `network.har` + `metadata.json`.
- **Profile_Status**: trạng thái 1 profile, gồm: `active`, `limited`, `quota_full`, `session_expired`, `disabled`, `deleted`.
  - `active`: dùng được.
  - `limited`: bị Apple rate-limit, **tạm thời** không dùng cho đến khi qua `limited_until` timestamp.
  - `quota_full`: profile đã chạm `HME_QUOTA_LIMIT` Apple-side (mặc định 700); tạm thời không pick cho đến khi qua `quota_retry_until` (Quota_Retry_TTL). Khác `limited` ở nguyên nhân (user-side full slot vs Apple rate-limit) và TTL (15 phút mặc định vs 24 giờ).
  - `session_expired`: Session_Bundle hết hạn / 2FA bắt lại, phải re-bootstrap manual.
  - `disabled`: bị soft-delete hoặc fail không recover được.
  - `deleted`: profile_dir đã xóa khỏi disk, record giữ lại để audit.
- **Audit_Log**: bảng `icloud_audit_log` ghi mỗi sự kiện vòng đời. Tập `event_type` đầy đủ (gồm cả nhánh deactivate/reactivate/delete/update-meta/export/job-*) được liệt kê đầy đủ trong Requirement 6.2.
- **Limited_TTL**: khoảng thời gian tối thiểu một profile ở trạng thái `limited` trước khi cho retry. Cấu hình qua env, default `24h`.
- **Round_Robin_Cursor**: con trỏ persistent (`pool_state.round_robin_cursor` trong DB) lưu apple_id được pick gần nhất, để lượt sau pick profile kế tiếp.
- **Pool_Exhausted**: trạng thái khi không còn profile nào ở `active` để pick.
- **Infinite_Generate_Mode**: chế độ Job `kind='generate'` chạy vô hạn — không có `count` cố định, vòng lặp tự động pick profile → tạo email → switch profile, chỉ dừng khi user gọi action `stop` (Requirement 13.6) hoặc `pause` (Requirement 13.7), hoặc khi gặp fatal error không recover được (vd corrupt DB). Khi `restart` (Requirement 13.8), job mới luôn extract Session_Bundle fresh từ `profile_dir` ở vòng đầu, KHÔNG reuse bất kỳ in-memory state nào của job cũ (job cũ đã terminate, in-memory state mất sạch theo Requirement 12.6).
- **Pool_Exhausted_Wait**: behavior khi mọi profile ở trạng thái không pick được (`limited`, `session_expired`, `quota_full`, `disabled`) trong Infinite_Generate_Mode — Job KHÔNG transition `failed`, mà compute `wake_at = min(limited_until / quota_retry_until)` từ tập profile có thể tự recover (`limited` ∪ `quota_full`), sleep đến lúc đó (capped bởi env `ICLOUD_INFINITE_WAIT_MAX_SEC`, default 86400 giây = 24 giờ), rồi loop pick lại. Trong lúc sleep vẫn check `cancellation_event` và `pause_event` mỗi giây để response action user kịp thời.
- **Quota_Retry_TTL**: khoảng thời gian tối thiểu một profile ở trạng thái `quota_full` trước khi cho retry pick. Cấu hình qua env `ICLOUD_QUOTA_RETRY_MINUTES`, default 15 phút.
- **Label_Default**: chuỗi `YYYYMMDD` theo UTC tại thời điểm bắt đầu batch tạo email, dùng làm giá trị mặc định cho `icloud_emails.label` khi user không truyền `--label`. Mục đích: cho phép truy vấn / xóa hàng loạt email theo ngày tạo (`email deactivate --by-date YYYYMMDD`, `email delete --by-date YYYYMMDD`...). User có thể override bằng tham số CLI/API.
- **Web_API**: REST API expose qua FastAPI/web app hiện có. Phase sau MVP.
- **Web_UI**: layer giao diện web hiện có của tool. Feature mở 1 tab `HME` mới với 3 sub-page (`Profiles`, `Jobs`, `Emails`) bám sát `Web_API`. Phase sau MVP.
- **HME_Lifecycle**: 4 nhóm action quản lý 1 HME_Email đã reserve: `list_sync` (đồng bộ DB ↔ Apple), `deactivate` (ẩn email, `isActive=false` Apple-side, `anonymousId` còn — có thể reactivate), `reactivate` (kích hoạt lại email đã deactivated), `delete` (xoá hẳn Apple-side, free slot quota). Cộng thêm `update-meta` cho label/note. Lifecycle Apple-side: `generate → reserve → (deactivate ↔ reactivate)* → delete`.
- **Email_Status_Enum**: tập trạng thái cho `icloud_emails.status` — `created` (mới reserve, Apple-side active), `reconciled` (đồng bộ từ Apple-side qua `list_sync`/`reconcile`, default active), `deactivated` (Apple-side `isActive=false`, anonymousId còn — có thể reactivate), `revoked` (alias backward-compat của `deactivated`, deprecated cho row mới), `deleted` (Apple-side đã xoá hẳn, free slot — terminal), `disabled` (DB-side mark khi reconcile thấy mismatch không recover được — terminal), `used_for_chatgpt` (semantic mới: email đã được dùng để đăng ký ChatGPT, set khi flow ChatGPT signup hoàn tất). Vòng đời cho phép: `created → deactivated → created (qua reactivate) → deleted`. `deleted` và `disabled` là terminal.
- **Job**: 1 lượt chạy lifecycle async (auto-generate, deactivate_bulk, reactivate_bulk, delete_bulk, list_sync, bootstrap, check_all, update_meta_bulk, export). Persistent trong bảng `icloud_jobs` với `status` ∈ `{queued, running, paused, completed, failed, cancelled}`. Mỗi Job có log append-only stream qua SSE.
- **Job_Action**: thao tác user kích hoạt trên 1 Job — `start` (`queued → running`), `stop` (`running|paused → cancelled`, partial result), `pause` (`running → paused`), `resume` (`paused → running`), `restart` (clone params sang `job_id` mới, run từ đầu, không thay đổi job cũ; job mới có `parent_job_id = old_job_id`).
- **Apple_HME_Endpoint_Set**: tập 7 endpoint chuẩn của Apple HME API trên host hardcode `https://p68-maildomainws.icloud.com` — `POST /v1/hme/generate`, `POST /v1/hme/reserve`, `GET /v2/hme/list`, `POST /v1/hme/deactivate` (body `{anonymousId}`), `POST /v1/hme/reactivate` (body `{anonymousId}`), `POST /v1/hme/delete` (body `{anonymousId}`), `POST /v1/hme/updateMetaData` (body `{anonymousId, label, note}`). Host cố định `p68` cho mọi account — KHÔNG dynamic theo `Session_Bundle.maildomainws_host` (refactor B — Apple HME API chỉ phục vụ trên 1 host duy nhất).
- **dsid_extract_pattern**: ~~deprecated~~ — refactor B đã bỏ. Apple HME API KHÔNG enforce `dsid` query param khi cookies hợp lệ; tool truyền `dsid=""` cho mọi request.
- **Profile_Lock**: filelock per Apple_ID đặt tại `runtime/icloud_profiles/<apple_id>/.lock` (hoặc `runtime/locks/icloud-<apple_id>.lock`), nhằm prevent corrupt `profile_dir` khi 2 process cùng truy cập 1 Apple_ID. Có 2 mode:
  - `write` (exclusive): bắt buộc cho Bootstrap_Flow headed (Requirement 12.14) và Recorder.start_session headed (Requirement 12.16) — chỉ 1 process được giữ tại 1 thời điểm, blocking mọi acquire khác (cả `read` lẫn `write`).
  - `read` (shared): bắt buộc cho `extract_session_bundle` headless (Requirement 12.15) — nhiều process có thể giữ đồng thời nhưng SHALL block khi có process khác đang giữ `write` lock.
  - Implement qua thư viện `filelock` (đã có / sẽ thêm vào `pyproject.toml`).
- **Cursor_Atomic_Pick**: pattern atomic giữa SELECT next profile + UPDATE `pool_state.round_robin_cursor` — Pool_Manager SHALL wrap toàn bộ block `SELECT next active profile` + `UPDATE round_robin_cursor` trong 1 transaction SQLite mode `BEGIN IMMEDIATE` để write-lock connection ngay từ đầu transaction (không đợi đến lần INSERT/UPDATE đầu tiên), đảm bảo 2 process song song serialize qua write-lock và KHÔNG bao giờ pick cùng 1 profile khi `ICLOUD_HME_PROFILE_PARALLELISM > 1` hoặc `ICLOUD_JOB_MAX_PARALLEL > 1`. Mặc định SQLite write-lock timeout 5 giây — quá hạn raise `IcloudPoolError` với message `pool_pick_locked` để caller retry / fail-fast.
- **Add_Profile_Flow**: flow web cho phép user thêm 1 profile mới qua thao tác manual trong Camoufox headed, chạy server-side. Khác Bootstrap_Flow ở chỗ: Bootstrap_Flow yêu cầu user nhập sẵn `apple_id` (đã có row trong DB) trước khi mở browser; Add_Profile_Flow KHÔNG yêu cầu apple_id input, chạy 1 session ngắn theo state machine `idle → recording → saving|cancelling → done|cancelled|failed`, extract `apple_id` từ cookie sau khi user login xong + bấm `Lưu`. Profile_dir tạm đặt tại `runtime/icloud_profiles/.adding/<session_id>/`, được rename sang `runtime/icloud_profiles/<apple_id>/` lúc save thành công, hoặc xoá lúc cancel/fail. Bộ máy này hoàn toàn server-side (Camoufox chạy trên máy backend), không phải Camoufox của browser end-user — phù hợp deployment local-first hiện tại của tool.
- **Add_Profile_Session**: 1 lượt user chạy Add_Profile_Flow, định danh bằng `session_id` (uuid4). Mỗi session có lifecycle in-memory trên backend (process restart → mất, treat như cancel + cleanup). State chuyển qua: `recording` (Camoufox đang chạy headed, chờ user tương tác), `saving` (user bấm `Lưu`, backend đang extract + persist), `cancelling` (user bấm `Huỷ` hoặc timeout, backend đang stop browser + xoá profile_dir), `done` (terminal — apple_id persist OK), `cancelled` (terminal — đã xoá profile_dir), `failed` (terminal — lỗi extract / persist). Session SHALL có TTL hard cap 30 phút (env `ICLOUD_ADD_PROFILE_TIMEOUT_SEC`) — quá hạn server-side tự transition `cancelling → cancelled`, audit `profile_add_timeout`, để tránh Camoufox zombie.
- **Add_Profile_Lock_Single**: invariant per-process — chỉ tối đa 1 Add_Profile_Session ở state `recording|saving|cancelling` cùng lúc trong cùng process. UI nút `+ Thêm profile` SHALL bị disable khi có session đang chạy. Backend endpoint `POST /api/icloud/profiles/add/start` SHALL return HTTP 409 nếu vi phạm.
- **AddProfileSession**: đại diện 1 lượt user thêm profile mới qua Web_UI (Requirement 14). Lifecycle ephemeral in-memory: `launching → browser_open → verifying → saved | cancelled | failed`. Có TTL configurable (`ICLOUD_ADD_PROFILE_TTL_MINUTES`, default 30 phút) để auto-cancel khi user mở Camoufox xong nhưng quên bấm Lưu/Huỷ. Mỗi session gắn 2 `asyncio.Event`: `save_event` (user bấm Lưu) và `cancel_event` (user bấm Huỷ). 1 Apple_ID chỉ có tối đa 1 session active tại 1 thời điểm.
- **Timestamp_Format**: format chuẩn cho mọi cột timestamp ISO trên DB và mọi field datetime trong dataclass — `strftime('%Y-%m-%dT%H:%M:%fZ', 'now')` (UTC + suffix `Z` + millisecond precision). Áp dụng đồng nhất cho:
  - `icloud_audit_log.timestamp_iso`.
  - `icloud_accounts.last_used_at`, `icloud_accounts.limited_until`, `icloud_accounts.quota_retry_until`.
  - `icloud_emails.created_at`, `icloud_emails.used_at`, `icloud_emails.deactivated_at`, `icloud_emails.reactivated_at`, `icloud_emails.deleted_at`, `icloud_emails.last_sync_at`.
  - `icloud_jobs.started_at`, `icloud_jobs.ended_at`, `icloud_jobs.updated_at`, `icloud_jobs.created_at`.
  - Mọi field datetime trong dataclass / response API tương ứng (vd `BootstrapResult.bootstrapped_at`, `Session_Bundle.extracted_at`, `CheckResult.checked_at`).

## Requirements

> **Thứ tự ưu tiên**: Requirement 1–8, Requirement 11, Requirement 12 thuộc MVP (bắt buộc để chốt phase đầu). Requirement 9, Requirement 10, Requirement 13 thuộc phase sau MVP. Riêng phần `list_sync` (Requirement 9.12) được implement dưới dạng `reconcile` trong Requirement 8 ở MVP để phục vụ crash recovery; Requirement 9.12 ở phase 2 chỉ wrap lại CLI/API mới + thêm các nhánh trạng thái (`deactivated`, `external_change`).

---

### Requirement 1: Ghi lại thao tác Camoufox + HAR cho discovery

**User Story:** Là dev tự động hóa flow HME, tôi muốn record Playwright action log và HAR file khi user thao tác manual trong Camoufox, để có dữ liệu phân tích selector + endpoint cho cả flow tạo và xóa email.

#### Acceptance Criteria

1. WHEN user chạy lệnh `recording start` với `apple_id`, THE Recorder SHALL launch Camoufox headed với profile dir của Apple ID và navigate đến `https://www.icloud.com/`.
2. WHEN Recording_Session bắt đầu, THE Recorder SHALL bật Playwright tracing với option `screenshots=True, snapshots=True, sources=True` và bật HAR record với option `recordHar.path=runtime/icloud_recordings/<session_id>/network.har, recordHar.mode=full`.
3. WHILE Recording_Session đang chạy, THE Recorder SHALL ghi mỗi DOM event (click, input, navigation, keypress) vào `actions.jsonl` với schema `{timestamp_iso, event_type, target_selector, url, value_redacted_or_null}`.
4. WHILE Recording_Session đang chạy, THE Recorder SHALL redact giá trị input của field thuộc danh sách `[password, code, otp, secret]` thành chuỗi `<redacted>` trước khi ghi vào `actions.jsonl`.
5. WHEN user kết thúc Recording_Session bằng phím Enter trong terminal, THE Recorder SHALL stop tracing, flush HAR file, ghi `metadata.json` chứa `{session_id, apple_id, started_at, ended_at, exit_reason}`, và đóng Camoufox.
6. IF Camoufox crash hoặc user Ctrl+C giữa Recording_Session, THEN THE Recorder SHALL flush log đã capture đến thời điểm đó và ghi `metadata.json` với `exit_reason="crashed"` hoặc `exit_reason="interrupted"`.
7. THE Recorder SHALL hỗ trợ ghi cả flow _tạo_ và flow _xóa_ HME, không phân biệt kịch bản — phân loại do user dán nhãn vào `metadata.json.scenario` qua tham số CLI.
8. WHERE biến môi trường `ICLOUD_RECORDING_RETENTION_DAYS` được set, THE Recorder SHALL xóa thư mục Recording_Session có `started_at` cũ hơn N ngày sau mỗi lần `recording start`.
9. THE Recorder SHALL ghi sự kiện `recording_start` và `recording_stop` vào Audit_Log với `session_id` và `apple_id`.

---

### Requirement 2: Quản lý nhiều profile iCloud với round-robin

**User Story:** Là user vận hành pool, tôi muốn nhiều profile iCloud được dùng luân phiên đều nhau, để phân tải và tránh chạm rate-limit cùng lúc.

#### Acceptance Criteria

1. THE Pool_Manager SHALL hỗ trợ N iCloud_Profile cùng tồn tại, mỗi profile gắn 1–1 với 1 Apple_ID qua bảng `icloud_accounts`.
2. WHEN HME_Generator request 1 profile để tạo email, THE Pool_Manager SHALL pick Apple_ID kế tiếp Round_Robin_Cursor trong tập profile có `Profile_Status = active`. Pool_Manager SHALL chỉ filter theo status enum và SHALL NOT check counter `hme_count` ở bước pick — việc check `hme_count >= HME_QUOTA_LIMIT` chuyển sang trách nhiệm của HME_Generator sau pick (theo Requirement 3.22), để giảm coupling giữa Pool_Manager và domain logic của Generator.
3. WHEN Pool_Manager pick xong 1 profile, THE Pool_Manager SHALL update Round_Robin_Cursor = apple_id vừa pick, persistent vào DB; IF việc update cursor fail trong khi pick đã thành công, THEN THE Pool_Manager SHALL trả về profile đã pick (không rollback) và ghi cảnh báo `cursor_update_failed` vào Audit_Log để chấp nhận round-robin tạm bị lệch.
4. IF không có profile nào ở `Profile_Status = active`, THEN THE Pool_Manager SHALL raise `IcloudPoolError` với message liệt kê count theo từng status thuộc Profile_Status mở rộng (`active`, `limited`, `quota_full`, `session_expired`, `disabled`, `deleted`).
5. WHEN HME_Generator báo profile bị rate-limit, THE Pool_Manager SHALL set `Profile_Status = limited`, `limited_until = now() + Limited_TTL`, và ghi sự kiện `mark_limited` vào Audit_Log trong cùng 1 DB transaction; IF bất kỳ phần nào trong transaction fail, THEN THE Pool_Manager SHALL rollback toàn bộ thay đổi và raise lỗi cho caller.
6. WHILE `now() < limited_until`, THE Pool_Manager SHALL không pick profile đó.
7. WHEN `now() >= limited_until` và Profile_Status đang là `limited`, THE Pool_Manager SHALL transition profile về `active` lúc lần pick kế tiếp và ghi sự kiện `limited_retry` vào Audit_Log.
8. WHEN HME_Generator báo profile bị `session_expired` (auth error), THE Pool_Manager SHALL set `Profile_Status = session_expired` và không tự retry — chỉ về `active` sau khi user chạy `bootstrap` lại.
9. WHERE biến môi trường `ICLOUD_LIMITED_TTL_HOURS` được set, THE Pool_Manager SHALL dùng giá trị đó cho Limited_TTL thay cho default 24 giờ.
10. WHEN HME_Generator báo profile chạm quota cap (lookup `icloud_accounts.hme_count >= HME_QUOTA_LIMIT`), THE Pool_Manager SHALL set `Profile_Status = 'quota_full'`, `quota_retry_until = now() + Quota_Retry_TTL`, và ghi sự kiện `mark_quota_full` vào Audit_Log trong cùng 1 DB transaction; IF bất kỳ phần nào trong transaction fail, THEN THE Pool_Manager SHALL rollback toàn bộ thay đổi và raise lỗi cho caller.
11. WHILE `now() < quota_retry_until` và `Profile_Status = 'quota_full'`, THE Pool_Manager SHALL không pick profile đó.
12. WHEN `now() >= quota_retry_until` và `Profile_Status = 'quota_full'`, THE Pool_Manager SHALL transition profile về `active` lúc lần pick kế tiếp và ghi sự kiện `quota_retry` vào Audit_Log; tại thời điểm transition Pool_Manager SHALL re-check `icloud_accounts.hme_count` — IF vẫn `>= HME_QUOTA_LIMIT`, THEN THE Pool_Manager SHALL set lại `Profile_Status = 'quota_full'` với `quota_retry_until = now() + Quota_Retry_TTL` mới (caller phải delete email Apple-side trước hoặc tăng cap qua env để giải phóng slot).
13. WHERE biến môi trường `ICLOUD_QUOTA_RETRY_MINUTES` được set, THE Pool_Manager SHALL dùng giá trị đó cho Quota_Retry_TTL thay cho default 15 phút.
14. WHERE biến môi trường `ICLOUD_HME_QUOTA_LIMIT` được set, THE Pool_Manager SHALL dùng giá trị đó cho `HME_QUOTA_LIMIT` thay cho default 700.
15. WHEN `Pool_Manager.pick_active_profile()` được gọi, THE Pool_Manager SHALL wrap toàn bộ logic `SELECT next active profile` + `UPDATE pool_state.round_robin_cursor` trong 1 transaction SQLite mode `BEGIN IMMEDIATE` (write-lock connection ngay từ đầu transaction, không đợi đến lần write đầu tiên) để đảm bảo 2 process song song KHÔNG pick cùng 1 profile khi `ICLOUD_HME_PROFILE_PARALLELISM > 1` hoặc `ICLOUD_JOB_MAX_PARALLEL > 1` (theo Cursor_Atomic_Pick). IF SQLite trả `database is locked` (timeout default 5 giây), THEN THE Pool_Manager SHALL raise `IcloudPoolError` với message `pool_pick_locked` để caller (HME_Generator / Profile_Checker / HME_Manager) retry hoặc fail-fast tuỳ context.
16. WHEN HME_Generator được khởi tạo với `profile_parallelism = N` (qua env `ICLOUD_HME_PROFILE_PARALLELISM`), THE HME_Generator SHALL clamp giá trị effective về `min(N, count(active_profiles))` lúc start mỗi batch — số profile chạy song song SHALL NOT vượt số profile khả dụng. WHEN `count(active_profiles) = 0` lúc start batch, THE HME_Generator SHALL raise `IcloudPoolError` ngay (Pool_Exhausted) và SHALL NOT tạo task song song nào.

---

### Requirement 12: Bootstrap login-once và Session Bundle extraction

**User Story:** Là user, tôi muốn login Apple ID + 2FA chỉ 1 lần đầu cho mỗi profile, sau đó mọi flow tự động (tạo HME, check, revoke) chỉ mở browser ngắn để lấy session rồi đóng, không bao giờ phải nhập 2FA lại — để giữ nguyên phiên đăng nhập và không tốn thời gian thao tác.

#### Acceptance Criteria

1. THE Bootstrap_Flow SHALL là entry point DUY NHẤT chạm UI login Apple ID — chỉ chạy khi user gọi command `bootstrap` thủ công, không bao giờ tự động trigger từ HME_Generator / Profile_Checker / HME_Manager.
2. WHEN user chạy `bootstrap apple_id=X`, THE Bootstrap_Flow SHALL launch Camoufox headed với `profile_dir` của X, navigate `https://www.icloud.com/`, đợi user login + 2FA xong, verify cookies `X-APPLE-WEBAUTH-*`, và đóng browser để Camoufox flush state vào `profile_dir`.
3. WHEN HME_Generator / Profile_Checker / HME_Manager cần gọi HME_API_Endpoint trên 1 profile, THE component SHALL launch Camoufox headless với `profile_dir`, navigate `https://www.icloud.com/` chỉ để Apple webapp `/setup/ws/1/validate` flush cookies vào BrowserContext, trích xuất Session_Bundle, đóng browser, rồi mới gọi HME_API_Endpoint qua HTTP client (httpx) với cookies từ Session_Bundle.
4. THE Session_Bundle extraction SHALL đọc cookies `X-APPLE-WEBAUTH-*` từ `BrowserContext.cookies('https://www.icloud.com/')` và build `SessionBundle` immutable chỉ chứa `apple_id` + `cookies` (dict copy) + `extracted_at` UTC. THE Session_Bundle extraction SHALL NOT phụ thuộc `window.webAuth` global (Apple đã gỡ object này khỏi webapp hiện tại — `page.evaluate('window.webAuth')` trả `undefined`); THE Session_Bundle extraction SHALL NOT extract `dsid` / `clientId` / `scnt` / `X-Apple-ID-Session-Id` / `maildomainws_host` / `user_agent` vì Apple HME REST API không enforce các field này khi cookies hợp lệ (verified với `test/check_hme_minimal_call.py`).
5. IF cookies dict empty hoặc KHÔNG có ÍT NHẤT 1 cookie thuộc tập marker login (`X-APPLE-WEBAUTH-USER` / `-TOKEN` / `-PCS-Mail`), THEN THE component SHALL raise `SessionExtractError` với `missing_fields=['cookies']`, ghi audit event `session_extract_fail` với payload `{apple_id, missing_fields, reason, available_cookie_names}`, và KHÔNG gọi HME_API_Endpoint.
6. THE Session_Bundle SHALL ephemeral — chỉ giữ in-memory trong process lifetime, KHÔNG persist xuống disk, KHÔNG log raw values của cookies (chỉ log key names + counter); THE Session_Bundle SHALL NOT cache cross-process — mỗi process run mới SHALL launch Camoufox mới để extract Session_Bundle fresh từ `profile_dir`, KHÔNG đọc cookie/Session_Bundle từ run trước.
7. THE component SHALL ghi audit event `session_extract` (success) với payload `{has_user_cookie, has_token_cookie, has_pcs_mail_cookie, cookie_count, extracted_at}` mỗi lần trích xuất Session_Bundle thành công. Payload SHALL NOT chứa raw cookie value.
8. WHERE component xử lý batch nhiều thao tác trên cùng profile (ví dụ tạo nhiều HME cho 1 Apple_ID), THE component SHALL cache Session_Bundle in-memory và tái dùng cho mọi thao tác trong batch, chỉ mở Camoufox lại khi: (a) chuyển sang profile khác, hoặc (b) Session_Bundle bị API mark expired.
9. IF API trả `HmeAuthError` (session/cookie expired) trong batch đang dùng cached Session_Bundle, THEN THE component SHALL invalidate cached Session_Bundle, mark profile `session_expired` qua Pool_Manager, và KHÔNG mở Camoufox để tự re-login.
10. THE Bootstrap_Flow SHALL là cách DUY NHẤT đưa profile từ `session_expired` về `active` — sau khi bootstrap thành công, Pool_Manager SHALL clear `last_error` + `limited_until` và set `Profile_Status = active`.
11. WHEN Camoufox được dùng để extract Session_Bundle, THE component SHALL dùng `headless=True` mặc định và đóng browser ngay sau extraction, không giữ browser mở chờ thao tác UI.
12. THE HmeClient SHALL nhận Session_Bundle (không nhận Page object) làm tham số khởi tạo, và SHALL gọi HME_API_Endpoint qua HTTP client thuần với headers + cookies từ Session_Bundle, để client có thể test riêng mà không cần browser.
13. WHERE component xử lý batch trên cùng 1 Apple_ID trong cùng process run, THE component SHALL launch Camoufox đúng 1 lần ở đầu batch để extract Session_Bundle, đóng Camoufox ngay sau extraction, reuse Session_Bundle in-memory cho mọi request httpx trong batch, và SHALL NOT giữ Camoufox mở chạy nền chờ thao tác giữa 2 request httpx kế tiếp.
14. WHEN Bootstrap_Flow chạy cho 1 Apple_ID, THE Bootstrap_Flow SHALL acquire `Profile_Lock` ở mode `write` (exclusive) trên `runtime/icloud_profiles/<apple_id>/.lock` TRƯỚC KHI launch Camoufox. IF lock đã bị acquire bởi process khác (timeout default 30 giây), THEN THE Bootstrap_Flow SHALL raise `BootstrapError` với reason `profile_locked_by_another_process` và exit ngay (SHALL NOT launch Camoufox).
15. WHEN `extract_session_bundle` chạy cho 1 Apple_ID, THE component SHALL acquire `Profile_Lock` ở mode `read` (shared) trên cùng path `runtime/icloud_profiles/<apple_id>/.lock`. IF process khác đang giữ `write` lock (ví dụ Bootstrap_Flow đang chạy), THEN THE component SHALL chờ tối đa 60 giây; IF vẫn không acquire được sau timeout, THEN THE component SHALL raise `SessionExtractError` với reason `profile_locked_by_bootstrap` và caller (HME_Generator / Profile_Checker / HME_Manager) SHALL switch sang profile khác qua Pool_Manager (SHALL NOT retry trong cùng vòng pick).
16. WHEN Recorder.start_session chạy cho 1 Apple_ID, THE Recorder SHALL acquire `Profile_Lock` mode `write` (giống Bootstrap_Flow). IF lock conflict, THEN THE Recorder SHALL raise lỗi tương đương Requirement 12.14 với reason `recorder_profile_locked` và exit ngay.
17. IF Camoufox launch fail hoặc verify cookies `X-APPLE-WEBAUTH-*` fail trong Bootstrap_Flow (Requirement 12.2), THEN THE Bootstrap_Flow SHALL retry tối đa 2 lần (tổng 3 attempts) với pause 5 giây giữa các attempt; IF vẫn fail sau attempt thứ 3, THEN THE Bootstrap_Flow SHALL raise `BootstrapError` với reason `cookie_verify_failed_after_retry` và ghi audit event `profile_bootstrap_fail` với payload `{apple_id, attempt, reason}` cho count attempt cuối.

---

### Requirement 3: Tạo email HME với audit trail và idempotency

**User Story:** Là user, tôi muốn tạo N email HME từ pool và biết chính xác email nào do profile nào tạo + thời điểm nào, để khi cần truy vết/cleanup không bị mất context.

#### Acceptance Criteria

1. WHEN user request tạo `count = N` email với `label`, THE HME_Generator SHALL pick profile qua Pool_Manager và tạo từng email cho đến khi đủ N hoặc Pool_Exhausted.
2. WHEN HME_Generator chuyển sang 1 profile mới (lần đầu hoặc sau khi cũ fail), THE HME_Generator SHALL launch Camoufox headless ngắn với `profile_dir`, trích xuất Session_Bundle theo Requirement 2a, và đóng browser TRƯỚC KHI gọi bất kỳ HME_API_Endpoint nào.
3. THE HME_Generator SHALL gọi `generate` và `reserve` qua HTTP client (httpx) sử dụng Session_Bundle đã extract, KHÔNG gọi qua Page object hay Camoufox đang mở.
4. WHILE HME_Generator còn xử lý nhiều email trên cùng 1 Apple_ID, THE HME_Generator SHALL tái dùng Session_Bundle in-memory đã cache, chỉ mở Camoufox lại khi chuyển profile hoặc khi cached Session_Bundle bị invalidated do auth error.
5. WHEN 1 HME_Email được Apple reserve thành công, THE HME_Generator SHALL persist trong cùng 1 DB transaction: `icloud_emails(email, apple_id, label, note, hme_id, status='created', created_at)` và `icloud_accounts.hme_count += 1`.
6. THE HME_Generator SHALL ghi sự kiện `create_attempt`, `create_success`, hoặc `create_fail` vào Audit_Log với payload `{apple_id, email_or_null, error_or_null}` cho mỗi lần thử tạo.
7. IF API Apple trả response thuộc lớp `HmeQuotaError` hoặc message chứa marker rate-limit, THEN THE HME_Generator SHALL dừng vòng tạo trên profile hiện tại, gọi Pool_Manager mark profile `limited`, và pick profile kế tiếp để tiếp tục đến đủ N.
8. IF API Apple trả response thuộc lớp `HmeAuthError`, THEN THE HME_Generator SHALL invalidate cached Session_Bundle của profile đó, gọi Pool_Manager mark profile `session_expired`, và pick profile kế tiếp; THE HME_Generator SHALL NOT tự động mở Camoufox để re-login.
9. IF tất cả profile bị `limited` hoặc `session_expired` trước khi đủ N, THEN THE HME_Generator SHALL dừng và return `GenerationResult` chứa `created < requested` cùng list `disabled_profiles` và `failures`.
10. WHEN HME_Generator restart sau crash, THE HME_Generator SHALL không tạo lại email đã có row trong `icloud_emails` và SHALL coi `icloud_emails` là source-of-truth cho email đã tạo.
11. THE HME_Generator SHALL áp dụng random delay trong khoảng `[delay_min, delay_max]` giây giữa 2 lần `reserve` cùng profile, với default `(2.0, 5.0)`.
12. IF HME_Generator nhận tín hiệu interrupt (SIGINT/SIGTERM), THEN THE HME_Generator SHALL hoàn tất transaction DB của email đang tạo (nếu Apple đã reserve xong), không bắt đầu email mới, và return `GenerationResult` partial.
13. WHEN HME_Generator tạo 1 email, THE HME_Generator SHALL gọi `generate` để sinh HME_Candidate, sau đó gọi `reserve` với `(candidate, label, note)` để chốt; THE HME_Generator SHALL chỉ INSERT row trong `icloud_emails` sau khi `reserve` trả `success=true`.
14. IF response `reserve` báo lỗi candidate đã bị taken (errorMessage chứa marker case-insensitive: `already`, `taken`, `unavailable`, `duplicate`), THEN THE HME_Generator SHALL coi đây là race vô hại, gọi lại `generate` để lấy candidate mới và retry tối đa `ICLOUD_HME_RACE_RETRY_MAX` lần (default 3); THE HME_Generator SHALL NOT mark profile `limited` cho lỗi này; IF cuối cùng `reserve` thành công sau retry, THEN THE HME_Generator SHALL NOT count thành `create_fail` trong audit.
15. WHEN HME_Generator retry candidate sau lỗi taken, THE HME_Generator SHALL ghi audit event `candidate_retry` với payload `{apple_id, attempt, reason}` cho mỗi lần retry.
16. THE HME_Generator SHALL xử lý TUẦN TỰ trong cùng 1 profile, không tạo song song nhiều email trên cùng 1 Apple_ID.
17. WHERE biến môi trường `ICLOUD_HME_PROFILE_PARALLELISM` được set với giá trị nguyên N > 1, THE HME_Generator SHALL cho phép song song giữa các profile khác nhau tối đa N profile cùng lúc, mỗi profile vẫn tuần tự bên trong; default N = 1.
18. WHERE user không truyền `--label` cho command tạo email, THE HME_Generator SHALL tính Label_Default = `strftime('%Y%m%d', UTC)` đúng 1 lần tại thời điểm bắt đầu batch và áp dụng cho mọi email trong batch; THE HME_Generator SHALL NOT tính lại Label_Default kể cả khi batch kéo dài qua nửa đêm UTC.
19. WHERE user truyền `--label X` (X non-empty), THE HME_Generator SHALL dùng giá trị X làm `icloud_emails.label` cho mọi email trong batch và SHALL NOT override bằng Label_Default.
20. WHERE caller request `count = 0` hoặc `count = -1` hoặc `count = "infinite"`, THE HME_Generator SHALL bật **Infinite_Generate_Mode** với 2 mode tuỳ context caller:
    - **Blocking mode (MVP)**: WHEN caller là CLI hoặc Python sync (không truyền `cancellation_event` / `pause_event` / `resume_event`), THE HME_Generator SHALL chạy vòng lặp vô hạn và chỉ dừng khi nhận signal `SIGINT` (Ctrl+C) hoặc `SIGTERM`. WHEN signal được nhận, THE HME_Generator SHALL hoàn tất transaction DB của email đang reserve (nếu Apple đã trả 200), SHALL NOT bắt đầu email mới, và return `GenerationResult` partial (giống Requirement 3.12).
    - **Event-controlled mode (phase sau MVP)**: WHEN caller là JobManager (truyền cả 3 event `cancellation_event` / `pause_event` / `resume_event`), THE HME_Generator SHALL dùng các event đó thay cho signal handler, để JobManager control từ xa qua action `stop` / `pause` / `resume` (Requirement 13.6, 13.7).
21. WHILE Infinite_Generate_Mode đang chạy ở **event-controlled mode** (Requirement 3.20 case 2), THE HME_Generator SHALL kiểm tra `cancellation_event` và `pause_event` (Requirement 13.6, 13.7) sau MỖI lần `reserve` thành công và sau MỖI lần switch profile, để response action user trong vòng tối đa 1 chu kỳ tạo email (~5–30 giây tuỳ delay setting). WHILE Infinite_Generate_Mode chạy ở **blocking mode** (Requirement 3.20 case 1), THE HME_Generator SHALL check signal flag (set bởi signal handler cho SIGINT/SIGTERM) ở cùng các điểm tương đương — semantic giống event check.
22. WHEN HME_Generator pick được profile và check `icloud_accounts.hme_count >= HME_QUOTA_LIMIT`, THE HME_Generator SHALL gọi `Pool_Manager.mark_quota_full(apple_id, reason=f'hme_count={hme_count}')` (Requirement 2.10) thay vì gọi `generate` Apple, ghi audit event `email_skip_quota_full` với payload `{apple_id, hme_count}`, và pick profile kế tiếp ngay (SHALL NOT apply random delay theo Requirement 3.11 cho trường hợp skip này).
23. IF Pool_Manager raise `IcloudPoolError` (Pool_Exhausted) trong Infinite_Generate_Mode, THEN THE HME_Generator SHALL kích hoạt **Pool_Exhausted_Wait** behavior:
    - THE HME_Generator SHALL compute `wake_at = min(limited_until, quota_retry_until)` từ tập profile có `Profile_Status ∈ {limited, quota_full}` (chỉ tính profile có thể tự recover).
    - IF không có profile nào `limited` hoặc `quota_full` (chỉ còn `session_expired` / `disabled` / `deleted`), THEN THE HME_Generator SHALL transition job sang `status='failed'` với `result_json.reason = 'no_recoverable_profile'` và return — vì không profile nào có thể tự recover, user phải bootstrap thủ công.
    - ELSE THE HME_Generator SHALL compute `wake_seconds = max(1, (wake_at - now()).total_seconds())`, capped bởi `ICLOUD_INFINITE_WAIT_MAX_SEC` (default 86400 giây = 24 giờ), update `result_json.waiting_until = wake_at_iso`, append log JSONL với schema `{level='info', message=f"pool exhausted, waiting until {wake_at_iso}", payload={wake_at, wake_seconds, by_status_count}}`, và ghi audit event `infinite_wait_start` (Requirement 6.2).
    - THE HME_Generator SHALL `await asyncio.sleep(wake_seconds)` chia nhỏ thành chunks 1 giây, MỖI chunk check `cancellation_event` và `pause_event` — IF event set, THEN THE HME_Generator SHALL break sleep ngay và respect transition (`cancelled` / `paused`) thay vì sleep tiếp.
    - WHEN sleep kết thúc (timeout / cancellation / pause), THE HME_Generator SHALL ghi audit event `infinite_wait_end` với payload `{slept_seconds, woken_by ∈ {timeout, cancellation, pause}}`, clear `result_json.waiting_until`, và loop lại từ đầu (pick profile lần nữa). Pool_Manager sẽ tự transition `limited` / `quota_full` về `active` qua Requirement 2.7 / 2.12 lúc pick kế tiếp.
24. WHILE Infinite_Generate_Mode đang trong Pool_Exhausted_Wait, THE Job SHALL giữ `status='running'` (không tạo state mới). THE Web_UI Page Jobs SHALL hiển thị badge phụ "waiting until HH:MM" lấy từ field `result_json.waiting_until` được HME_Generator update mỗi lần enter wait (theo Requirement 10.21).
25. WHEN HME_Generator gặp fatal error không classify được (exception ngoài `HmeClientError` / `SessionExtractError` / `IcloudPoolError`), THE HME_Generator SHALL re-raise để JobManager mark `status='failed'` với `result_json.reason = str(exc)` + audit event `job_failed`. Infinite_Generate_Mode SHALL NOT swallow exception lạ.
26. WHEN HME_Generator được spawn qua `JobManager.restart` (Requirement 13.8) cho 1 job parent ở Infinite_Generate_Mode, THE HME_Generator SHALL extract Session_Bundle FRESH từ `profile_dir` cho mọi profile trong vòng lặp đầu tiên — SHALL NOT reuse bất kỳ Session_Bundle in-memory nào từ job parent (job cũ đã terminate, in-memory state mất sạch theo Requirement 12.6).
27. THE `HME_Generator.generate(...)` signature SHALL accept 3 tham số optional `cancellation_event`, `pause_event`, `resume_event` ∈ `Optional[asyncio.Event]`:
    - WHERE cả 3 event đều `None` (default), THE HME_Generator SHALL hoạt động ở blocking mode (Requirement 3.20 case 1) — install signal handler cho `SIGINT` / `SIGTERM` lúc enter `generate()`, restore handler cũ lúc exit (try/finally).
    - WHERE cả 3 event đều non-None, THE HME_Generator SHALL hoạt động ở event-controlled mode (Requirement 3.20 case 2) — SHALL NOT install signal handler (để JobManager control toàn cục).
    - WHERE 1 hoặc 2 event được truyền (mix), THE HME_Generator SHALL raise `ValueError("cancellation_event, pause_event, resume_event must all be None or all non-None")` fail-fast.

---

### Requirement 4: Check trạng thái profile

**User Story:** Là user, tôi muốn check 1 hoặc tất cả profile xem còn login được không và có đang bị limit không, để biết khi nào cần re-bootstrap.

#### Acceptance Criteria

1. WHEN user request `check apple_id=X`, THE Profile_Checker SHALL launch Camoufox headless với `profile_dir` của X, trích xuất Session_Bundle theo Requirement 2a, đóng browser, gọi `GET /v2/hme/list` qua HTTP client với Session_Bundle, và return `CheckResult{apple_id, ok, status, hme_count_remote, hme_count_local, error_or_null}`.
2. WHEN user request `check --all`, THE Profile_Checker SHALL check tuần tự từng profile có `Profile_Status ∈ {active, limited}` và return list `CheckResult`.
3. WHEN response API là 200 + body.success=true, THE Profile_Checker SHALL set `CheckResult.ok = true` và `CheckResult.status = 'active'`.
4. WHEN response API thuộc lớp `HmeAuthError`, THE Profile_Checker SHALL set `CheckResult.status = 'session_expired'`, và WHERE flag `--auto-mark` bật, THE Profile_Checker SHALL update `Profile_Status = session_expired` trong DB.
5. WHEN response API thuộc lớp `HmeQuotaError` hoặc chứa marker rate-limit, THE Profile_Checker SHALL set `CheckResult.status = 'limited'`, và WHERE flag `--auto-mark` bật, THE Profile_Checker SHALL update `Profile_Status = limited` với `limited_until = now() + Limited_TTL`.
6. IF `profile_dir` không tồn tại trên disk, THEN THE Profile_Checker SHALL set `CheckResult.status = 'missing_profile'` và `CheckResult.ok = false`.
7. WHEN user re-chạy `bootstrap` thành công cho 1 profile đang `session_expired` hoặc `limited`, THE Pool_Manager SHALL transition profile về `active` và clear `last_error` + `limited_until`.
8. WHEN Profile_Checker probe session 1 profile, THE Profile_Checker SHALL gọi `GET /v2/hme/list` (read-only, không tốn slot quota), và SHALL NOT dùng `generate` hoặc `reserve` cho mục đích probe.
9. THE Profile_Checker SHALL NOT thao tác UI, SHALL NOT nhập credential hay 2FA, và SHALL NOT tự mở Camoufox headed — chỉ headless ngắn để extract Session_Bundle rồi đóng.

---

### Requirement 5: Xóa profile

**User Story:** Là user, tôi muốn xóa 1 profile khỏi pool, để dọn profile không còn dùng mà vẫn giữ được audit trail email đã tạo từ profile đó.

#### Acceptance Criteria

1. WHEN user request `profile delete apple_id=X`, THE Pool_Manager SHALL xóa thư mục `profile_dir` của X trên disk.
2. WHEN user request `profile delete apple_id=X`, THE Pool_Manager SHALL set `Profile_Status = deleted` và `profile_dir = NULL` trong `icloud_accounts`, không xóa row.
3. THE Pool_Manager SHALL preserve mọi row `icloud_emails` thuộc Apple ID X (`apple_id` foreign key vẫn hợp lệ vì row `icloud_accounts` còn).
4. IF Apple ID X không tồn tại trong DB, THEN THE Pool_Manager SHALL return error `apple_id_not_found` và không thay đổi disk.
5. IF Apple ID X đang ở `Profile_Status = deleted`, THEN THE Pool_Manager SHALL return error `already_deleted` và không thực hiện thao tác.
6. THE Pool_Manager SHALL ghi sự kiện `profile_delete` (success) hoặc `profile_delete_fail` (mọi lỗi như `apple_id_not_found`, `already_deleted`, lỗi xóa disk) vào Audit_Log với payload `{apple_id, hme_count_at_delete, reason_or_null}`.
7. WHILE Apple ID X ở `Profile_Status = deleted`, THE Pool_Manager SHALL không pick profile X cho bất kỳ thao tác nào.

---

### Requirement 6: Audit trail thống nhất

**User Story:** Là user vận hành, tôi muốn xem log đầy đủ mỗi sự kiện vòng đời pool để debug khi pool hành xử bất thường.

#### Acceptance Criteria

1. THE Pool_Manager, HME_Generator, Profile_Checker, Recorder SHALL ghi mỗi sự kiện vòng đời vào bảng `icloud_audit_log` với schema `{id, timestamp_iso, event_type, apple_id_or_null, payload_json, error_or_null}`.
2. THE Audit_Log SHALL hỗ trợ `event_type` thuộc tập: `create_attempt`, `create_success`, `create_fail`, `candidate_retry`, `mark_limited`, `limited_retry`, `mark_session_expired`, `mark_disabled`, `mark_quota_full`, `quota_retry`, `email_skip_quota_full`, `pool_pick_locked`, `infinite_wait_start`, `infinite_wait_end`, `profile_bootstrap`, `profile_bootstrap_fail`, `profile_reactivate`, `profile_delete`, `profile_delete_fail`, `profile_add_start`, `profile_add_success`, `profile_add_cancel`, `profile_add_timeout`, `profile_add_fail`, `email_deactivate`, `email_deactivate_fail`, `email_reactivate`, `email_reactivate_fail`, `email_delete`, `email_delete_fail`, `email_update_meta`, `email_update_meta_fail`, `email_mark_used`, `email_export`, `recording_start`, `recording_stop`, `session_extract`, `session_extract_fail`, `cursor_update_failed`, `reconcile_add`, `reconcile_disable`, `job_started`, `job_paused`, `job_resumed`, `job_completed`, `job_failed`, `job_cancelled`. Tên `email_revoke` / `email_revoke_fail` được giữ làm alias backward-compat read-only cho row audit cũ và SHALL NOT được ghi mới — convention mới SHALL dùng `email_deactivate` / `email_deactivate_fail`. 5 event nhánh Add Profile (theo Requirement 14) có payload chuẩn: `profile_add_start` payload `{session_id, profile_dir, started_at}`; `profile_add_success` payload `{session_id, apple_id, profile_dir_final, duration_seconds}`; `profile_add_cancel` payload `{session_id, duration_seconds, reason: 'user_cancel'}`; `profile_add_timeout` payload `{session_id, expired_after_sec}`; `profile_add_fail` payload `{session_id, reason ∈ {apple_id_not_extractable, apple_id_already_exists, cookies_not_ready, move_failed}, error_or_null}`. Các event lifecycle infinite mode mới có payload chuẩn: `infinite_wait_start` payload `{wake_at_iso, wake_seconds, by_status_count, reason}`; `infinite_wait_end` payload `{slept_seconds, woken_by ∈ {timeout, cancellation, pause}}`; `mark_quota_full` payload `{apple_id, hme_count, quota_retry_until}`; `quota_retry` payload `{apple_id, hme_count_at_retry}`; `email_skip_quota_full` payload `{apple_id, hme_count}`; `pool_pick_locked` payload `{wait_ms, parallelism}` (cảnh báo SQLite write-lock timeout theo Requirement 2.15); `profile_bootstrap_fail` payload `{apple_id, attempt, reason}` (theo Requirement 12.17).
3. THE Audit_Log SHALL được ghi trong cùng DB transaction với thay đổi state tương ứng (ví dụ `create_success` đi cùng INSERT `icloud_emails` + UPDATE `icloud_accounts.hme_count`).
4. WHEN user chạy lệnh `audit list`, THE Audit_Log SHALL trả về các sự kiện theo filter `--apple-id`, `--event-type`, `--since`, `--limit`, ordered by `timestamp_iso DESC`.
5. WHERE biến môi trường `ICLOUD_AUDIT_RETENTION_DAYS` được set, THE Audit_Log SHALL hỗ trợ command `audit cleanup` xóa row cũ hơn N ngày.
6. THE Audit_Log Repository SHALL tách 2 tập `event_type`:
   - `WRITABLE_EVENT_TYPES`: tập event được phép ghi mới — gồm 35 event của Requirement 6.2, KHÔNG bao gồm `email_revoke` / `email_revoke_fail` (alias backward-compat read-only theo Requirement 6.2).
   - `READABLE_EVENT_TYPES`: superset của `WRITABLE_EVENT_TYPES` cộng thêm 2 alias backward-compat (`email_revoke`, `email_revoke_fail`) để filter `audit list` đọc được audit cũ trước migration.
   - WHEN `Audit_Log.write(event_type=X, ...)` được gọi, IF `X NOT IN WRITABLE_EVENT_TYPES`, THEN THE Audit_Log Repository SHALL raise `ValueError(f"event_type {X} not writable; use {sorted(WRITABLE_EVENT_TYPES)}")` (fail-fast theo project-rules).
   - WHEN `Audit_Log.list(event_type=X, ...)` được gọi với filter, IF `X NOT IN READABLE_EVENT_TYPES`, THEN THE Audit_Log Repository SHALL raise `ValueError(f"event_type {X} unknown")`.
   - WHERE tham số `event_type` không truyền (hoặc `None`), THE Audit_Log.list SHALL return mọi event_type không filter.

---

### Requirement 7: Pool status report

**User Story:** Là user, tôi muốn xem tổng quan pool gồm count theo trạng thái + chi tiết từng profile, để biết pool còn capacity bao nhiêu.

#### Acceptance Criteria

1. WHEN user chạy `status`, THE Pool_Manager SHALL trả về tổng count theo `Profile_Status` thuộc tập đầy đủ (`active`, `limited`, `quota_full`, `session_expired`, `disabled`, `deleted`), tổng `hme_count` trên toàn pool, và `quota_soft_cap_per_account`.
2. THE Pool_Manager SHALL trả về list profile với `{apple_id, status, hme_count, quota_remaining, last_used_at, limited_until_or_null, quota_retry_until_or_null, last_error_or_null}` cho mỗi profile; `quota_retry_until` SHALL là ISO UTC khi `Profile_Status='quota_full'` và `NULL` cho mọi status khác.
3. THE Pool_Manager SHALL trả về count `icloud_emails` theo `status` thuộc Email_Status_Enum (`created`, `reconciled`, `deactivated`, `revoked`, `deleted`, `disabled`, `used_for_chatgpt`).
4. WHEN tổng `quota_remaining` toàn pool dưới ngưỡng cảnh báo (default 50), THE Pool_Manager SHALL include flag `low_capacity = true` trong report; ngược lại THE Pool_Manager SHALL include flag `low_capacity = false`.
5. WHEN có profile nào ở `Profile_Status = 'quota_full'`, THE Pool_Manager SHALL include trong report các field `quota_full_count` (số lượng) và `quota_full_profiles: [{apple_id, hme_count, quota_retry_until}]` để Web_UI render badge cảnh báo.

---

### Requirement 8: Idempotency và crash recovery

**User Story:** Là dev, tôi muốn tool an toàn khi crash giữa batch — không tạo trùng email, không mất email đã reserve, không double-count quota.

#### Acceptance Criteria

1. THE HME_Generator SHALL flush DB transaction (INSERT email + UPDATE hme_count + INSERT audit log) trước khi gọi API Apple cho email kế tiếp.
2. ~~Reconcile import từ Apple-side~~ — **DROPPED in refactor B**. DB là source-of-truth: tool chỉ quản email do tool tạo (đã INSERT lúc generate). Email Apple-side ngoài tool KHÔNG được import vào DB. Crash recovery dựa vào DB row đã commit lúc generate (R3.5 — INSERT email + UPDATE hme_count + audit trong cùng tx flush trước Apple call kế).
3. ~~Reconcile add Apple-side missing in DB~~ — **DROPPED in refactor B**. Use case "reconcile add" không còn áp dụng vì DB source-of-truth. Method ``HmeGenerator.reconcile()`` giữ signature làm no-op stub để CLI cũ không vỡ; trả 0.
4. WHEN ``HME_Manager.list_sync`` (R9.12) phát hiện email DB-side mà Apple-side missing, THE HME_Manager SHALL update DB theo nhánh tương ứng (xem R9.12) — KHÔNG dùng audit event ``reconcile_disable`` (legacy).
5. THE HME_Generator SHALL không bao giờ retry tự động cùng email với Apple (mỗi `generateAddress` cho email khác nhau theo design Apple).
6. WHEN HME_Generator gọi `GET /v2/hme/list` cho reconcile, THE HME_Generator SHALL parse response field `result.hmeEmails[]` với mỗi item theo schema `{hme, label, note, hmeId, isActive, createTimestamp, anonymousId}` và map `hmeId` (hoặc `anonymousId` khi `hmeId` vắng) vào `icloud_emails.hme_id`.

---

### Requirement 9: Quản lý vòng đời email HME (deactivate / reactivate / delete / update-meta / list-sync) — phase sau MVP

**User Story:** Là user, tôi muốn quản lý vòng đời email HME đã tạo gồm xem danh sách (đồng bộ với Apple), tắt tạm (deactivate), kích hoạt lại (reactivate), xoá hẳn (delete), update label/note — đơn lẻ hoặc bulk theo nhiều tiêu chí, để vận hành pool linh hoạt mà không phải thao tác thủ công trên icloud.com.

#### Acceptance Criteria

1. WHEN user request `email deactivate email=Y`, THE HME_Manager SHALL lookup `icloud_emails.email = Y` để lấy `apple_id` chủ và `hme_id` (= `anonymousId` Apple-side), trích xuất Session_Bundle của profile chủ theo Requirement 2a (mở Camoufox headless ngắn, lấy session, đóng), gọi `POST /v1/hme/deactivate` body `{anonymousId}` qua HTTP client với Session_Bundle, và update `icloud_emails.status = 'deactivated'` cùng `deactivated_at = now()`.
2. WHEN user request `email deactivate --bulk emails=[Y1, Y2, ...]`, THE HME_Manager SHALL group email theo `apple_id` chủ và xử lý từng group bằng cùng Session_Bundle cached để không phải mở Camoufox lại cho mỗi email trong cùng 1 profile.
3. IF profile chủ của email Y đang ở `Profile_Status ∈ {session_expired, deleted}`, THEN THE HME_Manager SHALL skip Y, ghi audit event `email_deactivate_fail` với reason `profile_unavailable`, và tiếp tục email kế tiếp.
4. IF API deactivate trả `HmeQuotaError` hoặc rate-limit, THEN THE HME_Manager SHALL dừng group hiện tại, mark profile `limited`, và return list email còn lại chưa xử lý.
5. WHEN deactivate thành công, THE HME_Manager SHALL ghi audit event `email_deactivate` với payload `{apple_id, email, hme_id}`.
6. IF API deactivate trả 404 / "not found" cho email Y (đã bị xóa ngoài tool), THEN THE HME_Manager SHALL update `icloud_emails.status = 'deleted'` cùng audit event `email_deactivate_fail` với reason `not_found_remote`.
7. THE HME_Manager SHALL áp dụng random delay `[1.0, 3.0]` giây giữa 2 lần gọi API (deactivate / reactivate / delete / update-meta) cùng profile, kể cả khi xử lý theo group bulk — delay là bắt buộc giữa mỗi cặp request kế tiếp trong cùng group.
8. IF API trả `HmeAuthError` cho email Y, THEN THE HME_Manager SHALL invalidate cached Session_Bundle, mark profile chủ `session_expired`, dừng group hiện tại, ghi audit event `email_deactivate_fail` với reason `session_expired`, và SHALL NOT tự mở Camoufox để re-login.
9. WHEN user request `email deactivate --by-label LABEL`, THE HME_Manager SHALL query `icloud_emails WHERE label = LABEL AND status IN ('created', 'reconciled')`, group kết quả theo `apple_id`, và xử lý deactivate từng group theo cùng cơ chế Requirement 9.2 (reuse Session_Bundle cached trong group, áp dụng delay theo Requirement 9.7).
10. WHEN user request `email deactivate --by-date YYYYMMDD`, THE HME_Manager SHALL convert lệnh thành `email deactivate --by-label YYYYMMDD` và xử lý y hệt Requirement 9.9 — tận dụng convention Label_Default.
11. THE HME_Manager SHALL hỗ trợ flag `--dry-run` cho mọi biến thể deactivate (single email, `--bulk`, `--by-label`, `--by-date`); WHERE `--dry-run` bật, THE HME_Manager SHALL chỉ trả về list email sẽ bị deactivate (gồm `email`, `apple_id`, `hme_id`, `label`, `created_at`) và SHALL NOT gọi HME_API_Endpoint, SHALL NOT update DB, SHALL NOT ghi audit event `email_deactivate`.
12. WHEN user request `email list-sync apple_id=X`, THE HME_Manager SHALL extract Session_Bundle cho profile X theo Requirement 2a, gọi `GET /v2/hme/list` qua HTTP client, parse `result.hmeEmails[]` theo schema `{hme, anonymousId, label, note, isActive, createTimestamp}`, và đồng bộ với DB trong cùng 1 transaction. THE list_sync SHALL coi DB là source-of-truth — chỉ UPDATE email DB-side đã có dựa trên trạng thái Apple-side; SHALL NOT INSERT email Apple-side mà DB chưa có (refactor B): tool chỉ quản email do tool tạo (đã INSERT lúc generate), email user tạo tay ngoài tool không được import. Các nhánh UPDATE:
    - email DB-side `status ∈ {created, reconciled}` mà Apple-side `isActive=false` → UPDATE `status='deactivated'` + `deactivated_at = now()`, ghi audit event `email_deactivate` với reason `external_change`.
    - email DB-side `status ∈ {created, reconciled}` mà Apple-side missing → UPDATE `status='deleted'` + `deleted_at = now()`, ghi audit event `email_delete` với reason `external_change`.
    - email DB-side `status='used_for_chatgpt'` mà Apple-side missing → UPDATE `status='disabled'` + `last_sync_at = now()`, ghi audit event `email_delete` với reason `apple_deleted_after_use` + payload `previous_status='used_for_chatgpt'` (A10 — refactor B review). HME này đã được dùng cho ChatGPT signup nhưng Apple đã xoá → forward không hoạt động, đánh dấu `disabled` thay vì `deleted` để admin biết phân biệt với delete chủ động.
    - email DB-side `status ∈ {deactivated, revoked}` mà Apple-side `isActive=true` → UPDATE `status='created'` + `reactivated_at = now()`, ghi audit event `email_reactivate` với reason `external_change`.
    - email Apple-side mà DB-side thiếu → bỏ qua, KHÔNG insert (DB source-of-truth).
13. WHEN user request `email reactivate email=Y`, THE HME_Manager SHALL lookup email Y, verify `icloud_emails.status ∈ {deactivated, revoked}`, trích xuất Session_Bundle theo Requirement 2a, gọi `POST /v1/hme/reactivate` body `{anonymousId}` qua HTTP client, và update `icloud_emails.status='created'` cùng `reactivated_at = now()`, ghi audit event `email_reactivate`. IF `icloud_emails.status ∈ {deleted, disabled}`, THEN THE HME_Manager SHALL từ chối với error `terminal_status` và SHALL NOT gọi API.
14. WHEN user request `email delete email=Y` (phân biệt với `deactivate`), THE HME_Manager SHALL trích xuất Session_Bundle, gọi `POST /v1/hme/delete` body `{anonymousId}` qua HTTP client, update `icloud_emails.status='deleted'` cùng `deleted_at = now()`, ghi audit event `email_delete`. Trạng thái `deleted` là terminal — THE HME_Manager SHALL từ chối lệnh `reactivate` hoặc `deactivate` cho email đã ở `deleted`.
15. IF API `delete` trả 404 hoặc errorMessage chứa marker case-insensitive `not found`, THEN THE HME_Manager SHALL coi email đã bị xoá ngoài tool, vẫn UPDATE `icloud_emails.status='deleted'` cùng `deleted_at = now()`, ghi audit event `email_delete` với reason `already_deleted_remote`.
16. WHEN user request `email update-meta email=Y --label L --note N`, THE HME_Manager SHALL trích xuất Session_Bundle, gọi `POST /v1/hme/updateMetaData` body `{anonymousId, label, note}` qua HTTP client, update `icloud_emails.label = L` và `icloud_emails.note = N`, ghi audit event `email_update_meta` với payload `{apple_id, email, label_old, label_new, note_old, note_new}`.
17. THE HME_Manager SHALL hỗ trợ tất cả các action `deactivate / reactivate / delete / update-meta` có biến thể bulk: `--bulk emails=[...]`, `--by-label LABEL`, `--by-date YYYYMMDD`. Bulk SHALL áp dụng cùng cơ chế group-by-apple_id + reuse Session_Bundle cached + delay `[1.0, 3.0]` giây như Requirement 9.7.
18. THE HME_Manager SHALL hỗ trợ flag `--dry-run` cho TẤT CẢ biến thể của tất cả 4 action (`deactivate`, `reactivate`, `delete`, `update-meta`); WHERE `--dry-run` bật, semantic giống Requirement 9.11 — chỉ trả list email sẽ bị tác động, SHALL NOT gọi HME_API_Endpoint, SHALL NOT update DB, SHALL NOT ghi audit event tương ứng.
19. WHEN user request `email mark-used email=Y --used-for chatgpt`, THE HME_Manager SHALL update `icloud_emails.status='used_for_chatgpt'` và `icloud_emails.used_for_email = Y` (giữ tương thích cột v5), ghi audit event `email_mark_used` với payload `{email, used_for}`. Action này SHALL NOT gọi API Apple — chỉ thay đổi DB-side.
20. WHEN user request `email export --format csv|json [--filter ...]`, THE HME_Manager SHALL query `icloud_emails` theo filter (`status`, `apple_id`, `label`, `date_range`), serialize ra CSV hoặc JSON với schema `{email, apple_id, label, note, hme_id, status, used_for_email, created_at, deactivated_at, reactivated_at, deleted_at, last_sync_at}`, ghi file ra path do user truyền (hoặc stdout cho CSV), và ghi audit event `email_export` với payload `{count, format, filter}`.

---

### Requirement 10: Web UI + API quản lý HME pool — phase sau MVP

**User Story:** Là user, tôi muốn web UI có 1 tab `HME` mới để quản lý profile, job tạo email, danh sách email full-feature (list rộng, multi-select, action menu, export, filter), để không phải mở terminal cho thao tác hằng ngày.

#### Acceptance Criteria

1. THE Web_API SHALL expose `GET /api/icloud/pool/status` trả về cấu trúc giống Requirement 7.
2. THE Web_API SHALL expose `GET /api/icloud/profiles` với query `?status=active|limited|...` trả list profile.
3. THE Web_API SHALL expose `POST /api/icloud/profiles/{apple_id}/check` chạy Profile_Checker và trả `CheckResult`.
4. THE Web_API SHALL expose `DELETE /api/icloud/profiles/{apple_id}` chạy Pool_Manager.delete_profile.
5. THE Web_API SHALL expose `GET /api/icloud/emails` với query `?status=&apple_id=&limit=` trả list email.
6. THE Web_API SHALL expose `POST /api/icloud/emails/generate` body `{count, label, note}` tạo job async qua JobManager (Requirement 13) và trả `{job_id}`; tiến độ stream qua endpoint SSE `GET /api/icloud/jobs/{job_id}/log/stream` (Requirement 10.17).
7. THE Web_API SHALL expose `DELETE /api/icloud/emails` body `{emails: [...]}` chạy `HME_Manager.deactivate_bulk` (đổi semantic từ revoke sang deactivate theo Requirement 9); WHERE query string chứa `?dry_run=true`, THE Web_API SHALL áp dụng dry-run theo Requirement 9.18.
8. THE Web_API SHALL expose `POST /api/icloud/recording/start` body `{apple_id, scenario}` trả `{session_id, recording_dir}` và `POST /api/icloud/recording/{session_id}/stop` để dừng.
9. THE Web_API SHALL expose `GET /api/icloud/audit` với filter `?apple_id=&event_type=&since=&limit=` trả Audit_Log.
10. THE Web_API SHALL gắn middleware auth dùng chung cho toàn bộ `/api/*` (đã có sẵn ở `web/auth.py`). Token chấp nhận qua: header `X-API-Token: <token>`, query string `?token=<token>`, hoặc cookie `gsh_token=<token>`. Token nguồn từ env `GPT_SIGNUP_WEB_TOKEN` (nếu set) hoặc auto-sinh ngẫu nhiên 1 lần per-process khi server start. Mọi endpoint `/api/icloud/*` SHALL dùng cùng middleware này — KHÔNG tách scheme auth riêng cho icloud (tránh 2 token store khác nhau, khó vận hành). Khi token sai/thiếu → HTTP 401 với body `{"detail": "missing or invalid auth token"}` + header `WWW-Authenticate: Token`.
10a. WHERE biến môi trường `GPT_SIGNUP_WEB_TOKEN` KHÔNG được set, THE Web_API SHALL auto-sinh token random per-process (loopback bind default) — token này được inject vào HTML qua `<meta name="auth-token">` cho UI lấy. Server bind 127.0.0.1 default → an toàn cho local dev. WHEN user opt-in non-loopback bind (multi-host deploy), user PHẢI set `GPT_SIGNUP_WEB_TOKEN` thủ công để stable token cross-restart.
11. THE Web_API SHALL expose `DELETE /api/icloud/emails/by-label/{label}` chạy `HME_Manager.deactivate_by_label(label)` (tương đương Requirement 9.9); WHERE query string chứa `?dry_run=true`, THE Web_API SHALL chỉ return list email sẽ bị deactivate mà không gọi API Apple (theo Requirement 9.18).
12. THE Web_UI SHALL expose 1 tab `HME` ở navigation chính của web app hiện có; tab này gồm 3 sub-page: `Profiles`, `Jobs`, `Emails`.
13. THE `Profiles` page SHALL hiển thị bảng profile với cột `apple_id`, `status` (badge), `hme_count`, `quota_remaining`, `last_used_at`, `limited_until`, `last_error`. THE page SHALL hỗ trợ action per-row: `Open` (mở Camoufox headed với `profile_dir` để user thao tác manual qua Bootstrap_Flow), `Bootstrap` (re-bootstrap khi `session_expired`), `Check` (run Profile_Checker với `--auto-mark`), `Delete` (xoá profile theo Requirement 5). THE page SHALL hỗ trợ button toolbar `+ Thêm profile` mở Camoufox headed qua flow Add_Profile (Requirement 14): backend launch browser ngay, UI hiển thị dialog với 2 nút `Lưu` / `Huỷ`. User login Apple ID + 2FA xong bấm `Lưu` → backend close browser, extract Apple_ID từ profile, persist vào `icloud_accounts`. Bấm `Huỷ` → backend close browser ngay + xoá profile_dir tạm. Toolbar SHALL NOT cho mở 2 dialog Add_Profile cùng lúc (per process).
14. THE `Jobs` page SHALL hiển thị bảng job với cột `job_id`, `kind`, `status` (badge), `progress` (`progress_done / progress_total`), `started_at`, `ended_at`, `apple_id_filter`, `label_filter`. THE page SHALL hỗ trợ action per-row: `Start`, `Stop`, `Pause`, `Resume`, `Restart`, `View Log` (mở side-drawer streaming log SSE realtime theo Requirement 10.17).
15. THE `Emails` page SHALL hiển thị bảng email full-width responsive (chiếm 100% width content area) với cột `email`, `apple_id`, `label`, `status` (badge), `created_at`, `deactivated_at`, `reactivated_at`, `deleted_at`, `used_for_email`, `hme_id`, `note`, `last_sync_at`. THE page SHALL hỗ trợ filter bar gồm `status` (multi-select), `apple_id` (select), `label` (text/regex), `date_range` (from-to). THE page SHALL hỗ trợ multi-select rows (checkbox cột đầu) + action toolbar bulk `Deactivate selected`, `Reactivate selected`, `Delete selected`, `Mark used for ChatGPT`, `Export selected (CSV/JSON)`. THE page SHALL hỗ trợ action per-row `Deactivate`, `Reactivate`, `Delete`, `Update label/note` (dialog inline), `View detail` (drawer hiển thị full audit log của email).
16. THE Web_API SHALL expose endpoint mới cho lifecycle email tương ứng Requirement 9:
    - `POST /api/icloud/emails/{email}/deactivate` (query `?dry_run=true|false`)
    - `POST /api/icloud/emails/{email}/reactivate`
    - `POST /api/icloud/emails/{email}/delete` (dùng POST thay vì DELETE để có body cho `?dry_run`)
    - `PATCH /api/icloud/emails/{email}` body `{label?, note?, used_for_email?}` (`label` + `note` đẩy qua `updateMetaData`; `used_for_email` chỉ DB-side theo Requirement 9.19)
    - `POST /api/icloud/emails/list-sync` body `{apple_id}` chạy `HME_Manager.list_sync` async qua JobManager và return `{job_id}`
    - `POST /api/icloud/emails/export` body `{format, filter}` return file download (CSV / JSON theo Requirement 9.20)
17. THE Web_API SHALL expose endpoint mới cho job management (mapping Requirement 13):
    - `GET /api/icloud/jobs` query `?kind=&status=&apple_id=&label=&since=&limit=` list job
    - `GET /api/icloud/jobs/{job_id}` detail 1 job
    - `POST /api/icloud/jobs/{job_id}/stop`
    - `POST /api/icloud/jobs/{job_id}/pause`
    - `POST /api/icloud/jobs/{job_id}/resume`
    - `POST /api/icloud/jobs/{job_id}/restart` return `{new_job_id}`
    - `GET /api/icloud/jobs/{job_id}/log/stream` SSE streaming log realtime
18. THE Web_UI SHALL áp dụng cùng auth pattern với Requirement 10.10: gửi header `X-API-Token: <token>` cho mọi request đến `/api/icloud/*`. Token được lấy qua helper `window.GptUi.getAuthToken()` (đọc từ `<meta name="auth-token">` server-inject HOẶC localStorage HOẶC URL `?token=`).
19. THE Web_UI Page Jobs SHALL có button toolbar `Stop All Generate Jobs` và `Stop All Jobs` gọi tương ứng `POST /api/icloud/jobs/stop-all?kind=generate` và `POST /api/icloud/jobs/stop-all` (Requirement 13.17). WHEN user click 1 trong 2 button, THE Web_UI SHALL hiển thị confirm dialog liệt kê `job_id`, `kind`, `status` của các job sẽ bị stop trước khi confirm; SHALL NOT gọi API stop-all nếu user huỷ confirm.
20. THE Web_UI Page Jobs SHALL có form `+ New Generate Job` gồm 2 mode loại trừ lẫn nhau:
    - Mode "Bounded" — input `count` (integer ≥ 1) + `label?` + `note?` → `POST /api/icloud/emails/generate` body `{count, label, note}`.
    - Mode "Infinite" — chỉ input `label?` + `note?` (không có `count`) → `POST /api/icloud/emails/generate` body `{infinite: true, label, note, count: null}` (theo Requirement 13.16).
21. WHEN job ở Infinite_Generate_Mode đang trong Pool_Exhausted_Wait, THE Web_UI Page Jobs row SHALL hiển thị badge phụ màu vàng `waiting until HH:MM (UTC)` cạnh badge `status='running'`. Giá trị `HH:MM` SHALL lấy từ `result_json.waiting_until` (do HME_Generator update theo Requirement 3.23 / 3.24).
22. THE Web_UI Page Profiles SHALL hiển thị profile có `Profile_Status = 'quota_full'` với badge màu cam phân biệt với `limited` (vàng) và `session_expired` (đỏ), kèm tooltip hiển thị `quota_retry_until` (định dạng ISO UTC). THE page SHALL áp dụng sort default theo thứ tự ưu tiên: `active` → `limited` → `quota_full` → `session_expired` → `disabled` → `deleted`.

---

### Requirement 11: HME API contract

**User Story:** Là dev triển khai HmeClient, tôi muốn contract API Apple được định nghĩa rõ trong requirements để implement đúng và phân loại lỗi đúng cách.

#### Acceptance Criteria

1. THE HmeClient SHALL gọi 3 endpoint chính xác: `POST https://p68-maildomainws.icloud.com/v1/hme/generate` (sinh candidate), `POST https://p68-maildomainws.icloud.com/v1/hme/reserve` (chốt candidate), `GET https://p68-maildomainws.icloud.com/v2/hme/list` (liệt kê email). THE host SHALL hardcode `p68-maildomainws.icloud.com` cho mọi profile vì Apple HME REST API chỉ phục vụ trên host này (verified với rtunazzz/hidemyemail-generator + nội bộ `test/check_hme_minimal_call.py` — host `p43`/`p109` từ `/setup/ws/1/validate.webservices` chỉ phục vụ setup endpoint, KHÔNG phục vụ HME API).
2. THE HmeClient SHALL gửi 4 query param bắt buộc trên mọi request: `clientBuildNumber`, `clientMasteringNumber`, `clientId`, `dsid`. THE `clientBuildNumber` + `clientMasteringNumber` SHALL hardcode value từ webapp Apple hiện hành (vd `2536Project32` / `2536B20`). THE `clientId` + `dsid` SHALL là chuỗi rỗng — Apple HME API KHÔNG enforce auth qua 2 query param này khi cookies hợp lệ; auth thực qua cookie `X-APPLE-WEBAUTH-*` trong Cookie header.
3. THE HmeClient SHALL nhận Session_Bundle (theo Requirement 2a) làm tham số khởi tạo và SHALL NOT nhận Page object / BrowserContext, để HmeClient có thể test riêng độc lập với Camoufox.
4. THE HmeClient SHALL gọi HME_API_Endpoint qua HTTP client `httpx.AsyncClient` với cookies từ Session_Bundle, KHÔNG mở Camoufox để gọi API; việc trích xuất Session_Bundle là trách nhiệm của caller (HME_Generator / Profile_Checker / HME_Manager) trước khi tạo HmeClient.
5. THE HmeClient SHALL set request headers cố định: `Origin: https://www.icloud.com`, `Referer: https://www.icloud.com/`, `Content-Type: text/plain`, `Accept: */*`, `User-Agent` = chuỗi placeholder Chrome 141 (`Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36`). THE HmeClient SHALL NOT gửi header `scnt` hoặc `X-Apple-ID-Session-Id` — Apple HME API KHÔNG enforce 2 header này; auth duy nhất qua cookies.
6. WHEN HmeClient nhận response từ HME_API_Endpoint, THE HmeClient SHALL parse theo schema `{success: bool, result: object, error: int|dict, reason: string}` và phân loại lỗi như sau khi `success != true`:
   - IF `error` là dict và `error.errorMessage` chứa marker rate-limit (case-insensitive: `rate limit`, `too many`, `limit reached`, `quota`), THEN THE HmeClient SHALL raise `HmeQuotaError`.
   - IF HTTP status thuộc tập `{401, 421, 440}`, THEN THE HmeClient SHALL raise `HmeAuthError`.
   - IF HTTP status là `429`, THEN THE HmeClient SHALL raise `HmeQuotaError`.
   - IF `error.errorMessage` chứa marker case-insensitive `unauthorized`, `not authenticated`, hoặc `session expired`, THEN THE HmeClient SHALL raise `HmeAuthError`.
   - IF `error` là số nguyên (lỗi tool-side: timeout/network), THEN THE HmeClient SHALL raise `HmeTransientError` để caller có thể retry trong cùng profile.
7. WHERE biến môi trường `ICLOUD_HME_HTTP_TIMEOUT_SEC` được set với giá trị nguyên dương, THE HmeClient SHALL áp dụng giá trị đó làm timeout cho mỗi request HME_API_Endpoint; ngược lại THE HmeClient SHALL áp dụng default 30 giây.
8. THE HmeClient SHALL dùng `httpx.AsyncClient` làm HTTP client, SHALL NOT dùng `requests` hay `aiohttp`, để giữ dependency đồng nhất với hotmail flow đã có sẵn `httpx` trong project; THE HmeClient SHALL set cookies từ `Session_Bundle.cookies` vào `client.cookies` (cookiejar) với domain `.icloud.com` để httpx tự gắn header `Cookie:` cho mọi request, SHALL NOT serialize cookie thành chuỗi và paste vào header `Cookie:` thủ công.

---

### Requirement 13: Job management — phase sau MVP

**User Story:** Là user, tôi muốn auto-create / auto-deactivate / auto-delete email được quản lý dạng Job có lifecycle (start/stop/pause/resume/restart) với log realtime, để vận hành trên web UI giống các tool DevOps hiện có (Outlook signup pool đã có pattern Job tương tự).

#### Acceptance Criteria

1. THE Job entity SHALL persistent trong bảng `icloud_jobs` với schema `{job_id (uuid), kind, status, progress_done, progress_total, params_json, result_json, started_at, ended_at, updated_at, parent_job_id, apple_id_filter, label_filter}`. `parent_job_id` SHALL trỏ đến job gốc khi được tạo qua action `restart`; `NULL` cho job tạo mới.
2. THE Job_Status SHALL ∈ tập `{queued, running, paused, completed, failed, cancelled}`. Transition hợp lệ:
   - `queued → running` (qua action `start`, hoặc auto khi worker pick)
   - `queued → cancelled` (qua action `stop` trước khi worker pick — user huỷ queue)
   - `running → paused` (qua action `pause`)
   - `paused → running` (qua action `resume`)
   - `running → completed` (auto khi handler xử lý xong toàn bộ unit work)
   - `running → failed` (auto khi handler raise exception không recover)
   - `running → cancelled` (qua action `stop`)
   - `paused → cancelled` (qua action `stop`)
3. THE Job SHALL hỗ trợ `kind` thuộc tập: `generate`, `deactivate_bulk`, `reactivate_bulk`, `delete_bulk`, `list_sync`, `bootstrap`, `check_all`, `update_meta_bulk`, `export`. Mỗi kind SHALL có handler async riêng đặt trong `icloud_hme/jobs/<kind>.py`.
4. WHEN user gọi `POST /api/icloud/emails/generate` (Requirement 10.6), THE JobManager SHALL tạo row job mới với `kind='generate'`, `status='queued'`, enqueue vào worker queue (asyncio.Queue / db-poll), và return `{job_id}` ngay (SHALL NOT block HTTP request đợi xử lý xong).
5. WHILE job ở `status='running'`, THE Job handler SHALL append log entry vào file `runtime/icloud_jobs/<job_id>/log.jsonl` với schema `{timestamp_iso, level, message, payload}` và `level ∈ {debug, info, warn, error}`. Log file là source of truth cho SSE stream của Requirement 10.17.
6. WHEN user gọi action `stop` trên 1 job, THE JobManager SHALL set `cancellation_event.set()` để handler check sau mỗi unit work (1 email). Handler SHALL hoàn tất unit hiện tại nếu Apple đã trả 200 (commit DB của unit đó), sau đó set `status='cancelled'`, ghi `result_json` partial, và ghi audit event `job_cancelled`.
7. WHEN user gọi action `pause` trên 1 job, THE JobManager SHALL set `pause_event.set()` để handler kiểm tra trước mỗi unit work; IF `pause_event` set, THEN handler SHALL chuyển `status='paused'`, ghi audit event `job_paused`, và `await pause_event.wait()` cho đến khi `resume_event.set()`. WHEN action `resume` được gọi, THE JobManager SHALL set `resume_event` để handler tiếp tục, chuyển `status='running'`, và ghi audit event `job_resumed`.
8. WHEN user gọi action `restart` trên 1 job, THE JobManager SHALL clone `params_json` sang job mới với `parent_job_id = old_job_id`, set `status='queued'` cho job mới, enqueue, và return `{new_job_id}`. Job cũ SHALL NOT bị thay đổi.
9. THE JobManager SHALL hỗ trợ tối đa N job ở `status='running'` đồng thời, với N lấy từ env `ICLOUD_JOB_MAX_PARALLEL` (default 1); IF số job `running` ≥ N, THEN job mới SHALL chờ slot ở `status='queued'`. Default 1 để tránh trigger rate-limit Apple đa profile cùng lúc; user có thể tăng khi pool có nhiều profile.
10. WHEN process crash hoặc restart trong khi 1 job đang `running`, THE JobManager SHALL detect job `running` không có tiến triển (`updated_at < now() - 5 phút`) lúc khởi động lại, mark `status='failed'` với `result_json.reason = 'process_crashed'`, ghi audit event `job_failed`, và SHALL NOT auto-resume — user phải gọi action `restart` thủ công.
11. THE JobManager SHALL hỗ trợ filter list job theo `kind`, `status`, `started_at` range, `apple_id_filter`, `label_filter` — tương ứng với query params của `GET /api/icloud/jobs` (Requirement 10.17).
12. THE Job log retention SHALL điều khiển bởi env `ICLOUD_JOB_LOG_RETENTION_DAYS` (default 30 ngày); THE JobManager SHALL cung cấp command CLI `job cleanup` xoá file log + row job có `ended_at < now() - N ngày` AND `status ∈ {completed, failed, cancelled}`.
13. WHEN handler hoàn tất 1 unit work, THE handler SHALL increment `icloud_jobs.progress_done` và update `icloud_jobs.updated_at = now()` trong cùng DB transaction với thay đổi DB của unit (INSERT/UPDATE `icloud_emails` + audit event), để đảm bảo crash recovery (Requirement 13.10) phát hiện job stuck chính xác.
14. WHEN job chuyển sang `status ∈ {completed, failed, cancelled}`, THE JobManager SHALL ghi audit event tương ứng (`job_completed`, `job_failed`, `job_cancelled`) với payload `{job_id, kind, progress_done, progress_total, duration_seconds}`.
15. THE Job entity SHALL hỗ trợ field `infinite: bool` trong `params_json` cho `kind='generate'`. WHEN `params_json.infinite = true`, THE handler SHALL chạy theo Infinite_Generate_Mode (Requirement 3.20–3.26); WHEN `params_json.infinite = false` hoặc không set, THE handler SHALL chạy bounded mode với `params_json.count` đã có.
16. WHEN UI Web gọi `POST /api/icloud/emails/generate` với body chứa `infinite: true`, THE Web_API SHALL set `params_json.infinite = true` và `params_json.count = null` cho row job mới. THE Web_API SHALL reject với HTTP 400 nếu body có cả `count > 0` lẫn `infinite = true` (loại trừ lẫn nhau), error message `count_and_infinite_mutually_exclusive`.
17. WHEN user gọi `POST /api/icloud/jobs/stop-all`, THE JobManager SHALL set `cancellation_event` cho TẤT CẢ job có `status ∈ {running, paused}`. Endpoint SHALL trả `{stopped_count}` ngay (không block đợi job thực sự transition); transition thực tế (`running|paused → cancelled`) qua audit event `job_cancelled` cho từng job theo Requirement 13.6. WHERE query `?kind=generate` được set, THE JobManager SHALL chỉ stop job có `kind='generate'` để không stop list_sync / check_all / bootstrap đang chạy. WHERE query `?kind` không set, THE JobManager SHALL stop tất cả job thuộc mọi kind. THE `stopped_count` SHALL là **best-effort count tại thời điểm SELECT** — có thể overcounting nhỏ nếu 1 job đang transition tự nhiên (`running → completed/failed`) đồng thời với SELECT, nhưng transition thực tế (`running|paused → cancelled`) vẫn qua audit event `job_cancelled` cho từng job theo Requirement 13.6 — caller SHALL coi `stopped_count` là estimate, không phải exact count.

---

### Requirement 14: Web Add Profile interactive flow — phase sau MVP

**User Story:** Là user vận hành pool qua Web UI, tôi muốn thêm 1 Apple_ID mới bằng cách bấm nút "Thêm Profile", tool tự mở Camoufox headed cho tôi login + 2FA, sau đó tôi bấm "Lưu" để commit profile hoặc "Huỷ" để bỏ và xoá sạch profile_dir đang ghi — không phải mở terminal CLI để chạy `bootstrap`.

#### Acceptance Criteria

1. THE Web_UI Page Profiles SHALL có button toolbar `+ Thêm Profile` mở dialog với:
   - 1 input field `apple_id` (email format) bắt buộc.
   - 1 input field `proxy` (URL format `http(s)://[user:pass@]host:port`) optional.
   - 2 button submit: `Mở Camoufox` (primary, gửi POST khởi tạo session), `Huỷ` (đóng dialog không gửi request).
2. WHEN user click `Mở Camoufox`, THE Web_API SHALL expose endpoint `POST /api/icloud/profiles/add/start` body `{apple_id, proxy?}` — endpoint SHALL:
   - Validate `apple_id` đúng email format và `proxy` (nếu có) đúng URL format; IF fail validation, THEN SHALL return HTTP 400 với body `{error: "invalid_input", field, reason}`.
   - Tạo 1 `AddProfileSession` mới với `session_id` (uuid4), persist in-memory state `{session_id, apple_id, proxy, status='launching', started_at, save_event: asyncio.Event, cancel_event: asyncio.Event}`.
   - Spawn 1 background task gọi `Bootstrap_Flow.bootstrap_interactive(apple_id, proxy, save_event, cancel_event, runtime_dir, pool_repo, audit_repo)` — hàm mới (Requirement 14.5).
   - Return ngay HTTP 202 với body `{session_id, status='launching'}` — KHÔNG block HTTP request đợi user thao tác xong.
3. THE Web_API SHALL expose endpoint `POST /api/icloud/profiles/add/{session_id}/save` để user trigger commit profile — endpoint SHALL:
   - Lookup `AddProfileSession` theo `session_id`; IF không tồn tại, THEN return HTTP 404 với body `{error: "session_not_found"}`.
   - IF `status NOT IN {browser_open, verifying}`, THEN return HTTP 409 với body `{error: "invalid_state", current_status}` (vd đã `saved` / `cancelled` / `failed`).
   - Set `save_event.set()` để background task tiếp tục flow verify cookies + persist DB.
   - Return HTTP 200 với body `{session_id, status='verifying'}` ngay sau khi set event (KHÔNG block đợi verify xong).
4. THE Web_API SHALL expose endpoint `POST /api/icloud/profiles/add/{session_id}/cancel` để user huỷ flow — endpoint SHALL:
   - Lookup `AddProfileSession`; IF không tồn tại, THEN return HTTP 404.
   - IF `status IN {saved, cancelled, failed}`, THEN return HTTP 409 với body `{error: "invalid_state", current_status}` (đã terminal).
   - Set `cancel_event.set()` để background task: (a) đóng Camoufox ngay (không đợi flush profile state), (b) xoá thư mục `profile_dir` đang ghi nếu đó là Apple_ID NEW (chưa từng có row trong `icloud_accounts` trước session này), (c) ghi audit event `profile_add_cancel` với payload `{session_id, apple_id, reason='user_cancel', removed_profile_dir: bool}`, (d) transition `status='cancelled'`.
   - Return HTTP 200 với body `{session_id, status='cancelling'}` ngay sau khi set event.
5. THE Bootstrap_Flow SHALL expose 1 hàm mới `async def bootstrap_interactive(apple_id, *, runtime_dir, pool_repo, audit_repo, save_event: asyncio.Event, cancel_event: asyncio.Event, proxy=None, log) -> BootstrapResult`. Hàm này KHÔNG dùng `_wait_for_enter` (terminal stdin) như `bootstrap()` MVP — thay bằng `await asyncio.wait([save_event.wait(), cancel_event.wait()], return_when=FIRST_COMPLETED)`. Behavior cụ thể:
   - Acquire `Profile_Lock` mode `write` (giống Requirement 12.14). IF lock conflict, THEN raise `BootstrapError(reason='profile_locked_by_another_process')` + audit `profile_add_fail` reason `profile_locked_by_another_process`, transition session `status='failed'`.
   - Launch Camoufox headed với `profile_dir`, navigate `https://www.icloud.com/mail/`, audit `profile_add_start`, transition session `status='browser_open'`.
   - Await `save_event` HOẶC `cancel_event` (không có timeout cứng — user tự quyết khi nào lưu/huỷ; xem Requirement 14.7 cho TTL).
   - WHEN `save_event` set TRƯỚC: transition `status='verifying'`, verify cookies `X-APPLE-WEBAUTH-*` (giống `_has_login_cookies`); IF verify pass, THEN persist DB atomic (giống `_persist_bootstrap_atomic` — upsert + reset status='active' + audit `profile_bootstrap`), đóng Camoufox, audit `profile_add_success`, transition `status='saved'`. IF verify fail, THEN audit `profile_add_fail` reason `cookies_not_ready`, đóng Camoufox, transition `status='failed'`. SHALL NOT retry tự động — user phải bấm `Huỷ` rồi `Mở Camoufox` lại để bắt đầu session mới.
   - WHEN `cancel_event` set TRƯỚC: đóng Camoufox NGAY (close context bypass flush), xoá `profile_dir` nếu Apple_ID là NEW (Requirement 14.4 case b), audit `profile_add_cancel`, transition `status='cancelled'`.
6. THE `AddProfileSession` SHALL có lifecycle status enum: `launching` (đang acquire lock + boot Camoufox) → `browser_open` (Camoufox đã navigate xong, đợi user) → `verifying` (user bấm Lưu, đang check cookies + persist DB) → `saved` (terminal success) | `cancelled` (terminal user-cancel) | `failed` (terminal error). Transition hợp lệ:
   - `launching → browser_open` (auto khi Camoufox sẵn sàng)
   - `launching → failed` (auto khi lock conflict / Camoufox launch fail)
   - `browser_open → verifying` (qua `save` action)
   - `browser_open → cancelled` (qua `cancel` action, hoặc TTL expire theo Requirement 14.7)
   - `verifying → saved` (auto khi cookies verify pass + DB persist OK)
   - `verifying → failed` (auto khi cookies verify fail)
   - `verifying → cancelled` (qua `cancel` action giữa lúc verify — race rare; cancel SHALL win, rollback DB nếu chưa commit)
7. WHERE biến môi trường `ICLOUD_ADD_PROFILE_TTL_MINUTES` được set với giá trị nguyên dương N, THE AddProfileSession SHALL auto-cancel sau N phút ở `status='browser_open'` (user không bấm Lưu/Huỷ). Default N = 30 phút. WHEN auto-cancel TTL trigger, THE Bootstrap_Flow SHALL set `cancel_event` nội bộ và transition `status='cancelled'` với audit event `profile_add_timeout` (KHÁC `profile_add_cancel` để distinguish nguồn gốc trigger), payload include `expired_after_sec`.
8. THE Web_API SHALL expose endpoint `GET /api/icloud/profiles/add/{session_id}/status` để Web_UI poll progress — endpoint SHALL return body `{session_id, apple_id, status, started_at, ended_at_or_null, error_or_null, profile_dir_or_null}`. THE Web_UI SHALL poll mỗi 1 giây WHILE `status NOT IN {saved, cancelled, failed}` để cập nhật UI realtime; khi hit terminal status, THE Web_UI SHALL stop poll và hiển thị kết quả.
9. THE Web_UI SHALL hiển thị dialog `Add Profile Progress` sau khi click `Mở Camoufox` với 2 button:
   - `Lưu` (primary, enabled khi `status='browser_open'`, disabled khi `status IN {launching, verifying, saved, cancelled, failed}`) — gọi `POST /api/icloud/profiles/add/{session_id}/save`.
   - `Huỷ` (secondary, enabled khi `status NOT IN {saved, cancelled, failed}`) — gọi `POST /api/icloud/profiles/add/{session_id}/cancel`.
   - Dialog SHALL có message hướng dẫn theo từng status: `launching` → "Đang mở Camoufox..."; `browser_open` → "Camoufox đã mở. Login Apple ID + 2FA xong rồi bấm Lưu, hoặc Huỷ để bỏ."; `verifying` → "Đang kiểm tra cookies..."; `saved` → "Đã lưu profile thành công."; `cancelled` → "Đã huỷ. Profile chưa được lưu."; `failed` → "Lỗi: {error}".
10. THE Audit_Log SHALL hỗ trợ thêm 5 event_type mới cho nhánh Add Profile (xem Requirement 6.2 để có payload schema chi tiết): `profile_add_start` (khi launch session, audit ngay sau khi acquire write lock thành công), `profile_add_success` (success terminal sau persist DB), `profile_add_cancel` (user-cancel terminal), `profile_add_timeout` (TTL expire terminal — Requirement 14.7), `profile_add_fail` (error terminal — lock conflict / Camoufox launch fail / verify cookies fail / persist DB fail).
11. THE AddProfileSession registry SHALL ephemeral in-memory (dict `{session_id: AddProfileSession}` trong process scope) — KHÔNG persist xuống DB vì lifecycle ngắn (≤ TTL phút) và tied vào running browser instance. WHEN process restart giữa lúc có session đang `browser_open`, THE Web_API SHALL coi mọi session in-memory là mất và Camoufox instance dangling SHALL được dọn lúc startup bằng cách: (a) scan thư mục `runtime/icloud_profiles/<safe_apple_id>/` có file marker `.add_profile_session.json` (do `bootstrap_interactive` tạo lúc launch), (b) với mỗi marker, IF marker ghi `status NOT IN {saved}`, THEN xoá `profile_dir` (NEW Apple_ID) hoặc giữ nguyên (Apple_ID đã từng có row), audit `profile_add_fail` reason `process_crashed_during_session`. Marker file SHALL bị xoá khi session transition terminal status `saved`.
12. WHEN Apple_ID đã từng có row trong `icloud_accounts` (re-bootstrap, vd profile bị `session_expired`), THE Bootstrap_Flow SHALL chấp nhận flow này như re-bootstrap (giống behavior Requirement 12.10) — `cancel` SHALL NOT xoá `profile_dir` (vì là profile cũ user đang có), chỉ đóng Camoufox và transition `cancelled`. Audit event `profile_add_cancel` SHALL include payload `removed_profile_dir: false` để distinguish với case NEW Apple_ID.
13. THE Web_API SHALL bảo vệ tất cả endpoint trong Requirement 14 (`/api/icloud/profiles/add/*`) bằng cùng middleware auth chung của tool (Requirement 10.10) — header `X-API-Token: <token>` (hoặc query `?token=` / cookie `gsh_token`). Token sai/thiếu → HTTP 401.
14. THE AddProfileSession SHALL chỉ cho phép TỐI ĐA 1 session active (`status NOT IN {saved, cancelled, failed}`) cho mỗi `apple_id` tại 1 thời điểm. WHEN user gọi `POST /api/icloud/profiles/add/start` cho `apple_id` đã có session active khác, THE Web_API SHALL return HTTP 409 với body `{error: "session_already_active", existing_session_id, existing_status}` — user phải save/cancel session cũ trước (hoặc đợi TTL expire).

---

### Requirement 14: Web flow Add Profile qua Camoufox headed (Add_Profile_Flow) — phase sau MVP

**User Story:** Là user, tôi muốn ấn nút `+ Thêm profile` trên web là Camoufox tự bật lên để tôi login Apple ID + 2FA tay; xong việc thì có 2 nút `Lưu` (chốt profile) hoặc `Huỷ` (bỏ, browser đóng + xoá profile đang ghi) — không phải gõ apple_id trước, không phải mở terminal.

#### Acceptance Criteria

1. WHEN user click button `+ Thêm profile` trên Page Profiles, THE Web_API SHALL accept request `POST /api/icloud/profiles/add/start` (body rỗng), tạo Add_Profile_Session mới với `session_id = uuid4()`, set state `recording`, tạo profile_dir tạm `runtime/icloud_profiles/.adding/<session_id>/`, launch Camoufox HEADED chỉ vào `https://www.icloud.com/`, ghi audit event `profile_add_start` với payload `{session_id, profile_dir, started_at}`, và return HTTP 200 body `{session_id, started_at, profile_dir}`. Web_API SHALL NOT block đợi user thao tác; Camoufox chạy nền dưới control của backend.
2. THE Web_UI SHALL hiển thị dialog modal sau khi `POST /api/icloud/profiles/add/start` trả 200, gồm:
   - Title: "Đang ghi profile mới".
   - Body: hướng dẫn user "Login Apple ID + 2FA trong cửa sổ Camoufox vừa mở. Khi đã đăng nhập xong và thấy iCloud webapp, bấm `Lưu`. Để huỷ và đóng browser, bấm `Huỷ`."
   - Button `Lưu` → gọi `POST /api/icloud/profiles/add/{session_id}/save`.
   - Button `Huỷ` → gọi `POST /api/icloud/profiles/add/{session_id}/cancel`.
   - Status indicator polling `GET /api/icloud/profiles/add/{session_id}/status` mỗi 2s để show state hiện tại.
3. WHEN user click `Lưu`, THE Web_API SHALL accept `POST /api/icloud/profiles/add/{session_id}/save`, transition session state `recording → saving`, và thực hiện chuỗi:
   - Đọc cookies từ Camoufox `BrowserContext.cookies('https://www.icloud.com/')`.
   - Extract `apple_id` từ cookie `X-APPLE-WEBAUTH-USER` (cùng pattern dsid_extract: split theo phần `email=...`, fallback parse `window.webAuth.dsInfo.appleId` qua `page.evaluate`).
   - Verify cookies bắt buộc `X-APPLE-WEBAUTH-PCS-Mail` + `X-APPLE-WEBAUTH-USER` đều có (login + 2FA hoàn tất).
   - Đóng Camoufox để flush state vào profile_dir tạm.
   - Rename profile_dir tạm `runtime/icloud_profiles/.adding/<session_id>/` → `runtime/icloud_profiles/<apple_id>/`.
   - Insert/upsert row `icloud_accounts(apple_id, profile_dir, status='active', hme_count=0)` qua `IcloudPoolRepository.upsert + update_status`.
   - Ghi audit event `profile_add_success` payload `{session_id, apple_id, profile_dir_final, duration_seconds}`.
   - Set state `saving → done`, return HTTP 200 body `{session_id, apple_id, status='active'}`.
4. IF lúc `save` không extract được `apple_id` (cookie thiếu / format không khớp), THEN THE Web_API SHALL transition `saving → failed`, đóng Camoufox, xoá profile_dir tạm, ghi audit `profile_add_fail` payload `{session_id, reason='apple_id_not_extractable'}`, và return HTTP 400 body `{error: 'apple_id_not_extractable', message, session_id}`. UI SHALL hiển thị thông báo lỗi và đóng dialog.
5. IF lúc `save` cookies bắt buộc thiếu (user chưa login xong / 2FA chưa qua), THEN THE Web_API SHALL transition `saving → failed`, đóng Camoufox, xoá profile_dir tạm, ghi audit `profile_add_fail` payload `{session_id, reason='cookies_not_ready'}`, và return HTTP 400 body `{error: 'cookies_not_ready', message: 'Hoàn tất login Apple ID + 2FA trước khi bấm Lưu', session_id}`.
6. IF lúc `save` extract được `apple_id = X` nhưng row `icloud_accounts.apple_id = X` đã tồn tại với `status ∈ {active, limited, quota_full, session_expired}` (chưa bị xoá), THEN THE Web_API SHALL transition `saving → failed`, đóng Camoufox, xoá profile_dir tạm (KHÔNG ghi đè profile_dir cũ), ghi audit `profile_add_fail` payload `{session_id, reason='apple_id_already_exists', apple_id: X}`, và return HTTP 409 body `{error: 'apple_id_already_exists', apple_id: X, session_id, message: 'Profile cho Apple ID này đã tồn tại. Dùng action Bootstrap để re-login thay vì thêm mới.'}`. WHERE row `icloud_accounts.apple_id = X` tồn tại với `status='deleted'`, THE Web_API SHALL coi đây là re-add hợp lệ — UPDATE row cũ về `status='active'`, set lại `profile_dir`, audit `profile_add_success` (không phải fail).
7. WHEN user click `Huỷ`, THE Web_API SHALL accept `POST /api/icloud/profiles/add/{session_id}/cancel`, transition session state từ `recording|saving` sang `cancelling`, đóng Camoufox NGAY (terminate browser process, không đợi flush), xoá thư mục profile_dir tạm `runtime/icloud_profiles/.adding/<session_id>/` (recursive), ghi audit event `profile_add_cancel` payload `{session_id, duration_seconds, reason: 'user_cancel'}`, transition `cancelling → cancelled`, và return HTTP 200 body `{session_id, status: 'cancelled'}`.
8. IF user đóng tab/dialog mà không bấm `Lưu` hoặc `Huỷ`, THEN THE Web_API SHALL coi session là zombie — sau timeout `ICLOUD_ADD_PROFILE_TIMEOUT_SEC` (default 1800 giây = 30 phút) tính từ `started_at`, server-side worker SHALL tự transition `recording → cancelling`, đóng Camoufox, xoá profile_dir tạm, ghi audit `profile_add_timeout` payload `{session_id, expired_after_sec}`, và transition sang `cancelled`.
9. THE Web_API SHALL expose `GET /api/icloud/profiles/add/{session_id}/status` trả về session state hiện tại với schema `{session_id, state ∈ {recording, saving, cancelling, done, cancelled, failed}, started_at, ended_at_or_null, apple_id_or_null, error_or_null, duration_seconds}`. UI SHALL dùng endpoint này để cập nhật dialog mỗi 2s.
10. THE Web_API SHALL enforce Add_Profile_Lock_Single: tại mọi thời điểm, nếu đã có Add_Profile_Session ở state ∈ `{recording, saving, cancelling}`, THEN `POST /api/icloud/profiles/add/start` SHALL return HTTP 409 body `{error: 'add_profile_in_progress', active_session_id, message: 'Hoàn tất hoặc huỷ session đang chạy trước khi bắt đầu session mới'}`. UI SHALL disable nút `+ Thêm profile` khi nhận 409 và hiển thị link đến dialog đang mở.
11. THE Web_API SHALL acquire NO `Profile_Lock` (theo Requirement 12.14) lúc launch Camoufox cho Add_Profile_Flow vì profile_dir tạm `runtime/icloud_profiles/.adding/<session_id>/` được isolate khỏi runtime profiles thật cho đến lúc `save` thành công. WHEN rename `<session_id>/` → `<apple_id>/` lúc save (Requirement 14.3), IF có Bootstrap_Flow / Recorder / extract_session_bundle khác đang giữ lock cho `apple_id` đó (rare — apple_id mới hiếm khi conflict), THEN rename SHALL chờ tối đa 5 giây + retry; vẫn fail thì transition `saving → failed`, audit `profile_add_fail` payload `{session_id, reason='move_failed', error}`, return HTTP 500.
12. WHEN process backend restart trong khi Add_Profile_Session đang ở state ∈ `{recording, saving, cancelling}`, THE Web_API SHALL detect zombie session lúc startup (in-memory state mất sau restart), tìm mọi thư mục `runtime/icloud_profiles/.adding/<*>/` còn sót, xoá chúng, ghi audit `profile_add_fail` payload `{session_id, reason='process_crashed'}` cho từng dir nếu có metadata, và SHALL NOT auto-resume — user phải tự bấm `+ Thêm profile` lại.
13. THE Web_API SHALL serve endpoint mới với path prefix `/api/icloud/profiles/add/*` và áp dụng cùng auth pattern (Requirement 10.10): require `Authorization: Bearer ${ICLOUD_API_AUTH_TOKEN}` header, return 401 nếu thiếu/sai.
14. THE Add_Profile_Flow SHALL không tích hợp với JobManager (Requirement 13) vì lifecycle ngắn (≤30 phút), in-memory, single-instance per process, và không cần SSE log realtime — dialog UI chỉ poll status 2s. Việc ghi audit qua `AuditLogRepository` trực tiếp đủ cho debug.
15. WHEN user thực hiện flow Add_Profile thành công (state `done`), THE Web_UI Page Profiles SHALL refresh table profile (re-fetch `GET /api/icloud/profiles`), hiển thị apple_id mới với badge `active`, đóng dialog modal, và hiện toast notification "Đã thêm profile {apple_id}".
16. WHERE biến môi trường `ICLOUD_ADD_PROFILE_TIMEOUT_SEC` được set với giá trị nguyên dương, THE Web_API SHALL áp dụng giá trị đó làm hard timeout cho Add_Profile_Session (Requirement 14.8); ngược lại default 1800 giây.

---

### Requirement 15: Web flow Open Profile qua Camoufox headed (Open_Profile_Flow) — phase sau MVP

**User Story:** Là user, tôi muốn ấn nút `Open` trên 1 row profile bất kỳ là Camoufox tự bật profile đó lên cho tôi xem trạng thái thật trên iCloud (đã login chưa, có lỗi gì, có 2FA bị bắt lại không); xong việc tôi có 2 nút `Lưu` (verify cookies + reactivate profile nếu đang `session_expired`) hoặc `Đóng` (chỉ đóng browser, không sửa DB) — không phải mở terminal chạy `bootstrap`.

#### Glossary bổ sung

- **Open_Profile_Flow**: flow web mở 1 profile EXISTING bằng Camoufox HEADED để user kiểm tra trạng thái session bằng mắt, hoặc đăng nhập lại khi `session_expired`. Khác `Bootstrap_Flow` (CLI, blocking, bắt buộc verify cookies) và `Add_Profile_Flow` (R14, web, profile mới): Open_Profile_Flow chạy trên `apple_id` ĐÃ TỒN TẠI trong DB, dùng đúng `profile_dir` thật `runtime/icloud_profiles/<apple_id>/`, hai nhánh kết thúc — **Save** (verify cookies → reset status='active' + audit `profile_reopen_save`) hoặc **Close** (đóng browser, KHÔNG đổi DB, audit `profile_reopen_close`).
- **Open_Profile_Session**: 1 lượt user chạy Open_Profile_Flow, định danh bằng `session_id` (uuid4) + `apple_id`. State machine: `opening` (đang launch Camoufox) → `open` (browser đã hiện, chờ user thao tác) → `saving | closing` → `saved | closed | failed`. Lifecycle in-memory; process restart → tự cleanup (browser process orphan kill khi backend exit).
- **Open_Profile_Lock_Single**: invariant per-process — chỉ tối đa 1 Open_Profile_Session ở state non-terminal cùng lúc trong cùng process. Lý do: Camoufox HEADED cần focus user, mở 2 cửa sổ song song UX kém + tốn RAM. UI nút `Open` SHALL bị disable trên mọi row khi có session đang chạy.
- **Open_Profile_Lock_Apple_ID**: bổ sung lên `Profile_Lock` (R12.14) — Open_Profile_Flow SHALL acquire `Profile_Lock` mode `write` (exclusive) trên `apple_id` đang mở để tránh conflict với Bootstrap_Flow / Recorder / extract_session_bundle (Generator/Checker/Manager) khác. Lock conflict → endpoint trả HTTP 409.

#### Acceptance Criteria

1. WHEN user click button `Open` trên row của 1 profile (apple_id = X) ở Page Profiles, THE Web_API SHALL accept request `POST /api/icloud/profiles/{apple_id}/open/start` (body rỗng), validate apple_id tồn tại trong DB + có `profile_dir` trên disk, tạo Open_Profile_Session mới với `session_id = uuid4()`, set state `opening`, acquire `Profile_Lock` mode `write` (timeout 5s) cho apple_id, launch Camoufox HEADED với `profile_dir` thật của apple_id, navigate `https://www.icloud.com/mail/`, ghi audit event `profile_reopen_start` với payload `{session_id, apple_id, profile_dir, started_at}`, transition `opening → open`, và return HTTP 200 body `{session_id, apple_id, started_at}`.
2. IF `apple_id` không tồn tại trong DB (HOẶC tồn tại nhưng `status = 'deleted'` hoặc `profile_dir IS NULL`), THEN THE Web_API SHALL return HTTP 404 body `{error: 'profile_not_found', apple_id, message: 'Profile không tồn tại hoặc đã xoá. Dùng + Add Profile để thêm mới.'}` và SHALL NOT launch Camoufox.
3. IF `Profile_Lock` mode `write` cho `apple_id` đã bị acquire bởi process / flow khác (Bootstrap_Flow / Recorder / Open_Profile_Flow khác), THEN THE Web_API SHALL return HTTP 409 body `{error: 'profile_locked', apple_id, message: 'Profile đang được dùng bởi flow khác (bootstrap / recorder / open). Đợi flow đó hoàn tất.'}` trong vòng 5 giây timeout, ghi audit `profile_reopen_fail` payload `{apple_id, reason='profile_locked'}`, và SHALL NOT launch Camoufox.
4. IF đã có Open_Profile_Session khác trong cùng process ở state non-terminal (`opening | open | saving | closing`), THEN THE Web_API SHALL return HTTP 409 body `{error: 'open_profile_in_progress', active_session_id, active_apple_id, message: 'Đã có profile khác đang mở. Đóng session đó trước.'}` (Open_Profile_Lock_Single).
5. THE Web_UI SHALL hiển thị dialog modal sau khi `POST /api/icloud/profiles/{apple_id}/open/start` trả 200, gồm:
   - Title: `Đang mở profile <apple_id>`.
   - Body: hướng dẫn user "Camoufox đã mở với profile của bạn. Kiểm tra trạng thái session — nếu Apple yêu cầu login lại / 2FA, hãy thực hiện trong cửa sổ Camoufox. Sau khi xong, bấm `Lưu` để verify + reactivate profile (nếu đang session_expired), hoặc `Đóng` để chỉ đóng browser không đổi DB."
   - Button `Lưu` → gọi `POST /api/icloud/profiles/{apple_id}/open/{session_id}/save`.
   - Button `Đóng` → gọi `POST /api/icloud/profiles/{apple_id}/open/{session_id}/close`.
   - Status indicator polling `GET /api/icloud/profiles/{apple_id}/open/{session_id}/status` mỗi 2s để show state hiện tại.
6. WHEN user click `Lưu`, THE Web_API SHALL accept `POST /api/icloud/profiles/{apple_id}/open/{session_id}/save`, transition state `open → saving`, và thực hiện chuỗi:
   - Đọc cookies từ Camoufox `BrowserContext.cookies('https://www.icloud.com/')`.
   - Verify có ÍT NHẤT 1 cookie marker thuộc set `{X-APPLE-WEBAUTH-USER, X-APPLE-WEBAUTH-TOKEN, X-APPLE-WEBAUTH-PCS-Mail}` (cùng tập Bootstrap_Flow R12.2 dùng, để đảm bảo session dùng được cho HME API về sau).
   - Đóng Camoufox để flush state vào profile_dir (KHÔNG xoá profile_dir, đây là profile thật).
   - Trong CÙNG 1 outer transaction: `IcloudPoolRepository.upsert(apple_id, profile_dir)` (refresh profile_dir trong row), `IcloudPoolRepository.update_status(apple_id, status='active', clear_error=True, clear_limited_until=True, clear_quota_retry_until=True)`, audit event `profile_reopen_save` payload `{session_id, apple_id, matched_cookies: sorted([...]), previous_status, duration_seconds}`. Decision audit event:
     - Nếu `previous_status ∈ {session_expired, disabled, limited, quota_full}` → audit thêm `profile_reactivate` cùng tx (cùng pattern Bootstrap_Flow R12.10).
     - Nếu `previous_status = 'active'` → chỉ ghi `profile_reopen_save`, KHÔNG ghi `profile_reactivate`.
   - Release `Profile_Lock` write.
   - Set state `saving → saved`, return HTTP 200 body `{session_id, apple_id, status: 'active', matched_cookies, previous_status}`.
7. IF lúc `save` cookies marker thiếu (user mở browser nhưng chưa login lại), THEN THE Web_API SHALL transition `saving → open` (recoverable — KHÔNG đóng browser, KHÔNG đổi DB), ghi audit `profile_reopen_fail` payload `{session_id, apple_id, reason='cookies_not_ready', recoverable: true}`, và return HTTP 400 body `{error: 'cookies_not_ready', message: 'Hoàn tất login Apple ID + 2FA trong Camoufox trước khi bấm Lưu', session_id, apple_id}`. UI SHALL hiển thị inline error trong dialog, KHÔNG đóng dialog, để user retry.
8. WHEN user click `Đóng`, THE Web_API SHALL accept `POST /api/icloud/profiles/{apple_id}/open/{session_id}/close`, transition state từ `open|saving` (idempotent từ saving) sang `closing`, đóng Camoufox NGAY (terminate browser process), KHÔNG sửa DB (giữ nguyên row + status), KHÔNG xoá profile_dir, ghi audit event `profile_reopen_close` payload `{session_id, apple_id, duration_seconds, reason: 'user_close'}`, release `Profile_Lock` write, transition `closing → closed`, và return HTTP 200 body `{session_id, apple_id, status: 'closed', previous_status_unchanged: true}`.
9. IF user đóng tab/dialog mà không bấm `Lưu` hoặc `Đóng`, THEN THE Web_API SHALL coi session là zombie — sau timeout `ICLOUD_OPEN_PROFILE_TIMEOUT_SEC` (default 1800 giây = 30 phút) tính từ `started_at`, server-side watchdog SHALL tự transition `open → closing`, đóng Camoufox, release `Profile_Lock`, ghi audit `profile_reopen_timeout` payload `{session_id, apple_id, expired_after_sec}`, transition sang `closed`. Behavior giống `Đóng` thường — KHÔNG sửa DB.
10. THE Web_API SHALL expose `GET /api/icloud/profiles/{apple_id}/open/{session_id}/status` trả về session state hiện tại với schema `{session_id, apple_id, state ∈ {opening, open, saving, closing, saved, closed, failed}, started_at, ended_at_or_null, error_or_null, error_reason_or_null, duration_seconds}`. UI SHALL dùng endpoint này để cập nhật dialog mỗi 2s. IF `session_id` không tồn tại trong active state lẫn FIFO terminal cache (cache size 32 entries giống Add_Profile_Flow R14.9 race fix), THEN return HTTP 404 body `{error: 'session_not_found', session_id, apple_id}`.
11. THE Web_API SHALL acquire `Profile_Lock` mode `write` cho `apple_id` TRƯỚC KHI launch Camoufox (Acceptance Criteria 1, R12.14 pattern). Lock SHALL được release ở 4 điểm: (a) `save` thành công, (b) `close` thành công, (c) `failed` terminal, (d) watchdog timeout (R15.9). IF lock release fail vì lý do nào (vd ngoại lệ filesystem), THEN THE Web_API SHALL log warning + best-effort tiếp tục — lock file orphan sẽ được dọn ở lần acquire kế tiếp (filelock library tự handle stale lock qua PID check).
12. WHEN process backend restart trong khi Open_Profile_Session đang ở state non-terminal, THE Web_API SHALL coi state là mất (in-memory) — Camoufox subprocess sẽ bị OS kill khi backend exit (parent process die), profile_dir thật KHÔNG bị ảnh hưởng (Open_Profile_Flow KHÔNG dùng `runtime/icloud_profiles/.adding/`), Profile_Lock orphan được filelock auto-clean qua PID check ở lần acquire kế. Backend startup SHALL NOT auto-resume — user phải tự bấm `Open` lại.
13. THE Web_API SHALL serve endpoint mới với path prefix `/api/icloud/profiles/{apple_id}/open/*` và áp dụng cùng auth pattern (R10.10): require `X-API-Token: <token>` header (qua `web/auth.py:require_token` middleware), return 401 nếu thiếu/sai.
14. THE Open_Profile_Flow SHALL không tích hợp với JobManager (R13) cùng lý do với Add_Profile_Flow (R14.14): lifecycle ngắn (≤30 phút), in-memory, single-instance per process, không cần SSE log realtime. Audit ghi qua `AuditLogRepository` trực tiếp đủ cho debug.
15. WHEN user thực hiện flow Open_Profile thành công (state `saved`), THE Web_UI Page Profiles SHALL refresh table profile (re-fetch `GET /api/icloud/profiles`) để cập nhật `status` mới, đóng dialog modal, và hiện toast "Đã verify profile {apple_id}, status mới: active". WHEN state `closed` (user bấm Đóng), THE Web_UI SHALL chỉ đóng dialog im lặng — KHÔNG refresh (vì DB không đổi), KHÔNG hiện toast.
16. WHERE biến môi trường `ICLOUD_OPEN_PROFILE_TIMEOUT_SEC` được set với giá trị nguyên dương, THE Web_API SHALL áp dụng giá trị đó làm hard timeout cho Open_Profile_Session (R15.9); ngược lại default 1800 giây.
17. THE CLI SHALL expose command tương đương: `python -m gpt_signup_hybrid.icloud_hme profile open --apple-id <X>` mở Camoufox HEADED + đợi user nhấn Enter để Save (verify cookies + reactivate, R15.6) hoặc gõ `q` + Enter để Close (R15.8). CLI SHALL acquire cùng `Profile_Lock` mode `write` như Web_API (R15.11). Khác Web_API: CLI blocking (giống Bootstrap_Flow CLI hiện có) — không có session_id, không poll status, chỉ 1 đường thẳng: launch → đợi user input → save/close → exit.
18. THE Open_Profile_Flow SHALL audit ÍT NHẤT 4 event types: `profile_reopen_start` (acquire lock + launch Camoufox OK), `profile_reopen_save` (user Save thành công + verify cookies pass), `profile_reopen_close` (user Close hoặc watchdog timeout), `profile_reopen_fail` (lock conflict / cookies_not_ready / unexpected error). Mọi event payload SHALL chứa `session_id` (trừ CLI mode — payload chứa `apple_id` thay session_id) + `apple_id`, KHÔNG log raw cookie value (chỉ matched cookie names).
