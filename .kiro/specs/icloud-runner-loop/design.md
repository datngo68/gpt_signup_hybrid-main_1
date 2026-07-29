# Design Document: iCloud Runner Loop

## Overview

**Vấn đề:** Lớp `icloud_hme/jobs/` (12 file, ~1.500 LOC) đang đóng vai trò thin-wrapper quanh service layer (generator/checker/manager): mỗi handler chỉ gọi 1 hàm service rồi serialize kết quả, kèm state machine 6 trạng thái, JSONL file logging, restart chain, crash detection. UI hiện tại quản lý job theo kiểu queue (enqueue/pause/resume/restart) — phức tạp hơn nhu cầu thực tế của user.

**Giải pháp:** Thay Job layer bằng module `Runner` ~200 LOC chạy **infinite loop** (`cycle → wait retry_interval → cycle`), chỉ dừng khi user bấm Stop hoặc gửi SIGINT. Logic core (`generator.py`, `checker.py`, `pool.py`, `manager.py`) giữ nguyên 100% — Runner gọi service trực tiếp y hệt cách handlers đang gọi. UI đổi từ job-manager sang **log viewer real-time** với 2 nút Start/Stop và panel hiển thị profile pool status. Khi pool exhausted ở cycle hiện tại, Runner sleep `retry_interval` rồi thử lại để các profile `limited` / `quota_full` có cơ hội recover (limited TTL, quota TTL).

**Phạm vi:** Refactor; không thay đổi behavior business của generator/checker/pool. CLI commands `bootstrap`, `profile open`, `profile delete`, `status`, `reconcile`, `email *`, `audit *` giữ nguyên (chạy 1-shot, không qua Runner). Chỉ `generate` và `check --all` chuyển sang Runner.

---

## Architecture

### High-Level Component Diagram (3 Layers)

```mermaid
graph TD
    subgraph "Layer 1: Presentation"
        CLI["CLI<br/>icloud_hme/cli.py<br/>(generate, check)"]
        WEB["Web Router<br/>icloud_hme/web/router.py<br/>(/api/icloud/run/*)"]
    end

    subgraph "Layer 2: Runner (NEW)"
        RUNNER["HmeRunner<br/>icloud_hme/runner.py<br/>~200 LOC"]
        EVENTS["asyncio.Event<br/>cancel / pause / resume"]
        LOGCB["log_callback<br/>async (level, msg, payload)"]
        RUNNER --- EVENTS
        RUNNER --- LOGCB
    end

    subgraph "Layer 3: Services (UNCHANGED)"
        GEN["HmeGenerator<br/>generator.py"]
        CHK["ProfileChecker<br/>checker.py"]
        POOL["IcloudPoolManager<br/>pool.py"]
        MGR["HmeManager<br/>manager.py"]
        REC["Recorder<br/>recorder.py"]
    end

    subgraph "Layer 4: Persistence"
        DB[("SQLite<br/>db/schema.py<br/>migration v7")]
    end

    CLI -->|"runner.start(action, params)"| RUNNER
    WEB -->|"asyncio.create_task(runner.start(...))"| RUNNER

    CLI -.->|"SIGINT → runner.stop()"| RUNNER
    WEB -.->|"POST /run/stop → runner.stop()"| RUNNER
    WEB -.->|"GET /run/log SSE"| LOGCB

    RUNNER -->|"_run_one_cycle dispatch"| GEN
    RUNNER -->|"_run_one_cycle dispatch"| CHK
    RUNNER -->|"_run_one_cycle dispatch"| MGR

    GEN --> POOL
    CHK --> POOL
    MGR --> POOL
    GEN --> REC
    POOL --> DB
    MGR --> DB

    classDef new fill:#d4f4dd,stroke:#2d7a3e,stroke-width:2px
    classDef unchanged fill:#e8e8e8,stroke:#666
    classDef removed fill:#fdd,stroke:#a33,stroke-dasharray: 4 4

    class RUNNER,EVENTS,LOGCB new
    class GEN,CHK,POOL,MGR,REC unchanged
```

**Quan sát:**
- Runner là single-instance per process (guard bằng `is_running` flag) — không cần queue/scheduler.
- Cancel-event được khởi tạo trong `start()` và truyền xuống generator/checker, để service layer cũng abort sớm.
- `log_callback` decouple Runner khỏi transport — CLI in stderr, Web push SSE, không cần ghi JSONL ra file.

### Data Flow — Một Cycle

```mermaid
flowchart LR
    S[Start cycle] --> P[Pool.pick_active_profile]
    P -->|profile| EX[Session.extract]
    EX --> CALL[Service call:<br/>generate / check / manager]
    CALL -->|OK| LOG1[log info<br/>+ stats++]
    CALL -->|QuotaError| Q[pool.mark_limited<br/>quota_retry_until]
    CALL -->|AuthError| A[pool.mark_session_expired]
    CALL -->|ClientError| ER[log error<br/>continue]
    LOG1 --> NEXT{Còn profile<br/>eligible?}
    Q --> NEXT
    A --> NEXT
    ER --> NEXT
    NEXT -->|Yes| P
    NEXT -->|No: pool exhausted| END[End cycle<br/>return result]
    P -->|None: pool exhausted| END
```

### Infinite Loop Sequence Diagram

```mermaid
sequenceDiagram
    participant U as User (CLI/Web)
    participant R as HmeRunner
    participant G as Generator/Checker/Manager
    participant L as log_callback

    U->>R: start(action, params)
    R->>R: Init events (cancel/pause/resume)
    R->>L: info "Runner started: {action}"

    loop Infinite Loop (until cancel)
        R->>R: cycle_count += 1
        R->>L: info "── Cycle #N ──"

        R->>G: _run_one_cycle(action, params)
        Note over G: bounded run<br/>(count_per_cycle hoặc<br/>tới khi pool exhausted)
        G-->>R: cycle_result {created, failures, ...}

        R->>R: total_stats += cycle_result
        R->>L: info "Cycle #N done: {result}"

        alt cancel_event.is_set()
            R->>R: break loop
        else continue
            R->>L: info "Waiting {retry_interval}s..."
            R->>R: _interruptible_sleep(retry_interval)
            Note over R: chunks 1s,<br/>check cancel/pause<br/>mỗi giây
            alt interrupted by cancel
                R->>R: break loop
            end
        end
    end

    U->>R: stop() (lúc bất kỳ)
    Note over R: cancel_event.set()<br/>→ loop dừng tại checkpoint

    R->>L: info "Runner stopped. Summary: {...}"
    R-->>U: summary {total_cycles, created, errors, skipped, stopped_by}
```

---

## Components and Interfaces

### Component 1: `HmeRunner`

**Purpose:** Loop controller — quản lý vòng đời infinite cycle, cancel/pause/resume signaling, stats aggregation, log fan-out. KHÔNG chứa business logic; mọi work delegate xuống service layer.

**Responsibilities:**
- Khởi tạo + reset state mỗi lần `start()` (cycle_count, stats, events).
- Gọi `_run_one_cycle()` lặp lại, sleep `retry_interval` giữa các cycle.
- Đảm bảo single-instance (`is_running` guard) → raise `RuntimeError` nếu start trùng.
- Forward `cancel_event` / `pause_event` / `resume_event` xuống generator/checker.
- Emit log qua `log_callback` async.
- Trả summary khi cancel; raise khi gặp lỗi không-recoverable.

**Class Skeleton:**

```python
# icloud_hme/runner.py
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .config import Settings
from .generator import HmeGenerator
from .checker import ProfileChecker
from .manager import HmeManager
from .pool import IcloudPoolManager


# Type alias cho log callback — async để Web có thể await SSE broadcast
LogCallback = Callable[[str, str, dict[str, Any]], Awaitable[None]]


@dataclass
class RunnerStats:
    """Tổng hợp stats trong toàn session (cộng dồn qua các cycle)."""
    created: int = 0
    errors: int = 0
    skipped: int = 0


class HmeRunner:
    """Infinite-loop runner thay thế JobManager.

    Lifecycle:
        - start(action, params): block tới khi cancel → return summary
        - stop(): set cancel_event, loop dừng tại checkpoint kế tiếp
        - pause()/resume(): tạm dừng giữa các unit work
        - is_running, current_action, cycle_count, stats: read-only inspection
    """

    def __init__(
        self,
        *,
        generator: HmeGenerator,
        checker: ProfileChecker,
        hme_manager: HmeManager,
        pool_manager: IcloudPoolManager,
        settings: Settings,
        log_callback: LogCallback,
        retry_interval: Optional[int] = None,
    ) -> None:
        self._generator = generator
        self._checker = checker
        self._hme_manager = hme_manager
        self._pool_mgr = pool_manager
        self._settings = settings
        self._log_cb = log_callback
        self._retry_interval = retry_interval or settings.icloud_retry_interval

        # Runtime state
        self._cancel_event: Optional[asyncio.Event] = None
        self._pause_event: Optional[asyncio.Event] = None
        self._resume_event: Optional[asyncio.Event] = None
        self._running: bool = False
        self._current_action: Optional[str] = None
        self._cycle_count: int = 0
        self._next_cycle_at: Optional[float] = None  # epoch seconds
        self._stats: RunnerStats = RunnerStats()

    # ── Public read-only properties ──────────────────────────────
    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def current_action(self) -> Optional[str]:
        return self._current_action

    @property
    def cycle_count(self) -> int:
        return self._cycle_count

    @property
    def stats(self) -> RunnerStats:
        return self._stats

    @property
    def retry_interval(self) -> int:
        return self._retry_interval

    @property
    def next_cycle_at(self) -> Optional[float]:
        return self._next_cycle_at

    # ── Lifecycle ────────────────────────────────────────────────
    async def start(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """Chạy infinite loop tới khi cancel.

        Preconditions:
            - is_running == False (raise RuntimeError nếu đã chạy)
            - action ∈ {generate, check_all, deactivate_bulk, reactivate_bulk,
                       delete_bulk, update_meta_bulk, list_sync}
            - params hợp lệ với action

        Postconditions:
            - cycle_count >= 1 (hoặc 0 nếu cancel ngay khi vào loop)
            - is_running == False sau khi return
            - Trả summary dict: {total_cycles, created, errors, skipped, stopped_by}
        """
        if self._running:
            raise RuntimeError("Runner đang chạy action khác")

        # Reset state
        self._running = True
        self._current_action = action
        self._cycle_count = 0
        self._stats = RunnerStats()
        self._cancel_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        self._resume_event = asyncio.Event()
        self._next_cycle_at = None

        try:
            await self._log_cb("info", f"Runner started: {action}", {"params": params})

            while not self._cancel_event.is_set():
                self._cycle_count += 1
                await self._log_cb(
                    "info",
                    f"── Cycle #{self._cycle_count} ──",
                    {"cycle": self._cycle_count},
                )

                cycle_result = await self._run_one_cycle(action, params)

                self._stats.created += int(cycle_result.get("created", 0))
                self._stats.errors += len(cycle_result.get("failures", []))
                self._stats.skipped += len(cycle_result.get("disabled_profiles", []))

                await self._log_cb(
                    "info",
                    f"Cycle #{self._cycle_count} done: {cycle_result}",
                    {"cycle": self._cycle_count, "result": cycle_result},
                )

                if self._cancel_event.is_set():
                    break

                # Sleep retry_interval với khả năng interrupt
                import time
                self._next_cycle_at = time.time() + self._retry_interval
                await self._log_cb(
                    "info",
                    f"Waiting {self._retry_interval}s before next cycle...",
                    {"retry_interval": self._retry_interval,
                     "next_cycle_at": self._next_cycle_at},
                )
                interrupted = await self._interruptible_sleep(self._retry_interval)
                self._next_cycle_at = None
                if interrupted:
                    break

            summary = {
                "total_cycles": self._cycle_count,
                "created": self._stats.created,
                "errors": self._stats.errors,
                "skipped": self._stats.skipped,
                "stopped_by": "user" if self._cancel_event.is_set() else "completed",
            }
            await self._log_cb(
                "info", f"Runner stopped. Summary: {summary}", summary
            )
            return summary

        except Exception as exc:
            await self._log_cb(
                "error",
                f"Runner fatal error: {type(exc).__name__}: {exc}",
                {"error_type": type(exc).__name__},
            )
            raise
        finally:
            self._running = False
            self._current_action = None
            self._next_cycle_at = None

    def stop(self) -> None:
        """Signal cancel — non-blocking. Loop dừng tại checkpoint kế tiếp."""
        if self._cancel_event is not None:
            self._cancel_event.set()
        # Đánh thức nếu đang chờ resume
        if self._resume_event is not None and self._pause_event is not None:
            if self._pause_event.is_set():
                self._resume_event.set()

    def pause(self) -> None:
        """Pause — checkpoint kế tiếp sẽ block tới khi resume."""
        if self._pause_event is not None:
            self._pause_event.set()

    def resume(self) -> None:
        """Resume sau pause."""
        if self._resume_event is not None and self._pause_event is not None:
            self._resume_event.set()

    # ── Internal ─────────────────────────────────────────────────
    async def _run_one_cycle(
        self, action: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Dispatch sang service layer cho 1 cycle bounded.

        Generator/checker/manager đã sẵn sàng nhận cancel/pause event,
        nên work bên trong cycle cũng interruptible.
        """
        if action == "generate":
            result = await self._generator.generate(
                count=params.get("count_per_cycle"),
                infinite=False,  # Runner quản lý infinite loop, không phải Generator
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

        if action == "check_all":
            results = await self._checker.check_all(
                auto_mark=params.get("auto_mark", True),
                proxy=params.get("proxy"),
                cancellation_event=self._cancel_event,
            )
            return {
                "checked": len(results),
                "ok": sum(1 for r in results if r.ok),
                "failed": sum(1 for r in results if not r.ok),
            }

        if action in ("deactivate_bulk", "reactivate_bulk", "delete_bulk"):
            method = getattr(self._hme_manager, action)
            result = await method(
                params.get("emails", []),
                dry_run=params.get("dry_run", False),
            )
            return {"succeeded": result.succeeded, "failed": result.failed}

        if action == "update_meta_bulk":
            result = await self._hme_manager.update_meta_bulk(
                params.get("items", []),
                dry_run=params.get("dry_run", False),
            )
            return {"succeeded": result.succeeded, "failed": result.failed}

        if action == "list_sync":
            diff = await self._hme_manager.list_sync(params["apple_id"])
            return {
                "inserted_active": diff.inserted_active,
                "inserted_inactive": diff.inserted_inactive,
                "unchanged": diff.unchanged,
            }

        raise ValueError(f"Unknown action: {action}")

    async def _interruptible_sleep(self, seconds: int) -> bool:
        """Sleep theo chunk 1s, kiểm tra cancel/pause mỗi vòng.

        Returns:
            True nếu bị cancel (caller nên break loop ngoài).
            False nếu sleep đủ thời gian.

        Postcondition:
            - Mọi tick (1s) đều check cancel_event → reaction time ≤ 1s
            - Khi pause_event set: block tới khi resume_event set
        """
        for _ in range(int(seconds)):
            if self._cancel_event is not None and self._cancel_event.is_set():
                return True

            if self._pause_event is not None and self._pause_event.is_set():
                await self._log_cb("info", "Paused. Waiting for resume...", {})
                if self._resume_event is not None:
                    await self._resume_event.wait()
                    self._resume_event.clear()
                self._pause_event.clear()
                if self._cancel_event is not None and self._cancel_event.is_set():
                    return True
                await self._log_cb("info", "Resumed.", {})

            await asyncio.sleep(1.0)

        return False
```

**Loop invariants (cho `start()`):**
- `0 <= self._cycle_count` luôn đúng và monotonic non-decreasing.
- `self._running == True` trong suốt thời gian thân `try`, đảm bảo `False` sau `finally`.
- Mỗi lần vào thân while: `self._cancel_event` đã được khởi tạo (≠ None).
- Stats không bao giờ giảm: `stats.created`, `stats.errors`, `stats.skipped` chỉ tăng.

---

### Component 2: Configuration Schema

**File:** `icloud_hme/config.py` — thêm field vào `Settings` dataclass.

```python
# icloud_hme/config.py — patch (chỉ phần mới thêm)
@dataclass(frozen=True)
class Settings:
    # ... các field hiện có ...

    # Runner config (mới)
    icloud_retry_interval: int = 900  # giây giữa 2 cycle (default 15 min)
    icloud_max_errors_per_cycle: int = 0  # 0 = không giới hạn

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "Settings":
        # ... logic hiện có ...
        return cls(
            # ... các field hiện có ...
            icloud_retry_interval=_int_env(
                env, "ICLOUD_RETRY_INTERVAL", default=900, min_value=10
            ),
            icloud_max_errors_per_cycle=_int_env(
                env, "ICLOUD_MAX_ERRORS_PER_CYCLE", default=0, min_value=0
            ),
        )
```

**Validation rules:**
- `icloud_retry_interval >= 10` (giây) — fail-fast nếu user set quá thấp gây rate-limit.
- `icloud_max_errors_per_cycle >= 0` — 0 nghĩa là không cap.
- Cả 2 đều load từ env, không hardcode trong code.

---

### Component 3: CLI Commands

**File:** `icloud_hme/cli.py` — thay 2 command, giữ nguyên các command 1-shot.

#### `generate` (chạy qua Runner — infinite loop)

```bash
# Drain all profiles mỗi cycle, đợi 15 min giữa cycles (default)
python -m gpt_signup_hybrid.icloud_hme generate

# Mỗi cycle tạo tối đa 50 email
python -m gpt_signup_hybrid.icloud_hme generate --count-per-cycle 50

# Tuỳ chỉnh retry interval
python -m gpt_signup_hybrid.icloud_hme generate --retry-interval 600

# Kèm label/note/proxy
python -m gpt_signup_hybrid.icloud_hme generate \
    --count-per-cycle 100 \
    --label "batch-2025-Q1" \
    --note "Auto-generated" \
    --proxy "http://user:pass@proxy:8080"

# Dừng: Ctrl+C → SIGINT → runner.stop() → graceful exit
```

**Flags:**

| Flag | Type | Default | Mô tả |
|------|------|---------|-------|
| `--count-per-cycle` | int / None | None | Số email tối đa mỗi cycle. None = drain tới khi pool exhausted |
| `--retry-interval` | int | từ env / 900 | Giây đợi giữa cycle |
| `--label` | str | None | Label gắn vào HME record |
| `--note` | str | None | Note gắn vào HME record |
| `--proxy` | str | None | HTTP proxy override |

**Bỏ flag:** `--infinite` (mọi lần chạy đều là infinite loop — Runner control).

#### `check --all` (chạy qua Runner — infinite loop)

```bash
# Check all profiles liên tục, retry mỗi 15 min
python -m gpt_signup_hybrid.icloud_hme check --all

# Tuỳ chỉnh interval, không auto-mark
python -m gpt_signup_hybrid.icloud_hme check --all \
    --retry-interval 1800 \
    --no-auto-mark
```

**Flags:**

| Flag | Type | Default | Mô tả |
|------|------|---------|-------|
| `--all` | bool | required | Bật chế độ check toàn bộ profiles qua Runner |
| `--retry-interval` | int | từ env / 900 | Giây đợi giữa cycle |
| `--auto-mark / --no-auto-mark` | bool | true | Tự động mark trạng thái dựa trên kết quả check |
| `--proxy` | str | None | HTTP proxy override |

#### Commands KHÔNG đi qua Runner (giữ nguyên 1-shot)

`bootstrap`, `profile open`, `profile delete`, `status`, `reconcile`, `email deactivate/reactivate/delete/mark-used/update-meta/list-sync/export`, `audit list/cleanup`.

#### Skeleton CLI handler

```python
# icloud_hme/cli.py — patch cho generate command
import signal

@app.command("generate")
def cmd_generate(
    count_per_cycle: Optional[int] = typer.Option(None, "--count-per-cycle"),
    retry_interval: Optional[int] = typer.Option(None, "--retry-interval"),
    label: Optional[str] = typer.Option(None, "--label"),
    note: Optional[str] = typer.Option(None, "--note"),
    proxy: Optional[str] = typer.Option(None, "--proxy"),
) -> None:
    settings = Settings.from_env(os.environ)
    runner = _build_runner(settings, log_callback=_cli_log)

    # SIGINT → graceful stop (Ctrl+C lần 1 = stop, lần 2 = force exit)
    def _handle_sigint(signum, frame):
        runner.stop()
    signal.signal(signal.SIGINT, _handle_sigint)

    summary = asyncio.run(
        runner.start(
            action="generate",
            params={
                "count_per_cycle": count_per_cycle,
                "label": label,
                "note": note,
                "proxy": proxy,
            },
        )
    )
    typer.echo(f"Final summary: {summary}")


async def _cli_log(level: str, message: str, payload: dict) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}][{level}] {message}", file=sys.stderr)
```

---

### Component 4: Web Endpoints

**File:** `icloud_hme/web/router.py` — thay block job endpoints bằng block runner endpoints.

#### Endpoint contract

| Method | Path | Body / Query | Response |
|--------|------|--------------|----------|
| `POST` | `/api/icloud/run` | `{action, params, retry_interval?}` | `{ok: true, action} \| 409 {error: "already_running"}` |
| `POST` | `/api/icloud/run/stop` | — | `{ok: true}` |
| `POST` | `/api/icloud/run/pause` | — | `{ok: true}` |
| `POST` | `/api/icloud/run/resume` | — | `{ok: true}` |
| `GET` | `/api/icloud/run/status` | — | `RunStatus` (xem dưới) |
| `GET` | `/api/icloud/run/log` | `?offset=N&limit=M` (paginate) | `{events: [LogEvent], next_offset}` |
| `GET` | `/api/icloud/run/log/stream` | — | SSE stream `LogEvent` |

#### Schemas

```python
# icloud_hme/web/schemas.py (mới hoặc append)
from pydantic import BaseModel, Field
from typing import Any, Literal, Optional

class RunRequest(BaseModel):
    action: Literal["generate", "check_all", "deactivate_bulk",
                    "reactivate_bulk", "delete_bulk",
                    "update_meta_bulk", "list_sync"]
    params: dict[str, Any] = Field(default_factory=dict)
    retry_interval: Optional[int] = Field(default=None, ge=10)


class RunStatus(BaseModel):
    running: bool
    action: Optional[str] = None
    cycle: int = 0
    stats: dict[str, int] = Field(default_factory=dict)  # created/errors/skipped
    retry_interval: int
    next_cycle_at: Optional[str] = None  # ISO 8601, None khi đang trong cycle


class LogEvent(BaseModel):
    ts: str          # ISO 8601 UTC
    level: Literal["info", "warn", "error"]
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)
    seq: int         # monotonic offset trong session hiện tại
```

#### Skeleton handler

```python
# icloud_hme/web/router.py — patch
@router.post("/api/icloud/run")
async def api_run_start(req: RunRequest, runner: HmeRunner = Depends(get_runner)):
    if runner.is_running:
        raise HTTPException(status_code=409, detail={"error": "already_running"})

    # Spawn task — KHÔNG await, để response trả về ngay
    log_buffer = get_log_buffer()
    log_buffer.clear()  # mỗi run mới buffer reset
    asyncio.create_task(
        runner.start(action=req.action, params=req.params)
    )
    return {"ok": True, "action": req.action}


@router.post("/api/icloud/run/stop")
async def api_run_stop(runner: HmeRunner = Depends(get_runner)):
    runner.stop()
    return {"ok": True}


@router.post("/api/icloud/run/pause")
async def api_run_pause(runner: HmeRunner = Depends(get_runner)):
    runner.pause()
    return {"ok": True}


@router.post("/api/icloud/run/resume")
async def api_run_resume(runner: HmeRunner = Depends(get_runner)):
    runner.resume()
    return {"ok": True}


@router.get("/api/icloud/run/status", response_model=RunStatus)
async def api_run_status(runner: HmeRunner = Depends(get_runner)):
    next_iso = (
        datetime.fromtimestamp(runner.next_cycle_at, tz=timezone.utc).isoformat()
        if runner.next_cycle_at
        else None
    )
    return RunStatus(
        running=runner.is_running,
        action=runner.current_action,
        cycle=runner.cycle_count,
        stats={
            "created": runner.stats.created,
            "errors": runner.stats.errors,
            "skipped": runner.stats.skipped,
        },
        retry_interval=runner.retry_interval,
        next_cycle_at=next_iso,
    )


@router.get("/api/icloud/run/log/stream")
async def api_run_log_stream(buffer: LogBuffer = Depends(get_log_buffer)):
    async def event_stream():
        async for event in buffer.subscribe():
            yield f"data: {event.model_dump_json()}\n\n"
    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

**Authentication:** mọi endpoint mới require Bearer token (giống endpoint cũ); reuse middleware có sẵn.

---

### Component 5: Log Callback Signature

**Type:** async callable, signature cố định, transport-agnostic.

```python
# Type alias định nghĩa trong icloud_hme/runner.py
LogCallback = Callable[[str, str, dict[str, Any]], Awaitable[None]]
#                       level message payload         return None
```

**Contract:**

| Param | Type | Mô tả |
|-------|------|-------|
| `level` | `str` | "info" \| "warn" \| "error" |
| `message` | `str` | Single-line human-readable |
| `payload` | `dict[str, Any]` | Structured data — JSON-serializable |

**Implementations:**

```python
# CLI implementation — print stderr
async def cli_log(level: str, message: str, payload: dict) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}][{level}] {message}", file=sys.stderr)


# Web implementation — push vào in-memory buffer + broadcast SSE
class LogBuffer:
    """Capped FIFO buffer + asyncio pub-sub cho SSE subscribers."""
    MAX_ENTRIES = 10_000

    def __init__(self) -> None:
        self._entries: collections.deque[LogEvent] = collections.deque(
            maxlen=self.MAX_ENTRIES
        )
        self._subscribers: set[asyncio.Queue] = set()
        self._seq: int = 0

    def clear(self) -> None:
        self._entries.clear()
        self._seq = 0

    async def push(self, level: str, message: str, payload: dict) -> None:
        self._seq += 1
        event = LogEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            level=level,
            message=message,
            payload=payload,
            seq=self._seq,
        )
        self._entries.append(event)
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # subscriber chậm — drop event cho subscriber đó

    async def subscribe(self) -> AsyncIterator[LogEvent]:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        # Replay history
        for ev in list(self._entries):
            await q.put(ev)
        self._subscribers.add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subscribers.discard(q)


# Web log_callback wrap LogBuffer.push
def make_web_log_callback(buffer: LogBuffer) -> LogCallback:
    async def _cb(level: str, message: str, payload: dict) -> None:
        await buffer.push(level, message, payload)
    return _cb
```

---

## Data Models

### Schema changes

| Thay đổi | File | Mô tả |
|----------|------|-------|
| **DROP** `JobRecord` dataclass | `icloud_hme/models.py` (line 202–228) | Bỏ entirely |
| **DROP** `JobLogEntry` dataclass | `icloud_hme/models.py` | Bỏ entirely |
| **DROP** `IcloudJobRepository` | `db/repositories.py` (~line 1894) | Bỏ entirely |
| **DROP** `DDL_ICLOUD_JOBS` + indexes | `db/schema.py` (line 193–214, 217–226) | Bỏ khỏi `ALL_DDL` list (line 244–245). Migration v6 entry GIỮ NGUYÊN để DB cũ migrate được |
| **ADD** Migration v7 — `DROP TABLE icloud_jobs` | `db/schema.py` | Bump `CURRENT_VERSION = 7` |
| **ADD** `Settings.icloud_retry_interval`, `icloud_max_errors_per_cycle` | `icloud_hme/config.py` | Field mới + load từ env |

### Migration v7

```python
# db/schema.py — patch
MIGRATIONS: dict[int, list[str]] = {
    # ... 1..6 giữ nguyên ...
    6: [
        # giữ nguyên DDL_ICLOUD_JOBS để DB cũ migrate được
        # tới đây trước khi v7 drop
    ],
    7: [
        "DROP TABLE IF EXISTS icloud_jobs;",
    ],
}

CURRENT_VERSION: int = 7
```

**Migration semantics:**
- DB version 6 (đã có `icloud_jobs`) → run v7 → `DROP TABLE` (idempotent qua `IF EXISTS`).
- DB version mới khởi tạo từ scratch → ALL_DDL không còn `DDL_ICLOUD_JOBS` → table không tồn tại → v7 `DROP IF EXISTS` no-op an toàn.
- **Không xoá** entry `MIGRATIONS[6]` để upgrade path từ DB v5/cũ hơn vẫn pass qua step 6 trước khi drop ở v7.

### Removed in-memory models

```python
# Bỏ khỏi icloud_hme/models.py
# @dataclass JobRecord:        ← XOÁ
#     job_id: str
#     job_type: str
#     status: str  # pending/running/paused/cancelled/completed/failed
#     params: dict
#     created_at: datetime
#     started_at: Optional[datetime]
#     finished_at: Optional[datetime]
#     # ... ~22 fields

# @dataclass JobLogEntry:      ← XOÁ
#     ts: datetime
#     level: str
#     message: str
#     payload: dict
```

### Removed exceptions

```python
# Bỏ khỏi icloud_hme/exceptions.py
# class JobError(IcloudHmeError): ...               ← XOÁ
# class JobNotFoundError(JobError): ...             ← XOÁ
# class JobInvalidTransitionError(JobError): ...    ← XOÁ
# class JobCrashedError(JobError): ...              ← XOÁ
```

### Validation rules (Runner state)

| Field | Rule |
|-------|------|
| `cycle_count` | `>= 0`, monotonic non-decreasing trong session |
| `stats.created`, `stats.errors`, `stats.skipped` | `>= 0`, monotonic non-decreasing |
| `is_running` | mutex via Python single-threaded asyncio (1 process = 1 runner instance) |
| `retry_interval` | `>= 10` giây (validate ở `Settings`) |
| `next_cycle_at` | `None` hoặc epoch float trong tương lai |

---

## Error Handling

### Cancellation Flow

```mermaid
sequenceDiagram
    participant U as User
    participant CLI as CLI / Web
    participant R as Runner
    participant G as Generator

    U->>CLI: Ctrl+C / POST /run/stop
    CLI->>R: runner.stop()
    R->>R: cancel_event.set()
    Note over R: Đánh thức resume_event<br/>nếu đang pause

    par Trong cycle (Generator đang chạy)
        G->>G: check cancellation_event<br/>tại các checkpoint
        G-->>R: return GenerateResult sớm<br/>(processed_count < count)
    and Trong sleep retry_interval
        R->>R: _interruptible_sleep<br/>tick 1s detect cancel
        R->>R: return True (interrupted)
    end

    R->>R: break loop
    R->>CLI: return summary<br/>{stopped_by: "user"}
```

**Đảm bảo:**
- **Reaction time ≤ 1s** trong sleep phase (chunk 1s).
- **Reaction time = bounded** trong cycle phase, do generator/checker đã có sẵn checkpoint cancellation.
- Service layer KHÔNG ném exception khi cancel — return result partial. Runner update stats partial bình thường.

### Pause/Resume Flow

```mermaid
sequenceDiagram
    participant U as User
    participant R as Runner
    participant G as Generator

    U->>R: pause()
    R->>R: pause_event.set()

    Note over G: Generator vẫn forward pause_event<br/>→ block giữa 2 profile

    alt Đang trong cycle
        G->>G: kiểm pause_event<br/>tại checkpoint
        G->>G: await resume_event.wait()
    else Đang sleep retry_interval
        R->>R: tick phát hiện pause_event<br/>log "Paused..."
        R->>R: await resume_event.wait()
    end

    U->>R: resume()
    R->>R: resume_event.set()
    Note over R,G: pause_event clear<br/>flow tiếp tục
```

**Race condition handling:**
- `stop()` trong khi đang pause: `stop()` set cả `cancel_event` và `resume_event` để đánh thức blocking await → loop kiểm `cancel_event` ngay sau → break.
- `pause()` rồi `pause()` lại: idempotent — `set()` 2 lần là no-op.

### Fail-Fast Policy

| Tình huống | Hành vi |
|------------|---------|
| `runner.start()` khi `is_running=True` | Raise `RuntimeError("Runner đang chạy action khác")` ngay, KHÔNG queue |
| Web `POST /run` khi đang chạy | HTTP 409 Conflict `{"error": "already_running"}` |
| `action` không hợp lệ | Raise `ValueError(f"Unknown action: {action}")` trong `_run_one_cycle` → exception lên `start()` → log "fatal" → re-raise |
| Service layer raise (DB lỗi, network panic ngoài retry) | Log "error", re-raise → loop kết thúc với exception. KHÔNG swallow. |
| `retry_interval < 10` | Reject ở `Settings.from_env` — fail-fast khi load config |
| Pool empty hoàn toàn (không còn profile nào) | Cycle return `result.created=0, disabled_profiles=[...]` → vẫn sleep → cycle tiếp theo. KHÔNG raise (Runner không biết khi nào pool có profile mới) |

**Nguyên tắc:** Runner KHÔNG fallback che lỗi. Lỗi bất ngờ ở service layer = log + raise lên CLI/Web để user thấy stacktrace.

### Error scenarios cụ thể

| Scenario | Condition | Response | Recovery |
|----------|-----------|----------|----------|
| User Ctrl+C lần 1 | SIGINT | `runner.stop()` → graceful exit ≤ 1s sau khi đang sleep, ≤ checkpoint nếu đang work | Trả summary, exit 0 |
| User Ctrl+C lần 2 (force) | SIGINT lần 2 | Default Python KeyboardInterrupt → asyncio cancel → process exit | Stats có thể partial |
| DB lock | sqlite3 `database is locked` | Service layer raise → Runner log "error" → re-raise | Process exit, user retry |
| All profiles `session_expired` | Pool exhausted vĩnh viễn | Cycle xong với `created=0` → sleep → cycle mới (vô hạn cho đến khi user bootstrap profile mới) | User chạy `bootstrap` để có profile mới |
| Web SSE subscriber chậm | Queue full | Drop event cho subscriber đó (không block Runner) | Subscriber reconnect nhận snapshot từ buffer |
| Process crash giữa cycle | Bất ngờ | Không có recovery state — restart lại Runner = chạy cycle mới | Email đã tạo nằm trong DB từ trước, không mất |

---

## Correctness Properties

Các test property-based bên dưới dùng **Hypothesis** (Python). Lý do: codebase đã có `hypothesis` setup (xem `.hypothesis/` directory). Tham chiếu Requirements X.Y dùng numbering tạm (1.1..5.1) sẽ được khớp lại sau khi sinh `requirements.md` ở phase tiếp theo.

### Property 1: Start returns summary when cancelled

**Validates: Requirements 1.1**

```python
# test/test_runner_properties.py
from hypothesis import given, strategies as st
import asyncio
import pytest

@given(
    delay_before_cancel=st.floats(min_value=0.0, max_value=0.5),
    retry_interval=st.integers(min_value=1, max_value=3),
)
@pytest.mark.asyncio
async def test_start_returns_summary_when_cancelled(delay_before_cancel, retry_interval):
    """∀ delay ∈ [0, 0.5s], retry_interval ∈ [1, 3]:
       sau khi cancel_event set, start() RETURNS dict trong thời gian hữu hạn."""
    runner = build_test_runner(retry_interval=retry_interval)

    async def cancel_after_delay():
        await asyncio.sleep(delay_before_cancel)
        runner.stop()

    async with asyncio.timeout(retry_interval + 5):
        results = await asyncio.gather(
            runner.start(action="generate", params={"count_per_cycle": 1}),
            cancel_after_delay(),
        )
    summary = results[0]
    assert isinstance(summary, dict)
    assert summary["stopped_by"] == "user"
    assert "total_cycles" in summary
    assert summary["total_cycles"] >= 0
```

### Property 2: Cycle count monotonic non-decreasing

**Validates: Requirements 2.1**

```python
@given(num_observations=st.integers(min_value=2, max_value=20))
@pytest.mark.asyncio
async def test_cycle_count_monotonic(num_observations):
    """∀ thời điểm t1 <= t2 trong cùng session: cycle_count(t1) <= cycle_count(t2)."""
    runner = build_test_runner(retry_interval=1)

    observations = []

    async def observer():
        for _ in range(num_observations):
            observations.append(runner.cycle_count)
            await asyncio.sleep(0.3)
        runner.stop()

    await asyncio.gather(
        runner.start(action="generate", params={"count_per_cycle": 1}),
        observer(),
    )

    for i in range(1, len(observations)):
        assert observations[i] >= observations[i - 1], (
            f"cycle_count decreased: {observations[i-1]} → {observations[i]}"
        )
```

### Property 3: Interruptible sleep reacts within one second

**Validates: Requirements 3.1**

```python
@given(sleep_seconds=st.integers(min_value=2, max_value=10))
@pytest.mark.asyncio
async def test_interruptible_sleep_reacts_within_one_second(sleep_seconds):
    """∀ sleep_seconds ∈ [2, 10]: sau khi cancel_event.set() tại t0,
       _interruptible_sleep return tại t1 với (t1 - t0) <= 1.5s (1s tick + buffer)."""
    runner = build_test_runner()
    runner._cancel_event = asyncio.Event()
    runner._pause_event = asyncio.Event()
    runner._resume_event = asyncio.Event()

    import time
    cancel_at = None

    async def cancel_at_t():
        nonlocal cancel_at
        await asyncio.sleep(0.5)
        cancel_at = time.monotonic()
        runner._cancel_event.set()

    sleep_task = asyncio.create_task(runner._interruptible_sleep(sleep_seconds))
    await asyncio.gather(cancel_at_t(), sleep_task)
    returned_at = time.monotonic()
    interrupted = sleep_task.result()

    assert interrupted is True
    assert returned_at - cancel_at <= 1.5, (
        f"Reaction time too slow: {returned_at - cancel_at:.2f}s"
    )
```

### Property 4: Is running guard prevents concurrent start

**Validates: Requirements 4.1**

```python
@given(action_a=st.sampled_from(["generate", "check_all"]),
       action_b=st.sampled_from(["generate", "check_all"]))
@pytest.mark.asyncio
async def test_is_running_guard_prevents_concurrent_start(action_a, action_b):
    """∀ action_a, action_b: nếu start(action_a) đang chạy thì
       start(action_b) RAISE RuntimeError ngay."""
    runner = build_test_runner(retry_interval=10)

    first = asyncio.create_task(
        runner.start(action=action_a, params={"count_per_cycle": 1})
    )
    await asyncio.sleep(0.1)  # đợi first chiếm is_running

    assert runner.is_running is True

    with pytest.raises(RuntimeError, match="đang chạy action khác"):
        await runner.start(action=action_b, params={"count_per_cycle": 1})

    runner.stop()
    await first
    assert runner.is_running is False
```

### Property 5: Stats monotonic non-decreasing

**Validates: Requirements 5.1**

```python
@given(num_cycles=st.integers(min_value=1, max_value=5))
@pytest.mark.asyncio
async def test_stats_monotonic_non_decreasing(num_cycles):
    """∀ thời điểm t1 <= t2: stats.created(t1) <= stats.created(t2)
       (tương tự cho errors, skipped)."""
    runner = build_test_runner(retry_interval=1)
    snapshots: list[tuple[int, int, int]] = []

    async def sampler():
        for _ in range(num_cycles * 3):
            s = runner.stats
            snapshots.append((s.created, s.errors, s.skipped))
            await asyncio.sleep(0.2)
        runner.stop()

    await asyncio.gather(
        runner.start(action="generate", params={"count_per_cycle": 1}),
        sampler(),
    )

    for i in range(1, len(snapshots)):
        for field_idx in range(3):
            assert snapshots[i][field_idx] >= snapshots[i - 1][field_idx]
```

---

## Testing Strategy

### Unit Testing

- `HmeRunner.__init__` — wire dependencies, validate retry_interval.
- `HmeRunner._run_one_cycle` — mock service layer, verify dispatch đúng action → đúng method.
- `HmeRunner._interruptible_sleep` — verify reaction time, pause/resume handshake.
- `LogBuffer` — capped FIFO, subscriber pub-sub, replay history.

**Coverage goal:** core lifecycle (start/stop/pause/resume) ≥ 90% line coverage.

### Property-Based Testing

- Library: `hypothesis` (đã có setup ở `.hypothesis/`).
- 5 properties đã liệt kê ở section trên.
- Run: `python3 -m pytest test/test_runner_properties.py -v` (chạy 1 lần, không watch mode).

### Integration Testing

Manual smoke (theo project rules — không tự động viết test trừ khi user yêu cầu, nhưng có thể dùng để verify):

```bash
# test/smoke_runner_cli.py — script verify CLI Runner thực tế
# Chạy: python3 test/smoke_runner_cli.py

# Scenario 1: short retry interval, Ctrl+C sau 30s
python -m gpt_signup_hybrid.icloud_hme generate \
    --count-per-cycle 5 --retry-interval 10
# Verify: cycle 1 → tạo 5 email → đợi 10s → cycle 2 → ...
# Bấm Ctrl+C → graceful exit ≤ 1s → in summary

# Scenario 2: pool exhausted ngay
python -m gpt_signup_hybrid.icloud_hme generate \
    --count-per-cycle 100 --retry-interval 5
# Tất cả profile limited → cycle 1 created=0 → đợi 5s → ...
```

### Test File Layout (theo project rules)

- `test/syntax_check.py` — parse AST mọi file Python để catch syntax error sau refactor.
- `test/check_runner_cli.py` — wire Runner với mock service, verify CLI command flow.
- `test/test_runner_properties.py` — property-based tests (Hypothesis).
- `test/smoke_runner_web.py` — start uvicorn server, gọi POST /run, verify SSE stream.

---

## Files Impact Matrix

### XOÁ HOÀN TOÀN

| File | LOC | Lý do |
|------|-----|-------|
| `icloud_hme/jobs/__init__.py` | 43 | Job package init |
| `icloud_hme/jobs/manager.py` | 587 | JobManager (state machine) |
| `icloud_hme/jobs/handlers.py` | 342 | Handler dispatch table |
| `icloud_hme/jobs/generate.py` | 68 | Generate handler |
| `icloud_hme/jobs/bootstrap.py` | 73 | Bootstrap handler |
| `icloud_hme/jobs/check_all.py` | 64 | Check handler |
| `icloud_hme/jobs/deactivate_bulk.py` | 60 | Deactivate handler |
| `icloud_hme/jobs/delete_bulk.py` | 35 | Delete handler |
| `icloud_hme/jobs/reactivate_bulk.py` | 35 | Reactivate handler |
| `icloud_hme/jobs/update_meta_bulk.py` | 35 | Update meta handler |
| `icloud_hme/jobs/list_sync.py` | 58 | List sync handler |
| `icloud_hme/jobs/export.py` | 63 | Export handler |
| `test/check_icloud_job_repository.py` | — | Job repo test |
| `test/check_job_handlers_dispatch.py` | — | Handler test |
| `runtime/icloud_jobs/` (dir) | — | JSONL job log files cũ |
| **Tổng cộng** | **~1.463+** | |

### TẠO MỚI

| File | Est. LOC | Mô tả |
|------|----------|-------|
| `icloud_hme/runner.py` | ~250 | Class `HmeRunner` + `RunnerStats` + type aliases |
| `icloud_hme/web/log_buffer.py` | ~80 | `LogBuffer` capped FIFO + pub-sub |

### SỬA

| File | Thay đổi chính |
|------|----------------|
| `icloud_hme/cli.py` | Xoá `job_app` Typer group + 10 sub-commands (~200 LOC, line 1053–1295). Đổi `generate`, `check --all` chạy qua Runner |
| `icloud_hme/config.py` | Thêm `icloud_retry_interval`, `icloud_max_errors_per_cycle` vào `Settings` |
| `icloud_hme/models.py` | Xoá `JobRecord`, `JobLogEntry` dataclass (line 202–228) |
| `icloud_hme/exceptions.py` | Xoá `JobError`, `JobNotFoundError`, `JobInvalidTransitionError`, `JobCrashedError` |
| `icloud_hme/__init__.py` | Xoá imports/re-exports liên quan job |
| `icloud_hme/web/router.py` | Xoá 6 job endpoints (POST/GET /api/icloud/jobs/*). Thêm 6 runner endpoints (POST/GET /api/icloud/run/*) |
| `icloud_hme/web/schemas.py` | Bỏ `JobResponse`, `JobLogResponse`. Thêm `RunRequest`, `RunStatus`, `LogEvent` |
| `db/repositories.py` | Xoá class `IcloudJobRepository` (~line 1894+) |
| `db/schema.py` | Xoá `DDL_ICLOUD_JOBS` + indexes khỏi `ALL_DDL` (line 193–214, 217–226, 244–245). Giữ entry `MIGRATIONS[6]`. Thêm `MIGRATIONS[7] = ["DROP TABLE IF EXISTS icloud_jobs;"]`. Bump `CURRENT_VERSION = 7` |
| `web/static/hme.js` | Thay panel job-manager bằng log-viewer + Start/Stop button + profile sidebar |
| `web/static/index.html` | Update layout: log viewer 80% + profile panel 20% (xem mockup ở plan gốc) |

### KHÔNG ĐỘNG VÀO (giữ nguyên 100%)

| File | LOC | Lý do |
|------|-----|-------|
| `icloud_hme/generator.py` | 842 | Core generate — Runner gọi trực tiếp |
| `icloud_hme/checker.py` | 339 | Core check |
| `icloud_hme/pool.py` | 654 | Pool state machine |
| `icloud_hme/manager.py` | 1.194 | HME lifecycle manager |
| `icloud_hme/client.py` | — | Apple API client |
| `icloud_hme/session.py` | — | Session bundle extractor |
| `icloud_hme/bootstrap.py` | — | Bootstrap flow (CLI 1-shot) |
| `icloud_hme/recorder.py` | — | Recording sessions |
| `icloud_hme/profile_lock.py` | — | Profile locking |
| `icloud_hme/repository.py` | — | iCloud repos chính |

**Lưu ý:** Có thể cần sửa **1 chỗ** trong `generator.py` nếu hiện tại `generate(count=None, infinite=False)` raise — phải accept `count=None` ở mode bounded = "chạy tới khi pool exhausted". Cần verify trước khi implement Phase 2.

---

## Risks & Mitigations

| # | Rủi ro | Tác động | Mitigation |
|---|--------|----------|------------|
| R1 | Web nhận 2 request `POST /run` đồng thời | Chạy đè, corrupt stats | `is_running` guard ở Runner + HTTP 409 ở router. Atomic check via Python single-thread asyncio |
| R2 | Mất log khi process crash giữa session | User không xem được log đã chạy | Optional ghi log ra text file song song SSE (configurable). Crash = restart lại Runner = cycle mới, KHÔNG cần recover state |
| R3 | Pause/resume không hoạt động sau bỏ Job layer | UX bug | `HmeGenerator.generate()` đã sẵn sàng nhận `pause_event`/`resume_event`. Runner forward trực tiếp xuống generator → không mất feature |
| R4 | Bootstrap cần headed browser, không vào loop được | Bootstrap sẽ vỡ nếu force qua Runner | Bootstrap **không** đi qua Runner — giữ CLI 1-shot blocking như hiện tại. User chạy `bootstrap` bằng tay trước khi `generate` |
| R5 | DB `icloud_jobs` còn data cũ ở user prod | Migration v7 fail | `DROP TABLE IF EXISTS` idempotent. Test trên DB version 6 trước khi release |
| R6 | Generator `infinite=True` mode conflict với Runner | Double infinite loop | Runner gọi `generate(infinite=False, count=count_per_cycle)` luôn. Generator **không** chạy infinite mode khi Runner orchestrate |
| R7 | Retry interval quá ngắn → rate limit | Apple block IP/profile | Default 900s (= `quota_retry_minutes`). Validate `>= 10` ở `Settings.from_env`. UI warn nếu user nhập < 300s |
| R8 | `generate(count=None, infinite=False)` raise | Runner phase 2 không chạy được | Phase 1 verify trước: nếu raise, sửa generator để accept `count=None` = "drain tới pool exhausted" |
| R9 | SSE subscriber rò rỉ memory khi disconnect | OOM | `LogBuffer.subscribe()` finally remove subscriber khỏi set. Queue bounded `maxsize=1000`, drop event nếu subscriber chậm |
| R10 | SIGINT lần 1 không stop được (đang trong service work) | User phải Ctrl+C 2 lần | Service layer (generator/checker) đã có sẵn checkpoint cancel. Reaction ≤ 1 cycle work unit. Lần 2 = Python KeyboardInterrupt force kill (acceptable trade-off) |
| R11 | Frontend cũ vẫn gọi job API sau deploy | UI vỡ với 404 | Deploy cùng lúc backend + frontend. Coordinate với user trước khi merge |
| R12 | Migration v7 chạy nhưng app code mới chưa deploy | App vẫn import `IcloudJobRepository` → ImportError | Deploy code trước, migrate DB sau. Hoặc deploy đồng thời (atomic). Migration KHÔNG phá DB cũ vì `DROP IF EXISTS` |

---

## Dependencies

**Python packages:** không thêm dependency mới.
- `asyncio` (stdlib) — events, tasks, sleep.
- `dataclasses` (stdlib) — `RunnerStats`.
- `typing` (stdlib) — type aliases.
- `signal` (stdlib) — SIGINT handler trong CLI.
- `collections.deque` (stdlib) — capped FIFO trong `LogBuffer`.

**Tái sử dụng package có sẵn:**
- `typer` — CLI (đã dùng).
- `fastapi` + `pydantic` — Web router (đã dùng).
- `hypothesis` — property-based tests (đã setup ở `.hypothesis/`).

**Frontend:**
- Vanilla JS hiện có (xem `web/static/hme.js`) — KHÔNG thêm framework.
- SSE qua native `EventSource` API.
