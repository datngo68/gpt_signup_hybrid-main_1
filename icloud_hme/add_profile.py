"""Add_Profile_Flow — server-side state machine cho web `+ Thêm profile` (R14).

Refs:
    requirements.md R14
    design.md §16 AddProfileService
    tasks.md task 41

Flow:
    POST /add/start  → state RECORDING (Camoufox HEADED đã mở, chờ user thao tác)
    POST /add/<id>/save    → state SAVING → DONE | FAILED
    POST /add/<id>/cancel  → state CANCELLING → CANCELLED
    Watchdog timeout       → state CANCELLING → CANCELLED (audit profile_add_timeout)

Khác Bootstrap_Flow ở 3 điểm:
    1. Không yêu cầu apple_id input lúc start — extract sau khi user login xong.
    2. Profile_dir tạm cô lập tại runtime/icloud_profiles/.adding/<session_id>/,
       chỉ rename → runtime/icloud_profiles/<apple_id>/ lúc save thành công.
    3. Lifecycle in-memory; process restart = cleanup orphan, không persist
       session metadata xuống DB.

Single-instance invariant (R14.10): tại mọi thời điểm chỉ ≤ 1 session ở state
∈ {RECORDING, SAVING, CANCELLING}.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import urllib.parse
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .exceptions import AddProfileError, IcloudError
from .session import launch_camoufox

if TYPE_CHECKING:  # pragma: no cover
    from db.repositories import AuditLogRepository, IcloudPoolRepository


# ─── Constants ─────────────────────────────────────────────────────────────

# Cookie markers chứng minh login + 2FA xong (R14.5). Cần ÍT NHẤT 1 marker
# — khớp logic bootstrap.py. X-APPLE-WEBAUTH-PCS-Mail không phải account nào
# cũng có (phụ thuộc Advanced Data Protection / region / account type).
_LOGIN_COOKIE_MARKERS: tuple[str, ...] = (
    "X-APPLE-WEBAUTH-USER",
    "X-APPLE-WEBAUTH-TOKEN",
    "X-APPLE-WEBAUTH-PCS-Mail",
)

# Status profile cản trở re-add (R14.6). Status='deleted' KHÔNG cản trở — coi
# là re-add hợp lệ, ghi đè row cũ.
_BLOCKING_STATUSES: frozenset[str] = frozenset(
    {"active", "limited", "quota_full", "session_expired"}
)

# Move profile_dir tạm → final: retry khi gặp file-lock conflict (R14.11).
_MOVE_RETRY_TIMEOUT_SEC: float = 5.0
_MOVE_RETRY_INTERVAL_SEC: float = 0.5


def _utc_now() -> datetime:
    """UTC naive datetime giống convention bootstrap.py."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _format_iso(value: datetime) -> str:
    """ISO 8601 UTC + suffix Z — Timestamp_Format (P30)."""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ─── State machine ─────────────────────────────────────────────────────────


class AddProfileState(str, Enum):
    """6 state cho Add_Profile_Session (design §16, R14)."""

    RECORDING = "recording"   # Camoufox đang mở, user đang login + 2FA
    SAVING = "saving"         # User bấm Lưu, đang extract + persist
    CANCELLING = "cancelling" # User bấm Huỷ hoặc watchdog timeout, đang cleanup
    DONE = "done"             # Terminal — apple_id active trong DB
    CANCELLED = "cancelled"   # Terminal — đã xoá profile_dir tạm
    FAILED = "failed"         # Terminal — extract / persist fail


_TERMINAL_STATES: frozenset[AddProfileState] = frozenset(
    {AddProfileState.DONE, AddProfileState.CANCELLED, AddProfileState.FAILED}
)
_ACTIVE_STATES: frozenset[AddProfileState] = frozenset(
    {
        AddProfileState.RECORDING,
        AddProfileState.SAVING,
        AddProfileState.CANCELLING,
    }
)


@dataclass
class AddProfileSession:
    """In-memory state cho 1 lượt add profile.

    Chỉ field public được serialize qua API; field bắt đầu `_` là handle
    internal (Camoufox context, watchdog task) — KHÔNG serialize.
    """

    session_id: str
    state: AddProfileState
    profile_dir_temp: Path
    started_at: datetime
    ended_at: datetime | None = None
    apple_id: str | None = None
    profile_dir_final: Path | None = None
    error: str | None = None
    error_reason: str | None = None
    # Internal — không xuất qua API
    _camoufox_ctx_mgr: Any | None = field(default=None, repr=False)
    _camoufox_ctx: Any | None = field(default=None, repr=False)
    _watchdog_task: asyncio.Task | None = field(default=None, repr=False)


# ─── Service ───────────────────────────────────────────────────────────────


class AddProfileService:
    """State machine + Camoufox lifecycle cho Add_Profile_Flow.

    Single-instance per process (R14.10). Web router lazy init lúc request đầu
    tiên + cleanup_orphan_on_startup() trước khi accept request.
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
            raise ValueError(
                f"timeout_sec must be > 0 (got {timeout_sec!r})"
            )
        self._runtime_dir = runtime_dir
        self._pool_repo = pool_repo
        self._audit_repo = audit_repo
        self._timeout_sec = timeout_sec
        self._log = log or (lambda _msg: None)
        self._lock = asyncio.Lock()
        self._active: AddProfileSession | None = None
        # Cache session đã terminal — giải race giữa save/cancel hoàn tất và
        # UI poll status (R14.9): khi `_active = None`, status() vẫn trả được
        # FIFO cache để giữ session đã terminal cho UI poll thêm vài round
        # sau khi save/cancel/fail (R14.9). Race: server clear _active ngay
        # khi terminal, UI poll /status tiếp 1-2s → cache giúp tránh false
        # 'session_not_found'. 32 entry đủ rộng cho user retry liên tục
        # (1 bug-report cũ từng evict sau 8 retry).
        self._terminal_cache: dict[str, AddProfileSession] = {}
        self._terminal_cache_max = 32

    # ── Properties ─────────────────────────────────────────────────────────

    @property
    def adding_dir(self) -> Path:
        """Dir cha chứa mọi profile_dir tạm: runtime/icloud_profiles/.adding/"""
        return self._runtime_dir / "icloud_profiles" / ".adding"

    @property
    def profiles_dir(self) -> Path:
        """Dir chứa profile thật: runtime/icloud_profiles/<apple_id>/"""
        return self._runtime_dir / "icloud_profiles"

    def has_active_session(self) -> bool:
        """Có session đang ở state non-terminal hay không (R14.10)."""
        return self._active is not None and self._active.state in _ACTIVE_STATES

    # ── Public API ─────────────────────────────────────────────────────────

    async def start(self) -> AddProfileSession:
        """Tạo session mới + launch Camoufox HEADED + spawn watchdog (R14.1, R14.10).

        Raise:
            AddProfileError(reason='add_profile_in_progress'): nếu đã có session
                ở state ∈ {RECORDING, SAVING, CANCELLING}.
        """
        async with self._lock:
            if self.has_active_session():
                assert self._active is not None
                raise AddProfileError(
                    reason="add_profile_in_progress",
                    message=(
                        f"Có session Add_Profile khác đang chạy "
                        f"(session_id={self._active.session_id}, "
                        f"state={self._active.state.value})"
                    ),
                    session_id=self._active.session_id,
                )
            session_id = uuid.uuid4().hex
            profile_dir = self.adding_dir / session_id
            profile_dir.mkdir(parents=True, exist_ok=True)
            session = AddProfileSession(
                session_id=session_id,
                state=AddProfileState.RECORDING,
                profile_dir_temp=profile_dir,
                started_at=_utc_now(),
            )
            self._active = session

        # Launch Camoufox NGOÀI lock (chậm, không nên block lock).
        try:
            await self._launch_camoufox(session)
        except Exception as exc:
            self._log(f"launch_camoufox fail session_id={session_id}: {exc!r}")
            await self._fail(session, reason="unexpected", error=repr(exc))
            raise AddProfileError(
                reason="unexpected",
                message=f"Camoufox launch failed: {exc}",
                session_id=session_id,
            ) from exc

        # Audit + watchdog SHALL chỉ chạy sau khi Camoufox launch OK.
        self._audit_repo.write(
            event_type="profile_add_start",
            apple_id=None,
            payload={
                "session_id": session.session_id,
                "profile_dir": str(profile_dir),
                "started_at": _format_iso(session.started_at),
            },
        )
        session._watchdog_task = asyncio.create_task(self._watchdog(session))
        self._log(f"session started session_id={session_id} timeout={self._timeout_sec}s")
        return session

    async def save(
        self, session_id: str, *, apple_id_hint: str | None = None
    ) -> AddProfileSession:
        """Extract apple_id + verify cookies + close Camoufox + rename
        profile_dir + persist DB (R14.3).

        Args:
            session_id: ID session đang record.
            apple_id_hint: User-provided email từ UI input. Nếu set + valid
                format, SHALL ưu tiên trên auto-extract — vì user mới biết
                chính xác apple_id đã login (auto-extract qua page.evaluate
                / cookie không reliable trên iCloud webapp đa iframe). Auto-
                extract giữ làm fallback khi user không nhập.

        Raise AddProfileError với reason theo R14.4-R14.6, R14.11.

        Recoverable vs terminal failure:
            - ``apple_id_not_extractable`` / ``apple_id_mismatch`` /
              ``cookies_not_ready``: user chưa login xong, gõ nhầm Apple ID
              → KHÔNG đóng browser, KHÔNG xoá profile_dir tạm, transition
              state SAVING → RECORDING để user retry. Browser vẫn open,
              cookies vẫn còn.
            - các reason khác → terminal, transition state → FAILED + cleanup.
            - ``apple_id_already_exists`` / ``move_failed`` / ``unexpected``:
              terminal → close browser + cleanup + state FAILED.
        """
        session = await self._begin_save(session_id)

        # Bước 1: resolve apple_id (hint → fallback auto-extract) + verify
        # cookies. Recoverable nếu fail — user retry được sau khi login xong
        # hoặc nhập hint chính xác.
        try:
            apple_id = await self._resolve_apple_id(session, hint=apple_id_hint)
            await self._verify_required_cookies(session)
        except AddProfileError as exc:
            if exc.reason in (
                "apple_id_not_extractable",
                "apple_id_mismatch",
                "cookies_not_ready",
            ):
                # Recoverable: revert state về RECORDING, giữ browser mở.
                session.state = AddProfileState.RECORDING
                session.error = str(exc)
                session.error_reason = exc.reason
                self._audit_repo.write(
                    event_type="profile_add_fail",
                    apple_id=None,
                    payload={
                        "session_id": session.session_id,
                        "reason": exc.reason,
                        "error": str(exc),
                        "recoverable": True,
                    },
                )
                self._log(
                    f"session save recoverable fail "
                    f"session_id={session_id} reason={exc.reason} → state→RECORDING"
                )
                raise
            # Terminal AddProfileError (chỉ xảy ra ở Camoufox context không
            # sẵn sàng — browser bị kill ngoài tool).
            await self._fail(session, reason=exc.reason, error=str(exc))
            raise

        # Bước 2: commit (terminal — extract đã OK, mọi fail từ đây phải
        # đóng browser + cleanup vì state DB đã bắt đầu thay đổi).
        try:
            await self._close_camoufox(session, force=False)
            await self._move_profile_dir(session, apple_id)
            self._persist_account(session, apple_id)
        except AddProfileError as exc:
            await self._fail(session, reason=exc.reason, error=str(exc))
            raise
        except Exception as exc:  # noqa: BLE001 — catch tất để cleanup
            await self._fail(session, reason="unexpected", error=repr(exc))
            raise AddProfileError(
                reason="unexpected",
                message=f"save failed: {exc}",
                session_id=session_id,
            ) from exc

        # Success path
        session.state = AddProfileState.DONE
        session.ended_at = _utc_now()
        session.apple_id = apple_id
        duration_sec = (session.ended_at - session.started_at).total_seconds()
        self._audit_repo.write(
            event_type="profile_add_success",
            apple_id=apple_id,
            payload={
                "session_id": session.session_id,
                "apple_id": apple_id,
                "profile_dir_final": str(session.profile_dir_final),
                "duration_seconds": duration_sec,
            },
        )
        self._cancel_watchdog(session)
        async with self._lock:
            self._remember_terminal(session)
            self._active = None
        self._log(
            f"session saved session_id={session_id} apple_id={apple_id} "
            f"duration={duration_sec:.1f}s"
        )
        return session

    async def cancel(self, session_id: str) -> AddProfileSession:
        """User bấm Huỷ — đóng Camoufox NGAY + xoá profile_dir tạm (R14.7)."""
        async with self._lock:
            session = self._require_active(session_id)
            if session.state in _TERMINAL_STATES:
                # Idempotent: đã terminal rồi → return state hiện tại, không
                # raise.
                return session
            session.state = AddProfileState.CANCELLING

        await self._close_camoufox(session, force=True)
        self._cleanup_temp_dir(session)
        session.state = AddProfileState.CANCELLED
        session.ended_at = _utc_now()
        duration_sec = (session.ended_at - session.started_at).total_seconds()
        self._audit_repo.write(
            event_type="profile_add_cancel",
            apple_id=None,
            payload={
                "session_id": session.session_id,
                "duration_seconds": duration_sec,
                "reason": "user_cancel",
            },
        )
        self._cancel_watchdog(session)
        async with self._lock:
            self._remember_terminal(session)
            self._active = None
        self._log(
            f"session cancelled session_id={session_id} duration={duration_sec:.1f}s"
        )
        return session

    def status(self, session_id: str) -> AddProfileSession:
        """Trả session state hiện tại — non-blocking (R14.9).

        Race semantics: khi save/cancel/_fail hoàn tất, `_active` được clear
        nhưng UI vẫn poll thêm 1-2 lần. Để tránh false 'session_not_found',
        terminal session cuối cùng được lưu vào `_terminal_cache` (FIFO 32
        entry); status() đọc theo thứ tự: active match → terminal cache →
        404.

        Raise AddProfileError(reason='session_not_found') nếu session_id
        không khớp active lẫn terminal cache.
        """
        if self._active is not None and self._active.session_id == session_id:
            return self._active
        cached = self._terminal_cache.get(session_id)
        if cached is not None:
            return cached
        raise AddProfileError(
            reason="session_not_found",
            message=f"Session {session_id} không tồn tại hoặc đã evict khỏi cache",
            session_id=session_id,
        )

    def _remember_terminal(self, session: AddProfileSession) -> None:
        """Lưu session đã terminal vào cache + evict FIFO khi full.

        Gọi từ save/cancel/_fail/_watchdog ngay trước khi clear `_active`.
        """
        if len(self._terminal_cache) >= self._terminal_cache_max:
            # Evict oldest (FIFO order — Python 3.7+ dict giữ insertion order).
            try:
                oldest_key = next(iter(self._terminal_cache))
                del self._terminal_cache[oldest_key]
            except StopIteration:
                pass
        self._terminal_cache[session.session_id] = session

    def cleanup_orphan_on_startup(self) -> int:
        """Quét adding_dir, xoá orphan dir từ process trước (R14.12).

        Return:
            Số dir đã xoá.
        """
        if not self.adding_dir.exists():
            return 0
        count = 0
        for child in self.adding_dir.iterdir():
            if not child.is_dir():
                continue
            session_id = child.name
            try:
                self._audit_repo.write(
                    event_type="profile_add_fail",
                    apple_id=None,
                    payload={
                        "session_id": session_id,
                        "reason": "process_crashed",
                        "error": None,
                    },
                )
            except Exception as exc:  # noqa: BLE001 — best-effort audit
                self._log(
                    f"cleanup_orphan audit fail session_id={session_id}: {exc!r}"
                )
            shutil.rmtree(child, ignore_errors=True)
            count += 1
            self._log(f"cleanup_orphan removed session_id={session_id}")
        return count

    # ── Internal helpers ───────────────────────────────────────────────────

    async def _begin_save(self, session_id: str) -> AddProfileSession:
        """Validate state RECORDING + transition → SAVING under lock."""
        async with self._lock:
            session = self._require_active(session_id)
            if session.state is not AddProfileState.RECORDING:
                raise AddProfileError(
                    reason="invalid_state",
                    message=(
                        f"Session {session_id} state={session.state.value}, "
                        f"chỉ có thể save khi state=recording"
                    ),
                    session_id=session_id,
                )
            session.state = AddProfileState.SAVING
            return session

    async def _launch_camoufox(self, session: AddProfileSession) -> None:
        """Launch Camoufox HEADED + navigate icloud.com/mail/ (R14.1).

        Lưu context manager + ctx vào session để `_close_camoufox` xài lại.

        Navigate `/mail/` (KHÔNG phải `/`) vì:
            - `window.webAuth.dsInfo.appleId` chỉ populate sau khi user vào 1
              app cụ thể (Mail / Settings / Drive). Trang root `/` chỉ là
              dashboard, webAuth chưa init.
            - Cùng pattern bootstrap.py + extract_session_bundle dùng `/mail/`
              hoặc `/settings/`.
            - User login xong ở `/mail/` → webAuth populate ngay → save extract OK.
        """
        ctx_mgr = launch_camoufox(
            profile_dir=session.profile_dir_temp,
            headless=False,
            proxy=None,
        )
        ctx = await ctx_mgr.__aenter__()
        session._camoufox_ctx_mgr = ctx_mgr
        session._camoufox_ctx = ctx
        # Navigate, KHÔNG raise nếu network chậm — user vẫn có thể nhập URL tay
        # trong Camoufox.
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(
                "https://www.icloud.com/mail/",
                wait_until="domcontentloaded",
            )
        except Exception as exc:  # noqa: BLE001
            self._log(f"navigate warning session_id={session.session_id}: {exc!r}")

    async def _close_camoufox(
        self, session: AddProfileSession, *, force: bool
    ) -> None:
        """Đóng Camoufox. force=True → terminate ngay (cancel/timeout path).

        Best-effort — KHÔNG raise nếu Camoufox đã chết.
        """
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

    async def _extract_apple_id(self, session: AddProfileSession) -> str:
        """Extract apple_id qua nhiều strategy (R14.4).

        Apple iCloud webapp KHÔNG luôn expose ``window.webAuth.dsInfo.appleId``
        — phụ thuộc account type, region, app đang mở, và thời điểm webapp
        init. Tool thử lần lượt 8 strategy (0-7), lấy match đầu tiên.

        Strategy order:
          0. Navigate → ``/settings/`` + settle 4s (force webAuth init —
             ``/settings/`` is the most reliable page to populate
             ``window.webAuth``; same pattern as ``session.py:
             extract_session_bundle`` which navigates to ``/settings/``).
          1. ``window.webAuth.dsInfo.appleId`` — official global.
          2. ``window.cloudkit.session.userInfo.emailAddress`` — CloudKit.
          3. iframe scan — Mail/Calendar/Drive load app trong iframe.
          4. localStorage — cached appleId.
          5. DOM scan — email visible trong UI.
          6. Cookie parse ``X-APPLE-WEBAUTH-USER`` — extract ``email=`` field,
             fallback ``t=`` field, fallback ``a=`` field.
          7. POST ``/setup/ws/1/validate`` qua page.evaluate — last resort
             gọi Apple setup API trực tiếp với cookies authenticated.

        Cả 7 fail → raise AddProfileError(reason='apple_id_not_extractable').
        """
        ctx = session._camoufox_ctx
        if ctx is None:
            raise AddProfileError(
                reason="apple_id_not_extractable",
                message="Camoufox context không sẵn sàng",
                session_id=session.session_id,
            )

        # Helper: validate email-shape + lowercase
        def _ok(value: object) -> str | None:
            if isinstance(value, str) and "@" in value and "." in value:
                v = value.strip().lower()
                if 5 <= len(v) <= 254:
                    return v
            return None

        # Get all pages (main + popup + new tab)
        pages = list(ctx.pages) if ctx.pages else []
        if not pages:
            pages = [await ctx.new_page()]

        # ── Strategy 0: navigate to /settings/ + settle ──────────────────
        # window.webAuth is populated asynchronously by Apple's JS bundle.
        # /settings/ là page reliable nhất cho webAuth init (same as
        # session.py:extract_session_bundle).
        #
        # UX note (A12): trước khi navigate, check page đang ở 1 trong các
        # apps icloud.com (mail/settings/drive/...) — nếu user đã có 1 app
        # mở thì webAuth thường đã được init bởi app đó. Skip navigate để
        # tránh user thấy view nhảy đột ngột từ /mail/ sang /settings/.
        # Settle 4s vẫn chạy để eval webAuth có thời gian populate.
        settings_page = pages[0]
        try:
            current_url = settings_page.url or ""
            already_in_icloud_app = (
                "icloud.com/settings" in current_url
                or "icloud.com/mail" in current_url
                or "icloud.com/drive" in current_url
                or "icloud.com/photos" in current_url
                or "icloud.com/iclouddrive" in current_url
                or "icloud.com/calendar" in current_url
            )
            if not already_in_icloud_app:
                self._log(
                    f"extract_apple_id navigating to /settings/ "
                    f"session_id={session.session_id} from={current_url!r}"
                )
                await settings_page.goto(
                    "https://www.icloud.com/settings/",
                    wait_until="domcontentloaded",
                )
            else:
                self._log(
                    f"extract_apple_id skip navigate (already in app) "
                    f"session_id={session.session_id} url={current_url!r}"
                )
            await asyncio.sleep(4.0)
        except Exception as exc:  # noqa: BLE001
            self._log(
                f"extract_apple_id navigate /settings/ fail "
                f"session_id={session.session_id}: {exc!r}"
            )

        # Refresh pages list after navigation
        pages = list(ctx.pages) if ctx.pages else pages

        # ── Strategy 1-2-4: page.evaluate global + localStorage ───────────
        eval_script = """
() => {
    const out = {webAuth: null, cloudkit: null, ls: null, ck: null,
                 _debug_webAuth_type: typeof window.webAuth,
                 _debug_webAuth_keys: null,
                 _debug_dsInfo_keys: null,
                 _debug_dsInfo_dump: null};
    try {
        const wa = window.webAuth;
        if (wa != null) {
            out._debug_webAuth_keys = Object.keys(wa).slice(0, 30);
            const ds = wa.dsInfo;
            if (ds != null) {
                out._debug_dsInfo_keys = Object.keys(ds).slice(0, 40);
                out._debug_dsInfo_dump = {};
                // Dump mọi field chứa 'mail', 'apple', 'id', 'name', 'email'
                for (const k of Object.keys(ds)) {
                    const lk = k.toLowerCase();
                    if (lk.includes('mail') || lk.includes('apple')
                        || lk.includes('id') || lk.includes('name')
                        || lk.includes('email') || lk.includes('user')) {
                        try { out._debug_dsInfo_dump[k] = String(ds[k]).slice(0, 120); }
                        catch(e) { out._debug_dsInfo_dump[k] = '<error>'; }
                    }
                }
                out.webAuth = ds.appleId
                    || ds.primaryEmail
                    || ds.dsAppleId
                    || ds.primaryEmailVerified
                    || ds.appleIdEntries && ds.appleIdEntries[0]
                    || null;
            }
        }
    } catch (e) { out._debug_webAuth_err = String(e); }
    try {
        if (window.cloudkit && window.cloudkit.session
            && window.cloudkit.session.userInfo) {
            out.cloudkit = window.cloudkit.session.userInfo.emailAddress
                || window.cloudkit.session.userInfo.primaryEmail
                || null;
        }
    } catch (e) {}
    try {
        for (let i = 0; i < localStorage.length; i++) {
            const k = localStorage.key(i);
            if (!k) continue;
            const lk = k.toLowerCase();
            if (lk.includes('email') || lk.includes('appleid') || lk.includes('user')) {
                const v = localStorage.getItem(k);
                if (v && v.includes('@')) {
                    out.ls = v;
                    break;
                }
            }
        }
    } catch (e) {}
    try {
        if (typeof __ck !== 'undefined' && __ck && __ck.config
            && __ck.config.userInfo) {
            out.ck = __ck.config.userInfo.emailAddress
                || __ck.config.userInfo.primaryEmail
                || null;
        }
    } catch (e) {}
    return out;
}
"""
        for page in pages:
            try:
                result = await page.evaluate(eval_script)
            except Exception as exc:  # noqa: BLE001
                self._log(
                    f"page.evaluate (strategy 1-2-4) fail "
                    f"session_id={session.session_id} url={page.url!r}: {exc!r}"
                )
                continue
            if not isinstance(result, dict):
                continue
            # Log debug info separately for readability
            debug_keys = {
                k: v for k, v in result.items() if k.startswith("_debug_")
            }
            main_result = {
                k: v for k, v in result.items() if not k.startswith("_debug_")
            }
            self._log(
                f"extract_apple_id session_id={session.session_id} "
                f"page={page.url!r} result={main_result!r}"
            )
            if any(v is not None for v in debug_keys.values()):
                self._log(
                    f"extract_apple_id DEBUG session_id={session.session_id} "
                    f"{debug_keys!r}"
                )
            for key in ("webAuth", "cloudkit", "ck", "ls"):
                v = _ok(result.get(key))
                if v:
                    self._log(
                        f"extract_apple_id MATCH source={key} value={v}"
                    )
                    return v

        # ── Strategy 3: iframe scan ───────────────────────────────────────
        for page in pages:
            try:
                frames = page.frames
            except Exception:
                continue
            for frame in frames:
                if frame == page.main_frame:
                    continue
                try:
                    v = await frame.evaluate(
                        "() => (window.webAuth && window.webAuth.dsInfo "
                        "&& (window.webAuth.dsInfo.appleId "
                        "|| window.webAuth.dsInfo.primaryEmail)) || null"
                    )
                except Exception:
                    continue
                ok = _ok(v)
                if ok:
                    self._log(
                        f"extract_apple_id MATCH source=iframe url={frame.url!r}"
                    )
                    return ok

        # ── Strategy 5: DOM scan — tìm email visible trong UI ─────────────
        dom_script = r"""
() => {
    const re = /[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/;
    const seen = new Set();
    const candidates = [];
    const sels = [
        '[aria-label*="@"]',
        '[title*="@"]',
        '[data-email]',
        '.account-email',
        '.user-email',
        '.email',
    ];
    for (const sel of sels) {
        try {
            for (const el of document.querySelectorAll(sel)) {
                const txt = (el.getAttribute('aria-label')
                    || el.getAttribute('title')
                    || el.getAttribute('data-email')
                    || el.textContent
                    || '').trim();
                const m = txt.match(re);
                if (m && !seen.has(m[0])) {
                    seen.add(m[0]);
                    candidates.push(m[0]);
                }
            }
        } catch (e) {}
    }
    if (candidates.length === 0) {
        const body = document.body && document.body.innerText || '';
        const m = body.match(re);
        if (m) candidates.push(m[0]);
    }
    return candidates;
}
"""
        for page in pages:
            try:
                cands = await page.evaluate(dom_script)
            except Exception:
                continue
            if not isinstance(cands, list):
                continue
            self._log(
                f"extract_apple_id DOM scan session_id={session.session_id} "
                f"page={page.url!r} candidates={cands!r}"
            )
            for cand in cands:
                v = _ok(cand)
                if v and not v.endswith("@apple.com"):
                    self._log(
                        f"extract_apple_id MATCH source=dom value={v}"
                    )
                    return v

        # ── Strategy 6: parse cookie X-APPLE-WEBAUTH-USER ─────────────────
        try:
            cookies = await ctx.cookies("https://www.icloud.com/")
        except Exception:
            cookies = []
        cookie_names = sorted({c.get("name", "") for c in cookies})
        self._log(
            f"extract_apple_id session_id={session.session_id} "
            f"cookie_count={len(cookies)} names={cookie_names}"
        )
        user_cookie = next(
            (c for c in cookies if c.get("name") == "X-APPLE-WEBAUTH-USER"),
            None,
        )
        if user_cookie:
            raw_value = user_cookie.get("value", "")
            v = _parse_apple_id_from_user_cookie(raw_value)
            if v:
                ok = _ok(v)
                if ok:
                    self._log(
                        f"extract_apple_id MATCH source=cookie value={ok}"
                    )
                    return ok
            self._log(
                f"extract_apple_id cookie X-APPLE-WEBAUTH-USER present "
                f"but no email extracted. raw_len={len(raw_value)} "
                f"decoded_preview={urllib.parse.unquote(raw_value)[:120]!r}"
            )

        # ── Strategy 7: fetch /setup/ws/1/validate via page context ───────
        # Use the browser's authenticated context to call Apple's setup API.
        # This returns account info including appleId when cookies are valid.
        for page in pages:
            try:
                setup_resp = await page.evaluate("""
async () => {
    try {
        const r = await fetch('https://setup.icloud.com/setup/ws/1/validate', {
            method: 'POST',
            credentials: 'include',
            headers: {
                'Accept': 'application/json',
                'Content-Type': 'application/json',
                'Origin': 'https://www.icloud.com',
                'Referer': 'https://www.icloud.com/'
            }
        });
        const t = await r.text();
        try { return JSON.parse(t); } catch(e) { return {_raw: t.slice(0, 500)}; }
    } catch(e) { return {_error: String(e)}; }
}
""")
            except Exception as exc:  # noqa: BLE001
                self._log(
                    f"extract_apple_id setup/validate fetch fail "
                    f"session_id={session.session_id}: {exc!r}"
                )
                continue
            if isinstance(setup_resp, dict):
                self._log(
                    f"extract_apple_id setup/validate response keys="
                    f"{list(setup_resp.keys())[:20]!r}"
                )
                # Apple setup response has dsInfo.appleId
                ds_info = setup_resp.get("dsInfo")
                if isinstance(ds_info, dict):
                    for field_name in (
                        "appleId", "primaryEmail", "dsAppleId",
                        "primaryEmailVerified",
                    ):
                        candidate = ds_info.get(field_name)
                        ok = _ok(candidate)
                        if ok:
                            self._log(
                                f"extract_apple_id MATCH source=setup_api "
                                f"field={field_name} value={ok}"
                            )
                            return ok
            break  # only try once

        # Tất cả fail.
        raise AddProfileError(
            reason="apple_id_not_extractable",
            message=(
                "Không trích xuất được apple_id từ Camoufox. "
                "Check: (1) login + 2FA xong; (2) thấy email/avatar góc trên phải; "
                "(3) thử mở Mail / Settings / Drive. "
                "Server log có chi tiết cookies + DOM candidates để debug. "
                "Hoặc nhập Apple ID thủ công vào ô input rồi bấm Lưu lại."
            ),
            session_id=session.session_id,
        )

    async def _resolve_apple_id(
        self, session: AddProfileSession, *, hint: str | None
    ) -> str:
        """Resolve apple_id theo priority: user hint (verified) → auto-extract.

        User hint có ưu tiên cao nhất vì:
            - User mới biết chính xác Apple ID đã login (auto-extract qua
              page.evaluate / cookie không reliable trên iCloud webapp đa
              iframe — webAuth.dsInfo có lúc không có appleId, cookie
              X-APPLE-WEBAUTH-USER chỉ chứa dsid numeric).
            - Pattern khớp Bootstrap_Flow MVP (CLI ``bootstrap APPLE_ID``
              luôn yêu cầu user nhập sẵn) — Web flow chỉ là Bootstrap headed
              + UI control thay terminal.

        Verify cross-check (review B — A17): nếu auto-extract phát hiện 1
        apple_id KHÁC hint → raise ``AddProfileError('apple_id_mismatch')``
        thay vì silently dùng hint. Tránh user gõ nhầm dẫn tới profile_dir
        mapping sai (cookies thực thuộc account A, DB ghi profile cho B —
        không reversible). Khi auto-extract fail (None) thì hint vẫn được
        chấp nhận — pattern fail-soft cho trường hợp Apple webapp chặn
        auto-extract.

        Args:
            session: Session đang record.
            hint: User-provided email từ UI input. Validate format ``*@*``,
                normalize lowercase + strip. None hoặc empty → fallback
                auto-extract (best-effort).

        Returns:
            Apple ID đã normalize, sẵn sàng cho ``_move_profile_dir``.

        Raises:
            AddProfileError(reason='apple_id_not_extractable'): hint invalid
                AND auto-extract fail. Recoverable — user retry với hint
                đúng hoặc tiếp tục login.
            AddProfileError(reason='apple_id_mismatch'): hint != extracted.
                Recoverable — user kiểm lại hint hoặc bỏ trống để dùng
                auto-extract.
        """
        normalized = self._normalize_apple_id_hint(hint)
        if normalized is None:
            # Không có hint → auto-extract (best-effort).
            return await self._extract_apple_id(session)

        # Có hint → verify cross-check qua auto-extract.
        try:
            extracted = await self._extract_apple_id(session)
        except AddProfileError as exc:
            if exc.reason == "apple_id_not_extractable":
                # Auto-extract fail → trust hint (fail-soft).
                self._log(
                    f"resolve_apple_id session_id={session.session_id} "
                    f"source=user_hint value={normalized} "
                    f"verify=skipped (auto_extract_failed)"
                )
                return normalized
            raise

        if extracted.strip().lower() == normalized:
            self._log(
                f"resolve_apple_id session_id={session.session_id} "
                f"source=user_hint value={normalized} verify=match"
            )
            return normalized

        self._log(
            f"resolve_apple_id MISMATCH session_id={session.session_id} "
            f"hint={normalized!r} extracted={extracted!r} → reject"
        )
        raise AddProfileError(
            reason="apple_id_mismatch",
            message=(
                f"Apple ID nhập '{normalized}' KHÔNG khớp account đã login "
                f"trong Camoufox ('{extracted}'). "
                f"Kiểm tra lại email hoặc bỏ trống ô nhập để tool tự lấy."
            ),
            session_id=session.session_id,
        )

    @staticmethod
    def _normalize_apple_id_hint(hint: str | None) -> str | None:
        """Validate + normalize user hint. Return None nếu invalid/empty.

        Apple ID là email format. Chấp nhận:
            - non-empty string
            - chứa đúng 1 dấu '@'
            - phần trước @ + phần sau @ đều non-empty
            - phần sau @ chứa ít nhất 1 dấu '.'

        KHÔNG raise — caller fallback auto-extract khi hint invalid.
        """
        if not hint or not isinstance(hint, str):
            return None
        normalized = hint.strip().lower()
        if not normalized:
            return None
        parts = normalized.split("@")
        if len(parts) != 2:
            return None
        local, domain = parts
        if not local or not domain or "." not in domain:
            return None
        return normalized

    async def _verify_required_cookies(
        self, session: AddProfileSession
    ) -> None:
        """Verify ÍT NHẤT 1 login cookie marker đã có (R14.5).

        Khớp logic bootstrap.py — cần bất kỳ 1 trong 3 marker:
        X-APPLE-WEBAUTH-USER / X-APPLE-WEBAUTH-TOKEN / X-APPLE-WEBAUTH-PCS-Mail.
        Không yêu cầu tất cả vì X-APPLE-WEBAUTH-PCS-Mail không phải account
        nào cũng có.
        """
        ctx = session._camoufox_ctx
        if ctx is None:
            raise AddProfileError(
                reason="cookies_not_ready",
                message="Camoufox context đã đóng",
                session_id=session.session_id,
            )
        try:
            cookies = await ctx.cookies("https://www.icloud.com/")
        except Exception as exc:  # noqa: BLE001
            raise AddProfileError(
                reason="cookies_not_ready",
                message=f"Đọc cookies fail: {exc}",
                session_id=session.session_id,
            ) from exc
        names = {c.get("name") for c in cookies}
        matched = names & set(_LOGIN_COOKIE_MARKERS)
        if not matched:
            raise AddProfileError(
                reason="cookies_not_ready",
                message=(
                    f"Hoàn tất login Apple ID + 2FA trước khi bấm Lưu. "
                    f"Không tìm thấy cookie login marker nào trong: "
                    f"{list(_LOGIN_COOKIE_MARKERS)}"
                ),
                session_id=session.session_id,
            )

    async def _move_profile_dir(
        self, session: AddProfileSession, apple_id: str
    ) -> None:
        """Rename .adding/<session_id>/ → icloud_profiles/<apple_id>/ (R14.3, R14.6, R14.11).

        Check existing apple_id row trước khi move:
            - row None → mới hoàn toàn, move OK.
            - row.status ∈ blocking → raise apple_id_already_exists (R14.6).
            - row.status='deleted' → re-add path, move OK.

        Retry move khi gặp OSError (file lock conflict, R14.11).
        """
        existing = self._pool_repo.get(apple_id)
        if existing is not None and existing.status in _BLOCKING_STATUSES:
            raise AddProfileError(
                reason="apple_id_already_exists",
                message=(
                    f"Profile cho Apple ID {apple_id} đã tồn tại "
                    f"(status={existing.status}). "
                    f"Dùng action Bootstrap để re-login thay vì thêm mới."
                ),
                session_id=session.session_id,
            )

        # Tính path đích — không dùng ensure_profile_dir vì sẽ tạo dir trước khi
        # move (gây conflict). Tự build path từ apple_id chuẩn hóa qua module
        # session.
        from .session import _safe_apple_id  # type: ignore[attr-defined]

        target_dir = self.profiles_dir / _safe_apple_id(apple_id)

        # Nếu target đã tồn tại (re-add deleted profile) → xoá trước.
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)

        # Đảm bảo parent dir tồn tại.
        target_dir.parent.mkdir(parents=True, exist_ok=True)

        # Retry loop cho OSError (R14.11). 5s timeout, 0.5s interval.
        deadline = asyncio.get_event_loop().time() + _MOVE_RETRY_TIMEOUT_SEC
        last_exc: Exception | None = None
        while asyncio.get_event_loop().time() < deadline:
            try:
                shutil.move(str(session.profile_dir_temp), str(target_dir))
                session.profile_dir_final = target_dir
                return
            except OSError as exc:
                last_exc = exc
                await asyncio.sleep(_MOVE_RETRY_INTERVAL_SEC)
        raise AddProfileError(
            reason="move_failed",
            message=(
                f"Rename profile_dir tạm fail sau "
                f"{_MOVE_RETRY_TIMEOUT_SEC}s: {last_exc}"
            ),
            session_id=session.session_id,
        )

    def _persist_account(
        self, session: AddProfileSession, apple_id: str
    ) -> None:
        """Upsert + update_status + clear flags trong cùng tx (R14.3)."""
        engine = self._pool_repo.engine
        assert session.profile_dir_final is not None
        with engine.transaction():
            self._pool_repo.upsert(apple_id, session.profile_dir_final)
            self._pool_repo.update_status(
                apple_id,
                status="active",
                clear_error=True,
                clear_limited_until=True,
                clear_quota_retry_until=True,
            )

    def _cleanup_temp_dir(self, session: AddProfileSession) -> None:
        """Best-effort: xoá profile_dir tạm. KHÔNG raise."""
        try:
            if session.profile_dir_temp.exists():
                shutil.rmtree(session.profile_dir_temp, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001
            self._log(
                f"cleanup_temp_dir fail session_id={session.session_id}: {exc!r}"
            )

    async def _fail(
        self,
        session: AddProfileSession,
        *,
        reason: str,
        error: str | None,
    ) -> None:
        """Transition → FAILED + close Camoufox + cleanup + audit.

        Trường hợp đặc biệt apple_id_already_exists: KHÔNG ghi đè row cũ trong
        DB (đã được _move_profile_dir check trước khi gọi shutil.move). Cleanup
        profile_dir tạm bình thường.
        """
        await self._close_camoufox(session, force=True)
        self._cleanup_temp_dir(session)
        session.state = AddProfileState.FAILED
        session.ended_at = _utc_now()
        session.error_reason = reason
        session.error = error
        try:
            self._audit_repo.write(
                event_type="profile_add_fail",
                apple_id=None,
                payload={
                    "session_id": session.session_id,
                    "reason": reason,
                    "error": error,
                },
            )
        except Exception as exc:  # noqa: BLE001 — best-effort audit
            self._log(
                f"audit profile_add_fail fail session_id={session.session_id}: {exc!r}"
            )
        self._cancel_watchdog(session)
        async with self._lock:
            if self._active is session:
                self._remember_terminal(session)
                self._active = None

    async def _watchdog(self, session: AddProfileSession) -> None:
        """Hard timeout cancel sau timeout_sec từ started_at (R14.8)."""
        try:
            await asyncio.sleep(self._timeout_sec)
        except asyncio.CancelledError:
            return
        # Vẫn ở state non-terminal → force timeout cancel.
        if session.state not in _ACTIVE_STATES:
            return
        try:
            session.state = AddProfileState.CANCELLING
            await self._close_camoufox(session, force=True)
            self._cleanup_temp_dir(session)
            session.state = AddProfileState.CANCELLED
            session.ended_at = _utc_now()
            self._audit_repo.write(
                event_type="profile_add_timeout",
                apple_id=None,
                payload={
                    "session_id": session.session_id,
                    "expired_after_sec": self._timeout_sec,
                },
            )
            self._log(
                f"watchdog timeout session_id={session.session_id} "
                f"after={self._timeout_sec}s"
            )
        except Exception as exc:  # noqa: BLE001 — KHÔNG để watchdog crash
            self._log(
                f"watchdog fail session_id={session.session_id}: {exc!r}"
            )
        finally:
            async with self._lock:
                if self._active is session:
                    self._remember_terminal(session)
                    self._active = None

    def _cancel_watchdog(self, session: AddProfileSession) -> None:
        """Cancel watchdog task nếu đang chạy. Best-effort."""
        task = session._watchdog_task
        if task is None or task.done():
            return
        task.cancel()
        session._watchdog_task = None

    def _require_active(self, session_id: str) -> AddProfileSession:
        if self._active is None or self._active.session_id != session_id:
            raise AddProfileError(
                reason="session_not_found",
                message=f"Session {session_id} không active",
                session_id=session_id,
            )
        return self._active


# ─── Cookie parser helpers ─────────────────────────────────────────────────

# X-APPLE-WEBAUTH-USER cookie format varies by account/region. Known formats:
#   Format A: v=2:s=4:d=NNNN:a=...:email=user@domain.com:t=...
#   Format B: v=1:s=0:d=NNNN  (no email field — only dsid numeric)
#   Format C: t=user@icloud.com:d=NNNN
#   Format D: URL-encoded variant of above (@ → %40, : → %3A)
# Fields are colon-separated key=value pairs. URL-decode first.

# Patterns to try, in priority order:
_COOKIE_EMAIL_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"email=([^:&]+)"),        # explicit email= field
    re.compile(r"appleId=([^:&]+)"),      # explicit appleId= field
    re.compile(r"t=([^:&]+@[^:&]+)"),     # t= field containing email
    re.compile(r"a=([^:&]+@[^:&]+)"),     # a= field containing email
)
# Last resort: any email-like string in the cookie value
_COOKIE_EMAIL_FALLBACK = re.compile(
    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
)


def _parse_apple_id_from_user_cookie(cookie_value: str) -> str | None:
    """Extract apple_id từ X-APPLE-WEBAUTH-USER cookie value.

    Tries multiple field patterns (email=, appleId=, t=, a=), then falls back
    to regex scan for any email-like string in the decoded cookie.

    Return None nếu không tìm được email hoặc value trống.
    """
    if not cookie_value:
        return None
    decoded = urllib.parse.unquote(cookie_value)

    for pattern in _COOKIE_EMAIL_PATTERNS:
        match = pattern.search(decoded)
        if match:
            candidate = match.group(1).strip().strip('"')
            if "@" in candidate and "." in candidate:
                return candidate

    # Fallback: scan for any email-like string
    match = _COOKIE_EMAIL_FALLBACK.search(decoded)
    if match:
        candidate = match.group(0)
        if not candidate.endswith("@apple.com"):
            return candidate

    return None
