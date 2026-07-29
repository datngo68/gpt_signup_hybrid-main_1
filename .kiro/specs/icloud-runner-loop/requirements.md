# Requirements Document

## Introduction

**Feature:** iCloud Runner Loop



Refactor lớp `icloud_hme/jobs/` (~1.500 LOC, 12 file) thành module `HmeRunner` (~250 LOC) chạy infinite loop. Logic core của `HmeGenerator`, `ProfileChecker`, `HmeManager`, `IcloudPoolManager` giữ nguyên 100%; Runner chỉ điều phối lifecycle, dispatch action xuống service layer, sleep `retry_interval` giữa các cycle, và phát log qua callback bất đồng bộ. CLI và Web đổi từ quản lý job sang Start/Stop + log viewer real-time. Tài liệu này bao gồm hành vi Runner, cấu hình, surface CLI/Web/Frontend, ràng buộc gỡ Job layer, và migration schema từ v6 → v7.

## Glossary

- **HmeRunner** — Class trong `icloud_hme/runner.py` đóng vai loop controller; quản lý cancel/pause/resume event, cycle counter, stats, log fan-out.
- **HmeGenerator** — Service tạo HME (`icloud_hme/generator.py`); Runner gọi `generate(infinite=False, count=count_per_cycle, ...)`.
- **ProfileChecker** — Service kiểm tra trạng thái profile (`icloud_hme/checker.py`); Runner gọi `check_all(...)`.
- **HmeManager** — Service quản trị HME bulk (`icloud_hme/manager.py`); cung cấp `deactivate_bulk`, `reactivate_bulk`, `delete_bulk`, `update_meta_bulk`, `list_sync`.
- **IcloudPoolManager** — State machine pool profile (`icloud_hme/pool.py`); chuyển trạng thái active/limited/quota_full/session_expired theo TTL.
- **Settings** — Dataclass cấu hình (`icloud_hme/config.py`); load từ env qua `Settings.from_env`.
- **CLI** — Typer app trong `icloud_hme/cli.py`.
- **Web_Router** — FastAPI router trong `icloud_hme/web/router.py`.
- **Frontend_UI** — Code tĩnh trong `web/static/` (vanilla JS + HTML).
- **Database_Migration** — Logic migration trong `db/schema.py` chạy theo `CURRENT_VERSION` và `MIGRATIONS`.
- **Cycle** — Một vòng `_run_one_cycle`: Runner gọi service layer 1 lần, service xử lý tới khi pool exhausted hoặc đạt `count_per_cycle`.
- **Pool** — Tập hợp iCloud profile do `IcloudPoolManager` quản lý.
- **Profile** — Một bản ghi iCloud (apple_id, password, session bundle) ở 1 trong 4 trạng thái: active, limited, quota_full, session_expired.
- **Session** — Session bundle (cookies + tokens) extract từ Profile để gọi Apple HME API.
- **Quota** — Số HME đã tạo trên 1 Profile (giới hạn 700/profile theo Apple).
- **retry_interval** — Số giây Runner sleep giữa 2 cycle (default 900, env `ICLOUD_RETRY_INTERVAL`, min 10).
- **cancel_event** — `asyncio.Event` Runner set khi muốn dừng loop.
- **pause_event / resume_event** — Cặp `asyncio.Event` đồng bộ pause/resume giữa Runner và service layer.
- **log_callback** — Async callable signature `(level: str, message: str, payload: dict) -> Awaitable[None]`; transport-agnostic.
- **LogBuffer** — Capped FIFO + asyncio pub-sub trong `icloud_hme/web/log_buffer.py`; giữ tối đa 10.000 entry và broadcast cho mọi SSE subscriber.
- **LogEvent** — Pydantic schema `{ts, level, message, payload, seq}`.
- **RunnerStats** — Dataclass `{created: int, errors: int, skipped: int}`, đơn điệu không giảm trong session.
- **JSONL** — JSON Lines format file log của Job layer cũ; **không** còn dùng trong Runner.
- **SSE** — Server-Sent Events; HTTP stream Content-Type `text/event-stream`.
- **Bearer token** — Token xác thực truyền qua header `Authorization: Bearer <token>`; reuse middleware có sẵn.
- **EARS** — Easy Approach to Requirements Syntax.

## Requirements

### Requirement 1: Lifecycle Runner và Summary khi cancel

**User Story:** Là một operator vận hành iCloud HME, tôi muốn điều khiển vòng đời HmeRunner qua API rõ ràng (start / stop / pause / resume) và nhận summary khi loop dừng, để có thể bắt đầu vòng tạo email vô tận có kiểm soát và xem được tổng kết phiên chạy.

#### Acceptance Criteria

1. WHEN `HmeRunner.start(action, params)` được gọi và sau đó `cancel_event` được set qua `HmeRunner.stop()`, THE HmeRunner SHALL trả về một dict bao gồm các key `total_cycles`, `created`, `errors`, `skipped`, `stopped_by` trong tối đa `retry_interval + 5` giây kể từ lúc set `cancel_event`.
2. WHEN `HmeRunner.stop()` được gọi WHILE `is_running == True`, THE HmeRunner SHALL set `cancel_event` mà không raise exception và không thay đổi trực tiếp `cycle_count` hay `stats`.
3. WHEN `HmeRunner.pause()` được gọi WHILE `is_running == True`, THE HmeRunner SHALL set `pause_event`.
4. WHEN `HmeRunner.resume()` được gọi WHILE `pause_event.is_set() == True`, THE HmeRunner SHALL set `resume_event`.
5. WHEN `HmeRunner.stop()` được gọi WHILE `pause_event.is_set() == True`, THE HmeRunner SHALL set cả `cancel_event` và `resume_event` để đánh thức blocking await đang chờ resume.
6. WHEN `HmeRunner.start` thoát (return summary hoặc raise), THE HmeRunner SHALL gán `is_running = False`, `current_action = None`, và `next_cycle_at = None` trong block `finally`.

### Requirement 2: Infinite Cycle Loop và Cycle Counter

**User Story:** Là một operator, tôi muốn HmeRunner chạy liên tục theo vòng `cycle → wait → cycle` và tự đếm số cycle, để các profile bị `limited` hoặc `quota_full` có cơ hội recover sau khi TTL hết hạn mà không cần can thiệp tay.

#### Acceptance Criteria

1. WHILE `HmeRunner.start` đang chạy và `cancel_event.is_set() == False`, THE HmeRunner SHALL giữ `cycle_count` đơn điệu không giảm và tăng đúng 1 mỗi khi bắt đầu cycle mới.
2. WHEN một cycle hoàn thành, THE HmeRunner SHALL gọi `log_callback` với `level == "info"` và message chứa số thứ tự cycle cùng tóm tắt `cycle_result`.
3. WHEN một cycle hoàn thành và `cancel_event.is_set() == False`, THE HmeRunner SHALL chuyển sang giai đoạn `_interruptible_sleep(retry_interval)` trước khi vào cycle kế tiếp.
4. WHILE `cancel_event.is_set() == False`, THE HmeRunner SHALL không tự thoát loop kể cả khi `cycle_result.created == 0` hoặc `cycle_result.disabled_profiles == []`.

### Requirement 3: Interruptible Retry Interval

**User Story:** Là một operator, tôi muốn giai đoạn sleep giữa các cycle phản ứng nhanh với lệnh dừng và pause, để khi gửi Stop hoặc SIGINT thì Runner thoát trong tối đa khoảng 1 giây.

#### Acceptance Criteria

1. WHEN `cancel_event` được set tại thời điểm `t0` WHILE HmeRunner đang trong `_interruptible_sleep`, THE HmeRunner SHALL trả `True` từ `_interruptible_sleep` tại thời điểm `t1` với `t1 - t0 ≤ 1.5` giây.
2. WHILE HmeRunner đang trong `_interruptible_sleep`, THE HmeRunner SHALL kiểm tra `cancel_event` và `pause_event` đúng một lần mỗi 1 giây.
3. WHEN `pause_event` được set WHILE HmeRunner đang trong `_interruptible_sleep`, THE HmeRunner SHALL gọi `log_callback` với message chứa từ khóa "Paused" và block bằng `await resume_event.wait()` cho tới khi `resume_event` được set.
4. WHEN `resume_event` được set WHILE `_interruptible_sleep` đang block chờ resume, THE HmeRunner SHALL clear `resume_event` và `pause_event` trước khi tiếp tục đếm thời gian sleep còn lại.

### Requirement 4: Single-Instance Concurrency Guard

**User Story:** Là một operator, tôi muốn HmeRunner chỉ chạy đúng 1 instance trong 1 process, để hai action không ghi đè state hoặc gọi service layer đồng thời gây xung đột pool.

#### Acceptance Criteria

1. IF `HmeRunner.start(action, params)` được gọi WHILE `is_running == True`, THEN THE HmeRunner SHALL raise `RuntimeError` với message tiếng Việt "Runner đang chạy action khác" mà không thay đổi `cycle_count`, `stats`, hay `current_action`.
2. WHEN `POST /api/icloud/run` được gọi WHILE `HmeRunner.is_running == True`, THE Web_Router SHALL trả HTTP 409 với body JSON `{"error": "already_running"}`.
3. WHEN `HmeRunner.start` được gọi lần đầu WHILE `is_running == False`, THE HmeRunner SHALL gán `is_running = True` và `current_action = action` trước khi vào thân vòng `while`.

### Requirement 5: Cumulative Stats Aggregation

**User Story:** Là một operator, tôi muốn xem tổng số HME đã tạo, số lỗi, và số profile bị skip cộng dồn qua các cycle, để theo dõi tiến độ session hiện tại trong UI và summary cuối phiên.

#### Acceptance Criteria

1. WHILE `HmeRunner.start` đang chạy, THE HmeRunner SHALL giữ `stats.created`, `stats.errors`, `stats.skipped` đơn điệu không giảm trong toàn bộ session.
2. WHEN một cycle hoàn thành với `cycle_result`, THE HmeRunner SHALL cộng `cycle_result["created"]` vào `stats.created`, `len(cycle_result["failures"])` vào `stats.errors`, và `len(cycle_result["disabled_profiles"])` vào `stats.skipped`.
3. WHEN `HmeRunner.start` được gọi mới WHILE `is_running == False`, THE HmeRunner SHALL reset `stats` về `RunnerStats(created=0, errors=0, skipped=0)` trước khi vào thân vòng `while`.

### Requirement 6: Service Action Dispatch

**User Story:** Là một developer maintain Runner, tôi muốn `_run_one_cycle` dispatch sang đúng phương thức service layer dựa trên `action`, để giữ logic core của generator/checker/manager không đổi 100%.

#### Acceptance Criteria

1. WHEN `action == "generate"`, THE HmeRunner SHALL gọi `HmeGenerator.generate` với `infinite=False`, `count=params.get("count_per_cycle")`, `label=params.get("label")`, `note=params.get("note")`, `proxy=params.get("proxy")`, và forward `cancel_event`, `pause_event`, `resume_event`.
2. WHEN `action == "check_all"`, THE HmeRunner SHALL gọi `ProfileChecker.check_all` với `auto_mark=params.get("auto_mark", True)`, `proxy=params.get("proxy")`, và forward `cancel_event`.
3. WHEN `action ∈ {"deactivate_bulk", "reactivate_bulk", "delete_bulk"}`, THE HmeRunner SHALL gọi phương thức cùng tên trên `HmeManager` với `params.get("emails", [])` và `dry_run=params.get("dry_run", False)`.
4. WHEN `action == "update_meta_bulk"`, THE HmeRunner SHALL gọi `HmeManager.update_meta_bulk` với `params.get("items", [])` và `dry_run=params.get("dry_run", False)`.
5. WHEN `action == "list_sync"`, THE HmeRunner SHALL gọi `HmeManager.list_sync` với `params["apple_id"]` và trả `cycle_result` chứa các key `inserted_active`, `inserted_inactive`, `unchanged`.
6. IF `action` không thuộc tập 7 giá trị `{"generate", "check_all", "deactivate_bulk", "reactivate_bulk", "delete_bulk", "update_meta_bulk", "list_sync"}`, THEN THE HmeRunner SHALL raise `ValueError` với message chứa giá trị `action` không hợp lệ.
7. THE HmeRunner SHALL không sửa source code bên trong `HmeGenerator`, `ProfileChecker`, `HmeManager`, `IcloudPoolManager` ngoài việc gọi public method của chúng.

### Requirement 7: Runner Configuration via Environment

**User Story:** Là một operator triển khai, tôi muốn chỉnh `retry_interval` và `max_errors_per_cycle` qua biến môi trường với validate fail-fast, để tinh chỉnh tần suất cycle khớp Apple rate-limit mà không cần sửa code và không bị bug runtime do giá trị xấu.

#### Acceptance Criteria

1. WHEN `Settings.from_env(env)` được gọi với `env` chứa key `ICLOUD_RETRY_INTERVAL`, THE Settings SHALL parse giá trị thành `int` và gán vào field `icloud_retry_interval`.
2. WHEN `Settings.from_env(env)` được gọi với `env` không chứa `ICLOUD_RETRY_INTERVAL`, THE Settings SHALL gán `icloud_retry_interval = 900`.
3. IF `ICLOUD_RETRY_INTERVAL < 10`, THEN THE Settings.from_env SHALL raise lỗi cấu hình ngay lập tức và không trả về Settings.
4. WHEN `Settings.from_env(env)` được gọi với `env` chứa key `ICLOUD_MAX_ERRORS_PER_CYCLE`, THE Settings SHALL parse giá trị thành `int` và gán vào field `icloud_max_errors_per_cycle`.
5. WHEN `Settings.from_env(env)` được gọi với `env` không chứa `ICLOUD_MAX_ERRORS_PER_CYCLE`, THE Settings SHALL gán `icloud_max_errors_per_cycle = 0`.
6. IF `ICLOUD_MAX_ERRORS_PER_CYCLE < 0`, THEN THE Settings.from_env SHALL raise lỗi cấu hình ngay lập tức và không trả về Settings.

### Requirement 8: CLI Command Surface

**User Story:** Là một operator dùng CLI, tôi muốn lệnh `generate` và `check --all` chạy qua HmeRunner ở chế độ infinite loop và các lệnh 1-shot khác giữ nguyên hành vi cũ, để phân tách rõ giữa work liên tục (Runner) và work thao tác đơn (1-shot).

#### Acceptance Criteria

1. WHEN người dùng chạy `python -m gpt_signup_hybrid.icloud_hme generate`, THE CLI SHALL khởi tạo `HmeRunner` với `log_callback` ghi stderr và gọi `HmeRunner.start(action="generate", params=...)` cho tới khi `cancel_event` được set.
2. WHEN người dùng chạy `python -m gpt_signup_hybrid.icloud_hme check --all`, THE CLI SHALL khởi tạo `HmeRunner` với `log_callback` ghi stderr và gọi `HmeRunner.start(action="check_all", params=...)` cho tới khi `cancel_event` được set.
3. WHEN process CLI nhận tín hiệu `SIGINT` WHILE HmeRunner đang chạy, THE CLI SHALL gọi `HmeRunner.stop()` thay vì để Python raise `KeyboardInterrupt`.
4. WHERE người dùng chạy `generate` với cờ `--count-per-cycle <N>`, THE CLI SHALL truyền giá trị `<N>` vào `params["count_per_cycle"]`.
5. WHERE người dùng chạy `generate` hoặc `check` với cờ `--retry-interval <S>`, THE CLI SHALL khởi tạo `HmeRunner` với `retry_interval = <S>` thay vì lấy từ `Settings.icloud_retry_interval`.
6. THE CLI SHALL không cung cấp cờ `--infinite` cho lệnh `generate`.
7. THE CLI SHALL giữ hành vi 1-shot blocking cho các lệnh `bootstrap`, `profile open`, `profile delete`, `status`, `reconcile`, `email deactivate`, `email reactivate`, `email delete`, `email mark-used`, `email update-meta`, `email list-sync`, `email export`, `audit list`, `audit cleanup` (không chạy qua HmeRunner).

### Requirement 9: Web HTTP Endpoints với Bearer Auth

**User Story:** Là một operator dùng Web UI, tôi muốn các endpoint HTTP để start, stop, pause, resume, xem status và stream log real-time, đồng thời mọi endpoint phải bảo vệ bằng Bearer token, để điều khiển Runner từ xa an toàn.

#### Acceptance Criteria

1. WHEN `POST /api/icloud/run` nhận body `{action, params, retry_interval?}` hợp lệ và `HmeRunner.is_running == False`, THE Web_Router SHALL spawn `asyncio.create_task(runner.start(...))` và trả HTTP 200 với body `{"ok": true, "action": <action>}` mà không block tới khi loop kết thúc.
2. WHEN `POST /api/icloud/run/stop` được gọi, THE Web_Router SHALL gọi `HmeRunner.stop()` và trả HTTP 200 với body `{"ok": true}`.
3. WHEN `POST /api/icloud/run/pause` được gọi, THE Web_Router SHALL gọi `HmeRunner.pause()` và trả HTTP 200 với body `{"ok": true}`.
4. WHEN `POST /api/icloud/run/resume` được gọi, THE Web_Router SHALL gọi `HmeRunner.resume()` và trả HTTP 200 với body `{"ok": true}`.
5. WHEN `GET /api/icloud/run/status` được gọi, THE Web_Router SHALL trả body `RunStatus` chứa `running: bool`, `action: str | null`, `cycle: int`, `stats: {created, errors, skipped}`, `retry_interval: int`, `next_cycle_at: str | null` (ISO 8601 UTC khi đang sleep, `null` khi đang trong cycle hoặc idle).
6. WHEN `GET /api/icloud/run/log` nhận query `offset=<N>` và `limit=<M>`, THE Web_Router SHALL trả body `{"events": [LogEvent], "next_offset": <int>}` chứa các `LogEvent` có `seq > <N>`, tối đa `<M>` event, lấy từ `LogBuffer`.
7. WHEN `GET /api/icloud/run/log/stream` được gọi, THE Web_Router SHALL trả response Content-Type `text/event-stream` và stream từng `LogEvent` dưới dạng `data: <json>\n\n` cho tới khi client disconnect.
8. IF một request đến bất kỳ path `/api/icloud/run/*` nào không có header `Authorization: Bearer <token>` hợp lệ, THEN THE Web_Router SHALL trả HTTP 401 và không gọi bất kỳ phương thức nào trên `HmeRunner`.

### Requirement 10: Log Callback Contract và LogBuffer Pub-Sub

**User Story:** Là một developer wire HmeRunner vào CLI hay Web, tôi muốn `log_callback` có signature async cố định và LogBuffer làm pub-sub cho SSE, để decouple Runner khỏi transport (stderr / SSE / file).

#### Acceptance Criteria

1. THE HmeRunner SHALL gọi `log_callback` với 3 tham số positional theo thứ tự `(level: str, message: str, payload: dict)` và `await` kết quả `Awaitable[None]`.
2. WHEN HmeRunner phát log do bắt đầu cycle, kết thúc cycle, bắt đầu sleep `retry_interval`, pause/resume, hoặc gặp lỗi fatal, THE HmeRunner SHALL truyền `level` thuộc tập `{"info", "warn", "error"}`.
3. WHERE Runner được wire bởi CLI, THE log_callback SHALL ghi message ra stderr với format `[HH:MM:SS][level] message`.
4. WHERE Runner được wire bởi Web, THE log_callback SHALL gọi `LogBuffer.push(level, message, payload)` để buffer hóa và broadcast event tới mọi SSE subscriber đang lắng nghe.
5. THE LogBuffer SHALL giữ tối đa 10.000 entry theo FIFO và tự động loại entry cũ nhất khi vượt giới hạn.
6. WHEN `POST /api/icloud/run` chuyển `HmeRunner.is_running` từ `False` sang `True`, THE LogBuffer SHALL clear toàn bộ entry trước đó và reset `seq` về 0.
7. WHERE một SSE subscriber có `asyncio.Queue` đầy, THE LogBuffer SHALL drop event mới cho subscriber đó mà không block các subscriber khác và không block HmeRunner.

### Requirement 11: Job Layer Removal

**User Story:** Là một maintainer codebase, tôi muốn xoá hoàn toàn lớp Job (~1.500 LOC) khỏi repo, để giảm complexity và đảm bảo single source of truth cho điều phối là HmeRunner.

#### Acceptance Criteria

1. THE icloud_hme package SHALL không chứa thư mục `icloud_hme/jobs/` cũng như 12 file con (`__init__.py`, `manager.py`, `handlers.py`, `generate.py`, `bootstrap.py`, `check_all.py`, `deactivate_bulk.py`, `delete_bulk.py`, `reactivate_bulk.py`, `update_meta_bulk.py`, `list_sync.py`, `export.py`).
2. THE module `icloud_hme/models.py` SHALL không chứa dataclass `JobRecord` và `JobLogEntry`.
3. THE module `db/repositories.py` SHALL không chứa class `IcloudJobRepository`.
4. THE module `icloud_hme/exceptions.py` SHALL không chứa các class `JobError`, `JobNotFoundError`, `JobInvalidTransitionError`, `JobCrashedError`.
5. THE module `icloud_hme/web/router.py` SHALL không chứa các route `POST /api/icloud/emails/generate` (job-enqueue), `GET /api/icloud/jobs/{job_id}`, `POST /api/icloud/jobs/{job_id}/{action}`, `GET /api/icloud/jobs/{job_id}/log`, `GET /api/icloud/jobs/{job_id}/log/stream`, `GET /api/icloud/jobs`.
6. THE module `icloud_hme/cli.py` SHALL không chứa Typer group `job` cùng các subcommand `enqueue`, `list`, `get`, `status`, `stop`, `pause`, `resume`, `restart`, `stop-all`, `log`.
7. THE module `icloud_hme/__init__.py` SHALL không chứa `import` hay re-export bất kỳ symbol nào thuộc package `icloud_hme.jobs`.
8. THE thư mục `test/` SHALL không chứa các file `check_icloud_job_repository.py` và `check_job_handlers_dispatch.py`.

### Requirement 12: Database Schema Migration v6 → v7

**User Story:** Là một operator triển khai, tôi muốn migration tự xoá bảng `icloud_jobs` khi nâng cấp DB và idempotent với cả DB cũ lẫn DB mới khởi tạo, để dữ liệu Job layer cũ được dọn mà không phá DB hiện hành.

#### Acceptance Criteria

1. THE module `db/schema.py` SHALL khai báo `CURRENT_VERSION = 7`.
2. THE dict `db.schema.MIGRATIONS` SHALL chứa entry với key `7` là danh sách câu lệnh SQL bao gồm `"DROP TABLE IF EXISTS icloud_jobs;"`.
3. THE dict `db.schema.MIGRATIONS` SHALL giữ nguyên entry với key `6` để DB phiên bản ≤ 5 vẫn migrate qua được v6 trước khi chạy v7.
4. THE list `db.schema.ALL_DDL` SHALL không chứa `DDL_ICLOUD_JOBS` và các DDL index của bảng `icloud_jobs`.
5. WHEN migration v7 chạy trên DB ở version 6 đã có bảng `icloud_jobs` chứa dữ liệu, THE Database_Migration SHALL drop bảng đó và bump schema version lên 7 mà không raise lỗi.
6. WHEN migration v7 chạy trên DB mới khởi tạo từ `ALL_DDL` hiện hành (không chứa bảng `icloud_jobs`), THE Database_Migration SHALL hoàn tất no-op nhờ mệnh đề `IF EXISTS` trong câu lệnh `DROP TABLE`.

### Requirement 13: Frontend Log Viewer UI

**User Story:** Là một operator dùng Web UI, tôi muốn panel duy nhất gồm log viewer real-time, nút Start/Stop, và sidebar trạng thái pool, thay cho UI quản lý job phức tạp trước đây, để vận hành nhanh và quan sát rõ trạng thái Runner.

#### Acceptance Criteria

1. THE Frontend_UI SHALL render một log viewer panel kết nối tới `GET /api/icloud/run/log/stream` qua `EventSource` và auto-scroll xuống cuối khi nhận `LogEvent` mới.
2. THE Frontend_UI SHALL render nút Start gọi `POST /api/icloud/run` và nút Stop gọi `POST /api/icloud/run/stop`.
3. WHILE response của `GET /api/icloud/run/status` có `running == true`, THE Frontend_UI SHALL disable nút Start, enable nút Stop, và hiển thị badge với chữ "RUNNING".
4. WHILE response của `GET /api/icloud/run/status` có `running == false`, THE Frontend_UI SHALL enable nút Start, disable nút Stop, và hiển thị badge với chữ "IDLE".
5. THE Frontend_UI SHALL render sidebar liệt kê các Profile cùng trạng thái pool (`active` / `limited` / `quota_full` / `session_expired`) và `hme_count / 700`.
6. WHEN một `LogEvent` nhận được có `level == "warn"` hoặc `level == "error"`, THE Frontend_UI SHALL render dòng log đó với màu phân biệt rõ với level `"info"`.
7. WHILE `next_cycle_at` trong response của `GET /api/icloud/run/status` khác `null`, THE Frontend_UI SHALL hiển thị countdown đến thời điểm `next_cycle_at` với độ chính xác tới giây.
