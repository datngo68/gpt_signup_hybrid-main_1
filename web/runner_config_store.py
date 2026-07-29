"""Persist Runner form config (action, count_per_cycle, retry_interval,
label, note) ra JSON file để UI form không reset khi reload tab hoặc khi
backend restart.

Storage: ``<runtime_dir>/icloud/runner_config.json``.

File format (strict; bất kỳ key thừa / value sai type đều raise):

    {
      "action": "generate",
      "count_per_cycle": null | int>0,
      "retry_interval": int>=10 | null,
      "label": str | null,
      "note": str | null
    }

Semantics:
- ``count_per_cycle = null`` → không clamp số HME / cycle (Generator dùng
  pool quota); UI hiển thị placeholder ``optional``.
- ``retry_interval = null`` → Runner đọc default ``settings.icloud_retry_interval``.
- ``label = null`` / empty string đều coi như "không set" (không gửi trong
  ``RunRequest.params``).

Concurrency: Web server FastAPI single-process; file ghi bằng atomic
``write + rename``. Không cần lock cross-process vì runner_lock đã đảm
bảo chỉ 1 worker mutate runner config tại 1 thời điểm.

Fail-fast: load() raise ``RunnerConfigError`` khi JSON corrupt / schema
sai → caller (icloud_routes._init_services) tự quyết định fallback
default vs propagate (hiện default = log warn + dùng giá trị mặc định
ban đầu, KHÔNG xoá file để user soi).
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, ClassVar, Optional


# Khớp whitelist ``RunAction`` ở icloud_hme/web/schemas.py — duplicate
# tại đây để runner_config_store không depend ngược vào icloud_hme module.
_VALID_ACTIONS: tuple[str, ...] = (
    "generate",
    "check_all",
    "deactivate_bulk",
    "reactivate_bulk",
    "delete_bulk",
    "update_meta_bulk",
    "list_sync",
)

_RETRY_INTERVAL_MIN = 10  # khớp R7.3 / RunRequest.retry_interval ge=10


class RunnerConfigError(ValueError):
    """Schema / parse error khi đọc-ghi runner_config.json."""


@dataclass(frozen=True)
class RunnerConfig:
    """Snapshot config form Runner — immutable.

    Tất cả field optional theo nghĩa "user chưa set"; UI sẽ render placeholder.
    """

    action: str = "generate"
    count_per_cycle: Optional[int] = None
    retry_interval: Optional[int] = None
    label: Optional[str] = None
    note: Optional[str] = None

    # Default instance dùng khi file chưa tồn tại.
    DEFAULT: ClassVar["RunnerConfig"]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def default(cls) -> "RunnerConfig":
        return cls()

    @classmethod
    def from_dict(cls, raw: Any) -> "RunnerConfig":
        """Validate dict + build RunnerConfig. Raise RunnerConfigError nếu sai schema."""
        if not isinstance(raw, dict):
            raise RunnerConfigError(
                f"Expected JSON object, got {type(raw).__name__}"
            )
        allowed = {"action", "count_per_cycle", "retry_interval", "label", "note"}
        extra_keys = set(raw.keys()) - allowed
        if extra_keys:
            raise RunnerConfigError(
                f"Unexpected keys in runner_config: {sorted(extra_keys)!r}"
            )

        action = raw.get("action", "generate")
        if not isinstance(action, str):
            raise RunnerConfigError(
                f"action must be string, got {type(action).__name__}"
            )
        if action not in _VALID_ACTIONS:
            raise RunnerConfigError(
                f"action must be one of {_VALID_ACTIONS}, got {action!r}"
            )

        count = raw.get("count_per_cycle", None)
        if count is not None:
            if isinstance(count, bool) or not isinstance(count, int):
                raise RunnerConfigError(
                    f"count_per_cycle must be int or null, got "
                    f"{type(count).__name__}"
                )
            if count <= 0:
                raise RunnerConfigError(
                    f"count_per_cycle must be > 0 when set, got {count}"
                )

        retry = raw.get("retry_interval", None)
        if retry is not None:
            if isinstance(retry, bool) or not isinstance(retry, int):
                raise RunnerConfigError(
                    f"retry_interval must be int or null, got "
                    f"{type(retry).__name__}"
                )
            if retry < _RETRY_INTERVAL_MIN:
                raise RunnerConfigError(
                    f"retry_interval must be >= {_RETRY_INTERVAL_MIN} when set, "
                    f"got {retry}"
                )

        label = raw.get("label", None)
        if label is not None and not isinstance(label, str):
            raise RunnerConfigError(
                f"label must be string or null, got {type(label).__name__}"
            )
        if isinstance(label, str) and len(label) > 200:
            raise RunnerConfigError(
                f"label too long ({len(label)} chars, max 200)"
            )

        note = raw.get("note", None)
        if note is not None and not isinstance(note, str):
            raise RunnerConfigError(
                f"note must be string or null, got {type(note).__name__}"
            )
        if isinstance(note, str) and len(note) > 1000:
            raise RunnerConfigError(
                f"note too long ({len(note)} chars, max 1000)"
            )

        # Normalize empty string → None để UI/runner xử lý nhất quán.
        return cls(
            action=action,
            count_per_cycle=count,
            retry_interval=retry,
            label=label if label else None,
            note=note if note else None,
        )


RunnerConfig.DEFAULT = RunnerConfig()


class RunnerConfigStore:
    """File-backed JSON store cho RunnerConfig.

    Path: ``<runtime_dir>/icloud/runner_config.json``.

    load() chiến lược fail-soft:
        - File không tồn tại → return DEFAULT (KHÔNG tạo file rỗng).
        - File parse fail (corrupt JSON / schema sai) → raise
          RunnerConfigError; caller xử lý.

    save() ghi atomic: write tmp → fsync → rename. Đảm bảo crash giữa chừng
    không để lại file rỗng / half-written gây lỗi load().
    """

    FILENAME = "runner_config.json"

    def __init__(self, runtime_dir: Path | str) -> None:
        runtime_dir = Path(runtime_dir)
        self._dir = runtime_dir / "icloud"
        self._path = self._dir / self.FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> RunnerConfig:
        """Đọc + validate. Raise RunnerConfigError nếu sai schema.

        File chưa tồn tại → trả DEFAULT.
        """
        if not self._path.exists():
            return RunnerConfig.default()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RunnerConfigError(
                f"runner_config.json corrupt: {exc.msg} (line {exc.lineno})"
            ) from exc
        except OSError as exc:
            raise RunnerConfigError(
                f"runner_config.json read error: {exc}"
            ) from exc
        return RunnerConfig.from_dict(raw)

    def load_or_default(self) -> tuple[RunnerConfig, Optional[str]]:
        """Variant không raise — trả (config, error_msg).

        Dùng ở init server: nếu file corrupt thì log warn + fallback default
        thay vì kill cả Web_API. error_msg=None khi load OK.
        """
        try:
            return self.load(), None
        except RunnerConfigError as exc:
            return RunnerConfig.default(), str(exc)

    def save(self, config: RunnerConfig) -> None:
        """Atomic write JSON ra file."""
        self._dir.mkdir(parents=True, exist_ok=True)
        # tempfile + os.replace = atomic trên cùng filesystem.
        fd, tmp_path = tempfile.mkstemp(
            prefix=".runner_config_", suffix=".tmp", dir=str(self._dir)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump(
                    config.to_dict(),
                    fp,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp_path, self._path)
        except Exception:
            # Cleanup tmp khi rename fail.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


__all__ = [
    "RunnerConfig",
    "RunnerConfigError",
    "RunnerConfigStore",
]
