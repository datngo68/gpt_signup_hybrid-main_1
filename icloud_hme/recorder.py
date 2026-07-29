"""Recorder — ghi Playwright action log + HAR cho discovery (R1).

Refs:
    requirements.md R1.1–R1.9, R12.16
    design.md §Components / 8. Recorder
    tasks.md task 19

Class ``Recorder`` orchestrate:
    1. ``start_session(apple_id, scenario)``:
       - Cleanup recording cũ > retention_days (R1.8).
       - Acquire ``Profile_Lock.write_lock(timeout=30)`` (R12.16).
       - Launch Camoufox HEADED với profile_dir.
       - Bật context.tracing + new_page với record_har.
       - Inject script ghi DOM event vào ``actions.jsonl``.
       - Redact value field name ∈ {password, code, otp, secret} (R1.4).
       - Audit ``recording_start``.
    2. ``stop_session(session_id, exit_reason)``:
       - Stop tracing + flush HAR.
       - Ghi ``metadata.json``.
       - Audit ``recording_stop``.

Dependency injection cho test:
    - ``camoufox_launcher_fn`` để mock Camoufox launch (test không cần real
      browser).

Lưu ý: file này chỉ implement skeleton + redaction logic + metadata IO;
real Camoufox interaction (tracing, HAR, page event listener) cần Playwright
async API và chỉ exercise được ở integration test manual.
"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .exceptions import IcloudError, ProfileLockError
from .models import RecordingSession
from .profile_lock import ProfileLock
from .session import ensure_profile_dir

if TYPE_CHECKING:
    from db.repositories import AuditLogRepository


# Field names cần redact value khi ghi vào actions.jsonl (R1.4).
REDACT_FIELDS: tuple[str, ...] = ("password", "code", "otp", "secret")
REDACTED_VALUE = "<redacted>"

# Profile_Lock write timeout (R12.16) giống Bootstrap_Flow.
_LOCK_TIMEOUT_SEC = 30.0


class RecorderError(IcloudError):
    """Recorder-specific failure (recorder_profile_locked, IO error, ...)."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _format_ts(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _redact_input_event(event: dict) -> dict:
    """Redact value của input event nếu field name ∈ REDACT_FIELDS.

    Args:
        event: dict raw từ DOM listener — keys typically:
            - event_type: 'input' | 'click' | 'keydown' | ...
            - target_selector: CSS selector
            - target_name: tên field (input name attribute)
            - value: raw value string (hoặc None)
            - url: page URL hiện tại
            - timestamp_iso: ISO 8601 UTC.

    Returns:
        Bản sao dict với ``value`` được redact nếu cần.
    """
    out = dict(event)
    name = (event.get("target_name") or "").strip().lower()
    if name in REDACT_FIELDS:
        out["value"] = REDACTED_VALUE
        out["redacted"] = True
    return out


def _append_action_log(log_path: Path, event: dict) -> None:
    """Append 1 event vào actions.jsonl (R1.3, atomic per-line)."""
    redacted = _redact_input_event(event)
    line = json.dumps(redacted, ensure_ascii=False, separators=(",", ":"))
    with log_path.open("a", encoding="utf-8") as fp:
        fp.write(line + "\n")


class Recorder:
    """Camoufox + Playwright tracing recorder per Apple_ID.

    Args:
        runtime_dir: Root runtime dir (recording đặt trong
            ``runtime_dir/icloud_recordings/<session_id>/``).
        audit_repo: Audit ghi event ``recording_start`` / ``recording_stop``.
        retention_days: Xóa session cũ hơn N ngày khi start_session
            (R1.8). None → giữ vô hạn.
        log: optional logger callable.
        camoufox_launcher_fn: inject để test mock; default = real
            ``launch_camoufox`` từ session.py.
    """

    def __init__(
        self,
        runtime_dir: Path,
        audit_repo: "AuditLogRepository",
        *,
        retention_days: int | None = None,
        log: Any = None,
        camoufox_launcher_fn: Callable | None = None,
    ) -> None:
        self._runtime_dir = Path(runtime_dir)
        self._audit_repo = audit_repo
        self._retention_days = retention_days
        self._log = log if callable(log) else (lambda *_a, **_k: None)
        self._camoufox_launcher = camoufox_launcher_fn  # None → import lazy
        # Sessions in-memory: session_id → state dict
        # Chứa context, lock_ctx, started_at, etc. để stop_session tham chiếu.
        self._sessions: dict[str, dict] = {}

    @property
    def recordings_dir(self) -> Path:
        """Top-level dir chứa mọi recording session."""
        return self._runtime_dir / "icloud_recordings"

    # =====================================================================
    # Public API
    # =====================================================================

    async def start_session(
        self,
        apple_id: str,
        *,
        scenario: str,
    ) -> RecordingSession:
        """Bắt đầu 1 recording session (R1.1, R1.2, R1.9).

        Returns ``RecordingSession`` immutable snapshot.
        """
        # Cleanup retention (R1.8) trước khi tạo session mới
        self._cleanup_retention()

        session_id = uuid.uuid4().hex
        recording_dir = self.recordings_dir / session_id
        recording_dir.mkdir(parents=True, exist_ok=True)

        # profile_dir resolution + lock
        profile_dir = ensure_profile_dir(self._runtime_dir, apple_id)
        lock_dir = profile_dir / ".lock"
        profile_lock = ProfileLock(lock_dir, apple_id)

        try:
            lock_ctx = profile_lock.write_lock(timeout=_LOCK_TIMEOUT_SEC)
            lock_ctx.__enter__()
        except ProfileLockError as exc:
            self._log(f"recorder lock conflict: {exc}")
            raise RecorderError(
                f"recorder_profile_locked apple_id={apple_id}"
            ) from exc

        started_at = _utc_now()
        session_state = {
            "session_id": session_id,
            "apple_id": apple_id,
            "scenario": scenario,
            "recording_dir": recording_dir,
            "started_at": started_at,
            "lock_ctx": lock_ctx,
            "browser_ctx": None,  # set bởi launcher
            "actions_log": recording_dir / "actions.jsonl",
        }

        # Launch Camoufox + setup tracing/HAR.
        # FAIL-FAST khi `camoufox_launcher_fn` chưa inject (A13 fix —
        # trước đây skip silently → user gọi `recording start` thấy session
        # OK nhưng KHÔNG có browser/HAR/actions.jsonl). Caller (CLI/Web)
        # phải tự cung cấp launcher real; test pass mock. Pattern tránh
        # advertise feature không hoạt động.
        if self._camoufox_launcher is None:
            try:
                lock_ctx.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            try:
                shutil.rmtree(recording_dir)
            except Exception:  # noqa: BLE001
                pass
            raise RecorderError(
                f"camoufox_launcher_fn chưa inject — Recorder skeleton "
                f"chưa wire Camoufox real (A13). CLI/Web caller phải "
                f"truyền launcher khi khởi tạo Recorder. apple_id={apple_id}"
            )

        try:
            camoufox_state = await self._camoufox_launcher(
                profile_dir=profile_dir,
                recording_dir=recording_dir,
                on_event=self._on_dom_event_factory(session_state),
            )
            session_state["browser_ctx"] = camoufox_state
        except Exception as exc:
            # Cleanup lock trước khi re-raise
            try:
                lock_ctx.__exit__(None, None, None)
            except Exception:
                pass
            raise RecorderError(
                f"camoufox_launch_failed apple_id={apple_id}: {exc}"
            ) from exc

        self._sessions[session_id] = session_state

        # Audit recording_start
        self._audit_repo.write(
            event_type="recording_start",
            apple_id=apple_id,
            payload={
                "session_id": session_id,
                "scenario": scenario,
                "started_at": _format_ts(started_at),
            },
        )

        return RecordingSession(
            session_id=session_id,
            apple_id=apple_id,
            scenario=scenario,
            recording_dir=recording_dir,
            started_at=started_at,
            ended_at=None,
            exit_reason=None,
        )

    async def stop_session(
        self,
        session_id: str,
        *,
        exit_reason: str = "normal",
    ) -> RecordingSession:
        """Stop session: flush tracing + HAR + metadata.json + audit.

        Args:
            session_id: ID đã trả từ ``start_session``.
            exit_reason: 'normal' | 'crashed' | 'interrupted' (R1.6).
        """
        state = self._sessions.get(session_id)
        if state is None:
            raise RecorderError(f"session_id không tồn tại: {session_id}")

        ended_at = _utc_now()

        # Stop Camoufox tracing + HAR (best-effort, vẫn flush logs nếu fail)
        ctx_data = state.get("browser_ctx")
        if ctx_data is not None and isinstance(ctx_data, dict):
            stop_fn = ctx_data.get("stop_fn")
            if callable(stop_fn):
                try:
                    await stop_fn()
                except Exception as exc:
                    self._log(f"camoufox stop error: {exc}")

        # Ghi metadata.json (R1.5)
        recording_dir: Path = state["recording_dir"]
        metadata = {
            "session_id": session_id,
            "apple_id": state["apple_id"],
            "scenario": state["scenario"],
            "started_at": _format_ts(state["started_at"]),
            "ended_at": _format_ts(ended_at),
            "exit_reason": exit_reason,
        }
        try:
            (recording_dir / "metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            self._log(f"metadata.json write error: {exc}")

        # Release Profile_Lock
        lock_ctx = state.get("lock_ctx")
        if lock_ctx is not None:
            try:
                lock_ctx.__exit__(None, None, None)
            except Exception:
                pass

        # Audit recording_stop
        self._audit_repo.write(
            event_type="recording_stop",
            apple_id=state["apple_id"],
            payload={
                "session_id": session_id,
                "ended_at": _format_ts(ended_at),
                "exit_reason": exit_reason,
            },
        )

        del self._sessions[session_id]

        return RecordingSession(
            session_id=session_id,
            apple_id=state["apple_id"],
            scenario=state["scenario"],
            recording_dir=recording_dir,
            started_at=state["started_at"],
            ended_at=ended_at,
            exit_reason=exit_reason,
        )

    # =====================================================================
    # Helpers
    # =====================================================================

    def _on_dom_event_factory(self, session_state: dict) -> Callable[[dict], None]:
        """Factory trả callback ghi 1 DOM event vào actions.jsonl.

        Camoufox launcher inject callback này vào page listener — mỗi event
        từ JS ghi dict {event_type, target_selector, target_name, value, url}
        gọi callback. Callback redact + append.
        """
        log_path: Path = session_state["actions_log"]

        def _on_event(event: dict) -> None:
            event_with_meta = dict(event)
            event_with_meta.setdefault(
                "timestamp_iso", _format_ts(_utc_now())
            )
            try:
                _append_action_log(log_path, event_with_meta)
            except Exception as exc:
                self._log(f"actions.jsonl append error: {exc}")

        return _on_event

    def _cleanup_retention(self) -> int:
        """Xóa session_dir cũ hơn ``retention_days``. Return số dir đã xóa.

        ``retention_days`` None → no-op (giữ vô hạn theo R1.8).

        2 nhóm xoá:
            1. Dir có ``metadata.json`` + ``started_at < cutoff`` → expired
               session bình thường, xoá.
            2. Dir KHÔNG có ``metadata.json`` (orphan) + filesystem mtime
               cũ hơn ``2 * retention_days`` → recorder crash giữa
               start_session và stop_session, không stop được kịp ghi
               metadata. Phải xoá để tránh disk leak (A14 fix). Window 2x
               để chắc chắn không xoá session đang record dở.
        """
        if self._retention_days is None:
            return 0
        recordings_dir = self.recordings_dir
        if not recordings_dir.exists():
            return 0
        now = _utc_now()
        cutoff = now - timedelta(days=self._retention_days)
        orphan_cutoff_ts = (
            now - timedelta(days=self._retention_days * 2)
        ).timestamp()
        removed = 0
        for entry in recordings_dir.iterdir():
            if not entry.is_dir():
                continue
            metadata_path = entry / "metadata.json"
            if not metadata_path.exists():
                # No metadata → orphan: chỉ xoá khi mtime quá cũ.
                try:
                    if entry.stat().st_mtime >= orphan_cutoff_ts:
                        continue
                    shutil.rmtree(entry)
                    removed += 1
                    self._log(f"cleanup orphan recording {entry.name}")
                except Exception as exc:  # noqa: BLE001
                    self._log(f"cleanup orphan skip {entry}: {exc}")
                continue
            try:
                meta = json.loads(metadata_path.read_text(encoding="utf-8"))
                started_iso = meta.get("started_at")
                if not started_iso:
                    continue
                started = _parse_iso(started_iso)
                if started is None or started >= cutoff:
                    continue
            except Exception:
                continue
            try:
                shutil.rmtree(entry)
                removed += 1
            except Exception as exc:
                self._log(f"cleanup retention skip {entry}: {exc}")
        return removed


def _parse_iso(value: str) -> datetime | None:
    raw = value
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


__all__ = [
    "RecorderError",
    "Recorder",
    "REDACT_FIELDS",
    "REDACTED_VALUE",
]
