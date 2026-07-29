# Plan: Bỏ Job Layer — Chuyển sang Runner (Infinite Loop) + Log Viewer

## Mục tiêu

Xoá toàn bộ `icloud_hme/jobs/` (~1,500 LOC) và thay bằng module `Runner` gọn nhẹ.
Logic core (generator, checker, pool, manager) **giữ nguyên 100%**.
UI chuyển từ quản lý job (enqueue/pause/resume/restart) sang **log viewer real-time** với nút Start/Stop.

**Quan trọng:** Runner chạy **loop vĩnh viễn** — không bao giờ tự dừng.
Hết 1 vòng (tất cả profiles đã xử lý hoặc skip) → đợi `retry_interval` → chạy lại.
Chỉ dừng khi user bấm **Stop** hoặc gửi SIGINT.

---

## Lý do

- Job layer là thin wrapper: mỗi handler trong `jobs/handlers.py` chỉ gọi 1 hàm service rồi serialize kết quả (10–20 dòng/handler).
- `HmeGenerator.generate()` đã có sẵn `cancellation_event` + `pause_event` + pool exhausted wait logic.
- State machine 6 trạng thái, JSONL file logging, restart chain, crash detection — quá mức cần thiết cho loop-based execution.
- User chỉ cần: bấm Run → xem log real-time → bấm Stop.

---

## Luồng chạy chính (Infinite Loop)

```
User bấm Start (hoặc CLI chạy command)
        │
        ▼
┌─ LOOP FOREVER ─────────────────────────────────────────┐
│                                                         │
│   Cycle #N bắt đầu                                     │
│   ┌─ FOR each profile in pool ───────────────────────┐  │
│   │                                                   │  │
│   │  pick_active_profile() → profile                  │  │
│   │      │                                            │  │
│   │      ├─ OK → extract session → generate/check     │  │
│   │      │       │                                    │  │
│   │      │       ├─ Success → ghi log, continue       │  │
│   │      │       ├─ QuotaError → mark_limited, skip   │  │
│   │      │       ├─ AuthError → mark_expired, skip    │  │
│   │      │       └─ ClientError → ghi log, skip       │  │
│   │      │                                            │  │
│   │      └─ Pool exhausted (ko còn profile eligible)  │  │
│   │          → break inner loop                       │  │
│   │                                                   │  │
│   └───────────────────────────────────────────────────┘  │
│                                                         │
│   Cycle #N kết thúc                                     │
│   Log: "Cycle #N done. created=X, skipped=Y, errors=Z"  │
│   Log: "Waiting {retry_interval}s before next cycle..."  │
│                                                         │
│   ┌─ SLEEP retry_interval ───────────────────────────┐  │
│   │  - Sleep chunks 1s, mỗi giây check cancel_event  │  │
│   │  - Nếu cancel → break LOOP FOREVER                │  │
│   │  - Nếu pause → await resume, rồi tiếp sleep      │  │
│   └──────────────────────────────────────────────────┘  │
│                                                         │
│   Quay lại đầu loop → Cycle #(N+1)                     │
│   (profiles bị limited có thể đã hết TTL → eligible lại)│
│                                                         │
└─────────────────────────────────────────────────────────┘
        │
        ▼ (chỉ khi user Stop / SIGINT)
   Runner trả final summary
```

### Khi nào profile "sống lại" giữa các cycle?

- `limited` profile: `limited_until` hết hạn (default 24h) → `pick_active_profile()` tự transition về `active`
- `quota_full` profile: `quota_retry_until` hết hạn (default 15 min) → pick tự transition
- `session_expired` profile: **KHÔNG tự recover** — cần user chạy `bootstrap` hoặc `profile open` lại

→ Retry interval phù hợp = **15–30 phút** (match `quota_retry_minutes`). Nhưng configurable.

---

## Kiến trúc mới

```
┌────────────────────────────────────────────────┐
│  CLI / Web UI                                   │
│  - Start(action, params)                        │
│  - Stop                                         │
│  - Real-time log stream (SSE)                   │
└──────────────┬─────────────────────────────────┘
               │
┌──────────────▼─────────────────────────────────┐
│  Runner (file mới: icloud_hme/runner.py)        │
│  - Infinite loop: cycle → wait → cycle → ...   │
│  - State: idle / running / stopping             │
│  - Configurable retry_interval (default 900s)   │
│  - Emit log events qua callback                 │
│  - Cancel qua asyncio.Event                     │
│  - Guard chống chạy đồng thời (is_running flag) │
└──────────────┬─────────────────────────────────┘
               │
┌──────────────▼─────────────────────────────────┐
│  Service layer (KHÔNG ĐỔI)                      │
│  - HmeGenerator.generate()                      │
│  - HmeManager.deactivate_bulk() / list_sync()   │
│  - ProfileChecker.check_all()                   │
│  - IcloudPoolManager                            │
│  - Recorder                                     │
└────────────────────────────────────────────────┘
```

---

## Cấu hình Runner

| Config | Env var | Default | Mô tả |
|--------|---------|---------|-------|
| `retry_interval` | `ICLOUD_RETRY_INTERVAL` | `900` (15 min) | Thời gian đợi giữa 2 cycle (seconds) |
| `delay_between_profiles` | (dùng generator delay_range) | `2.0–5.0s` | Delay giữa 2 email trong cùng profile |
| `max_errors_per_cycle` | `ICLOUD_MAX_ERRORS_PER_CYCLE` | `0` (unlimited) | Số lỗi liên tiếp trước khi skip luôn cycle (0=chạy hết) |

Thêm vào `config.py` → `Settings` dataclass.

---

## Thiết kế Runner

### File: `icloud_hme/runner.py`

```python
class HmeRunner:
    """Infinite-loop runner thay thế JobManager.
    
    Chạy vĩnh viễn: cycle qua profiles → đợi retry_interval → cycle lại.
    Chỉ dừng khi cancel_event được set (user Stop / SIGINT).
    """
    
    def __init__(self, *, generator, checker, hme_manager, pool_manager, 
                 pool_repo, audit_repo, settings, log_callback,
                 retry_interval: int = 900):
        self._generator = generator
        self._checker = checker
        self._hme_manager = hme_manager
        self._pool_mgr = pool_manager
        self._pool_repo = pool_repo
        self._audit_repo = audit_repo
        self._settings = settings
        self._log_cb = log_callback  # async callable(level, message, payload)
        self._retry_interval = retry_interval
        
        self._cancel_event: asyncio.Event | None = None
        self._pause_event: asyncio.Event | None = None
        self._resume_event: asyncio.Event | None = None
        self._running = False
        self._current_action: str | None = None
        self._cycle_count = 0
        self._total_stats = {"created": 0, "errors": 0, "skipped": 0}
    
    @property
    def is_running(self) -> bool: ...
    
    @property
    def current_action(self) -> str | None: ...
    
    @property
    def cycle_count(self) -> int: ...
    
    @property
    def stats(self) -> dict: ...

    async def start(self, action: str, params: dict) -> dict:
        """Bắt đầu infinite loop. Raise nếu đã running.
        
        Actions:
            - generate: params={count_per_cycle, label, note, proxy}
              count_per_cycle = số email tạo MỖI cycle. None = chạy tới khi
              hết profile eligible trong cycle đó.
            - check_all: params={auto_mark, proxy}
            - deactivate_bulk: params={emails, dry_run}
            - reactivate_bulk: params={emails, dry_run}  
            - delete_bulk: params={emails, dry_run}
            - update_meta_bulk: params={items, dry_run}
            - list_sync: params={apple_id}
        
        KHÔNG return cho tới khi bị cancel (loop vĩnh viễn).
        Khi cancel → return summary dict của toàn bộ session.
        """
        if self._running:
            raise RuntimeError("Runner đang chạy action khác")
        
        self._running = True
        self._current_action = action
        self._cycle_count = 0
        self._total_stats = {"created": 0, "errors": 0, "skipped": 0}
        self._cancel_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        self._resume_event = asyncio.Event()
        
        try:
            await self._log_cb("info", f"Runner started: {action}", {"params": params})
            
            # ── INFINITE LOOP ──────────────────────────────────
            while not self._cancel_event.is_set():
                self._cycle_count += 1
                await self._log_cb("info", f"── Cycle #{self._cycle_count} ──", {})
                
                # Chạy 1 cycle
                cycle_result = await self._run_one_cycle(action, params)
                
                # Cập nhật tổng stats
                self._total_stats["created"] += cycle_result.get("created", 0)
                self._total_stats["errors"] += len(cycle_result.get("failures", []))
                self._total_stats["skipped"] += len(cycle_result.get("disabled_profiles", []))
                
                await self._log_cb("info", 
                    f"Cycle #{self._cycle_count} done: {cycle_result}",
                    {"cycle": self._cycle_count})
                
                # Check cancel trước khi sleep
                if self._cancel_event.is_set():
                    break
                
                # ── WAIT retry_interval (sleep chunks 1s) ──────
                await self._log_cb("info", 
                    f"Waiting {self._retry_interval}s before next cycle...",
                    {"retry_interval": self._retry_interval})
                
                interrupted = await self._interruptible_sleep(self._retry_interval)
                if interrupted:
                    break
            # ── END LOOP ───────────────────────────────────────
            
            summary = {
                "total_cycles": self._cycle_count,
                **self._total_stats,
                "stopped_by": "user",
            }
            await self._log_cb("info", f"Runner stopped. Summary: {summary}", summary)
            return summary
            
        except Exception as exc:
            await self._log_cb("error", f"Runner fatal error: {exc}", {})
            raise
        finally:
            self._running = False
            self._current_action = None
    
    def stop(self):
        """Signal cancel — loop sẽ dừng sau khi hoàn thành unit work hiện tại."""
        if self._cancel_event:
            self._cancel_event.set()
    
    def pause(self):
        """Pause — loop sẽ tạm dừng ở checkpoint tiếp theo."""
        if self._pause_event:
            self._pause_event.set()
    
    def resume(self):
        """Resume sau pause."""
        if self._resume_event:
            self._resume_event.set()
            if self._pause_event:
                self._pause_event.clear()
    
    async def _run_one_cycle(self, action: str, params: dict) -> dict:
        """Chạy 1 cycle — dispatch sang service layer.
        
        Với generate: gọi generator.generate() với count=count_per_cycle.
        Generator tự handle: pick profile → generate → quota/auth error → switch.
        Khi pool exhausted → generator return (KHÔNG wait trong generator,
        wait ở outer loop của Runner).
        """
        if action == "generate":
            result = await self._generator.generate(
                count=params.get("count_per_cycle"),
                infinite=False,  # bounded per cycle, Runner loop là infinite
                label=params.get("label"),
                note=params.get("note"),
                proxy=params.get("proxy"),
                cancellation_event=self._cancel_event,
                pause_event=self._pause_event,
                resume_event=self._resume_event,
            )
            return {
                "created": result.created,
                "requested": result.requested,
                "failures": [
                    {"apple_id": f.apple_id, "error": f.error}
                    for f in result.failures
                ],
                "disabled_profiles": result.disabled_profiles,
            }
        
        elif action == "check_all":
            results = await self._checker.check_all(
                auto_mark=params.get("auto_mark", True),
                proxy=params.get("proxy"),
            )
            return {
                "checked": len(results),
                "ok": sum(1 for r in results if r.ok),
                "failed": sum(1 for r in results if not r.ok),
            }
        
        elif action in ("deactivate_bulk", "reactivate_bulk", "delete_bulk"):
            method = getattr(self._hme_manager, action)
            result = await method(
                params.get("emails", []),
                dry_run=params.get("dry_run", False),
            )
            return {"succeeded": result.succeeded, "failed": result.failed}
        
        elif action == "list_sync":
            diff = await self._hme_manager.list_sync(params["apple_id"])
            return {
                "inserted_active": diff.inserted_active,
                "inserted_inactive": diff.inserted_inactive,
                "unchanged": diff.unchanged,
            }
        
        else:
            raise ValueError(f"Unknown action: {action}")
    
    async def _interruptible_sleep(self, seconds: int) -> bool:
        """Sleep chunks 1s, check cancel/pause mỗi giây.
        
        Returns True nếu bị cancel (caller nên break loop).
        """
        for _ in range(seconds):
            if self._cancel_event and self._cancel_event.is_set():
                return True
            
            # Check pause
            if self._pause_event and self._pause_event.is_set():
                await self._log_cb("info", "Paused. Waiting for resume...", {})
                if self._resume_event:
                    await self._resume_event.wait()
                    self._resume_event.clear()
                    self._pause_event.clear()
                await self._log_cb("info", "Resumed.", {})
            
            await asyncio.sleep(1.0)
        
        return False
```

### Hai mode chạy generate

| Mode | Param | Hành vi |
|------|-------|---------|
| **Bounded per cycle** | `count_per_cycle=50` | Mỗi cycle tạo tối đa 50 email → hết 50 hoặc hết profile → đợi retry → cycle mới |
| **Drain all** | `count_per_cycle=None` | Mỗi cycle chạy tới khi pool exhausted → đợi retry → cycle mới (khi profile recover) |

Cả 2 mode đều loop vĩnh viễn. Khác nhau ở lượng work per cycle.

### Log callback

Runner nhận `log_callback` — async callable emit log events. Caller quyết định gửi đi đâu:
- **CLI**: print ra stderr (giống hiện tại `_emit_log`)
- **Web**: broadcast qua SSE (giống hiện tại nhưng không qua JSONL file)

```python
# CLI usage
async def cli_log(level, message, payload=None):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}][{level}] {message}", file=sys.stderr)

# Web usage — emit SSE event
async def sse_log(level, message, payload=None):
    event = {"ts": utc_now_iso(), "level": level, "message": message, "payload": payload or {}}
    await sse_broadcast(event)
```

---

## Các bước thực hiện

### Phase 1: Tạo Runner (KHÔNG phá code cũ)

- [ ] **1.1** Thêm config mới vào `config.py` → `Settings`:
  - `icloud_retry_interval: int` — env `ICLOUD_RETRY_INTERVAL`, default 900
  - `icloud_max_errors_per_cycle: int` — env `ICLOUD_MAX_ERRORS_PER_CYCLE`, default 0
- [ ] **1.2** Tạo file `icloud_hme/runner.py` với class `HmeRunner` theo thiết kế trên
  - Infinite loop: cycle → wait retry_interval → cycle
  - `_run_one_cycle()` dispatch sang service layer
  - `_interruptible_sleep()` sleep chunks 1s + check cancel/pause
  - `stop()` / `pause()` / `resume()` qua asyncio.Event
  - Stats tracking: cycle_count, total created/errors/skipped
- [ ] **1.3** Test Runner hoạt động đúng bằng cách gọi CLI generate qua Runner
  - Verify: chạy → tạo email → hết profile → đợi retry_interval → chạy lại
  - Verify: Ctrl+C dừng graceful giữa cycle hoặc giữa sleep

### Phase 2: Migrate CLI sang Runner

- [ ] **2.1** Sửa `cli.py` — command `generate` chạy qua Runner infinite loop:
  ```bash
  # Chạy vĩnh viễn, drain all profiles mỗi cycle, đợi 15 min giữa cycles
  python -m gpt_signup_hybrid.icloud_hme generate
  
  # Chạy vĩnh viễn, mỗi cycle tạo tối đa 50 email
  python -m gpt_signup_hybrid.icloud_hme generate --count-per-cycle 50
  
  # Tuỳ chỉnh retry interval
  python -m gpt_signup_hybrid.icloud_hme generate --retry-interval 600
  
  # Dừng: Ctrl+C (SIGINT) → Runner.stop() → hoàn thành unit work hiện tại → exit
  ```
  - CLI log callback = print stderr
  - SIGINT handler gọi `Runner.stop()` → graceful shutdown
  - Xoá flag `--infinite` (mọi lần chạy đều là infinite loop)
  - Thêm flag `--count-per-cycle` (optional, default None = drain all)
  - Thêm flag `--retry-interval` (optional, default 900s)
- [ ] **2.2** Sửa `cli.py` — command `check` chạy qua Runner infinite loop:
  ```bash
  # Check all profiles liên tục, retry mỗi 15 min
  python -m gpt_signup_hybrid.icloud_hme check --all
  ```
- [ ] **2.3** Giữ nguyên các command KHÔNG cần loop vĩnh viễn:
  - `bootstrap` — chạy 1 lần, headed browser, user interact tay
  - `profile open` — chạy 1 lần, headed browser
  - `profile delete` — chạy 1 lần
  - `status` — chạy 1 lần, in report
  - `reconcile` — chạy 1 lần
  - `email deactivate/reactivate/delete/mark-used/update-meta/list-sync/export` — chạy 1 lần, gọi service trực tiếp (KHÔNG qua Runner)
  - `audit list/cleanup` — chạy 1 lần

### Phase 3: Migrate Web sang Runner  

- [ ] **3.1** Tạo web endpoint mới:
  - `POST /api/icloud/run` — body: `{action, params, retry_interval?}` → start Runner loop → return `{ok, action}`
    - Spawn `asyncio.create_task(runner.start(...))` — không block response
    - Return 409 nếu đã running
  - `POST /api/icloud/run/stop` — gọi Runner.stop() → return `{ok}`
  - `POST /api/icloud/run/pause` — gọi Runner.pause() → return `{ok}`
  - `POST /api/icloud/run/resume` — gọi Runner.resume() → return `{ok}`
  - `GET /api/icloud/run/status` — return:
    ```json
    {
      "running": true,
      "action": "generate",
      "cycle": 3,
      "stats": {"created": 150, "errors": 2, "skipped": 1},
      "retry_interval": 900,
      "next_cycle_at": "2026-05-24T10:30:00Z"
    }
    ```
  - `GET /api/icloud/run/log` — SSE stream real-time log events
- [ ] **3.2** Web layer giữ in-memory log buffer (list[dict]) cho current run + optional ghi ra file text
  - Buffer clear khi start run mới
  - SSE endpoint stream từ buffer + subscribe new events
  - Endpoint `GET /api/icloud/run/log?offset=N` — paginated log history
  - Log buffer capped (giữ tối đa 10,000 entries, xoá cũ nhất khi đầy)
- [ ] **3.3** Xoá các web endpoint job cũ:
  - Xoá: `POST /api/icloud/emails/generate` (enqueue job)
  - Xoá: `GET /api/icloud/jobs/{job_id}`
  - Xoá: `POST /api/icloud/jobs/{job_id}/{action}` (stop/pause/resume/restart)
  - Xoá: `GET /api/icloud/jobs/{job_id}/log`
  - Xoá: `GET /api/icloud/jobs/{job_id}/log/stream`
  - Xoá: `GET /api/icloud/jobs` (list jobs)

### Phase 4: Xoá Job layer

- [ ] **4.1** Xoá thư mục `icloud_hme/jobs/` hoàn toàn (12 files, ~1,500 LOC):
  ```
  icloud_hme/jobs/__init__.py
  icloud_hme/jobs/manager.py        (587 LOC — JobManager)
  icloud_hme/jobs/handlers.py       (342 LOC — 9 handler functions)
  icloud_hme/jobs/generate.py       (68 LOC)
  icloud_hme/jobs/bootstrap.py      (73 LOC)
  icloud_hme/jobs/check_all.py      (64 LOC)
  icloud_hme/jobs/deactivate_bulk.py (60 LOC)
  icloud_hme/jobs/delete_bulk.py    (35 LOC)
  icloud_hme/jobs/reactivate_bulk.py (35 LOC)
  icloud_hme/jobs/update_meta_bulk.py (35 LOC)
  icloud_hme/jobs/list_sync.py      (58 LOC)
  icloud_hme/jobs/export.py         (63 LOC)
  ```
- [ ] **4.2** Xoá job CLI commands từ `cli.py`:
  ```
  job_app typer group + tất cả:
    job enqueue / job list / job get / job status / job stop / job pause / job resume / job restart / job stop-all / job log
  ```
  Khoảng ~200 LOC xoá từ cli.py (line 1053–1295)
- [ ] **4.3** Xoá `JobRecord` + `JobLogEntry` dataclass từ `models.py` (line 202–228)
- [ ] **4.4** Xoá job-related exceptions từ `exceptions.py`:
  ```
  JobError, JobNotFoundError, JobInvalidTransitionError, JobCrashedError
  ```
- [ ] **4.5** Xoá `IcloudJobRepository` từ `db/repositories.py` (tìm class bắt đầu ~line 1894)
- [ ] **4.6** Xoá DDL `icloud_jobs` table + indexes từ `db/schema.py`:
  - `DDL_ICLOUD_JOBS` (line 193–214)
  - `DDL_ICLOUD_JOBS_INDEXES` (line 217–226)
  - Xoá khỏi `ALL_DDL` list (line 244–245)
  - **KHÔNG xoá** migration entry `MIGRATIONS[6]` phần icloud_jobs — giữ để DB cũ không lỗi khi migrate. Chỉ thêm migration v7 để DROP TABLE
- [ ] **4.7** Thêm migration v7 vào `db/schema.py`:
  ```python
  7: [
      "DROP TABLE IF EXISTS icloud_jobs;",
  ]
  ```
  Cập nhật `CURRENT_VERSION = 7`
- [ ] **4.8** Xoá tất cả test files liên quan job:
  ```
  test/check_icloud_job_repository.py
  test/check_job_handlers_dispatch.py
  ```
- [ ] **4.9** Xoá import job từ `icloud_hme/__init__.py` (nếu có re-export)

### Phase 5: Update Web UI (frontend)

- [ ] **5.1** Thay thế Job tab/panel bằng Log Viewer:
  - Real-time log stream panel (auto-scroll, monospace font)
  - Hiển thị: timestamp + level (color-coded: info=xanh, warn=vàng, error=đỏ) + message
  - Filter by level (info/warn/error)
  - Hiển thị cycle separator: `── Cycle #3 ──` nổi bật
  - Hiển thị countdown: "Waiting 14:32 before next cycle..." (cập nhật mỗi giây)
- [ ] **5.2** Panel điều khiển:
  - Dropdown chọn action: `generate` (default) / `check_all`
  - Form params cho generate:
    - Input: `count_per_cycle` (optional, placeholder "All = drain tới hết")
    - Input: `label` (optional)
    - Input: `note` (optional)
    - Input: `retry_interval` (default 900, unit seconds)
    - Input: `proxy` (optional)
  - Nút **Start** (disabled khi running) — xanh lá
  - Nút **Stop** (enabled khi running) — đỏ
  - Status badge: `IDLE` (xám) / `RUNNING` (xanh) / `STOPPING...` (vàng)
  - Stats live: `Cycle #3 | Created: 150 | Errors: 2 | Skipped: 1`
- [ ] **5.3** Profile status sidebar:
  - Hiển thị list profiles + status (active/limited/quota_full/session_expired)
  - Badge màu theo status: active=xanh, limited=vàng, quota_full=cam, expired=đỏ
  - Hiển thị `hme_count / 700` (quota bar)
  - Hiển thị `limited_until` hoặc `quota_retry_until` countdown nếu có
  - Auto-refresh mỗi 30s hoặc khi nhận log event profile transition
- [ ] **5.4** Responsive layout:
  ```
  ┌──────────────────────────────────────────────────────────┐
  │  [Generate ▼] [count_per_cycle] [retry: 900s]  [START]  │
  │  Status: RUNNING  Cycle #3  Created: 150  Errors: 2     │
  ├────────────────────────────────┬─────────────────────────┤
  │  LOG VIEWER (80%)              │  PROFILES (20%)         │
  │                                │                         │
  │  10:30:01 [info] Cycle #3     │  ● user1@icloud.com     │
  │  10:30:02 [info] profile →    │    active  420/700       │
  │    user1@icloud.com            │                         │
  │  10:30:05 [info] created      │  ● user2@icloud.com     │
  │    abc@privaterelay.com        │    limited  until 11:00  │
  │  10:30:08 [warn] QuotaError   │                         │
  │    → mark_limited             │  ● user3@icloud.com     │
  │  10:30:08 [info] Cycle #3     │    quota_full  700/700   │
  │    done. created=5             │                         │
  │  10:30:08 [info] Waiting      │                         │
  │    900s before next cycle...   │                         │
  │    ▓▓▓▓▓░░░░░░░░ 5:32 left    │                         │
  │                                │                         │
  └────────────────────────────────┴─────────────────────────┘
  ```

### Phase 6: Cleanup + Docs

- [ ] **6.1** Update `CLAUDE.md` — xoá references đến job commands + thêm runner docs
- [ ] **6.2** Update `.kiro/specs/icloud-hme-pool/` — requirements + design + tasks
- [ ] **6.3** Xoá `runtime/icloud_jobs/` directory (JSONL log files cũ) trong README/setup
- [ ] **6.4** Verify: chạy CLI generate bounded + infinite + check + email lifecycle — tất cả phải work qua Runner

---

## Files bị ảnh hưởng (tổng hợp)

### Xoá hoàn toàn
| File | LOC | Lý do |
|------|-----|-------|
| `icloud_hme/jobs/__init__.py` | 43 | Job package |
| `icloud_hme/jobs/manager.py` | 587 | JobManager |
| `icloud_hme/jobs/handlers.py` | 342 | Handler dispatch |
| `icloud_hme/jobs/generate.py` | 68 | Generate handler |
| `icloud_hme/jobs/bootstrap.py` | 73 | Bootstrap handler |
| `icloud_hme/jobs/check_all.py` | 64 | Check handler |
| `icloud_hme/jobs/deactivate_bulk.py` | 60 | Deactivate handler |
| `icloud_hme/jobs/delete_bulk.py` | 35 | Delete handler |
| `icloud_hme/jobs/reactivate_bulk.py` | 35 | Reactivate handler |
| `icloud_hme/jobs/update_meta_bulk.py` | 35 | Update meta handler |
| `icloud_hme/jobs/list_sync.py` | 58 | List sync handler |
| `icloud_hme/jobs/export.py` | 63 | Export handler |
| `test/check_icloud_job_repository.py` | - | Job repo test |
| `test/check_job_handlers_dispatch.py` | - | Handler test |
| **Total xoá** | **~1,463+** | |

### Tạo mới
| File | Est. LOC | Mô tả |
|------|----------|-------|
| `icloud_hme/runner.py` | ~200 | HmeRunner class |

### Sửa
| File | Thay đổi |
|------|----------|
| `icloud_hme/cli.py` | Xoá job_app group (~200 LOC), optional wire CLI qua Runner |
| `icloud_hme/models.py` | Xoá `JobRecord` + `JobLogEntry` (~26 LOC) |
| `icloud_hme/exceptions.py` | Xoá 4 Job exceptions |
| `icloud_hme/__init__.py` | Xoá job imports/exports |
| `icloud_hme/web/router.py` | Xoá job endpoints, thêm runner endpoints |
| `db/repositories.py` | Xoá `IcloudJobRepository` class |
| `db/schema.py` | Xoá DDL_ICLOUD_JOBS, thêm migration v7 DROP TABLE |

### KHÔNG ĐỘNG VÀO (giữ nguyên 100%)
| File | LOC | Lý do |
|------|-----|-------|
| `icloud_hme/generator.py` | 842 | Core generate logic — Runner gọi trực tiếp |
| `icloud_hme/checker.py` | 339 | Core check logic |
| `icloud_hme/pool.py` | 654 | Pool state machine |
| `icloud_hme/manager.py` | 1194 | HME lifecycle manager |
| `icloud_hme/client.py` | - | Apple API client |
| `icloud_hme/session.py` | - | Session bundle extractor |
| `icloud_hme/bootstrap.py` | - | Bootstrap flow |
| `icloud_hme/recorder.py` | - | Recording sessions |
| `icloud_hme/profile_lock.py` | - | Profile locking |

---

## Rủi ro + Mitigation

| Rủi ro | Mitigation |
|--------|------------|
| Web chạy 2 generate đồng thời | `HmeRunner.is_running` guard — return 409 Conflict |
| Mất log khi process crash | Optional: ghi log ra text file song song SSE. Crash = restart lại, không cần recover state — loop tự chạy lại |
| Pause/resume bị mất khi bỏ job | `HmeGenerator.generate()` vẫn nhận `pause_event`/`resume_event` — Runner expose `.pause()` / `.resume()` trực tiếp |
| Bootstrap cần headed browser | Bootstrap giữ nguyên flow CLI blocking — KHÔNG đi qua Runner (vì cần user interact tay) |
| DB icloud_jobs còn data cũ | Migration v7 DROP TABLE — data cũ không cần giữ |
| Generator `infinite=True` mode conflict | Runner gọi `generate(infinite=False, count=count_per_cycle)` — Runner quản lý infinite loop, KHÔNG phải Generator. Generator chạy bounded per cycle, return khi xong hoặc pool exhausted |
| Retry interval quá ngắn gây rate limit | Default 900s (15 min) khớp `quota_retry_minutes`. UI cho phép tuỳ chỉnh nhưng warn nếu < 300s |

---

## Lưu ý quan trọng về Generator

Generator hiện có `infinite=True` mode với `_pool_exhausted_wait()` bên trong (sleep rồi retry pick).
**Runner KHÔNG dùng mode này.** Thay vào đó:

- Runner gọi `generate(infinite=False, count=count_per_cycle)` → Generator chạy bounded, return khi:
  - Tạo đủ `count_per_cycle` email, HOẶC
  - Pool exhausted (không còn profile eligible), HOẶC
  - Cancel event
- **Runner** quản lý retry: đợi `retry_interval` rồi gọi lại `generate()` cycle mới
- Tách biệt rõ: **Generator = worker 1 cycle**, **Runner = loop controller**

Nếu `count_per_cycle=None` → Generator chạy tới khi pool exhausted rồi return (bounded mode với count rất lớn hoặc xử lý None như "chạy tới hết").

**Cần kiểm tra**: `generate(count=None, infinite=False)` hiện tại xử lý thế nào khi count=None + infinite=False? Nếu nó raise error, cần sửa Generator để accept `count=None, infinite=False` = "chạy tới khi pool exhausted" (bounded nhưng không giới hạn số lượng, chỉ giới hạn bởi pool capacity).

---

## Thứ tự ưu tiên

1. **Phase 1**: Tạo Runner + config
2. **Phase 4**: Xoá Job layer (có thể làm song song với Phase 1 vì Runner không depend vào Job)
3. **Phase 2**: Wire CLI — thay generate/check command
4. **Phase 3**: Wire Web endpoints
5. **Phase 5**: Frontend Log Viewer UI
6. **Phase 6**: Cleanup docs

---

## Quy tắc khi implement

- **KHÔNG sửa** generator.py, checker.py, pool.py, manager.py, client.py, session.py, bootstrap.py, recorder.py, profile_lock.py — TRỪ KHI cần handle `count=None, infinite=False` case (xem note trên)
- Runner phải gọi service layer **y hệt** cách handlers.py đang gọi — chỉ bỏ lớp job wrapper
- Runner loop KHÔNG BAO GIỜ tự dừng — chỉ dừng khi `cancel_event.is_set()`
- Log callback phải async (để web có thể await SSE broadcast)
- Test bằng cách chạy CLI thực tế:
  ```bash
  # Chạy → tạo email → hết profile → đợi 10s → cycle mới → Ctrl+C
  python -m gpt_signup_hybrid.icloud_hme generate --retry-interval 10
  ```
- Mọi endpoint web mới phải require auth (Bearer token) giống cũ
- Mọi sleep trong Runner phải interruptible (chunks 1s + check cancel)
