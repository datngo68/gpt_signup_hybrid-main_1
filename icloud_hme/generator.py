"""HME_Generator — tạo HME email với audit trail + idempotency (R3, R8).

Refs:
    requirements.md R3.1–R3.27, R8.1–R8.6, R12.8, R12.9
    design.md §Components / 5. HME_Generator
    tasks.md task 15

Class ``HmeGenerator`` orchestrate flow tạo HME email:
    - Pick profile qua ``IcloudPoolManager`` (round-robin).
    - Extract ``SessionBundle`` 1 lần đầu batch / sau khi switch profile
      (R12.8 cache in-memory cho cùng profile run).
    - Tạo email tuần tự qua ``HmeClient.generate → reserve``.
    - INSERT email + UPDATE hme_count + audit ``create_success`` trong
      CÙNG outer tx (R3.5, R6.3).
    - Reserve race (``HmeReserveTaken``) → audit ``candidate_retry``, retry
      ``race_retry_max`` lần — KHÔNG đếm fail (R3.14, R3.15).
    - ``HmeQuotaError`` → ``pool.mark_limited``, switch profile (R3.7).
    - ``HmeAuthError`` → invalidate bundle, ``pool.mark_session_expired``,
      switch profile (R3.8, R12.9).
    - Post-pick check ``hme_count >= HME_QUOTA_LIMIT`` → ``mark_quota_full``
      + audit ``email_skip_quota_full`` + continue NO delay (R3.22).

Mode:
    - ``Bounded``: ``infinite=False`` + ``count`` integer >= 1 → dừng khi
      created == count.
    - ``Bounded_Drain``: ``infinite=False`` + ``count`` ∈ ``_INFINITE_SENTINELS``
      ({None, 0, -1, 'infinite'}) → drain liên tục cho profile pool hiện tại,
      thoát khi pool exhausted hoặc cancel/pause (KHÔNG vào Pool_Exhausted_Wait).
      Dùng cho HmeRunner gọi ``generate(infinite=False, count=count_per_cycle)``
      với ``count_per_cycle=None`` (icloud-runner-loop R6.1) — Runner tự quản
      lý infinite loop ngoài, mỗi cycle drain pool tối đa rồi sleep retry.
    - ``Infinite_Generate_Mode``: ``infinite=True`` → chỉ dừng vì
      cancel/pause/fatal; pool exhausted → ``_pool_exhausted_wait`` (R3.20,
      R3.23, R3.24).

Pool_Exhausted_Wait branch (R3.23, R3.24):
    - ``pool.pick_active_profile()`` raise ``IcloudPoolError`` →
      compute ``wake_at = min(limited_until, quota_retry_until)`` từ tập
      profile có thể tự recover ``R = {limited, quota_full}``.
    - ``R`` rỗng → return GenerationResult với reason='no_recoverable_profile'.
    - Sleep chunks 1s + check cancellation/pause event mỗi giây (R3.24)
      capped bởi ``infinite_wait_max_sec``.
    - Audit ``infinite_wait_start`` / ``infinite_wait_end``.
"""

from __future__ import annotations

import asyncio
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Literal

from .client import HmeClient
from .exceptions import (
    HmeAuthError,
    HmeClientError,
    HmeQuotaError,
    HmeReserveTaken,
    IcloudPoolError,
    SessionExtractError,
)
from .models import (
    AppleAccount,
    FailureRecord,
    GenerationResult,
    SessionBundle,
)
from .session import extract_session_bundle

if TYPE_CHECKING:
    from db.repositories import AuditLogRepository, IcloudPoolRepository

    from .pool import IcloudPoolManager


# Default delay range (R3.11): random 2.0–5.0s giữa 2 reserve cùng profile.
_DEFAULT_DELAY_RANGE: tuple[float, float] = (2.0, 5.0)

# Default candidate retry max khi gặp HmeReserveTaken (R3.14).
_DEFAULT_RACE_RETRY_MAX: int = 3

# Profile parallelism — MVP: 1 (R3.17). Cho phép Generator chạy nhiều profile
# tuần tự nhưng KHÔNG concurrent — tránh race với Pool_Manager pick.
_DEFAULT_PROFILE_PARALLELISM: int = 1

# Max wait Pool_Exhausted_Wait — capped 24h (R3.23 default).
_DEFAULT_INFINITE_WAIT_MAX_SEC: int = 86400

# Sentinel value cho "infinite mode" (R3.20).
_INFINITE_SENTINELS: tuple = (None, 0, -1, "infinite")


def _utc_now() -> datetime:
    """UTC naive datetime — match Timestamp_Format (Property 30)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _format_label_default(now: datetime | None = None) -> str:
    """Label_Default = strftime('%Y%m%d', UTC) — R3.18, Property 6."""
    t = now if now is not None else _utc_now()
    return t.strftime("%Y%m%d")


def _format_ts(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_ts_str(raw) -> datetime | None:
    """Parse iso str → datetime UTC naive. None nếu không parse được.

    Reused trong _pin_pick để build AppleAccount.last_used_at.
    """
    if raw is None:
        return None
    s = raw if isinstance(raw, str) else str(raw)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


class HmeGenerator:
    """Orchestrator tạo HME email với audit + idempotency (R3, R8).

    Args:
        pool: ``IcloudPoolManager`` cho pick / mark transition.
        pool_repo: ``IcloudPoolRepository`` cho INSERT email + counter.
        audit_repo: ``AuditLogRepository`` cho audit ghi cùng outer tx.
        race_retry_max: max retry khi gặp ``HmeReserveTaken`` (R3.14).
        delay_range: (min, max) seconds random delay giữa 2 reserve (R3.11).
        profile_parallelism: số profile chạy song song (R3.17, MVP 1).
        infinite_wait_max_sec: cap sleep ở Pool_Exhausted_Wait (R3.23).
        log: optional logger callable.
        extract_session_bundle_fn: inject để test mock; default = real
            ``extract_session_bundle`` (R12.3).
        client_factory: inject để test mock; default = real ``HmeClient``.
        sleep_fn: inject để test bypass real ``asyncio.sleep`` (R3.11/R3.24).
    """

    def __init__(
        self,
        pool: "IcloudPoolManager",
        pool_repo: "IcloudPoolRepository",
        audit_repo: "AuditLogRepository",
        *,
        race_retry_max: int = _DEFAULT_RACE_RETRY_MAX,
        delay_range: tuple[float, float] = _DEFAULT_DELAY_RANGE,
        profile_parallelism: int = _DEFAULT_PROFILE_PARALLELISM,
        infinite_wait_max_sec: int = _DEFAULT_INFINITE_WAIT_MAX_SEC,
        hme_quota_limit: int = 700,
        log: Any = None,
        extract_session_bundle_fn: Callable[..., Any] | None = None,
        client_factory: Callable[..., HmeClient] | None = None,
        sleep_fn: Callable[[float], Any] | None = None,
    ) -> None:
        if race_retry_max < 0:
            raise ValueError(f"race_retry_max phải >= 0, got {race_retry_max}")
        if delay_range[0] < 0 or delay_range[1] < delay_range[0]:
            raise ValueError(f"delay_range không hợp lệ: {delay_range}")
        if profile_parallelism < 1:
            raise ValueError(
                f"profile_parallelism phải >= 1, got {profile_parallelism}"
            )
        if infinite_wait_max_sec < 1:
            raise ValueError(
                f"infinite_wait_max_sec phải >= 1, got {infinite_wait_max_sec}"
            )
        if hme_quota_limit <= 0:
            raise ValueError(f"hme_quota_limit phải > 0, got {hme_quota_limit}")
        self._pool = pool
        self._pool_repo = pool_repo
        self._audit_repo = audit_repo
        self._race_retry_max = race_retry_max
        self._delay_range = delay_range
        self._profile_parallelism = profile_parallelism
        self._infinite_wait_max_sec = infinite_wait_max_sec
        self._hme_quota_limit = hme_quota_limit
        self._log = log if callable(log) else (lambda *_a, **_k: None)
        self._extract_fn = extract_session_bundle_fn or extract_session_bundle
        self._client_factory = client_factory or (
            lambda bundle: HmeClient(bundle, log=self._log)
        )
        self._sleep = sleep_fn or asyncio.sleep

    # =====================================================================
    # Public API
    # =====================================================================

    async def generate(
        self,
        *,
        count: int | str | None = None,
        infinite: bool = False,
        label: str | None = None,
        note: str | None = None,
        proxy: str | None = None,
        cancellation_event: asyncio.Event | None = None,
        pause_event: asyncio.Event | None = None,
        resume_event: asyncio.Event | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        target_apple_id: str | None = None,
    ) -> GenerationResult:
        """Tạo N email HME (bounded) hoặc vô hạn (Infinite_Generate_Mode).

        Args:
            count: số email cần tạo (bounded). Sentinel ∈ {None, 0, -1,
                'infinite'} → infinite mode.
            infinite: True → infinite mode (override count).
            label: Label cho mỗi email. None/empty → Label_Default
                (strftime('%Y%m%d', UTC), R3.18).
            note: Note cho mỗi email. None → empty string.
            proxy: Proxy URL cho extract Camoufox.
            cancellation_event: Event báo dừng — mỗi reserve check
                trước/sau (R3.21).
            pause_event / resume_event: Pause-resume cho Job_Action 'pause'.
            on_progress: callback ``(created, requested)`` mỗi reserve OK.
            target_apple_id: Khi set, generator KHÔNG dùng
                ``pool.pick_active_profile()`` (round-robin) mà pin chỉ
                chạy đúng profile này. Dùng cho HmeRunner per-profile
                rotate (icloud-runner-loop). Nếu profile không
                eligible (status != active sau khi tự transition tại
                thời điểm gọi, hoặc apple_id không tồn tại / hme_count
                vượt quota) → trả ``GenerationResult`` rỗng + log lý do,
                KHÔNG raise. ``infinite=True`` không tương thích với
                target_apple_id (bounded-only) → raise ValueError.

        Returns:
            ``GenerationResult`` với requested/created/emails/failures/
            disabled_profiles/label.
        """
        if target_apple_id is not None and infinite:
            raise ValueError(
                "target_apple_id không tương thích infinite=True; dùng "
                "bounded mode (count >= 1 hoặc count=None bounded_drain)."
            )
        # Resolve mode (icloud-runner-loop R6.1 + R3.20):
        #   - infinite=True → Infinite_Generate_Mode (Pool_Exhausted_Wait)
        #   - infinite=False + count sentinel (None/0/-1/'infinite')
        #     → Bounded_Drain: drain pool, thoát khi exhausted hoặc cancel
        #   - infinite=False + count int>=1 → Bounded: dừng khi created==count
        effective_infinite = bool(infinite)
        if effective_infinite:
            requested = 0
            bounded_drain = False
        elif count in _INFINITE_SENTINELS:
            # HmeRunner gọi với count=None → bounded drain unlimited per cycle.
            requested = 0
            bounded_drain = True
        else:
            requested = int(count)  # type: ignore[arg-type]
            if requested < 1:
                raise ValueError(
                    f"count phải >= 1 khi infinite=False, got {requested}"
                )
            bounded_drain = False

        # Resolve label (1 lần đầu batch — R3.18, Property 6)
        if label is None or label == "":
            effective_label = _format_label_default()
        else:
            effective_label = label

        result = GenerationResult(
            requested=requested,
            created=0,
            emails=[],
            failures=[],
            disabled_profiles=[],
            label=effective_label,
        )

        self._log(
            f"generate start: count={count} infinite={effective_infinite} "
            f"label={effective_label!r}"
        )

        # Cache Session_Bundle in-memory cho cùng profile (R12.8). Reset khi
        # switch profile hoặc nhận HmeAuthError.
        cached_bundle: SessionBundle | None = None
        cached_apple_id: str | None = None
        cached_client: HmeClient | None = None

        async def close_cached() -> None:
            nonlocal cached_client
            if cached_client is not None:
                try:
                    await cached_client.aclose()
                except Exception:
                    pass
                cached_client = None

        try:
            # ── Outer loop: pick profile + tạo emails ────────────────────
            while True:
                # 1. Pre-check cancellation/pause
                if await self._check_cancel_or_pause(
                    cancellation_event, pause_event, resume_event
                ):
                    break

                # 2. Bounded mode termination check (chỉ khi count>=1)
                if requested > 0 and result.created >= requested:
                    break

                # 3. Pick profile
                if target_apple_id is not None:
                    # Pin mode (icloud-runner-loop per-profile cycle): bỏ
                    # qua round-robin pool.pick_active_profile(); chỉ dùng
                    # đúng profile target. Dùng `_pin_pick` resolve trực
                    # tiếp từ DB. Nếu không eligible → exit silent với
                    # disabled_profiles để runner ghi nhận (KHÔNG fallback
                    # sang profile khác — caller (Runner) tự rotate).
                    account = self._pin_pick(target_apple_id)
                    if account is None:
                        # Profile không tồn tại / status terminal /
                        # quota_full chưa đến hạn / limited chưa đến hạn.
                        # Caller đã decide skip; KHÔNG mark gì thêm ở đây.
                        result.disabled_profiles.append(target_apple_id)
                        break
                else:
                    try:
                        account = self._pool.pick_active_profile()
                    except IcloudPoolError as exc:
                        if "pool_pick_locked" in str(exc):
                            # Lock conflict — short wait + retry
                            self._log(f"pool_pick_locked: {exc}")
                            await self._sleep(1.0)
                            continue
                        # Pool exhausted → wait branch (Infinite mode), drain
                        # exit (Bounded_Drain), hoặc fail-fast (Bounded fixed)
                        if effective_infinite:
                            action = await self._pool_exhausted_wait(
                                cancellation_event=cancellation_event,
                                pause_event=pause_event,
                                resume_event=resume_event,
                            )
                            if action == "no_recoverable":
                                self._log(
                                    "no_recoverable_profile — exit infinite loop"
                                )
                                break
                            if action == "cancelled":
                                break
                            # action='timeout' or 'pause' → continue outer
                            continue
                        if bounded_drain:
                            # Drain mode (HmeRunner cycle): pool hết slot ở
                            # cycle này → thoát êm để Runner sleep retry_interval
                            # cho profile recover ở cycle kế tiếp (R6.1).
                            self._log(
                                f"bounded_drain pool exhausted: created="
                                f"{result.created} → exit cycle"
                            )
                            break
                        # Bounded mode (count>=1) + pool exhausted → fail-fast với partial
                        self._log(f"bounded mode pool exhausted: {exc}")
                        result.failures.append(
                            FailureRecord(
                                apple_id="",
                                error_class="IcloudPoolError",
                                error=str(exc),
                            )
                        )
                        break

                # 4. Switch profile? Invalidate cached bundle nếu khác apple_id
                if cached_apple_id != account.apple_id:
                    await close_cached()
                    cached_bundle = None
                    cached_apple_id = None

                # 5. Post-pick check hme_count >= quota (R3.22)
                if account.hme_count >= self._hme_quota_limit:
                    self._pool.mark_quota_full(
                        account.apple_id,
                        reason=f"hme_count={account.hme_count}",
                    )
                    self._audit_repo.write(
                        event_type="email_skip_quota_full",
                        apple_id=account.apple_id,
                        payload={
                            "apple_id": account.apple_id,
                            "hme_count": account.hme_count,
                        },
                    )
                    # NO delay — switch profile NGAY (R3.22)
                    continue

                # 6. Extract bundle (cache reuse, R12.8)
                if cached_bundle is None:
                    try:
                        cached_bundle = await self._extract_fn(
                            profile_dir=account.profile_dir,
                            apple_id=account.apple_id,
                            audit_repo=self._audit_repo,
                            proxy=proxy,
                            log=self._log,
                        )
                    except SessionExtractError as exc:
                        self._log(
                            f"session_extract_fail apple_id={account.apple_id}: {exc}"
                        )
                        # Mark profile session_expired + switch
                        self._pool.mark_session_expired(
                            account.apple_id,
                            reason=f"session_extract_fail: {exc}",
                        )
                        result.disabled_profiles.append(account.apple_id)
                        continue
                    cached_apple_id = account.apple_id
                    cached_client = self._client_factory(cached_bundle)

                assert cached_client is not None  # narrow type

                # 7. Inner loop: tạo emails liên tục cho profile này
                profile_done = await self._inner_generate_loop(
                    account=account,
                    client=cached_client,
                    label=effective_label,
                    note=note,
                    result=result,
                    effective_infinite=effective_infinite,
                    cancellation_event=cancellation_event,
                    pause_event=pause_event,
                    resume_event=resume_event,
                    on_progress=on_progress,
                )
                if profile_done == "switch":
                    # Profile bị mark limited/session_expired — invalidate
                    await close_cached()
                    cached_bundle = None
                    cached_apple_id = None
                    if account.apple_id not in result.disabled_profiles:
                        result.disabled_profiles.append(account.apple_id)
                    if target_apple_id is not None:
                        # Pin mode: caller đã cố định 1 profile, không
                        # rotate sang profile khác. Break để Runner xử lý
                        # vòng kế tiếp.
                        break
                    continue
                if profile_done == "fatal":
                    # Fatal sau race retry hết — record failure, KHÔNG
                    # mark_limited (Property 5 — race ≠ rate-limit). Add
                    # vào disabled_profiles để outer skip pick lại profile
                    # này trong cùng batch (in-memory only).
                    result.failures.append(
                        FailureRecord(
                            apple_id=account.apple_id,
                            error_class="HmeReserveTaken",
                            error="candidate_taken_after_retry",
                        )
                    )
                    if account.apple_id not in result.disabled_profiles:
                        result.disabled_profiles.append(account.apple_id)
                    await close_cached()
                    cached_bundle = None
                    cached_apple_id = None
                    if target_apple_id is not None:
                        # Pin mode: 1 profile, fatal → break luôn.
                        break
                    # Bounded mode: nếu không còn profile nào pool có thể
                    # pick → break tránh infinite loop. Profile đang
                    # `limited` / `quota_full` SẮP recover (limited_until /
                    # quota_retry_until expired) vẫn count là eligible — Pool
                    # SQL pick đã filter theo timestamp. Trước fix A5 chỉ
                    # check `status='active'` → bỏ qua profile sắp recover →
                    # bounded `-n N` dừng partial khi pool còn capacity.
                    if not effective_infinite:
                        all_accounts = self._pool_repo.list_all()
                        eligible = [
                            a
                            for a in all_accounts
                            if a.apple_id != account.apple_id
                            and a.apple_id not in result.disabled_profiles
                            and a.status in (
                                "active", "limited", "quota_full",
                            )
                        ]
                        if not eligible:
                            break
                    continue
                if profile_done == "stop":
                    break
                # 'continue' → switch profile (round-robin)
                # Bounded done → outer loop check terminates
                if target_apple_id is not None:
                    # Pin mode: inner loop return path khác stop/switch/fatal
                    # (vd bounded done: created==requested) — break để Runner
                    # xử lý cycle kế. Trước đây chỉ relevant cho round-robin.
                    break

        finally:
            await close_cached()

        self._log(
            f"generate end: created={result.created}/{result.requested or 'inf'} "
            f"failures={len(result.failures)} disabled={len(result.disabled_profiles)}"
        )
        return result

    # =====================================================================
    # Pin pick (icloud-runner-loop per-profile cycle)
    # =====================================================================

    def _pin_pick(self, apple_id: str) -> AppleAccount | None:
        """Resolve 1 profile cụ thể cho pin mode (target_apple_id).

        Khác ``IcloudPoolManager.pick_active_profile``:
            - KHÔNG dùng round-robin cursor.
            - KHÔNG raise IcloudPoolError; eligibility miss → trả None.
            - Áp cùng quy tắc auto-transition limited/quota_full→active
              khi đã đến hạn (R2.7, R2.12) qua
              ``pool_manager.pick_active_profile`` indirect không khả thi
              (round-robin) → tự kiểm tra trong DB-tx ngắn.

        Eligibility:
            - status='active' → trả ngay.
            - status='limited' AND limited_until<=now → transition active +
              audit ``limited_retry``, trả profile.
            - status='quota_full' AND quota_retry_until<=now AND
              hme_count<HME_QUOTA_LIMIT → transition active + audit
              ``quota_retry``, trả profile.
            - Còn lại → None.

        Returns: AppleAccount eligible hoặc None.
        """
        engine = self._pool_repo.engine
        now = _utc_now()
        now_iso = _format_ts(now)
        with engine.transaction(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT apple_id, profile_dir, status, hme_count,
                       limited_until, quota_retry_until,
                       last_used_at, last_error
                FROM icloud_accounts
                WHERE apple_id = ?
                """,
                (apple_id,),
            ).fetchone()
            if row is None:
                return None
            status = row["status"]
            if status == "active":
                pass
            elif (
                status == "limited"
                and row["limited_until"] is not None
                and row["limited_until"] <= now_iso
            ):
                conn.execute(
                    "UPDATE icloud_accounts SET status='active', "
                    "limited_until=NULL WHERE apple_id = ?",
                    (apple_id,),
                )
                self._audit_repo.write(
                    event_type="limited_retry",
                    apple_id=apple_id,
                    payload={
                        "apple_id": apple_id,
                        "previous_status": "limited",
                        "transitioned_at": now_iso,
                        "via": "pin_pick",
                    },
                )
            elif (
                status == "quota_full"
                and row["quota_retry_until"] is not None
                and row["quota_retry_until"] <= now_iso
                and row["hme_count"] < self._hme_quota_limit
            ):
                conn.execute(
                    "UPDATE icloud_accounts SET status='active', "
                    "quota_retry_until=NULL WHERE apple_id = ?",
                    (apple_id,),
                )
                self._audit_repo.write(
                    event_type="quota_retry",
                    apple_id=apple_id,
                    payload={
                        "apple_id": apple_id,
                        "hme_count": row["hme_count"],
                        "previous_status": "quota_full",
                        "transitioned_at": now_iso,
                        "via": "pin_pick",
                    },
                )
            else:
                return None
            from pathlib import Path as _Path
            profile_dir = (
                _Path(row["profile_dir"]) if row["profile_dir"] else None
            )
            return AppleAccount(
                apple_id=row["apple_id"],
                profile_dir=profile_dir,
                status="active",
                hme_count=row["hme_count"],
                limited_until=None,
                quota_retry_until=None,
                last_used_at=_parse_ts_str(row["last_used_at"]),
                last_error=row["last_error"],
            )

    # =====================================================================
    # Reconcile (R8.3)
    # =====================================================================

    async def reconcile(self, apple_id: str, *, proxy: str | None = None) -> int:
        """No-op trong refactor B (DB source-of-truth).

        Trước đây: probe Apple-side list → INSERT email DB chưa có →
        ``reconcile_add``. Giờ DB là source-of-truth — tool chỉ quản email
        do nó tạo (đã INSERT lúc generate). Email Apple-side ngoài tool
        KHÔNG được import vào DB.

        Method giữ signature để CLI / caller cũ không vỡ; trả 0 + log
        thông báo. Caller muốn detect drift → dùng ``HmeManager.list_sync``
        (chỉ áp UPDATE cho email DB đã có).
        """
        self._log(
            f"reconcile apple_id={apple_id} no-op "
            f"(DB source-of-truth — refactor B; "
            f"dùng HmeManager.list_sync để cập nhật trạng thái)"
        )
        return 0

    # =====================================================================
    # Inner loop — tạo email tuần tự cho 1 profile
    # =====================================================================

    async def _inner_generate_loop(
        self,
        *,
        account: AppleAccount,
        client: HmeClient,
        label: str,
        note: str | None,
        result: GenerationResult,
        effective_infinite: bool,
        cancellation_event: asyncio.Event | None,
        pause_event: asyncio.Event | None,
        resume_event: asyncio.Event | None,
        on_progress: Callable[[int, int], None] | None,
    ) -> Literal["stop", "switch", "fatal"]:
        """Tạo emails liên tục cho 1 profile.

        Returns:
            'stop' — cancel/bounded done → caller break outer loop.
            'switch' — profile bị mark limited/quota_full/session_expired
                hoặc DB error → caller invalidate cached bundle, pick profile
                khác.
            'fatal' — race retry hết → caller record failure + skip profile
                trong batch (in-memory only).

        Caller (``generate``) MUST handle 3 giá trị này; không return path
        nào khác — Literal annotation enforce ở mypy/runtime check (A4 fix).
        """
        # Check hme_count còn slot không (re-read trong tx mới)
        # MVP: tạo tới khi profile gặp lỗi quota/auth hoặc bounded done.
        while True:
            # Check cancel/pause trước mỗi reserve (R3.21)
            if await self._check_cancel_or_pause(
                cancellation_event, pause_event, resume_event
            ):
                return "stop"

            # Bounded done? (chỉ khi count>=1, KHÔNG cho bounded_drain)
            if result.requested > 0 and result.created >= result.requested:
                return "stop"

            # Re-check quota soft cap (có thể đã vượt do increment)
            current = self._pool_repo.get(account.apple_id)
            if current is None or current.hme_count >= self._hme_quota_limit:
                # mark_quota_full (R3.22)
                if current is not None:
                    self._pool.mark_quota_full(
                        account.apple_id,
                        reason=f"hme_count={current.hme_count}",
                    )
                    self._audit_repo.write(
                        event_type="email_skip_quota_full",
                        apple_id=account.apple_id,
                        payload={
                            "apple_id": account.apple_id,
                            "hme_count": current.hme_count,
                        },
                    )
                return "switch"

            # Audit create_attempt
            self._audit_repo.write(
                event_type="create_attempt",
                apple_id=account.apple_id,
                payload={"apple_id": account.apple_id, "label": label},
            )

            # generate → reserve với race retry (R3.14, R3.15)
            reserved = await self._generate_and_reserve_with_retry(
                account=account,
                client=client,
                label=label,
                note=note,
            )
            if reserved is None:
                # Reserve fail không recover được sau retry — record failure
                # Đã được handle bởi _generate_and_reserve_with_retry (raise)
                return "switch"
            if reserved == "_quota":
                # HmeQuotaError → mark_limited + switch (R3.7)
                self._pool.mark_limited(
                    account.apple_id, reason="HmeQuotaError"
                )
                return "switch"
            if reserved == "_auth":
                # HmeAuthError → mark_session_expired + switch (R3.8, R12.9)
                self._pool.mark_session_expired(
                    account.apple_id, reason="HmeAuthError"
                )
                return "switch"
            if reserved == "_fatal":
                # Fatal error sau race retry hết — return 'fatal' để outer
                # mark_limited (cooldown) tránh infinite loop pick lại profile
                return "fatal"

            # Reserve OK — atomic INSERT email + UPDATE counter + audit (R3.5, R6.3)
            email = reserved.email
            hme_id = reserved.hme_id
            engine = self._pool_repo.engine
            try:
                with engine.transaction() as _conn:
                    self._pool_repo.insert_email(
                        email=email,
                        apple_id=account.apple_id,
                        label=label,
                        note=note,
                        hme_id=hme_id,
                        status="created",
                    )
                    new_count = (
                        self._pool_repo.increment_hme_count_and_set_last_used(
                            account.apple_id, when=_utc_now()
                        )
                    )
                    self._audit_repo.write(
                        event_type="create_success",
                        apple_id=account.apple_id,
                        payload={
                            "email": email,
                            "label": label,
                            "hme_id": hme_id,
                            "hme_count_after": new_count,
                        },
                    )
            except Exception as exc:  # noqa: BLE001
                # DB error sau Apple đã reserve — fatal, record failure
                # nhưng KHÔNG re-call Apple revert (Apple đã chốt slot).
                self._log(
                    f"DB persist failed sau reserve email={email}: {exc}"
                )
                self._audit_repo.write(
                    event_type="create_fail",
                    apple_id=account.apple_id,
                    payload={
                        "email": email,
                        "reason": "db_persist_failed",
                        "error": str(exc),
                    },
                )
                result.failures.append(
                    FailureRecord(
                        apple_id=account.apple_id,
                        error_class=type(exc).__name__,
                        error=str(exc),
                    )
                )
                return "switch"

            result.created += 1
            result.emails.append(email)
            if on_progress is not None:
                try:
                    on_progress(result.created, result.requested)
                except Exception:
                    pass

            # Random delay giữa 2 reserve cùng profile (R3.11)
            delay = random.uniform(self._delay_range[0], self._delay_range[1])
            try:
                await self._sleep(delay)
            except Exception:
                pass

    async def _generate_and_reserve_with_retry(
        self,
        *,
        account: AppleAccount,
        client: HmeClient,
        label: str,
        note: str | None,
    ):
        """generate → reserve với candidate-taken retry (R3.14, R3.15).

        Returns:
            ``ReservedHme`` instance — success.
            ``'_quota'`` — HmeQuotaError → caller mark_limited.
            ``'_auth'`` — HmeAuthError → caller mark_session_expired.
            ``'_fatal'`` — fatal error đã record failure.
        """
        for retry in range(self._race_retry_max + 1):
            try:
                candidate_obj = await client.generate()
                reserved = await client.reserve(
                    candidate_obj.candidate, label, note
                )
                return reserved
            except HmeReserveTaken as exc:
                # Race — retry với candidate mới (R3.14, R3.15) — KHÔNG đếm fail
                self._audit_repo.write(
                    event_type="candidate_retry",
                    apple_id=account.apple_id,
                    payload={
                        "retry": retry + 1,
                        "max_retry": self._race_retry_max,
                        "error": str(exc),
                    },
                )
                if retry >= self._race_retry_max:
                    # Hết retry — coi như profile có vấn đề, mark limited để
                    # round-robin không pick ngay; outer loop sẽ switch.
                    return "_fatal"
                continue
            except HmeQuotaError as exc:
                self._log(f"HmeQuotaError apple_id={account.apple_id}: {exc}")
                self._audit_repo.write(
                    event_type="create_fail",
                    apple_id=account.apple_id,
                    payload={"reason": "rate_limit", "error": str(exc)},
                )
                return "_quota"
            except HmeAuthError as exc:
                self._log(f"HmeAuthError apple_id={account.apple_id}: {exc}")
                self._audit_repo.write(
                    event_type="create_fail",
                    apple_id=account.apple_id,
                    payload={"reason": "session_expired", "error": str(exc)},
                )
                return "_auth"
            except HmeClientError as exc:
                self._log(
                    f"HmeClientError apple_id={account.apple_id}: {exc}"
                )
                self._audit_repo.write(
                    event_type="create_fail",
                    apple_id=account.apple_id,
                    payload={
                        "reason": "client_error",
                        "error_class": type(exc).__name__,
                        "error": str(exc),
                    },
                )
                return "_fatal"

    # =====================================================================
    # Pool_Exhausted_Wait (R3.23, R3.24)
    # =====================================================================

    async def _pool_exhausted_wait(
        self,
        *,
        cancellation_event: asyncio.Event | None,
        pause_event: asyncio.Event | None,
        resume_event: asyncio.Event | None,
    ) -> str:
        """Sleep tới wake_at + check events mỗi 1s. Return action.

        Returns:
            'no_recoverable' — không có profile nào có thể tự recover.
            'cancelled' — cancellation_event set giữa sleep.
            'pause' — pause_event set giữa sleep.
            'timeout' — sleep hết wake_seconds, retry pick.
        """
        # Compute wake_at từ tập profile có thể recover
        wake_at = self._compute_wake_at()
        if wake_at is None:
            self._audit_repo.write(
                event_type="job_failed",
                apple_id=None,
                payload={"reason": "no_recoverable_profile"},
            )
            return "no_recoverable"

        now = _utc_now()
        wake_seconds = max(1, int((wake_at - now).total_seconds()))
        wake_seconds = min(wake_seconds, self._infinite_wait_max_sec)

        self._audit_repo.write(
            event_type="infinite_wait_start",
            apple_id=None,
            payload={
                "wake_at_iso": _format_ts(wake_at),
                "wake_seconds": wake_seconds,
            },
        )

        slept = 0
        woken_by = "timeout"
        while slept < wake_seconds:
            try:
                await self._sleep(1.0)
            except Exception:
                pass
            slept += 1
            if cancellation_event is not None and cancellation_event.is_set():
                woken_by = "cancellation"
                break
            if pause_event is not None and pause_event.is_set():
                woken_by = "pause"
                break

        self._audit_repo.write(
            event_type="infinite_wait_end",
            apple_id=None,
            payload={"slept_seconds": slept, "woken_by": woken_by},
        )

        if woken_by == "cancellation":
            return "cancelled"
        if woken_by == "pause":
            # Pause: outer loop sẽ check pause_event và await resume
            return "pause"
        return "timeout"

    def _compute_wake_at(self) -> datetime | None:
        """Tìm wake_at = min(limited_until, quota_retry_until) từ tập
        profile {limited, quota_full}. None nếu không có.
        """
        accounts = self._pool_repo.list_all()
        candidates: list[datetime] = []
        for acc in accounts:
            if acc.status == "limited" and acc.limited_until is not None:
                candidates.append(acc.limited_until)
            elif (
                acc.status == "quota_full"
                and acc.quota_retry_until is not None
            ):
                candidates.append(acc.quota_retry_until)
        if not candidates:
            return None
        return min(candidates)

    # =====================================================================
    # Cancellation / pause helpers (R3.21)
    # =====================================================================

    async def _check_cancel_or_pause(
        self,
        cancellation_event: asyncio.Event | None,
        pause_event: asyncio.Event | None,
        resume_event: asyncio.Event | None,
    ) -> bool:
        """Check events. Return True nếu cancel; await resume nếu pause.

        Pause logic: nếu pause_event set, audit ``job_paused``, await
        resume_event. Sau resume audit ``job_resumed``.
        """
        if cancellation_event is not None and cancellation_event.is_set():
            return True
        if pause_event is not None and pause_event.is_set():
            self._audit_repo.write(
                event_type="job_paused",
                apple_id=None,
                payload={},
            )
            if resume_event is not None:
                # Wait resume + cancel concurrent
                resume_task = asyncio.create_task(resume_event.wait())
                cancel_task = (
                    asyncio.create_task(cancellation_event.wait())
                    if cancellation_event is not None
                    else None
                )
                tasks = [resume_task]
                if cancel_task is not None:
                    tasks.append(cancel_task)
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for p in pending:
                    p.cancel()
                if cancel_task is not None and cancel_task in done:
                    return True
                # Reset pause_event để cho phép pause lần kế tiếp
                pause_event.clear()
                if resume_event is not None:
                    resume_event.clear()
            self._audit_repo.write(
                event_type="job_resumed",
                apple_id=None,
                payload={},
            )
        return False


__all__ = ["HmeGenerator"]
