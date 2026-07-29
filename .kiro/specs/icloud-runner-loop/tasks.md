# Implementation Plan: iCloud Runner Loop

## Overview

Refactor lớp `icloud_hme/jobs/` (~1.500 LOC) thành module `HmeRunner` (~250 LOC) chạy infinite loop. Core service layer (generator/checker/manager/pool) giữ nguyên 100%; Runner chỉ điều phối lifecycle, dispatch action, sleep `retry_interval` giữa cycle, fan-out log qua callback async. CLI và Web đổi từ quản lý job sang Start/Stop + log viewer real-time.

Plan này tận dụng parallel execution: Phase 1 (tạo Runner) và Phase 4 (xoá Job layer) độc lập nhau, có thể chạy song song trong các wave đầu để rút ngắn thời gian. Phase 2 (CLI), Phase 3 (Web) phụ thuộc Runner; Phase 5 (Frontend) phụ thuộc Web; Phase 6 (Docs) ở cuối.

Ngôn ngữ: Python (server) + Vanilla JS (frontend), khớp codebase hiện tại.

## Tasks

- [x] 1. Foundation — config + Runner skeleton + verify generator
  - [x] 1.1 Thêm Settings fields + env validation cho Runner config
    - File: `icloud_hme/config.py`
    - Thêm field `icloud_retry_interval: int = 900` và `icloud_max_errors_per_cycle: int = 0` vào dataclass `Settings`
    - Mở rộng `Settings.from_env(env)` để parse `ICLOUD_RETRY_INTERVAL` (min 10) và `ICLOUD_MAX_ERRORS_PER_CYCLE` (min 0)
    - Fail-fast nếu value < min: raise lỗi cấu hình rõ ràng (không trả Settings)
    - Default 900 khi env vắng key `ICLOUD_RETRY_INTERVAL`; default 0 khi vắng `ICLOUD_MAX_ERRORS_PER_CYCLE`
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6_
  
  - [x] 1.2 Tạo file runner.py skeleton với RunnerStats + LogCallback type alias
    - File: `icloud_hme/runner.py` (mới)
    - Định nghĩa type alias `LogCallback = Callable[[str, str, dict[str, Any]], Awaitable[None]]`
    - Tạo `@dataclass RunnerStats` với 3 field `created: int = 0`, `errors: int = 0`, `skipped: int = 0`
    - Khai báo class `HmeRunner` với constructor `__init__` nhận generator, checker, hme_manager, pool_manager, settings, log_callback, retry_interval (optional)
    - Khai báo các property read-only: `is_running`, `current_action`, `cycle_count`, `stats`, `retry_interval`, `next_cycle_at`
    - Chưa implement thân `start/stop/pause/resume` — để stub raise NotImplementedError
    - _Requirements: 5.1, 5.2, 5.3, 10.1_
  
  - [x] 1.3 Implement HmeRunner lifecycle (start/stop/pause/resume + _interruptible_sleep)
    - File: `icloud_hme/runner.py`
    - Implement `start(action, params)`: reset state, init 3 asyncio.Event (cancel/pause/resume), vào `while not cancel_event.is_set()` loop; tăng `cycle_count` mỗi vòng, cộng dồn stats từ `cycle_result`, log start/end cycle, gọi `_interruptible_sleep(retry_interval)` giữa cycle, finally set `is_running=False`, `current_action=None`, `next_cycle_at=None`
    - Implement `stop()`: set cancel_event; nếu pause_event đang set thì set thêm resume_event để đánh thức blocking await
    - Implement `pause()`: set pause_event
    - Implement `resume()`: set resume_event
    - Implement `_interruptible_sleep(seconds) -> bool`: vòng lặp tick 1s, mỗi tick check cancel_event (return True nếu set), check pause_event (log "Paused", await resume_event, clear cả pause/resume), `await asyncio.sleep(1.0)`; trả False khi sleep đủ
    - Single-instance guard: nếu `is_running == True` raise `RuntimeError("Runner đang chạy action khác")` ngay đầu `start()` mà không đụng cycle_count/stats/current_action
    - Reset state khi start mới: `cycle_count = 0`, `stats = RunnerStats()` trước khi vào while
    - Track `next_cycle_at` (epoch float) khi vào sleep, set None khi vào cycle hoặc khi return
    - Trả summary dict `{total_cycles, created, errors, skipped, stopped_by}` khi cancel
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.1, 2.2, 2.3, 2.4, 3.1, 3.2, 3.3, 3.4, 4.1, 4.3, 5.1, 5.2, 5.3, 10.1, 10.2_
  
  - [x] 1.4 Implement _run_one_cycle dispatch sang service layer
    - File: `icloud_hme/runner.py`
    - Implement `_run_one_cycle(action, params) -> dict`: switch theo action
    - `action == "generate"`: gọi `self._generator.generate(infinite=False, count=params.get("count_per_cycle"), label=..., note=..., proxy=..., cancellation_event=, pause_event=, resume_event=)`; map result thành dict `{created, requested, failures, disabled_profiles}`
    - `action == "check_all"`: gọi `self._checker.check_all(auto_mark=, proxy=, cancellation_event=)`; map sang `{checked, ok, failed}`
    - `action ∈ {"deactivate_bulk", "reactivate_bulk", "delete_bulk"}`: `getattr(self._hme_manager, action)` rồi await với `(emails, dry_run)`; trả `{succeeded, failed}`
    - `action == "update_meta_bulk"`: gọi `update_meta_bulk(items, dry_run)`; trả `{succeeded, failed}`
    - `action == "list_sync"`: gọi `list_sync(apple_id)`; trả `{inserted_active, inserted_inactive, unchanged}`
    - Action không hợp lệ: raise `ValueError(f"Unknown action: {action}")`
    - KHÔNG sửa generator/checker/hme_manager — chỉ gọi public method
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7_
  
  - [x] 1.5 Verify HmeGenerator.generate xử lý đúng count=None với infinite=False
    - File: `icloud_hme/generator.py` (đọc trước, chỉ sửa nếu cần)
    - Đọc signature `generate(...)` và thân hàm để xác định behavior khi `count=None, infinite=False`
    - Kỳ vọng (theo design R8): `count=None, infinite=False` = drain tới khi pool exhausted, không raise
    - Nếu hiện tại raise hoặc đi vào infinite branch sai → sửa nhánh điều kiện để bounded mode chấp nhận `count=None` = unlimited per cycle
    - Nếu đã đúng → no-op, ghi note xác nhận
    - Không thay đổi business logic, chỉ điều chỉnh handling `count is None` ở entry point
    - _Requirements: 6.1_
  
  - [ ]* 1.6 Property-based tests cho HmeRunner
    - File: `test/test_runner_properties.py` (mới)
    - **Property 1: Start returns summary when cancelled** — Validates: Requirements 1.1
    - **Property 2: Cycle count monotonic non-decreasing** — Validates: Requirements 2.1
    - **Property 3: Interruptible sleep reacts within one second** — Validates: Requirements 3.1
    - **Property 4: Is running guard prevents concurrent start** — Validates: Requirements 4.1
    - **Property 5: Stats monotonic non-decreasing** — Validates: Requirements 5.1
    - Dùng `hypothesis` (đã có setup ở `.hypothesis/`); helper `build_test_runner` mock generator/checker/manager
    - Chạy: `python3 -m pytest test/test_runner_properties.py -v`
    - _Requirements: 1.1, 2.1, 3.1, 4.1, 5.1_

- [x] 2. Job layer removal — xoá hoàn toàn ~1.500 LOC (parallel với task 1.x)
  - [x] 2.1 Thêm migration v7 DROP TABLE icloud_jobs + bump CURRENT_VERSION
    - File: `db/schema.py`
    - Thêm entry `MIGRATIONS[7] = ["DROP TABLE IF EXISTS icloud_jobs;"]`
    - Bump `CURRENT_VERSION = 7`
    - Giữ nguyên `MIGRATIONS[6]` (kể cả `DDL_ICLOUD_JOBS` ref bên trong) để DB version ≤ 5 vẫn pass qua step 6 trước khi v7 drop
    - _Requirements: 12.1, 12.2, 12.3, 12.5, 12.6_
  
  - [x] 2.2 Xoá DDL_ICLOUD_JOBS + indexes khỏi ALL_DDL
    - File: `db/schema.py`
    - Xoá `DDL_ICLOUD_JOBS` (~line 193–214) và `DDL_ICLOUD_JOBS_INDEXES` (~line 217–226) khỏi list `ALL_DDL` (~line 244–245)
    - **KHÔNG xoá** constant `DDL_ICLOUD_JOBS` nếu `MIGRATIONS[6]` còn reference; tách định nghĩa khỏi `ALL_DDL` để DB mới khởi tạo không tạo bảng
    - DB mới khởi tạo từ `ALL_DDL` sẽ không có bảng `icloud_jobs`; v7 `DROP IF EXISTS` chạy no-op
    - _Requirements: 12.4, 12.6_
  
  - [x] 2.3 Xoá class IcloudJobRepository khỏi db/repositories.py
    - File: `db/repositories.py`
    - Xoá toàn bộ class `IcloudJobRepository` (~bắt đầu line 1894)
    - Xoá import liên quan (nếu có dataclass JobRecord/JobLogEntry import từ models)
    - _Requirements: 11.3_
  
  - [x] 2.4 Xoá JobRecord + JobLogEntry dataclass khỏi models.py
    - File: `icloud_hme/models.py`
    - Xoá dataclass `JobRecord` (~line 202–228) và `JobLogEntry`
    - Xoá import phụ trợ (datetime, Optional...) nếu không còn ai dùng
    - _Requirements: 11.2_
  
  - [x] 2.5 Xoá 4 Job exceptions khỏi exceptions.py
    - File: `icloud_hme/exceptions.py`
    - Xoá class `JobError`, `JobNotFoundError`, `JobInvalidTransitionError`, `JobCrashedError`
    - Giữ nguyên các exception khác (`IcloudHmeError`, pool/session/quota errors)
    - _Requirements: 11.4_
  
  - [x] 2.6 Xoá toàn bộ thư mục icloud_hme/jobs/
    - File: `icloud_hme/jobs/` (xoá directory + 12 file con)
    - Files xoá: `__init__.py`, `manager.py`, `handlers.py`, `generate.py`, `bootstrap.py`, `check_all.py`, `deactivate_bulk.py`, `delete_bulk.py`, `reactivate_bulk.py`, `update_meta_bulk.py`, `list_sync.py`, `export.py`
    - Tổng ~1.463 LOC
    - _Requirements: 11.1_
  
  - [x] 2.7 Xoá job CLI commands khỏi cli.py
    - File: `icloud_hme/cli.py`
    - Xoá Typer group `job_app` cùng 10 subcommand: `enqueue`, `list`, `get`, `status`, `stop`, `pause`, `resume`, `restart`, `stop-all`, `log` (~line 1053–1295)
    - Xoá imports liên quan job (JobManager, JobRecord, IcloudJobRepository...)
    - Tạm thời để 2 command `generate` và `check` ở trạng thái cũ (chưa migrate qua Runner — sẽ làm ở task 3.1, 3.2)
    - _Requirements: 11.6_
  
  - [x] 2.8 Xoá job web endpoints khỏi router.py
    - File: `icloud_hme/web/router.py`
    - Xoá: `POST /api/icloud/emails/generate` (job-enqueue), `GET /api/icloud/jobs/{job_id}`, `POST /api/icloud/jobs/{job_id}/{action}`, `GET /api/icloud/jobs/{job_id}/log`, `GET /api/icloud/jobs/{job_id}/log/stream`, `GET /api/icloud/jobs`
    - Xoá imports + Pydantic schema cũ (JobResponse, JobLogResponse) khỏi `icloud_hme/web/schemas.py`
    - Giữ nguyên các endpoint không liên quan job (profile, status, audit...)
    - _Requirements: 11.5_
  
  - [x] 2.9 Clean imports/exports trong __init__.py
    - File: `icloud_hme/__init__.py`
    - Xoá mọi `import` hoặc `from .jobs import ...` và mục liên quan trong `__all__`
    - Đảm bảo package vẫn import được sau khi `jobs/` directory bị xoá
    - _Requirements: 11.7_
  
  - [x] 2.10 Xoá test files của Job layer
    - Files xoá: `test/check_icloud_job_repository.py`, `test/check_job_handlers_dispatch.py`
    - _Requirements: 11.8_

- [x] 3. Checkpoint — verify Runner + Job removal hoàn tất
  - Chạy `python3 test/syntax_check.py` để parse AST mọi file Python (không còn import jobs)
  - Chạy `python3 -m pytest test/test_runner_properties.py -v` (nếu task 1.6 đã làm)
  - Verify CLI import được: `python3 -c "from icloud_hme import cli"` (chỉ check import, không chạy)
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. CLI migration — wire generate/check qua HmeRunner
  - [x] 4.1 CLI generate command chạy qua Runner với SIGINT handler
    - File: `icloud_hme/cli.py`
    - Sửa command `generate`: build `Settings.from_env(os.environ)`, build `HmeRunner` với `log_callback=_cli_log` (in stderr format `[HH:MM:SS][level] message`)
    - Đăng ký SIGINT handler gọi `runner.stop()` thay vì để Python raise KeyboardInterrupt
    - `asyncio.run(runner.start(action="generate", params={...}))` cho tới khi cancel
    - In summary cuối session
    - Bỏ flag `--infinite` (mọi lần chạy đều là infinite loop)
    - _Requirements: 8.1, 8.3, 8.6, 10.3_
  
  - [x] 4.2 CLI check --all command chạy qua Runner
    - File: `icloud_hme/cli.py`
    - Sửa command `check`: nếu cờ `--all` được set, build `HmeRunner` (giống 4.1) và gọi `runner.start(action="check_all", params={"auto_mark": ..., "proxy": ...})`
    - SIGINT handler giống generate
    - Nếu không có `--all` → giữ behavior cũ (1-shot single profile check)
    - _Requirements: 8.2, 8.3_
  
  - [x] 4.3 Thêm flags --count-per-cycle và --retry-interval
    - File: `icloud_hme/cli.py`
    - Thêm `--count-per-cycle <N>` (Optional[int], default None) cho command `generate` → truyền vào `params["count_per_cycle"]`
    - Thêm `--retry-interval <S>` (Optional[int], default None) cho cả `generate` và `check`: nếu set thì khởi tạo `HmeRunner(retry_interval=<S>)` thay vì lấy từ Settings
    - Validate `<S> >= 10` (raise nếu nhỏ hơn)
    - Verify các command 1-shot khác (`bootstrap`, `profile open`, `profile delete`, `status`, `reconcile`, `email *`, `audit *`) KHÔNG đi qua Runner
    - _Requirements: 8.4, 8.5, 8.7_
  
  - [ ]* 4.4 Smoke test CLI Runner integration
    - File: `test/smoke_runner_cli.py` (mới)
    - Mock generator/checker (build_test_runner pattern); spawn subprocess `python3 -m gpt_signup_hybrid.icloud_hme generate --count-per-cycle 1 --retry-interval 2`; sau ~3s gửi SIGINT; verify exit code 0 + stderr chứa "Runner started" và "Runner stopped"
    - _Requirements: 8.1, 8.3, 8.6_

- [x] 5. Web migration — LogBuffer + endpoints + auth
  - [x] 5.1 Tạo LogBuffer pub-sub class
    - File: `icloud_hme/web/log_buffer.py` (mới)
    - Class `LogBuffer` với `MAX_ENTRIES = 10_000`, `_entries: collections.deque(maxlen=10_000)`, `_subscribers: set[asyncio.Queue]`, `_seq: int = 0`
    - Method `clear()`: clear entries + reset seq về 0
    - Method `push(level, message, payload)`: tăng seq, build LogEvent, append vào deque, broadcast non-blocking sang mọi subscriber via `q.put_nowait(...)`; bắt `asyncio.QueueFull` để drop event cho subscriber chậm
    - Method `subscribe() -> AsyncIterator[LogEvent]`: tạo `asyncio.Queue(maxsize=1000)`, replay history từ deque, add vào set, yield event qua `q.get()`; finally remove khỏi set
    - Helper `make_web_log_callback(buffer) -> LogCallback` wrap `buffer.push`
    - _Requirements: 10.4, 10.5, 10.6, 10.7_
  
  - [x] 5.2 Định nghĩa Pydantic schemas RunRequest, RunStatus, LogEvent
    - File: `icloud_hme/web/schemas.py`
    - `RunRequest`: `action: Literal[7 giá trị]`, `params: dict[str, Any] = Field(default_factory=dict)`, `retry_interval: Optional[int] = Field(default=None, ge=10)`
    - `RunStatus`: `running: bool`, `action: Optional[str]`, `cycle: int = 0`, `stats: dict[str, int]`, `retry_interval: int`, `next_cycle_at: Optional[str]` (ISO 8601 UTC)
    - `LogEvent`: `ts: str` (ISO 8601 UTC), `level: Literal["info", "warn", "error"]`, `message: str`, `payload: dict[str, Any]`, `seq: int`
    - _Requirements: 9.1, 9.5, 10.2_
  
  - [x] 5.3 Web endpoints POST /api/icloud/run /stop /pause /resume
    - File: `icloud_hme/web/router.py`
    - `POST /api/icloud/run`: validate `RunRequest`; nếu `runner.is_running` raise HTTPException 409 `{"error": "already_running"}`; gọi `log_buffer.clear()`; spawn `asyncio.create_task(runner.start(action=req.action, params=req.params))` (KHÔNG await); return `{"ok": True, "action": req.action}`
    - `POST /api/icloud/run/stop`: gọi `runner.stop()`; return `{"ok": True}`
    - `POST /api/icloud/run/pause`: gọi `runner.pause()`; return `{"ok": True}`
    - `POST /api/icloud/run/resume`: gọi `runner.resume()`; return `{"ok": True}`
    - Wire DI: `runner: HmeRunner = Depends(get_runner)`, `buffer: LogBuffer = Depends(get_log_buffer)`
    - Nếu retry_interval truyền trong body khác `runner.retry_interval` hiện tại → rebuild Runner instance (singleton replacement)
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 4.2_
  
  - [x] 5.4 Web endpoint GET /api/icloud/run/status
    - File: `icloud_hme/web/router.py`
    - Build `RunStatus` từ runner properties: `running=runner.is_running`, `action=runner.current_action`, `cycle=runner.cycle_count`, `stats={created, errors, skipped}`, `retry_interval=runner.retry_interval`, `next_cycle_at` = ISO 8601 UTC từ `runner.next_cycle_at` (epoch float) hoặc None
    - _Requirements: 9.5_
  
  - [x] 5.5 Web endpoints GET /api/icloud/run/log + log/stream SSE
    - File: `icloud_hme/web/router.py`
    - `GET /api/icloud/run/log?offset=N&limit=M`: trả `{events: [LogEvent có seq > N, max M], next_offset: int}`; lấy snapshot từ `LogBuffer._entries`, filter bằng seq
    - `GET /api/icloud/run/log/stream`: trả `StreamingResponse(media_type="text/event-stream")`; generator yield `f"data: {event.model_dump_json()}\n\n"` qua `LogBuffer.subscribe()`; close khi client disconnect
    - _Requirements: 9.6, 9.7_
  
  - [x] 5.6 Wire Bearer auth middleware cho /api/icloud/run/*
    - File: `icloud_hme/web/router.py`
    - Reuse middleware/dependency Bearer token có sẵn trong project
    - Áp dụng cho tất cả 7 endpoint mới (`/run`, `/run/stop`, `/run/pause`, `/run/resume`, `/run/status`, `/run/log`, `/run/log/stream`) qua dependency hoặc router-level dependency
    - Trả HTTP 401 (không gọi Runner method) nếu thiếu/sai header `Authorization: Bearer <token>`
    - _Requirements: 9.8_
  
  - [ ]* 5.7 Smoke test Web Runner end-to-end
    - File: `test/smoke_runner_web.py` (mới)
    - Start uvicorn TestClient với router; gọi `POST /api/icloud/run` với Bearer token + body `{action: "generate", params: {count_per_cycle: 1}, retry_interval: 2}`; verify 200; gọi `GET /api/icloud/run/status` verify `running=True`; subscribe SSE 1s, verify nhận event "Cycle #1"; gọi `POST /api/icloud/run/stop` verify 200
    - Verify 401 khi không có Bearer header
    - Verify 409 khi gọi POST /run lần 2 trong khi đang chạy
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8_

- [x] 6. Frontend Log Viewer UI
  - [x] 6.1 Thay panel job-manager bằng log viewer panel
    - File: `web/static/hme.js`, `web/static/index.html`
    - Xoá DOM/JS quản lý job (enqueue form, job list table, job detail modal, restart button)
    - Thêm `<pre>` hoặc `<div>` log viewer auto-scroll: kết nối `EventSource('/api/icloud/run/log/stream')` (kèm Bearer token qua header)
    - Render `LogEvent`: `[ts][level] message` với màu khác biệt cho `level == "warn"` (vàng) và `level == "error"` (đỏ); `info` mặc định (trắng/xám)
    - Auto-scroll xuống cuối khi nhận event mới
    - _Requirements: 13.1, 13.6_
  
  - [x] 6.2 Nút Start/Stop + status badge
    - File: `web/static/hme.js`, `web/static/index.html`
    - Nút Start gọi `POST /api/icloud/run` với body `{action, params, retry_interval}`
    - Nút Stop gọi `POST /api/icloud/run/stop`
    - Poll `GET /api/icloud/run/status` mỗi 2s: cập nhật badge "RUNNING" (xanh) / "IDLE" (xám); nếu `running == true`: disable Start, enable Stop; ngược lại: enable Start, disable Stop
    - Form Start: dropdown chọn action (`generate` / `check_all`), input `count_per_cycle` (optional), input `retry_interval` (default 900), input `label`/`note`/`proxy` (optional)
    - Hiển thị stats live: `Cycle #N | Created: X | Errors: Y | Skipped: Z`
    - _Requirements: 13.2, 13.3, 13.4_
  
  - [x] 6.3 Profile sidebar với pool status
    - File: `web/static/hme.js`, `web/static/index.html`
    - Sidebar (~20% width) liệt kê profiles từ endpoint có sẵn (e.g. `GET /api/icloud/profiles`)
    - Render mỗi profile: apple_id + badge status (`active` xanh / `limited` vàng / `quota_full` cam / `session_expired` đỏ) + `hme_count / 700` quota bar
    - Auto-refresh mỗi 30s hoặc khi nhận log event chứa profile transition (`payload.apple_id`)
    - _Requirements: 13.5_
  
  - [x] 6.4 Countdown next_cycle_at trong status panel
    - File: `web/static/hme.js`
    - Khi `RunStatus.next_cycle_at != null`: tính `delta = next_cycle_at - now`, render countdown format `MM:SS` cập nhật mỗi giây bằng `setInterval`
    - Khi `next_cycle_at == null` (đang trong cycle hoặc idle): ẩn countdown
    - _Requirements: 13.7_

- [x] 7. Cleanup docs + final smoke
  - [x] 7.1 Update CLAUDE.md
    - File: `CLAUDE.md`
    - Xoá section/refs về job commands (`job enqueue`, `job list`, `job get`, ...)
    - Thêm section "iCloud Runner" mô tả: Runner infinite loop, 7 actions, retry_interval, env config, CLI commands `generate` / `check --all`, web endpoints `/api/icloud/run/*`
    - Update file layout/structure note (xoá ref `icloud_hme/jobs/`)
    - _Requirements: dọn docs sau Phase 5_
  
  - [x] 7.2 Update icloud_hme/README.md
    - File: `icloud_hme/README.md`
    - Thay section Job lifecycle bằng Runner lifecycle diagram (mermaid hoặc ASCII)
    - Update CLI examples (xoá `--infinite`, thêm `--count-per-cycle`, `--retry-interval`)
    - Update kiến trúc 3 layer: Presentation (CLI/Web) → Runner → Services
    - Xoá ref `runtime/icloud_jobs/` nếu có
    - _Requirements: dọn docs sau Phase 5_
  
  - [ ]* 7.3 Smoke E2E — chạy CLI generate thực tế ngắn hạn
    - File: `test/smoke_runner_e2e.py` (mới)
    - Spawn subprocess `python3 -m gpt_signup_hybrid.icloud_hme generate --count-per-cycle 1 --retry-interval 5` với env `ICLOUD_RETRY_INTERVAL=5`
    - Sau 12s gửi SIGINT, đợi process exit
    - Verify: exit code 0; stderr chứa ít nhất 2 log "── Cycle #1 ──" và "── Cycle #2 ──"; stderr chứa "Runner stopped. Summary"
    - Mock service layer ở mức Settings hoặc dùng test profile để không gọi Apple API thật
    - _Requirements: 8.1, 1.1, 2.1_
  
  - [x] 7.4 Final checkpoint
    - Chạy `python3 test/syntax_check.py` (parse AST mọi file `.py` trong repo) — verify không còn syntax error
    - Verify import: `python3 -c "from icloud_hme import cli, runner"` (không còn lỗi import jobs)
    - Verify migration: bật DB ở schema v6 sẵn có, run app, check `PRAGMA user_version` = 7 và `SELECT name FROM sqlite_master WHERE name='icloud_jobs'` = empty
    - Ensure all tests pass, ask the user if questions arise.

## Notes

- Các sub-task postfix `*` (1.6, 4.4, 5.7, 7.3) là test/smoke optional, có thể skip cho MVP nhưng nên chạy trước khi merge.
- Phase 1 (task 1.x) và Phase 4 (task 2.x) độc lập về file → khai thác parallel mạnh ở wave 0–2.
- Sau Phase 4 (job removal hoàn tất) Runner mới wire vào CLI/Web để tránh import vòng.
- `icloud_hme/cli.py` và `icloud_hme/web/router.py` bị nhiều task cùng đụng → phải sequential giữa các wave.
- Mọi sleep trong Runner phải interruptible (chunk 1s + check cancel) — đã định nghĩa rõ ở task 1.3.
- Bearer auth bắt buộc cho mọi endpoint `/api/icloud/run/*` — không có default insecure (tuân theo project-rules).
- Task 1.5 (verify generator) là precondition cho 1.4 + 4.1: nếu generator không support `count=None, infinite=False` → phải fix trước khi chạy Runner.
- Generator/checker/manager/pool **không bị sửa** ngoài tinh chỉnh nhỏ ở 1.5.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "1.5", "2.1", "2.3", "2.4", "2.5", "2.6", "2.10"] },
    { "id": 1, "tasks": ["1.3", "2.2", "2.7", "2.8"] },
    { "id": 2, "tasks": ["1.4", "2.9"] },
    { "id": 3, "tasks": ["1.6", "4.1", "5.1"] },
    { "id": 4, "tasks": ["4.2", "5.2"] },
    { "id": 5, "tasks": ["4.3", "5.3"] },
    { "id": 6, "tasks": ["4.4", "5.4"] },
    { "id": 7, "tasks": ["5.5"] },
    { "id": 8, "tasks": ["5.6"] },
    { "id": 9, "tasks": ["5.7", "6.1"] },
    { "id": 10, "tasks": ["6.2"] },
    { "id": 11, "tasks": ["6.3"] },
    { "id": 12, "tasks": ["6.4"] },
    { "id": 13, "tasks": ["7.1", "7.2"] },
    { "id": 14, "tasks": ["7.3"] }
  ]
}
```
