# Design Document

## Overview

Thêm một lớp cache session/cookie **file-based, cô lập theo instance** và một `Session_Provider` điều phối tái dùng. Mục tiêu: sau lần login/đăng ký đầu, các lần chạy sau mint token tươi từ cookie qua HTTP (không mở browser), chỉ full login khi cookie chết. Nhiều web chạy song song (khác `--db`) không đè cache của nhau.

Thiết kế bám sát precedent đã có trong repo:
- `_export_upi_token` (web/manager.py): atomic write `tmp+replace`, `0600`, `_safe_email_slug` → tổng quát hoá thành `Session_Store`.
- `session_phase.fetch_session_via_http(cookies, proxy)`: mint token từ cookie, đã có sẵn → dùng làm HTTP_Revalidate.
- `session_phase._scrub_jwt`: scrub JWT khỏi log.
- `config.Settings.runtime_dir`: gốc `runtime/` per `RUNTIME_DIR`.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  4 luồng job (reg / get-session / get-link / upi)            │
│      │ get(email, proxy, secret, login_fn)                   │
│      ▼                                                        │
│  Session_Provider  (session_provider.py)                     │
│   1. store.load(email)                                        │
│   2. có cookie? → fetch_session_via_http(cookies, proxy)      │
│        200+accessToken → REUSE (update token, last_validated) │
│   3. miss/fail → login_fn() (Full_Login) → store.save()       │
│      │                                                        │
│      ▼                                                        │
│  Session_Store  (session_store.py)                           │
│   load/save/delete + per-(instance,email) lock               │
│   path = runtime/session_cache/<db_stem>/<email_slug>.json   │
└─────────────────────────────────────────────────────────────┘
            │ config: Settings Store (per-instance DB)
            └── session.reuse_enabled / revalidate_http / cookie_max_age_hours
```

Quan hệ với DB: `session_results` **không đổi** — reg flow vẫn ghi như cũ cho lịch sử/UI. Session_Store là nguồn **reuse** riêng, file-based.

### Ranh giới module & hướng phụ thuộc (SOLID, chống phân mảnh)

Import một chiều — KHÔNG vòng:

```
session_store.py   (LEAF: chỉ stdlib + config.runtime_dir; KHÔNG import web/*, db/*, session_phase)
        ▲ import
session_provider.py  (import session_store + session_phase.fetch_session_via_http + login patterns)
        ▲ import
web/manager.py, web/server.py, web/upi_runner.py, autoreg/* (callers)
```

- **session_store.py** — trách nhiệm DUY NHẤT: persistence file (path/atomic/0600/lock) + helper cookie thuần. Là leaf → import được từ mọi nơi kể cả probe scripts, không tạo cycle.
- **session_provider.py** — trách nhiệm DUY NHẤT: orchestration reuse/login/save + seed + chuẩn hoá `SessionRecord`.
- **Gom DRY (xoá phân mảnh hiện có):**
  - `_safe_email_slug` đang ở `web/manager.py` → **chuyển vào session_store**; `web/manager.py` import lại (xoá bản cũ khi bỏ `_export_upi_token`).
  - `NON_RETRYABLE_PATTERNS` + `is_fatal_login_error()` **duplicate** ở `web/manager.py:2874` và `upi_runner.py:1737` → gom về **một** chỗ (`session_phase.py` cạnh `SessionError`); provider + 2 manager + runner import dùng chung.
  - `SESSION_TOKEN_COOKIE_NAMES` (tên cookie session-token) hiện rải rác (`session_phase._has_session_cookie`, get_session browser check) → định nghĩa 1 lần ở session_store.

## Components and Interfaces

### 1. `session_store.py` (module mới)

```python
# Helper thuần dùng chung (gom DRY)
SESSION_TOKEN_COOKIE_NAMES = ("__Secure-next-auth.session-token", "__Secure-next-auth.session-token.0")
def _safe_email_slug(email: str) -> str: ...            # chuyển từ web/manager.py
def has_session_token(cookies: list[dict]) -> bool: ... # dùng cho seed + reuse-gate

class SessionStore:
    # DI thay cho global: instance_id + runtime_dir inject lúc khởi tạo (testable, không phụ thuộc env ở import-time)
    def __init__(self, *, instance_id: str, runtime_dir: Path): ...
    # server startup: SessionStore(instance_id=engine.db_path.stem, runtime_dir=settings.runtime_dir)  (GAP 8, DI)
    # default factory: instance_id=Path(os.environ.get("GSH_DB_PATH","data")).stem khi không inject

    def _path_for(self, email: str) -> Path:
        # GAP collision: 1355/1370 email chứa '+' → slug map '+'→'_' gây đụng. Thêm hash email ĐẦY ĐỦ.
        h = hashlib.sha256(email.encode()).hexdigest()[:12]
        return self._cache_dir / f"{_safe_email_slug(email)}-{h}.json"

    def load(self, email: str) -> dict | None:
        rec = <đọc + parse file _path_for(email)>
        if rec is None:
            return None
        if rec.get("email") != email:        # R12.3: guard chống va chạm/sửa tay
            return None                       # coi như miss — KHÔNG trả session account khác
        return rec
    def save(self, email: str, record: dict) -> Path: ...   # atomic, 0600
    def delete(self, email: str) -> bool: ...
    def lock_for(self, email: str) -> asyncio.Lock: ...      # per (instance,email)
```

- `INSTANCE_ID` resolve một lần lúc import; fallback `"data"`.
- `_safe_email_slug` tái dùng (chuyển thành helper dùng chung; hiện nằm trong web/manager.py).
- Lock: dict `{email_slug: asyncio.Lock}` trong process (đủ vì mỗi instance = 1 process; khác instance đã tách đường dẫn).
- `load`: đọc JSON; file hỏng → trả None + log (không raise, fail-soft cho cache).

### 2. `Session_Cache_Record` (shape file)

```json
{
  "email": "a@b.com",
  "cookies": [ {"name":"__Secure-next-auth.session-token","value":"...","domain":"chatgpt.com", ...} ],
  "access_token": "eyJ...",
  "session_token": "eyJ...",
  "two_factor": {"secret":"...", "factor_id":"...", ...},
  "proxy": "http://...",
  "created_at": "2026-06-22T10:00:00",
  "last_validated_at": "2026-06-22T11:30:00"
}
```

> Canonical dùng key `cookies`. Các probe script đọc cookie chấp nhận cả `cookies` (mới) lẫn `session_cookies` (file `upi_tokens` cũ còn sót) — xem mục 7. `access_token` + `proxy` giữ nguyên tên để probe đọc không cần đổi.

**`SessionRecord` (một shape chuẩn — chống phân mảnh logic chuyển đổi):**

```python
@dataclass
class SessionRecord:
    email: str
    cookies: list[dict]
    access_token: str | None
    session_token: str | None
    two_factor: dict | None
    proxy: str | None
    created_at: str
    last_validated_at: str | None

    @classmethod
    def from_session_json(cls, email, data, proxy) -> "SessionRecord":
        # nguồn: get_session_pure_request / get_session(browser sau fix) / upi → session JSON + __cookies
        cookies = data.get("__cookies") or []
        return cls(email, cookies, data.get("accessToken"),
                   _session_token_from_cookies(cookies), None, proxy, _now_iso(), _now_iso())

    @classmethod
    def from_signup_result(cls, result, proxy) -> "SessionRecord":   # reg (save-only)
        return cls(result.email, result.cookies or [], result.access_token,
                   result.session_token, result.two_factor, proxy, _now_iso(), _now_iso())

    def to_dict(self) -> dict: ...
```

Chỉ **2 nguồn** thật sự: session-JSON (get-session/link/upi đều qua `get_session_pure_request`) và `SignupResult` (reg). `_session_token_from_cookies` trích `session_token` từ cookie `__Secure-next-auth.session-token` (R4.6).

### 3. `session_provider.py` (module mới)

```python
class SessionProvider:
    def __init__(self, store: SessionStore, settings_repo): ...

    async def acquire(
        self, *, email, proxy, log,
        login_fn,                     # async () -> dict (session JSON + __cookies)
        force_fresh: bool = False,    # True → bỏ reuse, full login (UPI re-login đổi IP)
        fatal_classifier=None,        # mặc định = is_fatal_login_error (gom DRY)
    ) -> dict:
        cfg = self._read_cfg()        # reuse_enabled / revalidate_http / cookie_max_age_hours
        async with self._store.lock_for(email):
            if cfg.reuse_enabled and not force_fresh:
                reused = await self._try_reuse(self._store.load(email), proxy, cfg, log)
                if reused is not None:
                    return reused
            try:
                session = await login_fn()        # Full_Login (per-flow, tự retry bên trong)
            except Exception as exc:
                if (fatal_classifier or is_fatal_login_error)(exc):
                    self._store.delete(email)     # GAP 5: chỉ xoá khi fatal
                raise
            self._store.save(email, SessionRecord.from_session_json(email, session, proxy).to_dict())
            return session                        # trả FULL session (còn __cookies) — flow tự strip ở biên persist/broadcast (R4.5)

    # Reg là SAVE-ONLY → KHÔNG gọi acquire. JobManager tự:
    #   store.save(email, SessionRecord.from_signup_result(result, proxy).to_dict())
    # → acquire chỉ phải hiểu MỘT shape (session-JSON) ⇒ SRP/OCP sạch, không sniff đa shape.

    async def _try_reuse(self, rec, proxy, cfg, log) -> dict | None:
        if not rec or not has_session_token(rec.get("cookies") or []):   # R2.6: cookie rỗng/không token → miss
            return None
        if not cfg.revalidate_http:
            return self._reuse_payload(rec) if _within_ttl(rec, cfg.cookie_max_age_hours) else None
        try:
            data = await fetch_session_via_http(cookies=rec["cookies"], proxy=rec.get("proxy") or proxy)
        except SessionError as exc:
            # GAP 9: proxy lưu có thể chết/xoay SID → thử lại 1 lần với proxy runtime
            if rec.get("proxy") and proxy and rec.get("proxy") != proxy:
                try:
                    data = await fetch_session_via_http(cookies=rec["cookies"], proxy=proxy)
                except SessionError as exc2:
                    log(f"[session-cache] revalidate fail (cả 2 proxy): {_scrub_jwt(str(exc2))}")
                    return None
            else:
                log(f"[session-cache] revalidate fail: {_scrub_jwt(str(exc))}")
                return None
        # cập nhật access_token + last_validated_at vào record, save lại
        data["__cookies"] = rec["cookies"]   # đính cookie để caller (UPI auth_sink) có cookie
        self._store.save(rec["email"], {**rec, "access_token": data.get("accessToken"), "last_validated_at": _now_iso()})
        return data
```

- `login_fn` injection giữ logic Full_Login ở từng flow (SOLID): provider không cần biết flow login kiểu gì.
- `acquire` chỉ xử lý **session-JSON** (mọi flow reuse-được đều qua `get_session_pure_request`); reg dùng `SessionRecord.from_signup_result` + `store.save` trực tiếp → bỏ hẳn việc sniff đa-shape (GAP 4 giải quyết bằng 2 constructor của `SessionRecord`).
- `_strip_cookies(session)`: helper bỏ key `__cookies` — **gọi tại biên persist/broadcast của từng flow** (get-session trước khi ghi `jobs.session_data`/SSE), KHÔNG gọi trong `acquire` (UPI cần `__cookies` để fill `auth_sink`/`check_plan`) (GAP 7).
- Đường reuse trả session JSON (từ `fetch_session_via_http`) + đính `session["__cookies"] = rec["cookies"]` để caller (UPI) có cookie.
- `fatal_classifier` mặc định = `is_fatal_login_error` (gom DRY 1 chỗ ở `session_phase`, thay 2 bản `NON_RETRYABLE_PATTERNS` đang trùng) (GAP 5).

### 4. Capture cookie (R4)

Hiện trạng đã verify trong code:
- **`get_session_pure_request`** (dùng bởi SessionJobManager + LinkJobManager combo): **đã export cookies** sẵn ở `session_data["__cookies"]` (session_phase.py ~:1497-1511, đọc từ `session.cookies.jar`). → Không cần sửa, chỉ đọc key này khi build record. **Phải strip `__cookies` trước khi broadcast SSE/persist DB jobs** để tránh leak.
- **UPI** (`run_upi_qr_probe`): đã trả `result.session_cookies` + `result.access_token` + `active_proxy`. → Dùng thẳng.
- **Reg** (`SignupResult`): đã có `.cookies`. → Dùng thẳng.
- **`_get_session_browser`** (Get Session browser mode): **đây là điểm DUY NHẤT mất cookie** — lấy `ctx.cookies()` ở `:330` chỉ để check readiness rồi `return session_data` (`:365`) bỏ cookie. → Cần surface cookies (gắn `session_data["__cookies"]` cho đồng nhất với pure_request, strip trước khi broadcast).

### 5. Wiring

| Flow | Thay đổi |
|------|----------|
| SessionJobManager | `_login_with_retry` bọc trong `provider.acquire(login_fn=...)`; persist DB jobs như cũ; strip `__cookies` trước khi persist/broadcast |
| LinkJobManager (combo) | thay `get_session_pure_request(...)`/`get_session(...)` bằng `provider.acquire(login_fn=...)`; chỉ cần `access_token` từ kết quả cho `get_checkout_url` |
| UpiJobManager + run_upi_qr_probe | xem mục 6 (login nằm trong runner — không wire ở manager đơn thuần) |
| JobManager (reg) | **save-only**: sau `session_repo.create(...)` thành công → `store.save(email, record)`; KHÔNG reuse |

### 6. Tích hợp UPI (login trong runner) — GAP 1, 2, 6

`run_upi_qr_probe` login ở Step 1 rồi dùng `access_token` (Bearer) cho checkout/approve. Cookie chatgpt KHÔNG cần cho Stripe steps. Phương án chọn (ít đụng lõi nhất):

- Thêm tham số `login_fn: Callable[[], Awaitable[dict]] | None = None` vào `run_upi_qr_probe`. Khi có, Step 1 gọi `login_fn()` thay cho `get_session_pure_request` trực tiếp. `UpiJobManager` truyền `login_fn = lambda: provider.acquire(email, proxy, login_fn=<full get_session_pure_request>, log=...)`.
- **Force-fresh khi re-login đổi IP**: `_run_upi_cycles` truyền cờ `force_fresh=True` cho các cycle có `relogin_block_streak>0` → `provider.acquire(..., force_fresh=True)` bỏ qua reuse, full login lấy IP mới (GAP 2). `acquire` nhận thêm tham số `force_fresh: bool = False`.
- **check_plan sau restart**: `UpiJobManager.check_plan` khi `job._session_cookies is None` → `store.load(email)` lấy `cookies` rồi `fetch_session_via_http` (GAP 6).

### 7. Tương thích probe scripts — GAP 3

- Canonical record dùng key `cookies`. Cập nhật `test/probe_hosted_upi.py`, `test/probe_account_entitlement.py`, `test/probe_upi_next_action.py`:
  - đổi thư mục quét sang `<runtime_dir>/session_cache/<Instance_Id>/`;
  - đọc cookie chấp nhận cả `cookies` (mới) lẫn `session_cookies` (file cũ còn sót).
- Không giữ đường ghi `runtime/upi_tokens` global song song (GAP 10/R10.3).

### 8. Import-on-init từ session_results (R11)

Seed cache một lần lúc startup từ dữ liệu lịch sử (thực tế: data.db có 809/1370 row dùng được).

```python
def seed_from_session_results(store: SessionStore, session_repo: SessionResultRepository, log) -> int:
    n = 0
    seen: set[str] = set()
    for row in session_repo.list_all():            # đã ORDER BY created_at DESC → email đầu là mới nhất
        email = row["email"]
        if email in seen:                           # dedupe: chỉ lấy row mới nhất/email (lần gặp đầu)
            continue
        seen.add(email)
        cookies = json.loads(row["cookies"]) if row.get("cookies") else []
        if not cookies or not _has_session_token(cookies):   # R11.3: bỏ cookie rỗng/không có session-token
            continue
        if store.load(email) is not None:           # R11.5: KHÔNG clobber record runtime đã có (idempotent, an toàn)
            continue
        store.save(email, {
            "email": email,
            "cookies": cookies,
            "access_token": row.get("access_token"),
            "session_token": row.get("session_token"),
            "two_factor": json.loads(row["two_factor"]) if row.get("two_factor") else None,
            "proxy": None,                            # R11.4: DB không lưu proxy
            "created_at": row.get("created_at"),
            "last_validated_at": None,                # chưa revalidate → lần acquire đầu sẽ HTTP-check
        })
        n += 1
    log(f"[session-cache] seed {n} record từ session_results")
    return n
```

- Dedupe `seen` + "skip nếu file đã tồn tại" → idempotent, tránh bug so sánh `created_at` lệch định dạng (DB `YYYY-MM-DD hh:mm:ss` vs file ISO `...T...`).

- Gọi ở `web/server.py` startup, sau khi `session_repo` + `SessionStore` sẵn sàng (R11.1, R11.6 best-effort try/except).
- Per-instance tự nhiên: `session_repo` thuộc engine của instance → ghi vào Cache_Dir instance đó (R11.2).
- Record seed có `last_validated_at=null` → lần `acquire` đầu luôn chạy HTTP_Revalidate (không tin cookie cũ mù quáng).

## Data Models

`_read_cfg()` đọc 3 key Settings Store:
- `session.reuse_enabled: bool = True`
- `session.revalidate_http: bool = True`
- `session.cookie_max_age_hours: int = 24` (≥1)

Thêm vào `_EXACT_KEYS` + `_validate_type_constraint()` (db/repositories.py) trước khi dùng (R7.1).

## Error Handling

- Cache là **fail-soft**: load/parse lỗi → coi như miss, Full_Login, không raise.
- `save` lỗi IO → log warning, **không** làm fail job (mirror best-effort như `_export_upi_token`).
- HTTP_Revalidate `SessionError` → miss → Full_Login. Log scrub JWT.
- Full_Login fatal (sai pass/2FA) → `fatal_classifier(exc)` True → `store.delete(email)` (R8.5/GAP 5) rồi raise như flow hiện tại; transient/mạng → KHÔNG xoá.
- Proxy trong record ưu tiên hơn proxy runtime cho revalidate (R3.6); nếu proxy record chết do mạng (không phải 401), thử lại 1 lần với proxy runtime trước khi miss (GAP 9); revalidate vẫn fail → Full_Login với proxy mới.
- UPI re-login đổi IP (`relogin_block_streak>0`) → `acquire(force_fresh=True)` bỏ reuse (GAP 2).

## Correctness Properties

### Property 1: Isolation theo instance
Với hai `Instance_Id` khác nhau, đường dẫn file cache cho cùng một email luôn khác nhau → không bao giờ đè dữ liệu của instance khác.
**Validates: Requirements 1.1, 1.2, 1.3, 1.4**

### Property 2: Atomicity
Reader đồng thời không bao giờ đọc file ghi dở; mọi ghi qua `tmp + os.replace`.
**Validates: Requirements 2.2, 5.3**

### Property 3: Fail-soft cache
Lỗi load/parse/save không bao giờ làm fail job; chỉ chuyển sang Full_Login hoặc bỏ qua lưu.
**Validates: Requirements 2.3, 3.4**

### Property 4: No-secret-leak
`access_token`/JWT không bao giờ đi qua SSE/`to_dict()`/log; mọi chuỗi `eyJ...` bị scrub.
**Validates: Requirements 8.2, 3.5**

### Property 5: Reuse-safety
Chỉ coi là Reuse_Hit khi có cookie và (HTTP 200 + accessToken) hoặc (còn trong TTL khi tắt revalidate); cookie rỗng không bao giờ là Reuse_Hit.
**Validates: Requirements 2.6, 3.2, 3.3**

### Property 6: Login-spam-bound
Trong một instance, lock per-email đảm bảo không có hai Full_Login song song cho cùng email.
**Validates: Requirements 5.1, 5.2**

### Property 7: UPI re-login đổi IP không bị cache chặn
Khi UPI re-login do block-streak (`force_fresh=True`), provider luôn Full_Login với proxy/IP mới, không bao giờ trả cookie cũ.
**Validates: Requirements 9.3, 9.4**

### Property 8: Seed idempotent + per-instance
Import từ session_results chỉ ghi row có cookie session-token, không clobber record runtime mới hơn, và mỗi instance chỉ seed từ DB của chính nó vào cache dir của chính nó.
**Validates: Requirements 11.1, 11.2, 11.3, 11.5**

### Property 9: Không phục vụ nhầm account
Hai email khác nhau luôn ra hai file cache khác nhau (slug + hash email đầy đủ); và `load` chỉ trả record khi `record["email"] == email` → không bao giờ trả session của account khác dù slug trùng.
**Validates: Requirements 12.1, 12.2, 12.3**

## Testing Strategy

Theo project-rules — script thật trong `test/`, in tiến trình realtime, không inline `-c`:
- `test/check_session_store_isolation.py` — 2 Instance_Id khác nhau ghi cùng email → 2 file khác path, không đè (R1).
- `test/check_session_store_atomic.py` — save atomic + chmod 0600 (R2).
- `test/check_session_provider_reuse.py` — mock `fetch_session_via_http` 200 → Reuse_Hit không gọi login_fn; mock fail → gọi login_fn + save (R3).
- `test/check_session_provider_cfg.py` — `reuse_enabled=false` luôn login; `revalidate_http=false` dùng TTL (R7).
- `test/syntax_check.py` — AST parse toàn bộ file Python đụng tới.

Mỗi test in `[PASS]/[FAIL] <id> — <mô tả>` theo từng case, `flush=True`.

## Open Decisions (chốt khi vào task)

1. ~~Cách surface cookie từ Get Session browser~~ → ĐÃ CHỐT: gắn `session_data["__cookies"]` đồng nhất với pure_request, strip trước broadcast.
2. `Session_Provider` đặt làm singleton per-process (giống các manager) khởi tạo ở `web/server.py` startup, inject `settings_repo`.
3. Cách inject reuse vào UPI: chọn `login_fn` param cho `run_upi_qr_probe` (mục 6) thay vì pre-acquire ở manager — giữ retry/IP logic của runner nguyên vẹn. Xác nhận khi vào task 6.3.
