# `icloud_hme` — iCloud Hide My Email Pool

Tool tạo + quản lý vòng đời email **Hide My Email** (HME) Apple-side, dùng nhiều Apple ID làm pool, round-robin pick, audit trail đầy đủ.

Hỗ trợ 2 mode:

- **CLI**: `python -m gpt_signup_hybrid.icloud_hme <command>` cho automation/script.
- **Web UI**: tab `HME` trong web app — `Profiles` / `Runner` / `Emails`.

---

## 1. Kiến trúc

Code chia 3 layer rõ ràng: **Presentation** (CLI/Web) gọi xuống **Runner** (loop controller), Runner dispatch xuống **Services** (logic core, không đổi).

```mermaid
graph TD
    subgraph "Layer 1: Presentation"
        CLI["CLI<br/>icloud_hme/cli.py<br/>(generate, check, ...)"]
        WEB["Web Router<br/>icloud_hme/web/router.py<br/>(/api/icloud/run/*)"]
    end

    subgraph "Layer 2: Runner"
        RUNNER["HmeRunner<br/>icloud_hme/runner.py"]
        EVENTS["asyncio.Event<br/>cancel / pause / resume"]
        LOGCB["log_callback<br/>async (level, msg, payload)"]
        BUFFER["LogBuffer<br/>icloud_hme/web/log_buffer.py"]
        RUNNER --- EVENTS
        RUNNER --- LOGCB
        LOGCB -.-> BUFFER
    end

    subgraph "Layer 3: Services (logic core, không đổi)"
        GEN["HmeGenerator<br/>generator.py"]
        CHK["ProfileChecker<br/>checker.py"]
        MGR["HmeManager<br/>manager.py"]
        POOL["IcloudPoolManager<br/>pool.py"]
        REC["Recorder<br/>recorder.py"]
        BOOT["bootstrap()<br/>bootstrap.py"]
    end

    subgraph "Layer 4: Persistence / Infra"
        DB[("SQLite<br/>db/repositories.py<br/>icloud_accounts<br/>icloud_emails<br/>icloud_audit_log<br/>pool_state")]
        CLIENT["HmeClient<br/>client.py (httpx async)"]
        SESS["session.py<br/>extract_session_bundle (Camoufox)"]
        LOCK["profile_lock.py<br/>RW lock per Apple_ID"]
    end

    CLI -->|"asyncio.run(runner.start(action, params))"| RUNNER
    WEB -->|"asyncio.create_task(runner.start(...))"| RUNNER
    CLI -.->|"SIGINT → runner.stop()"| RUNNER
    WEB -.->|"POST /run/stop → runner.stop()"| RUNNER
    WEB -.->|"GET /run/log/stream (SSE)"| BUFFER

    RUNNER -->|"_run_one_cycle dispatch"| GEN
    RUNNER -->|"_run_one_cycle dispatch"| CHK
    RUNNER -->|"_run_one_cycle dispatch"| MGR
    CLI -->|"1-shot, không qua Runner"| BOOT
    CLI -->|"1-shot, không qua Runner"| REC

    GEN --> POOL
    CHK --> POOL
    MGR --> POOL
    GEN --> CLIENT
    CHK --> CLIENT
    MGR --> CLIENT
    GEN --> SESS
    POOL --> DB
    MGR --> DB
    SESS --> LOCK
```

**Profile_Status state machine:**

```
[*] → active ↔ limited ↔ quota_full
       ↓         ↓
    session_expired / disabled
       ↓
    deleted (terminal)
```

**Runner lifecycle** (thay cho Job_Status state machine cũ):

```
   start(action, params)
        │
        ▼
   ┌─────────────┐  cycle++ + dispatch service     ┌────────────────┐
   │ run cycle   │ ──────────────────────────────► │ Service layer  │
   │ #N          │                                 │ generator/     │
   └─────────────┘                                 │ checker/       │
        │                                          │ manager        │
        │ cycle_result                             └────────────────┘
        ▼
   stats += result
        │
        │ cancel?  ──yes──► return summary
        │
        ▼ no
   ┌─────────────────────────┐
   │ _interruptible_sleep    │  pause/resume hỗ trợ qua asyncio.Event
   │ (retry_interval seconds)│  cancel kill loop trong tối đa 1.5s
   └─────────────────────────┘
        │
        └─► loop về cycle #N+1
```

Runner chạy **single-instance** mỗi process (`is_running` guard). Stop = set `cancel_event`, loop dừng tại checkpoint kế tiếp và trả summary `{total_cycles, created, errors, skipped, stopped_by}`.

---

## 2. Quy trình (lần đầu)

### 2.1 Bootstrap profile

```bash
python -m gpt_signup_hybrid.icloud_hme bootstrap --apple-id you@icloud.com
```

- Mở Camoufox **headed**.
- User login Apple ID + nhập 2FA tay.
- Đợi vào được iCloud Mail (thấy inbox) → quay lại terminal nhấn **Enter**.
- Tool verify cookies `X-APPLE-WEBAUTH-*` → save profile vào `runtime/icloud_profiles/<apple_id>/`.
- Dùng `q` + Enter để hủy.

Bootstrap có **3 attempt retry** (R12.17): nếu cookies verify fail, tool tự retry sau 5s. Sau 3 attempt fail → raise `BootstrapError`.

### 2.2 Verify profile

```bash
python -m gpt_signup_hybrid.icloud_hme check --apple-id you@icloud.com
```

Probe `/v2/hme/list` read-only (1-shot, không qua Runner). Output JSON:
- `status='active'` → profile dùng được.
- `status='session_expired'` → cookies expired, cần `bootstrap` lại.
- `status='limited'` → bị Apple rate-limit, đợi qua `limited_until`.

---

## 3. Tạo HME email — qua Runner (infinite loop)

Lệnh `generate` luôn chạy **infinite loop** qua `HmeRunner`: mỗi cycle gọi `HmeGenerator.generate(...)` rồi sleep `retry_interval` giây trước cycle kế tiếp. Dừng bằng `Ctrl+C` (SIGINT) → CLI gọi `runner.stop()` → in summary cuối session.

### 3.1 Drain mode (default)

```bash
# Mỗi cycle drain pool tới khi hết profile khả dụng, đợi 15 phút giữa cycles
python -m gpt_signup_hybrid.icloud_hme generate
```

### 3.2 Bounded count per cycle

```bash
# Mỗi cycle tạo tối đa 50 email
python -m gpt_signup_hybrid.icloud_hme generate --count-per-cycle 50
python -m gpt_signup_hybrid.icloud_hme generate -n 50

# Kèm label / note / proxy
python -m gpt_signup_hybrid.icloud_hme generate \
    --count-per-cycle 100 \
    --label chatgpt-jan \
    --note "test batch" \
    --proxy http://user:pass@proxy:8080
```

### 3.3 Tuỳ chỉnh interval

```bash
# Đợi 600s giữa các cycle (override env ICLOUD_RETRY_INTERVAL)
python -m gpt_signup_hybrid.icloud_hme generate --retry-interval 600
```

`--retry-interval` phải `>= 10`. Nếu không truyền, Runner lấy từ `Settings.icloud_retry_interval` (env `ICLOUD_RETRY_INTERVAL`, default 900).

### 3.4 Flag tổng hợp

| Flag | Type | Default | Mô tả |
|---|---|---|---|
| `--count-per-cycle` / `-n` | int / None | None | Số email tối đa MỖI cycle. None = drain tới khi pool exhausted |
| `--retry-interval` | int | env / 900 | Giây sleep giữa cycle (>= 10) |
| `--label` | str | None | Label gắn vào HME record |
| `--note` | str | None | Note gắn vào HME record |
| `--proxy` | str | None | HTTP proxy override |

> Cờ `--infinite` đã bị **xoá** — mọi lần chạy đều là infinite loop, do Runner control. Muốn 1-shot thì dùng `--count-per-cycle <N>` rồi `Ctrl+C` sau cycle đầu.

### 3.5 Label mặc định

Nếu không truyền `--label`, tool dùng `Label_Default = strftime('%Y%m%d', UTC)` (vd `20260524`) trong từng cycle.

Sau này có thể truy vấn / xóa hàng loạt theo ngày tạo qua `email list-sync` rồi bulk action theo email list.

### 3.6 Cycle behavior khi pool exhausted

Trong 1 cycle, `HmeGenerator` round-robin pick profile có `status='active'` tới khi pool exhausted (mọi profile `limited`/`quota_full`/`session_expired`/`disabled`) hoặc đạt `count_per_cycle`. Cycle return result, Runner cộng dồn vào `stats`, log tóm tắt, sleep `retry_interval`, rồi cycle kế tiếp — các profile `limited`/`quota_full` có cơ hội recover sau khi TTL hết hạn.

---

## 4. Email lifecycle (sau MVP)

Các lệnh `email *` chạy **1-shot blocking**, không qua Runner.

### 4.1 Single email actions

```bash
# Deactivate (Apple-side: isActive=false, vẫn có thể reactivate)
python -m gpt_signup_hybrid.icloud_hme email deactivate --email abc@icloud.com

# Reactivate
python -m gpt_signup_hybrid.icloud_hme email reactivate --email abc@icloud.com

# Delete (xóa hẳn Apple-side, free slot quota)
python -m gpt_signup_hybrid.icloud_hme email delete --email abc@icloud.com

# Mark used cho ChatGPT signup (DB-only, không gọi Apple API)
python -m gpt_signup_hybrid.icloud_hme email mark-used \
    --email abc@icloud.com \
    --used-for chatgpt-signup-001

# Dry-run (không call API, không UPDATE DB, chỉ preview)
python -m gpt_signup_hybrid.icloud_hme email deactivate --email abc@icloud.com --dry-run
```

**Status transition (R9.13, R9.14)**:

| Action | Valid status |
|---|---|
| `deactivate` | `created`, `reconciled` |
| `reactivate` | `deactivated`, `revoked` |
| `delete` | bất kỳ trừ `deleted` |
| `update_meta` | `created`, `reconciled`, `deactivated` |

Action gọi trên status không hợp lệ → raise `TerminalStatusError`, **không gọi API**, **không UPDATE DB**.

### 4.2 Sync DB ↔ Apple-side

```bash
python -m gpt_signup_hybrid.icloud_hme email list-sync --apple-id you@icloud.com
```

3 nhánh UPDATE diff (R9.12 — refactor B, DB source-of-truth):
- Apple inactive + DB `created/reconciled` → UPDATE `status='deactivated'`, audit `reason='external_change'`.
- Apple missing + DB `created/reconciled` → UPDATE `status='deleted'`, audit `reason='external_change'`.
- Apple missing + DB `used_for_chatgpt` → UPDATE `status='disabled'`, audit `reason='apple_deleted_after_use'` (HME đã dùng cho ChatGPT mà Apple xoá → forward không hoạt động).
- Apple active + DB `deactivated/revoked` → UPDATE `status='created'`, audit `reason='external_change'`.
- Apple-side mà DB không có → **bỏ qua, KHÔNG insert** (tool chỉ quản email do tool tạo).

### 4.3 Reconcile

**No-op trong refactor B.** DB là source-of-truth — `HmeGenerator.reconcile()` không còn import email Apple-side vào DB. Method giữ signature làm no-op để CLI cũ không vỡ; trả 0. Caller muốn detect drift dùng `email list-sync` (3 nhánh UPDATE).

---

## 5. Runner control — CLI + Web

`HmeRunner` là single-instance per process. CLI và Web dùng cùng instance qua DI.

### 5.1 CLI

Lệnh `generate` và `check --all` chạy qua Runner (xem mục 3 + 6). Các lệnh khác (`bootstrap`, `profile *`, `status`, `reconcile`, `email *`, `audit *`) đều **1-shot**, không đi qua Runner.

`SIGINT` (Ctrl+C) → CLI gọi `runner.stop()` → loop dừng tại checkpoint kế tiếp (≤ 1.5s) → CLI in summary `{total_cycles, created, errors, skipped, stopped_by}` rồi exit 0.

### 5.2 Web HTTP endpoints

Mọi endpoint `/api/icloud/run/*` đều **bắt buộc Bearer token** (`Authorization: Bearer <ICLOUD_API_AUTH_TOKEN>`). Thiếu/sai header → HTTP 401, không gọi method nào trên Runner.

| Method | Path | Body / Query | Response |
|---|---|---|---|
| `POST` | `/api/icloud/run` | `{action, params, retry_interval?}` | `{ok: true, action}` hoặc `409 {error: "already_running"}` |
| `POST` | `/api/icloud/run/stop` | — | `{ok: true}` |
| `POST` | `/api/icloud/run/pause` | — | `{ok: true}` |
| `POST` | `/api/icloud/run/resume` | — | `{ok: true}` |
| `GET` | `/api/icloud/run/status` | — | `RunStatus` |
| `GET` | `/api/icloud/run/log` | `?offset=N&limit=M` | `{events: [LogEvent], next_offset}` |
| `GET` | `/api/icloud/run/log/stream` | — | SSE stream `data: <LogEvent JSON>\n\n` |

`action` thuộc 7 giá trị: `generate`, `check_all`, `deactivate_bulk`, `reactivate_bulk`, `delete_bulk`, `update_meta_bulk`, `list_sync`.

`RunStatus`:
```json
{
  "running": true,
  "action": "generate",
  "cycle": 3,
  "stats": {"created": 12, "errors": 0, "skipped": 1},
  "retry_interval": 900,
  "next_cycle_at": "2026-05-24T10:30:00+00:00"
}
```

`LogEvent`:
```json
{"ts": "2026-05-24T10:15:00.123+00:00", "level": "info", "message": "Cycle #3 done: ...", "payload": {...}, "seq": 42}
```

`POST /api/icloud/run` chuyển `running: false → true` sẽ **clear LogBuffer** và reset `seq` về 0 — mỗi run là 1 session log mới.

---

## 6. Check profile bulk — qua Runner

```bash
# Check toàn bộ profile liên tục, retry mỗi 15 min (default)
python -m gpt_signup_hybrid.icloud_hme check --all

# Tuỳ chỉnh interval, không auto-mark
python -m gpt_signup_hybrid.icloud_hme check --all \
    --retry-interval 1800 \
    --no-auto-mark
```

| Flag | Type | Default | Mô tả |
|---|---|---|---|
| `--all` | bool | required | Bật infinite loop qua Runner |
| `--retry-interval` | int | env / 900 | Giây sleep giữa cycle (>= 10) |
| `--auto-mark / --no-auto-mark` | bool | true | Tự động mark trạng thái dựa trên kết quả |
| `--proxy` | str | None | HTTP proxy override |

Không có `--all` → 1-shot single profile (`--apple-id` bắt buộc), không qua Runner.

---

## 7. Pool management

### 7.1 Status report

```bash
python -m gpt_signup_hybrid.icloud_hme status
```

JSON output:
```json
{
  "by_status": {"active": 3, "limited": 1, "quota_full": 0, ...},
  "profiles": [...],
  "emails_by_status": {"created": 250, "reconciled": 30, ...},
  "quota_soft_cap_per_account": 700,
  "total_quota_remaining": 1850,
  "low_capacity": false,
  "quota_full_count": 0
}
```

### 7.2 Delete profile (R5)

```bash
python -m gpt_signup_hybrid.icloud_hme profile delete --apple-id old@icloud.com
```

- Xóa `runtime/icloud_profiles/<apple_id>/` trên disk.
- DB: `status='deleted'`, `profile_dir=NULL`.
- **Email rows giữ nguyên** (audit trail preservation, R5.3).
- Pool không pick lại profile này (R5.7).

---

## 8. Audit log (R6)

Mọi state mutation được ghi audit cùng transaction (R6.3 atomic).

```bash
# Liệt kê 100 event mới nhất
python -m gpt_signup_hybrid.icloud_hme audit list

# Filter
python -m gpt_signup_hybrid.icloud_hme audit list --apple-id you@icloud.com
python -m gpt_signup_hybrid.icloud_hme audit list --event-type create_success --limit 50
python -m gpt_signup_hybrid.icloud_hme audit list --since 2026-01-01T00:00:00Z

# Cleanup (R6.5)
python -m gpt_signup_hybrid.icloud_hme audit cleanup --days 90
```

`WRITABLE_EVENT_TYPES` (trong `db/repositories.py`) chia theo nhóm:

- HME generation: `create_attempt`, `create_success`, `create_fail`, `candidate_retry`
- Pool transition: `mark_limited`, `mark_session_expired`, `mark_disabled`, `mark_quota_full`, `limited_retry`, `quota_retry`, `pool_pick_locked`
- Profile lifecycle: `profile_bootstrap`, `profile_bootstrap_fail`, `profile_reactivate`, `profile_delete`, `profile_delete_fail`
- Email lifecycle: `email_deactivate`, `email_reactivate`, `email_delete`, `email_update_meta`, `email_mark_used`, `email_export`, `email_skip_quota_full` (+ `_fail` variants)
- Recording: `recording_start`, `recording_stop`
- Session: `session_extract`, `session_extract_fail`, `cursor_update_failed`
- Reconcile: `reconcile_add` (legacy — không còn được ghi sau refactor B), `reconcile_disable` (legacy)

---

## 9. Recording (R1) — discovery flow

Ghi Playwright action log + HAR khi user thao tác manual để phân tích selector/endpoint:

```bash
# Bắt đầu
python -m gpt_signup_hybrid.icloud_hme recording start \
    --apple-id you@icloud.com \
    --scenario create
# → Camoufox headed mở. User thao tác. Bấm 'q' để stop.

# Sau session, output ghi vào:
#   runtime/icloud_recordings/<session_id>/
#     ├── actions.jsonl   (DOM events, password/code/otp/secret được redact)
#     ├── network.har     (full HAR)
#     └── metadata.json   (session_id, scenario, started/ended, exit_reason)

# Stop bằng session ID (nếu muốn từ shell khác)
python -m gpt_signup_hybrid.icloud_hme recording stop \
    --session-id <id> \
    --exit-reason normal
```

Field name `password / code / otp / secret` trong input event được auto-redact thành `<redacted>` trước khi ghi `actions.jsonl` (R1.4).

---

## 10. Web UI

```bash
# Khởi động web server
python -m gpt_signup_hybrid.web

# Mở browser tab HME
# http://localhost:8000 → click tab "HME"
```

3 sub-page:

### 10.1 Profiles
- Bảng list profile + status badge + hme_count + quota_remaining + last_used_at.
- Filter theo status enum.
- Action per profile: **Delete** (R5), **Sync** (R9.12).
- Pool Status summary: `active=3 · limited=1 · quota_remaining: 1850/2100`.

### 10.2 Runner
- Form **Start**: dropdown `action` (`generate` / `check_all`), input `count_per_cycle` (optional), `retry_interval` (default 900), `label` / `note` / `proxy` (optional).
- Nút **Start** → `POST /api/icloud/run`. Nút **Stop** → `POST /api/icloud/run/stop`.
- Status badge: **RUNNING** (xanh) / **IDLE** (xám), poll `GET /api/icloud/run/status` mỗi 2s.
- Stats live: `Cycle #N | Created: X | Errors: Y | Skipped: Z`.
- Countdown `next_cycle_at` format `MM:SS` khi đang sleep.
- **Log viewer panel**: `EventSource('/api/icloud/run/log/stream')`, auto-scroll, render `[ts][level] message` với màu: `info` xám/trắng, `warn` vàng, `error` đỏ.

### 10.3 Emails
- Bảng list email + filter theo status + apple_id + label.
- Checkbox bulk select → **Deactivate** / **Reactivate** / **Delete** (có toggle Dry run) → wire qua `POST /api/icloud/run` với action `deactivate_bulk` / `reactivate_bulk` / `delete_bulk`.
- Form **Generate** mở modal: chọn action `generate`, set `count_per_cycle` / `label` / `note` → submit → redirect sang tab Runner.

---

## 11. Configuration

### 11.1 Environment variables

Cấu hình qua `.env` hoặc env vars:

| Env | Default | Mô tả |
|---|---|---|
| `ICLOUD_RETRY_INTERVAL` | `900` | Giây sleep giữa cycle (Runner). Min 10. |
| `ICLOUD_MAX_ERRORS_PER_CYCLE` | `0` | Cap số lỗi/cycle. 0 = không cap. |
| `ICLOUD_LIMITED_TTL_HOURS` | `24` | Profile `limited` cooldown (R2.9) |
| `ICLOUD_QUOTA_RETRY_MINUTES` | `15` | Profile `quota_full` cooldown (R2.13) |
| `ICLOUD_HME_QUOTA_LIMIT` | `700` | Apple-side soft cap per account (R2.14) |
| `ICLOUD_HME_PROFILE_PARALLELISM` | `1` | Số profile chạy song song (R3.17) |
| `ICLOUD_HME_HTTP_TIMEOUT_SEC` | `30` | HTTP timeout per request (R11.7) |
| `ICLOUD_HME_RACE_RETRY_MAX` | `3` | Retry max khi `HmeReserveTaken` (R3.14) |
| `ICLOUD_INFINITE_WAIT_MAX_SEC` | `86400` | Cap sleep ở Pool_Exhausted_Wait (R3.23) |
| `ICLOUD_RECORDING_RETENTION_DAYS` | (none) | Cleanup recording cũ > N ngày (R1.8) |
| `ICLOUD_AUDIT_RETENTION_DAYS` | (none) | Cleanup audit cũ > N ngày (R6.5) |
| `ICLOUD_API_AUTH_TOKEN` | (none) | Web_API Bearer token. **Web server fail-fast nếu unset** (R10.10a) |

Settings validate fail-fast: `ICLOUD_RETRY_INTERVAL < 10` hoặc `ICLOUD_MAX_ERRORS_PER_CYCLE < 0` → `Settings.from_env` raise lỗi cấu hình.

### 11.2 Database

SQLite tại `runtime/data.db` (schema **v7**).

Tables:
- `icloud_accounts` — pool profiles
- `icloud_emails` — HME email rows + lifecycle timestamp
- `icloud_audit_log` — audit trail append-only
- `pool_state` — round_robin_cursor key/value

Migration v6 → v7 chạy tự động lần đầu mở engine (DROP TABLE `icloud_jobs` nếu còn từ schema cũ).

### 11.3 Profile_Lock (R12.14, R12.15, R12.16)

Mỗi Apple_ID có 1 lock dir tại `runtime/icloud_profiles/<apple_id>/.lock/`:
- `write.lock` — exclusive cho `Bootstrap_Flow` + `Recorder.start_session`.
- `read.count` + `read.sentinel` — shared cho `extract_session_bundle`.

Pattern: writer block mọi reader; reader block writer; nhiều reader OK đồng thời.

---

## 12. Troubleshooting

### `BootstrapError: cookie_verify_failed_after_retry`

Cookies `X-APPLE-WEBAUTH-*` không được set sau khi user nhấn Enter. Khả năng:
- User chưa login đầy đủ (chưa pass 2FA).
- iCloud webapp chưa init xong khi nhấn Enter (đợi thấy inbox rồi mới Enter).
- Apple đổi cookie format (rare; check log để biết marker nào miss).

### `IcloudPoolError: pool_exhausted`

Mọi profile đều `limited`/`session_expired`/`disabled`/`deleted`. Cần:
- Đợi profile `limited` recover (qua `Limited_TTL`).
- `bootstrap` lại profile `session_expired`.
- Add profile mới qua `bootstrap --apple-id <new_id>`.

Khi chạy qua Runner, pool exhausted ở cycle hiện tại → cycle kết thúc, Runner sleep `retry_interval` rồi cycle kế tiếp tự retry — không cần can thiệp.

### `IcloudPoolError: pool_pick_locked`

2 process đang concurrent gọi `pick_active_profile()` — write-lock SQLite timeout 5s. Caller (Generator/Checker) tự retry. Nếu lặp lại, giảm `ICLOUD_HME_PROFILE_PARALLELISM` xuống 1.

### `HmeReserveTaken` lặp lại nhiều

Race với account khác cùng pick candidate. Tool retry tối đa `ICLOUD_HME_RACE_RETRY_MAX` lần. Nếu vẫn fail → mark profile fatal trong batch (KHÔNG mark `limited` vì race ≠ rate-limit, theo Property 5).

### `RuntimeError: Runner đang chạy action khác`

Single-instance guard: process đã có Runner đang chạy, không thể `start()` action thứ 2. Gọi `stop()` action đang chạy trước, hoặc dùng web `POST /api/icloud/run/stop`.

### Web UI: `503 Server unconfigured`

Env `ICLOUD_API_AUTH_TOKEN` chưa set. Set qua `.env` rồi restart server (R10.10a fail-fast).

### Profile bị `quota_full` mãi

`hme_count >= 700` (Apple soft cap). Xóa email Apple-side trước (qua `email delete` hoặc `list-sync`) để free slot. Hoặc tăng `ICLOUD_HME_QUOTA_LIMIT` qua env nếu Apple cấp slot cao hơn.

---

## 13. Test

```bash
# Syntax check toàn bộ module
python3 test/syntax_check.py

# Smoke + PBT suite
python3 test/run_all_pbt.py

# Single test
python3 test/check_engine_reentrant.py
python3 test/test_pool_pick_round_robin.py
```

**Property tests** cover các property core của pool/generator/checker/manager:
- Pool round-robin + atomic pick (P1, P2, P28)
- Pool atomicity state + audit (P3)
- HmeClient classification + request format (P4, P15)
- Candidate retry không tính fail (P5)
- Label_Default format (P6)
- Profile_Checker status mapping (P7)
- Profile delete preserves emails (P8)
- Audit list ordering + retention cleanup (P9, P10)
- Status conservation + low_capacity (P11)
- Reconcile diff symmetric (P12)
- Bulk lifecycle delay (P13)
- Dry-run no-side-effect (P14)
- Session_Bundle validation + cache reuse (P16, P17)
- Email lifecycle transitions (P18)
- list_sync 3-branch UPDATE diff + audit reason (P19, P23 — refactor B)
- Quota_full transition (P24)
- Pool_Exhausted_Wait compute + cancellable (P25, P26)
- Profile_Lock concurrent safety (P29)
- Timestamp_Format consistency (P30)

Runner-level property test (R1.1, R2.1, R3.1, R4.1, R5.1) ở `test/test_runner_properties.py` (spec icloud-runner-loop).

---

## 14. References

- Spec: `.kiro/specs/icloud-hme-pool/{requirements,design,tasks}.md`
- Spec Runner refactor: `.kiro/specs/icloud-runner-loop/{requirements,design,tasks}.md`
- Reverse-engineering Apple HME API: project `rtunazzz/hidemyemail-generator` (MIT)
- Project rules: `AGENTS.md`, `.kiro/steering/project-rules.md`

---

## 15. Phụ lục — Apple HME endpoints (refactor B — cookies-only)

Host: `https://p68-maildomainws.icloud.com` (hardcode cho mọi account — Apple HME REST API chỉ phục vụ trên 1 host duy nhất; verified với rtunazzz/hidemyemail-generator + nội bộ `test/check_hme_minimal_call.py`).

| Method | Path | Mô tả |
|---|---|---|
| POST | `/v1/hme/generate` | Sinh candidate (chưa reserve) |
| POST | `/v1/hme/reserve` | Chốt candidate → email thật |
| GET | `/v2/hme/list` | List mọi HME đã reserve |
| POST | `/v1/hme/deactivate` | Set `isActive=false` |
| POST | `/v1/hme/reactivate` | Reactivate đã deactivate |
| POST | `/v1/hme/delete` | Xóa hẳn (free slot) |
| POST | `/v1/hme/updateMetaData` | Update label/note |

4 query param trên mọi request: `clientBuildNumber`, `clientMasteringNumber`, `clientId`, `dsid`. `clientBuildNumber` + `clientMasteringNumber` hardcode value từ webapp Apple; `clientId` + `dsid` cố định empty string — Apple HME API không enforce auth qua các query param này.

Header cố định: `Origin: https://www.icloud.com`, `Referer: https://www.icloud.com/`, `Content-Type: text/plain`, `Accept: */*`, `User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ... Chrome/141.0.0.0 ...` (placeholder). KHÔNG gửi `scnt` hoặc `X-Apple-ID-Session-Id` — auth duy nhất qua cookie `X-APPLE-WEBAUTH-*`.

Cookies set qua `httpx.cookiejar` với domain `.icloud.com` (KHÔNG paste header `Cookie:` thủ công, R11.8).
