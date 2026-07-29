"""Open_Profile_Flow — server-side state machine cho web `Open` button + CLI `profile open` (R15).

Refs:
    requirements.md R15
    design.md §17 OpenProfileService

Mục đích: mở 1 profile EXISTING bằng Camoufox HEADED để user
    (a) check trạng thái session bằng mắt, hoặc
    (b) tự đăng nhập lại khi `session_expired`
mà không cần mở terminal chạy `bootstrap`. Hai nhánh kết thúc:
    - Save: verify cookies + reset status='active' + audit `profile_reopen_save`
      (+ `profile_reactivate` nếu previous_status ∈ REACTIVATE_STATUSES).
    - Close: đóng browser, KHÔNG sửa DB, audit `profile_reopen_close`.

Khác Add_Profile_Flow (R14):
    1. profile_dir THẬT (không tạm) — dùng `runtime/icloud_profiles/<apple_id>/`.
    2. apple_id biết trước (input từ row UI) — không extract từ cookies.
    3. Acquire `Profile_Lock` write mode (R12.14 pattern) — chống Bootstrap/Recorder/Open khác.
    4. Cancel = Close — KHÔNG xoá profile_dir, KHÔNG sửa DB.

Khác Bootstrap_Flow (R12):
    1. Có 2 nhánh terminal: Save (verify+reactivate) hoặc Close (không đổi DB).
    2. Web mode async + non-blocking (CLI mode blocking giống Bootstrap).
    3. Watchdog timeout tự transition `open → closed` thay vì raise.

Single-instance invariant (R15.4): tại mọi thời điểm chỉ ≤ 1 session ở state non-terminal
∈ {OPENING, OPEN, SAVING, CLOSING}.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .exceptions import OpenProfileError, ProfileLockError
from .profile_lock import ProfileLock
from .session import launch_camoufox

if TYPE_CHECKING:  # pragma: no cover
    from db.repositories import AuditLogRepository, IcloudPoolRepository


# ─── Constants ─────────────────────────────────────────────────────────────

# Cookie markers chứng minh login + 2FA xong (R15.6). Cần ÍT NHẤT 1 marker —
# khớp tập Bootstrap_Flow R12.2 (`_LOGIN_COOKIE_MARKERS`).
_LOGIN_COOKIE_MARKERS: tuple[str, ...] = (
    "X-APPLE-WEBAUTH-USER",
    "X-APPLE-WEBAUTH-TOKEN",
    "X-APPLE-WEBAUTH-PCS-Mail",
)

# Status mà Save SHALL audit thêm `profile_reactivate` (R15.6 + R12.10 pattern).
# 'active' và 'deleted' KHÔNG thuộc set này — 'active' chỉ refresh, 'deleted' bị
# chặn từ R15.2 nên không đến được _persist_save.
_REACTIVATE_STATUSES: frozenset[str] = frozenset(
    {"session_expired", "disabled", "limited", "quota_full"}
)

# Status block không cho `start()` (R15.2) — profile đã xoá/null không mở được.
_BLOCKED_STATUSES: frozenset[str] = frozenset({"deleted"})

# Profile_Lock write timeout (R15.3). Ngắn vì lock giữ lâu = user chờ — báo conflict
# sớm để user retry.
_LOCK_TIMEOUT_SEC: float = 5.0


def _utc_now() -> datetime:
    """UTC naive datetime — convention dùng chung với add_profile.py / bootstrap.py."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _format_iso(value: datetime) -> str:
    """ISO 8601 UTC + suffix Z — Timestamp_Format (P30)."""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ─── State machine ─────────────────────────────────────────────────────────


class OpenProfileState(str, Enum):
    """7 state cho Open_Profile_Session (R15, design §17)."""

    OPENING = "opening"   # Acquire lock + launch Camoufox
    OPEN = "open"         # Camoufox đã mở, chờ user thao tác
    SAVING = "saving"     # User bấm Lưu, đang verify cookies + persist
    CLOSING = "closing"   # User bấm Đóng / watchdog timeout, đang cleanup
    SAVED = "saved"       # Terminal — DB đã update
    CLOSED = "closed"     # Terminal — đã đóng browser, DB không đổi
    FAILED = "failed"     # Terminal — lock conflict / unexpected


_TERMINAL_STATES: frozenset[OpenProfileState] = frozenset(
    {OpenProfileState.SAVED, OpenProfileState.CLOSED, OpenProfileState.FAILED}
)
_ACTIVE_STATES: frozenset[OpenProfileState] = frozenset(
    {
        OpenProfileState.OPENING,
        OpenProfileState.OPEN,
        OpenProfileState.SAVING,
        OpenProfileState.CLOSING,
    }
)


@dataclass
class OpenProfileSession:
    """In-memory state cho 1 lượt open profile.

    Public field serialize qua API; field bắt đầu `_` là handle internal —
    KHÔNG serialize.
    """

    session_id: str
    apple_id: str
    state: OpenProfileState
    profile_dir: Path                       # THẬT, không tạm
    started_at: datetime
    ended_at: datetime | None = None
    matched_cookies: list[str] = field(default_factory=list)
    previous_status: str | None = None      # Status DB lúc start, để audit decision
    error: str | None = None
    error_reason: str | None = None
    # Internal — không xuất qua API
    _camoufox_ctx_mgr: Any | None = field(default=None, repr=False)
    _camoufox_ctx: Any | None = field(default=None, repr=False)
    _watchdog_task: asyncio.Task | None = field(default=None, repr=False)
    _profile_lock_ctx: Any | None = field(default=None, repr=False)


# ─── Service ───────────────────────────────────────────────────────────────


class OpenProfileService:
    """State machine + Camoufox lifecycle + Profile_Lock cho Open_Profile_Flow.

    Single-instance per process (R15.4). Web router lazy init.

    Args:
        runtime_dir: Runtime root — `runtime/icloud_profiles/<apple_id>/` là profile_dir thật.
        pool_repo: ``IcloudPoolRepository`` để get account + upsert + update_status.
        audit_repo: ``AuditLogRepository`` cho 5 event mới.
        timeout_sec: Hard cap watchdog timeout (R15.9, R15.16). Default 1800s.
        log: Logger callable hoặc None.
    """

    def __init__(
        self,
        runtime_dir: Path,
        pool_repo: "IcloudPoolRepository",
        audit_repo: "AuditLogRepository",
        *,
        timeout_sec: int = 1800,
        log: Callable[[str], None] | None = None,
    ) -> None:
        if timeout_sec <= 0:
            raise ValueError(f"timeout_sec must be > 0 (got {timeout_sec!r})")
        self._runtime_dir = runtime_dir
        self._pool_repo = pool_repo
        self._audit_repo = audit_repo
        self._timeout_sec = timeout_sec
        self._log = log or (lambda _msg: None)
        self._lock = asyncio.Lock()
        self._active: OpenProfileSession | None = None
        # FIFO 32 entries — giải race UI poll status sau terminal (cùng pattern
        # AddProfileService R14.9).
        self._terminal_cache: dict[str, OpenProfileSession] = {}
        self._terminal_cache_max = 32

    # ── Public API ─────────────────────────────────────────────────────────

    def has_active_session(self) -> bool:
        """Có session non-terminal không (R15.4)."""
        return self._active is not None and self._active.state in _ACTIVE_STATES

    async def start(self, apple_id: str) -> OpenProfileSession:
        """Validate apple_id + acquire write lock + launch Camoufox HEADED + spawn watchdog (R15.1).

        Raises:
            OpenProfileError(reason='profile_not_found'): apple_id missing/deleted/null.
            OpenProfileError(reason='profile_locked'): Profile_Lock conflict.
            OpenProfileError(reason='open_profile_in_progress'): đã có session khác.
        """
        # Validate apple_id non-empty + có row trong DB.
        apple_id_norm = (apple_id or "").strip().lower()
        if not apple_id_norm or "@" not in apple_id_norm:
            raise OpenProfileError(
                reason="profile_not_found",
                message=f"apple_id không hợp lệ: {apple_id!r}",
                apple_id=apple_id,
            )

        account = self._pool_repo.get(apple_id_norm)
        if account is None:
            raise OpenProfileError(
                reason="profile_not_found",
                message=f"apple_id không tồn tại trong pool: {apple_id_norm}",
                apple_id=apple_id_norm,
            )
        if account.status in _BLOCKED_STATUSES or account.profile_dir is None:
            raise OpenProfileError(
                reason="profile_not_found",
                message=(
                    f"Profile {apple_id_norm} đã xoá hoặc profile_dir null. "
                    f"Dùng + Add Profile để thêm mới."
                ),
                apple_id=apple_id_norm,
            )
        profile_dir = Path(account.profile_dir)
        if not profile_dir.exists():
            raise OpenProfileError(
                reason="profile_not_found",
                message=f"profile_dir không tồn tại trên disk: {profile_dir}",
                apple_id=apple_id_norm,
            )

        # Single-instance check (R15.4) under async lock.
        async with self._lock:
            if self.has_active_session():
                assert self._active is not None
                raise OpenProfileError(
                    reason="open_profile_in_progress",
                    message=(
                        f"Đã có session Open_Profile khác đang chạy "
                        f"(session_id={self._active.session_id}, "
                        f"apple_id={self._active.apple_id}, "
                        f"state={self._active.state.value})"
                    ),
                    session_id=self._active.session_id,
                    apple_id=self._active.apple_id,
                )

            # Tạo session in OPENING state. Lock + Camoufox launch ngoài async lock.
            session_id = uuid.uuid4().hex
            session = OpenProfileSession(
                session_id=session_id,
                apple_id=apple_id_norm,
                state=OpenProfileState.OPENING,
                profile_dir=profile_dir,
                started_at=_utc_now(),
                previous_status=account.status,
            )
            self._active = session

        # Acquire Profile_Lock write mode TRƯỚC KHI launch Camoufox (R15.11).
        lock_dir = profile_dir / ".lock"
        profile_lock = ProfileLock(lock_dir, apple_id_norm)
        try:
            lock_ctx = profile_lock.write_lock(timeout=_LOCK_TIMEOUT_SEC)
            lock_ctx.__enter__()
        except ProfileLockError as exc:
            # Audit fail + clear active.
            self._audit_repo.write(
                event_type="profile_reopen_fail",
                apple_id=apple_id_norm,
                payload={
                    "session_id": session_id,
                    "reason": "profile_locked",
                    "lock_mode": exc.mode,
                    "error": str(exc),
                },
            )
            session.state = OpenProfileState.FAILED
            session.ended_at = _utc_now()
            session.error_reason = "profile_locked"
            session.error = str(exc)
            async with self._lock:
                self._remember_terminal(session)
                if self._active is session:
                    self._active = None
            raise OpenProfileError(
                reason="profile_locked",
                message=(
                    f"Profile {apple_id_norm} đang được dùng bởi flow khác "
                    f"(bootstrap / recorder / open). Đợi flow đó hoàn tất."
                ),
                session_id=session_id,
                apple_id=apple_id_norm,
            ) from exc
        session._profile_lock_ctx = lock_ctx

        # Launch Camoufox HEADED.
        try:
            await self._launch_camoufox(session)
        except Exception as exc:
            self._log(f"launch_camoufox fail session_id={session_id}: {exc!r}")
            await self._fail(session, reason="unexpected", error=repr(exc))
            raise OpenProfileError(
                reason="unexpected",
                message=f"Camoufox launch failed: {exc}",
                session_id=session_id,
                apple_id=apple_id_norm,
            ) from exc

        # Audit + watchdog SHALL chỉ chạy sau khi Camoufox launch OK.
        self._audit_repo.write(
            event_type="profile_reopen_start",
            apple_id=apple_id_norm,
            payload={
                "session_id": session_id,
                "profile_dir": str(profile_dir),
                "previous_status": session.previous_status,
                "started_at": _format_iso(session.started_at),
            },
        )
        session.state = OpenProfileState.OPEN
        session._watchdog_task = asyncio.create_task(self._watchdog(session))
        self._log(
            f"session started session_id={session_id} apple_id={apple_id_norm} "
            f"previous_status={session.previous_status} timeout={self._timeout_sec}s"
        )
        return session

    async def save(self, session_id: str) -> OpenProfileSession:
        """Verify cookies + close Camoufox + persist DB (R15.6).

        Recoverable failure (R15.7): cookies marker thiếu → revert SAVING → OPEN,
        giữ browser, audit fail recoverable, raise. UI dialog giữ mở để user retry.
        """
        session = await self._begin_save(session_id)

        # Bước 1: verify cookies (recoverable nếu fail).
        try:
            matched = await self._verify_cookies(session)
        except OpenProfileError as exc:
            if exc.reason == "cookies_not_ready":
                # Recoverable: revert state về OPEN, giữ browser mở.
                session.state = OpenProfileState.OPEN
                session.error = str(exc)
                session.error_reason = exc.reason
                self._audit_repo.write(
                    event_type="profile_reopen_fail",
                    apple_id=session.apple_id,
                    payload={
                        "session_id": session.session_id,
                        "reason": exc.reason,
                        "error": str(exc),
                        "recoverable": True,
                    },
                )
                self._log(
                    f"session save recoverable fail "
                    f"session_id={session_id} reason={exc.reason} → state→OPEN"
                )
                raise
            # Terminal — Camoufox context không sẵn sàng (browser bị kill ngoài tool).
            await self._fail(session, reason=exc.reason, error=str(exc))
            raise

        # Bước 2: commit (terminal — verify đã OK, mọi fail từ đây phải đóng browser).
        try:
            await self._close_camoufox(session, force=False)
            self._persist_save(session, matched)
        except OpenProfileError as exc:
            await self._fail(session, reason=exc.reason, error=str(exc))
            raise
        except Exception as exc:  # noqa: BLE001
            await self._fail(session, reason="unexpected", error=repr(exc))
            raise OpenProfileError(
                reason="unexpected",
                message=f"save failed: {exc}",
                session_id=session_id,
                apple_id=session.apple_id,
            ) from exc

        # Success path
        session.state = OpenProfileState.SAVED
        session.ended_at = _utc_now()
        session.matched_cookies = list(matched)
        await self._release_lock(session)
        self._cancel_watchdog(session)
        async with self._lock:
            self._remember_terminal(session)
            if self._active is session:
                self._active = None
        duration_sec = (session.ended_at - session.started_at).total_seconds()
        self._log(
            f"session saved session_id={session_id} apple_id={session.apple_id} "
            f"matched={sorted(matched)} previous_status={session.previous_status} "
            f"duration={duration_sec:.1f}s"
        )
        return session

    async def close(self, session_id: str) -> OpenProfileSession:
        """User bấm Đóng — đóng Camoufox NGAY, KHÔNG sửa DB, KHÔNG xoá profile_dir (R15.8).

        Idempotent: gọi `close` từ state đã terminal → return state hiện tại,
        không raise.
        """
        async with self._lock:
            session = self._require_active(session_id)
            if session.state in _TERMINAL_STATES:
                return session
            session.state = OpenProfileState.CLOSING

        await self._close_camoufox(session, force=True)
        # Cố ý KHÔNG đụng DB, KHÔNG xoá profile_dir (đây là profile thật) (R15.8).
        session.state = OpenProfileState.CLOSED
        session.ended_at = _utc_now()
        duration_sec = (session.ended_at - session.started_at).total_seconds()
        self._audit_repo.write(
            event_type="profile_reopen_close",
            apple_id=session.apple_id,
            payload={
                "session_id": session.session_id,
                "duration_seconds": duration_sec,
                "reason": "user_close",
            },
        )
        await self._release_lock(session)
        self._cancel_watchdog(session)
        async with self._lock:
            self._remember_terminal(session)
            if self._active is session:
                self._active = None
        self._log(
            f"session closed session_id={session_id} apple_id={session.apple_id} "
            f"duration={duration_sec:.1f}s"
        )
        return session

    def status(self, session_id: str) -> OpenProfileSession:
        """Trả session state hiện tại — non-blocking (R15.10).

        Race semantics: terminal cache (FIFO 32) giữ session đã terminal cho UI
        poll thêm vài round sau khi save/close/fail (cùng pattern Add_Profile R14.9).
        Order: active match → terminal cache → 404.

        Raises:
            OpenProfileError(reason='session_not_found'): session_id không khớp
                active lẫn cache.
        """
        if self._active is not None and self._active.session_id == session_id:
            return self._active
        cached = self._terminal_cache.get(session_id)
        if cached is not None:
            return cached
        raise OpenProfileError(
            reason="session_not_found",
            message=f"Session {session_id} không tồn tại hoặc đã evict khỏi cache",
            session_id=session_id,
        )

    # ── Internal helpers ───────────────────────────────────────────────────

    def _remember_terminal(self, session: OpenProfileSession) -> None:
        """FIFO 32 entries — evict oldest khi full."""
        if len(self._terminal_cache) >= self._terminal_cache_max:
            try:
                oldest_key = next(iter(self._terminal_cache))
                del self._terminal_cache[oldest_key]
            except StopIteration:
                pass
        self._terminal_cache[session.session_id] = session

    def _require_active(self, session_id: str) -> OpenProfileSession:
        """Trả `_active` nếu session_id khớp; raise nếu không."""
        if self._active is None or self._active.session_id != session_id:
            cached = self._terminal_cache.get(session_id)
            if cached is not None:
                # Terminal cache — caller (close) sẽ idempotent return.
                return cached
            raise OpenProfileError(
                reason="session_not_found",
                message=f"Session {session_id} không tồn tại",
                session_id=session_id,
            )
        return self._active

    async def _begin_save(self, session_id: str) -> OpenProfileSession:
        """Validate state OPEN + transition → SAVING dưới async lock."""
        async with self._lock:
            session = self._require_active(session_id)
            if session.state is not OpenProfileState.OPEN:
                raise OpenProfileError(
                    reason="invalid_state",
                    message=(
                        f"Session {session_id} state={session.state.value}, "
                        f"chỉ có thể save khi state=open"
                    ),
                    session_id=session_id,
                    apple_id=session.apple_id,
                )
            session.state = OpenProfileState.SAVING
            return session

    async def _launch_camoufox(self, session: OpenProfileSession) -> None:
        """Launch Camoufox HEADED + navigate `/mail/` (R15.1)."""
        ctx_mgr = launch_camoufox(
            profile_dir=session.profile_dir,
            headless=False,
            proxy=None,
        )
        ctx = await ctx_mgr.__aenter__()
        session._camoufox_ctx_mgr = ctx_mgr
        session._camoufox_ctx = ctx
        # Navigate best-effort — user vẫn thao tác được nếu network chậm.
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(
                "https://www.icloud.com/mail/",
                wait_until="domcontentloaded",
            )
        except Exception as exc:  # noqa: BLE001
            self._log(f"navigate warning session_id={session.session_id}: {exc!r}")

    async def _close_camoufox(
        self, session: OpenProfileSession, *, force: bool
    ) -> None:
        """Đóng Camoufox. force=True → terminate ngay. Best-effort, không raise."""
        ctx_mgr = session._camoufox_ctx_mgr
        if ctx_mgr is None:
            return
        session._camoufox_ctx_mgr = None
        session._camoufox_ctx = None
        try:
            await ctx_mgr.__aexit__(None, None, None)
        except Exception as exc:  # noqa: BLE001
            if force:
                self._log(
                    f"close_camoufox force=True ignored exc "
                    f"session_id={session.session_id}: {exc!r}"
                )
            else:
                self._log(
                    f"close_camoufox warning session_id={session.session_id}: {exc!r}"
                )

    async def _verify_cookies(
        self, session: OpenProfileSession
    ) -> list[str]:
        """Verify ÍT NHẤT 1 login cookie marker đã có (R15.6, R12.2 pattern).

        Returns:
            Sorted list các marker khớp. Empty list (chưa raise) nếu không có
            cookies nào — caller raise OpenProfileError(reason='cookies_not_ready').

        Raises:
            OpenProfileError(reason='cookies_not_ready'): không match marker nào,
                hoặc Camoufox context đã đóng / đọc cookies fail.
        """
        ctx = session._camoufox_ctx
        if ctx is None:
            raise OpenProfileError(
                reason="cookies_not_ready",
                message="Camoufox context đã đóng",
                session_id=session.session_id,
                apple_id=session.apple_id,
            )
        try:
            cookies = await ctx.cookies("https://www.icloud.com/")
        except Exception as exc:  # noqa: BLE001
            raise OpenProfileError(
                reason="cookies_not_ready",
                message=f"Đọc cookies fail: {exc}",
                session_id=session.session_id,
                apple_id=session.apple_id,
            ) from exc
        names = {c.get("name") for c in cookies if c.get("name")}
        matched = sorted(names & set(_LOGIN_COOKIE_MARKERS))
        if not matched:
            raise OpenProfileError(
                reason="cookies_not_ready",
                message=(
                    f"Hoàn tất login Apple ID + 2FA trong Camoufox trước khi "
                    f"bấm Lưu. Không tìm thấy cookie login marker nào trong: "
                    f"{list(_LOGIN_COOKIE_MARKERS)}"
                ),
                session_id=session.session_id,
                apple_id=session.apple_id,
            )
        return matched

    def _persist_save(
        self, session: OpenProfileSession, matched: list[str]
    ) -> None:
        """1 outer-tx: upsert + reset status='active' + audit (R15.6, R6.3).

        Decision audit event:
            - previous_status ∈ REACTIVATE_STATUSES → audit `profile_reopen_save`
              + `profile_reactivate` (cùng pattern Bootstrap_Flow R12.10).
            - previous_status = 'active' → chỉ `profile_reopen_save`.
        """
        engine = self._pool_repo.engine
        previous_status = session.previous_status
        duration_sec = (
            (_utc_now() - session.started_at).total_seconds()
        )
        payload = {
            "session_id": session.session_id,
            "apple_id": session.apple_id,
            "matched_cookies": list(matched),
            "previous_status": previous_status,
            "duration_seconds": duration_sec,
        }
        with engine.transaction() as _conn:
            self._pool_repo.upsert(session.apple_id, session.profile_dir)
            self._pool_repo.update_status(
                session.apple_id,
                status="active",
                clear_error=True,
                clear_limited_until=True,
                clear_quota_retry_until=True,
            )
            self._audit_repo.write(
                event_type="profile_reopen_save",
                apple_id=session.apple_id,
                payload=payload,
            )
            if previous_status in _REACTIVATE_STATUSES:
                self._audit_repo.write(
                    event_type="profile_reactivate",
                    apple_id=session.apple_id,
                    payload={
                        "session_id": session.session_id,
                        "previous_status": previous_status,
                        "trigger": "open_profile_save",
                    },
                )
        self._log(
            f"persist_save apple_id={session.apple_id} "
            f"previous_status={previous_status} → active"
        )

    async def _release_lock(self, session: OpenProfileSession) -> None:
        """Release Profile_Lock write. Best-effort — log warning nếu fail (R15.11)."""
        ctx = session._profile_lock_ctx
        if ctx is None:
            return
        session._profile_lock_ctx = None
        try:
            ctx.__exit__(None, None, None)
        except Exception as exc:  # noqa: BLE001
            self._log(
                f"release_lock warning session_id={session.session_id} "
                f"apple_id={session.apple_id}: {exc!r}"
            )

    async def _fail(
        self,
        session: OpenProfileSession,
        *,
        reason: str,
        error: str,
    ) -> None:
        """Terminal failure path: đóng browser + release lock + audit + clear active.

        KHÔNG xoá profile_dir (đây là profile thật).
        Best-effort — KHÔNG raise.
        """
        await self._close_camoufox(session, force=True)
        session.state = OpenProfileState.FAILED
        session.ended_at = _utc_now()
        session.error = error
        session.error_reason = reason
        try:
            self._audit_repo.write(
                event_type="profile_reopen_fail",
                apple_id=session.apple_id,
                payload={
                    "session_id": session.session_id,
                    "reason": reason,
                    "error": error,
                },
            )
        except Exception as exc:  # noqa: BLE001
            self._log(
                f"audit profile_reopen_fail fail "
                f"session_id={session.session_id}: {exc!r}"
            )
        await self._release_lock(session)
        self._cancel_watchdog(session)
        async with self._lock:
            self._remember_terminal(session)
            if self._active is session:
                self._active = None

    async def _watchdog(self, session: OpenProfileSession) -> None:
        """Hard timeout cancel sau timeout_sec từ started_at (R15.9).

        Behavior giống `close()` — đóng browser, KHÔNG sửa DB, audit
        `profile_reopen_timeout` (KHÔNG phải `profile_reopen_close` để phân biệt
        nguồn).
        """
        try:
            await asyncio.sleep(self._timeout_sec)
        except asyncio.CancelledError:
            return
        # Nếu đã terminal hoặc đã được close, watchdog không làm gì.
        if session.state in _TERMINAL_STATES:
            return
        try:
            session.state = OpenProfileState.CLOSING
            await self._close_camoufox(session, force=True)
            session.state = OpenProfileState.CLOSED
            session.ended_at = _utc_now()
            duration_sec = (session.ended_at - session.started_at).total_seconds()
            try:
                self._audit_repo.write(
                    event_type="profile_reopen_timeout",
                    apple_id=session.apple_id,
                    payload={
                        "session_id": session.session_id,
                        "expired_after_sec": self._timeout_sec,
                        "duration_seconds": duration_sec,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                self._log(
                    f"audit profile_reopen_timeout fail "
                    f"session_id={session.session_id}: {exc!r}"
                )
            await self._release_lock(session)
            async with self._lock:
                self._remember_terminal(session)
                if self._active is session:
                    self._active = None
            self._log(
                f"watchdog timeout session_id={session.session_id} "
                f"apple_id={session.apple_id} after={self._timeout_sec}s"
            )
        except Exception as exc:  # noqa: BLE001
            self._log(
                f"watchdog exception session_id={session.session_id}: {exc!r}"
            )

    def _cancel_watchdog(self, session: OpenProfileSession) -> None:
        """Cancel watchdog task nếu đang chạy. Best-effort."""
        task = session._watchdog_task
        if task is None or task.done():
            return
        task.cancel()
        session._watchdog_task = None


__all__ = [
    "OpenProfileService",
    "OpenProfileSession",
    "OpenProfileState",
]
