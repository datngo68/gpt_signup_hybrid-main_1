"""Pydantic schemas cho icloud-runner-loop Web_API (task 5.2).

Refs:
    requirements.md R9.1, R9.5, R10.2.
    design.md §Components / Web Endpoints — schema block.

Schemas (Pydantic v2):
    - ``RunRequest``  — body của ``POST /api/icloud/run``.
    - ``RunStatus``   — response của ``GET /api/icloud/run/status``.
    - ``LogEvent``    — entry trong buffer + payload SSE stream
                        ``GET /api/icloud/run/log/stream``.

Design notes:
    - ``LogEvent`` được khai báo ở đây (single source of truth).
      ``icloud_hme/web/log_buffer.py`` import lại để xoá duplicate dataclass
      tạm. Set ``model_config = ConfigDict(frozen=True)`` để giữ tính
      immutable mà LogBuffer dựa vào (event đẩy vào subscriber queue
      không thể bị mutate ngoài).
    - ``Literal`` ép whitelist 7 action (R6.6) ngay tầng schema → request
      sai action bị 422 trước khi chạm Runner.
    - ``retry_interval`` ở ``RunRequest`` dùng ``ge=10`` (R7.3) — fail-fast
      ngang Settings; không hardcode min ngầm trong handler.
    - ``RunStatus.stats`` để ``dict[str, int]`` cho linh hoạt; handler tự
      build từ ``RunnerStats`` với 3 key ``created/errors/skipped``.
    - ``next_cycle_at`` là ISO 8601 UTC string (None khi đang trong cycle
      hoặc idle) — handler convert từ ``runner.next_cycle_at`` (epoch float).
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ── Action whitelist khớp R6.6 + design Component 4 RunRequest ──────────────
RunAction = Literal[
    "generate",
    "check_all",
    "deactivate_bulk",
    "reactivate_bulk",
    "delete_bulk",
    "update_meta_bulk",
    "list_sync",
]

# ── Log level whitelist khớp R10.2 ──────────────────────────────────────────
# Mở rộng "success" cho autoreg (signup hoàn tất → trigger UI refresh
# + render đặc biệt email|password|secret_2fa). icloud-runner-loop chỉ
# dùng info/warn/error.
LogLevel = Literal["info", "warn", "error", "success"]


class RunRequest(BaseModel):
    """Body cho ``POST /api/icloud/run``.

    Validation:
        - ``action`` thuộc 7 giá trị whitelist (R6.6, R9.1).
        - ``params`` dict tự do (Runner ``_run_one_cycle`` validate sâu).
        - ``retry_interval`` optional; nếu set phải ``>= 10`` (R7.3).
    """

    model_config = ConfigDict(extra="forbid")

    action: RunAction
    params: dict[str, Any] = Field(default_factory=dict)
    retry_interval: Optional[int] = Field(default=None, ge=10)


class RunStatus(BaseModel):
    """Response cho ``GET /api/icloud/run/status`` (R9.5)."""

    model_config = ConfigDict(extra="forbid")

    running: bool
    action: Optional[str] = None
    cycle: int = 0
    stats: dict[str, int] = Field(default_factory=dict)
    retry_interval: int
    next_cycle_at: Optional[str] = None  # ISO 8601 UTC khi đang sleep
    # Per-profile-cycle (icloud-runner-loop revised):
    # - ``current_apple_id``: profile đang chạy NGAY lúc snapshot (None khi
    #   giữa 2 profile / sleep / idle / action != 'generate').
    # - ``profile_states``: map apple_id → state literal:
    #   idle / running / waiting / done / cooldown / disabled.
    #   Empty dict khi runner idle hoặc action != 'generate'.
    current_apple_id: Optional[str] = None
    profile_states: dict[str, str] = Field(default_factory=dict)


class LogEvent(BaseModel):
    """Một entry trong ``LogBuffer`` + payload SSE stream (R10.2).

    Frozen để consumer (subscriber queue, SSE) không thể mutate event đã
    push — tránh race condition giữa multiple subscriber chia chung object.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: str          # ISO 8601 UTC (datetime.now(timezone.utc).isoformat())
    level: LogLevel
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)
    seq: int


__all__ = [
    "RunAction",
    "LogLevel",
    "RunRequest",
    "RunStatus",
    "LogEvent",
]
