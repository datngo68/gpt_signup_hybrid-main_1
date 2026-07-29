"""Pool_Manager — quản lý vòng đời iCloud_Profile (R2, R5, R7, R12.10).

Refs:
    requirements.md R2.1–R2.15, R5.1–R5.7, R7.1–R7.5, R12.10
    design.md §Components / 4. Pool_Manager
    tasks.md task 13

Class ``IcloudPoolManager`` wrap ``IcloudPoolRepository`` + áp luật pick /
transition state. Mọi mutation đi cùng audit trong CÙNG outer tx
(``engine.transaction()``) qua ``AuditLogRepository.write()`` — đảm bảo state
+ audit atomic (R6.3, R3.5).

Pick semantics (R2.2 + R2.15):
  - SELECT trong 1 ``engine.transaction(immediate=True)`` (BEGIN IMMEDIATE
    write-lock ngay từ đầu) — 2 process song song serialize qua write-lock
    (Cursor_Atomic_Pick).
  - Eligible = ``status='active'`` HOẶC (``status='limited'`` AND
    ``now>=limited_until``) HOẶC (``status='quota_full'`` AND
    ``now>=quota_retry_until`` AND ``hme_count<HME_QUOTA_LIMIT``).
  - ORDER BY (apple_id > round_robin_cursor) DESC, apple_id ASC, LIMIT 1
    — round-robin dựa cursor lưu ở ``pool_state.round_robin_cursor`` (R2.3).
  - Tự transition limited→active (audit ``limited_retry``) hoặc
    quota_full→active (audit ``quota_retry``) lúc pick.
  - SQLite ``database is locked`` (timeout 5s) → audit ``pool_pick_locked``
    + raise ``IcloudPoolError(message='pool_pick_locked')``.

Pool_Manager KHÔNG check ``hme_count`` ở pick (R2.2) — Generator post-pick
check (R3.22). Pool chỉ filter theo status enum, giảm coupling.
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .exceptions import IcloudPoolError
from .models import (
    AppleAccount,
    PoolStatusReport,
    ProfileDeleteResult,
    ProfileSnapshot,
)

if TYPE_CHECKING:
    from db.repositories import AuditLogRepository, IcloudPoolRepository


# Status enum (R2). Lifecycle:
#   active ↔ limited ↔ quota_full ↔ session_expired/disabled
#   * → deleted (terminal, profile_dir=NULL)
_ALL_STATUSES: tuple[str, ...] = (
    "active",
    "limited",
    "quota_full",
    "session_expired",
    "disabled",
    "deleted",
)

# Default low_capacity threshold (R7.4): khi total_quota_remaining < threshold
# → flag low_capacity=True. Override qua constructor.
_DEFAULT_LOW_CAPACITY_THRESHOLD: int = 50


def _utc_now() -> datetime:
    """UTC naive datetime — match Timestamp_Format (Property 30)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class IcloudPoolManager:
    """Pool state machine + round-robin pick (R2, R5, R7).

    Args:
        pool_repo: ``IcloudPoolRepository`` cho icloud_accounts/_emails/pool_state.
        audit_repo: ``AuditLogRepository`` ghi audit trong cùng tx (R6.3).
        limited_ttl_hours: Limited_TTL (R2.9). Default 24 (env
            ``ICLOUD_LIMITED_TTL_HOURS``).
        quota_retry_minutes: Quota_Retry_TTL (R2.13). Default 15 (env
            ``ICLOUD_QUOTA_RETRY_MINUTES``).
        hme_quota_limit: HME_QUOTA_LIMIT (R2.14). Default 700 (env
            ``ICLOUD_HME_QUOTA_LIMIT``).
        low_capacity_threshold: total_quota_remaining < threshold → flag
            low_capacity=True (R7.4). Default 50.
        log: Optional callable ``(msg: str) -> None`` — pass None để silent.
    """

    def __init__(
        self,
        pool_repo: "IcloudPoolRepository",
        audit_repo: "AuditLogRepository",
        *,
        limited_ttl_hours: int = 24,
        quota_retry_minutes: int = 15,
        hme_quota_limit: int = 700,
        low_capacity_threshold: int = _DEFAULT_LOW_CAPACITY_THRESHOLD,
        log=None,
    ) -> None:
        if limited_ttl_hours <= 0:
            raise ValueError(f"limited_ttl_hours phải > 0, got {limited_ttl_hours}")
        if quota_retry_minutes <= 0:
            raise ValueError(
                f"quota_retry_minutes phải > 0, got {quota_retry_minutes}"
            )
        if hme_quota_limit <= 0:
            raise ValueError(f"hme_quota_limit phải > 0, got {hme_quota_limit}")
        if low_capacity_threshold < 0:
            raise ValueError(
                f"low_capacity_threshold phải >= 0, got {low_capacity_threshold}"
            )
        self._pool_repo = pool_repo
        self._audit_repo = audit_repo
        self._limited_ttl = timedelta(hours=limited_ttl_hours)
        self._quota_retry_ttl = timedelta(minutes=quota_retry_minutes)
        self._hme_quota_limit = hme_quota_limit
        self._low_capacity_threshold = low_capacity_threshold
        self._log = log if log is not None else (lambda _msg: None)

    # =====================================================================
    # pick_active_profile (R2.2, R2.15) — atomic SELECT + UPDATE cursor
    # =====================================================================

    def pick_active_profile(self) -> AppleAccount:
        """Round-robin pick eligible profile, atomic via BEGIN IMMEDIATE.

        Eligibility (R2.2):
          - status='active'
          - status='limited' AND now>=limited_until → tự transition active
            + audit ``limited_retry`` (R2.7)
          - status='quota_full' AND now>=quota_retry_until AND
            hme_count<HME_QUOTA_LIMIT → tự transition active + audit
            ``quota_retry`` (R2.12)

        Round-robin: ORDER BY ``(apple_id > round_robin_cursor) DESC,
        apple_id ASC`` LIMIT 1.

        Atomic SELECT + UPDATE cursor + transition trong CÙNG
        ``engine.transaction(immediate=True)`` — 2 process song song serialize
        qua write-lock (R2.15, Cursor_Atomic_Pick).

        Raises:
            IcloudPoolError: Pool exhausted (no eligible) hoặc
                'database is locked' (timeout 5s) → audit
                ``pool_pick_locked`` + raise.
        """
        engine = self._pool_repo.engine
        now = _utc_now()
        wait_start = time.monotonic()

        # pool_exhausted_info được set bên trong tx khi không pick được;
        # raise SAU khi exit `with` để các UPDATE side-effect (extend
        # quota_retry_until cho R2.12 sub-B) được commit thay vì rollback.
        pool_exhausted_info: dict | None = None

        try:
            with engine.transaction(immediate=True) as conn:
                cursor_str = self._pool_repo.read_round_robin_cursor()
                cursor_value = cursor_str if cursor_str is not None else ""

                # SELECT eligible: status='active' OR (limited AND limited_until <= now)
                # OR (quota_full AND quota_retry_until <= now AND hme_count < limit).
                # Lưu ý: ORDER BY (apple_id > cursor) DESC để row > cursor xếp trước
                # row <= cursor → wrap-around tự nhiên (R2.3).
                now_iso = _format_ts(now)
                row = conn.execute(
                    """
                    SELECT apple_id, profile_dir, status, hme_count,
                           limited_until, quota_retry_until,
                           last_used_at, last_error
                    FROM icloud_accounts
                    WHERE
                        status = 'active'
                        OR (status = 'limited' AND limited_until IS NOT NULL
                            AND limited_until <= ?)
                        OR (status = 'quota_full'
                            AND quota_retry_until IS NOT NULL
                            AND quota_retry_until <= ?
                            AND hme_count < ?)
                    ORDER BY
                        (apple_id > ?) DESC,
                        apple_id ASC
                    LIMIT 1
                    """,
                    (now_iso, now_iso, self._hme_quota_limit, cursor_value),
                ).fetchone()

                if row is None:
                    # R2.12 sub-B (stay quota_full + extend retry):
                    # Profile có status='quota_full' đã đến hạn retry nhưng
                    # `hme_count >= HME_QUOTA_LIMIT` → SQL eligible filter loại
                    # ra (không match), Pool sắp raise pool_exhausted. Trước khi
                    # raise, extend `quota_retry_until = now + Quota_Retry_TTL`
                    # cho mọi row dạng này, đúng spec R2.12 sub-B (re-check
                    # hme_count tại thời điểm transition; nếu vẫn vượt cap thì
                    # set lại quota_full với retry_until mới).
                    new_quota_retry_until = now + self._quota_retry_ttl
                    new_qru_iso = _format_ts(new_quota_retry_until)
                    cur = conn.execute(
                        """
                        UPDATE icloud_accounts
                        SET quota_retry_until = ?
                        WHERE status = 'quota_full'
                          AND quota_retry_until IS NOT NULL
                          AND quota_retry_until <= ?
                          AND hme_count >= ?
                        """,
                        (new_qru_iso, now_iso, self._hme_quota_limit),
                    )
                    extended_count = cur.rowcount if cur.rowcount >= 0 else 0
                    # Pool_Exhausted: stash info + exit tx normally để
                    # COMMIT UPDATE; raise ngoài `with` (xem block sau).
                    by_status = self._count_by_status_in_tx(conn)
                    self._log(
                        f"pool exhausted: by_status={by_status} "
                        f"cursor={cursor_value!r} "
                        f"quota_retry_extended={extended_count}"
                    )
                    pool_exhausted_info = {
                        "by_status": by_status,
                        "cursor": cursor_value,
                        "quota_retry_extended": extended_count,
                        "new_quota_retry_until": new_qru_iso,
                    }
                    # `return` đi qua context manager → COMMIT.
                    # Không thể `return` (function phải raise/return AppleAccount);
                    # dùng pattern: break-out bằng cách rơi xuống ngoài `with`.
                else:
                    return self._handle_pick_row(conn, row, now)

            # Exit `with` không exception → tx COMMIT (UPDATE extend quota_retry_until
            # ở trên đã được persist). Audit ngoài tx (write tự open tx con).
            # Raise Pool_Exhausted ngoài tx.
            assert pool_exhausted_info is not None
            extended = pool_exhausted_info.get("quota_retry_extended", 0)
            if extended > 0:
                # A7 fix: ghi audit khi extend retry để observability không gap.
                # Spec R2.12 chỉ require audit transition→active; ở đây stay
                # quota_full nên dùng event_type 'quota_retry_extended' (alias
                # backward-compat của writable set).
                try:
                    self._audit_repo.write(
                        event_type="quota_retry",
                        apple_id=None,  # batch event — applies to N rows
                        payload={
                            "extended_count": extended,
                            "new_quota_retry_until": pool_exhausted_info.get(
                                "new_quota_retry_until"
                            ),
                            "reason": "still_full_at_retry",
                        },
                    )
                except Exception as exc:  # noqa: BLE001 — audit fail không break pick
                    self._log(f"audit quota_retry batch fail: {exc}")
            raise IcloudPoolError(
                f"pool_exhausted: by_status={pool_exhausted_info['by_status']}"
            )

        except IcloudPoolError:
            raise
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "database is locked" in msg or "busy" in msg:
                wait_ms = int((time.monotonic() - wait_start) * 1000)
                # Audit ngoài tx vì tx vừa fail.
                try:
                    self._audit_repo.write(
                        event_type="pool_pick_locked",
                        apple_id=None,
                        payload={"wait_ms": wait_ms},
                    )
                except Exception:
                    pass
                self._log(f"pool_pick_locked wait_ms={wait_ms}")
                raise IcloudPoolError("pool_pick_locked") from exc
            raise

    def _handle_pick_row(
        self, conn, row, now: datetime
    ) -> AppleAccount:
        """Auto-transition row vừa SELECT (limited/quota_full → active),
        update round_robin_cursor, build ``AppleAccount`` trả về.

        Tách helper để pick_active_profile dễ đọc + giữ tx scope ngắn (helper
        chạy trong cùng tx của caller — caller giữ ``with engine.transaction``).
        """
        old_status = row["status"]
        if old_status == "limited":
            conn.execute(
                "UPDATE icloud_accounts SET status='active', "
                "limited_until=NULL WHERE apple_id = ?",
                (row["apple_id"],),
            )
            self._audit_repo.write(
                event_type="limited_retry",
                apple_id=row["apple_id"],
                payload={
                    "apple_id": row["apple_id"],
                    "previous_status": "limited",
                    "transitioned_at": _format_ts(now),
                },
            )
        elif old_status == "quota_full":
            conn.execute(
                "UPDATE icloud_accounts SET status='active', "
                "quota_retry_until=NULL WHERE apple_id = ?",
                (row["apple_id"],),
            )
            self._audit_repo.write(
                event_type="quota_retry",
                apple_id=row["apple_id"],
                payload={
                    "apple_id": row["apple_id"],
                    "hme_count": row["hme_count"],
                    "previous_status": "quota_full",
                    "transitioned_at": _format_ts(now),
                },
            )

        # Update cursor — best-effort, R2.3 cho phép cursor lệch (caller
        # vẫn nhận profile đã pick). Audit ``cursor_update_failed`` nếu lỗi.
        try:
            self._pool_repo.write_round_robin_cursor(row["apple_id"])
        except Exception as exc:  # noqa: BLE001
            self._audit_repo.write(
                event_type="cursor_update_failed",
                apple_id=row["apple_id"],
                payload={"error": str(exc)},
            )
            self._log(f"cursor_update_failed: {exc}")

        # Build AppleAccount (transitioned status nếu vừa transition)
        effective_limited_until = (
            None if old_status == "limited" else _parse_ts(row["limited_until"])
        )
        effective_quota_retry_until = (
            None
            if old_status == "quota_full"
            else _parse_ts(row["quota_retry_until"])
        )
        profile_dir = (
            Path(row["profile_dir"]) if row["profile_dir"] else None
        )
        return AppleAccount(
            apple_id=row["apple_id"],
            profile_dir=profile_dir,
            status="active",
            hme_count=row["hme_count"],
            limited_until=effective_limited_until,
            quota_retry_until=effective_quota_retry_until,
            last_used_at=_parse_ts(row["last_used_at"]),
            last_error=row["last_error"],
        )

    # =====================================================================
    # Mark transitions (R2.5, R2.8, R2.10) — mỗi method 1 tx + audit
    # =====================================================================

    def mark_limited(self, apple_id: str, *, reason: str) -> None:
        """status='limited', limited_until=now+Limited_TTL, audit mark_limited."""
        engine = self._pool_repo.engine
        now = _utc_now()
        limited_until = now + self._limited_ttl
        with engine.transaction() as _conn:
            self._pool_repo.update_status(
                apple_id,
                status="limited",
                limited_until=limited_until,
                last_error=reason,
            )
            self._audit_repo.write(
                event_type="mark_limited",
                apple_id=apple_id,
                payload={
                    "apple_id": apple_id,
                    "reason": reason,
                    "limited_until": _format_ts(limited_until),
                },
            )

    def mark_session_expired(self, apple_id: str, *, reason: str) -> None:
        """status='session_expired', last_error=reason, audit."""
        engine = self._pool_repo.engine
        with engine.transaction() as _conn:
            self._pool_repo.update_status(
                apple_id,
                status="session_expired",
                last_error=reason,
            )
            self._audit_repo.write(
                event_type="mark_session_expired",
                apple_id=apple_id,
                payload={"apple_id": apple_id, "reason": reason},
            )

    def mark_disabled(self, apple_id: str, *, reason: str) -> None:
        """status='disabled', last_error=reason, audit mark_disabled."""
        engine = self._pool_repo.engine
        with engine.transaction() as _conn:
            self._pool_repo.update_status(
                apple_id,
                status="disabled",
                last_error=reason,
            )
            self._audit_repo.write(
                event_type="mark_disabled",
                apple_id=apple_id,
                payload={"apple_id": apple_id, "reason": reason},
            )

    def mark_quota_full(self, apple_id: str, *, reason: str) -> None:
        """status='quota_full', quota_retry_until=now+Quota_Retry_TTL,
        audit mark_quota_full với payload {hme_count, quota_retry_until} (R2.10)."""
        engine = self._pool_repo.engine
        now = _utc_now()
        retry_until = now + self._quota_retry_ttl
        with engine.transaction() as _conn:
            account = self._pool_repo.get(apple_id)
            hme_count = account.hme_count if account is not None else 0
            self._pool_repo.update_status(
                apple_id,
                status="quota_full",
                quota_retry_until=retry_until,
                last_error=reason,
            )
            self._audit_repo.write(
                event_type="mark_quota_full",
                apple_id=apple_id,
                payload={
                    "apple_id": apple_id,
                    "hme_count": hme_count,
                    "quota_retry_until": _format_ts(retry_until),
                    "reason": reason,
                },
            )

    def reactivate(self, apple_id: str) -> None:
        """status='active', clear last_error + limited_until + quota_retry_until,
        audit profile_reactivate (R12.10)."""
        engine = self._pool_repo.engine
        with engine.transaction() as _conn:
            existing = self._pool_repo.get(apple_id)
            previous_status = existing.status if existing else None
            self._pool_repo.update_status(
                apple_id,
                status="active",
                clear_error=True,
                clear_limited_until=True,
                clear_quota_retry_until=True,
            )
            self._audit_repo.write(
                event_type="profile_reactivate",
                apple_id=apple_id,
                payload={
                    "apple_id": apple_id,
                    "previous_status": previous_status,
                },
            )

    # =====================================================================
    # delete_profile (R5)
    # =====================================================================

    def delete_profile(self, apple_id: str) -> ProfileDeleteResult:
        """Xoá profile_dir trên disk + status='deleted' + profile_dir=NULL.

        Preserve icloud_emails (R5). Audit profile_delete hoặc
        profile_delete_fail cho mọi failure path.

        Returns:
            ProfileDeleteResult với deleted/profile_dir_removed/reason.
        """
        engine = self._pool_repo.engine
        existing = self._pool_repo.get(apple_id)

        if existing is None:
            self._audit_repo.write(
                event_type="profile_delete_fail",
                apple_id=apple_id,
                payload={"apple_id": apple_id, "reason": "apple_id_not_found"},
            )
            return ProfileDeleteResult(
                apple_id=apple_id,
                deleted=False,
                profile_dir_removed=False,
                hme_count_at_delete=0,
                reason="apple_id_not_found",
            )

        if existing.status == "deleted":
            self._audit_repo.write(
                event_type="profile_delete_fail",
                apple_id=apple_id,
                payload={"apple_id": apple_id, "reason": "already_deleted"},
            )
            return ProfileDeleteResult(
                apple_id=apple_id,
                deleted=False,
                profile_dir_removed=False,
                hme_count_at_delete=existing.hme_count,
                reason="already_deleted",
            )

        # 2-phase delete (A18 fix — refactor B):
        #   1. DB tx FIRST: UPDATE status='deleted' + audit. Nếu DB fail
        #      (lock, disk full DB-side, etc.) → raise, KHÔNG xoá disk.
        #      Profile_dir còn nguyên cho retry.
        #   2. DB OK → rmtree disk. Disk error chỉ log, KHÔNG raise vì DB
        #      đã ghi deleted (state đã commit). Audit secondary event với
        #      reason='disk_cleanup_fail' để admin xử lý orphan dir tay.
        #
        # Thứ tự cũ (rmtree trước, DB sau): nếu SIGKILL giữa 2 step →
        # profile_dir mất, DB vẫn active → pool tiếp tục pick → extract
        # fail → mark_session_expired (tự recover, nhưng tốn 1 cycle wasted).
        profile_dir = existing.profile_dir
        with engine.transaction() as conn:
            conn.execute(
                "UPDATE icloud_accounts SET status='deleted', profile_dir=NULL "
                "WHERE apple_id = ?",
                (apple_id,),
            )
            self._audit_repo.write(
                event_type="profile_delete",
                apple_id=apple_id,
                payload={
                    "apple_id": apple_id,
                    "hme_count_at_delete": existing.hme_count,
                    "profile_dir_removed": False,  # update sau rmtree
                },
            )

        # Phase 2: rmtree best-effort. Disk error → audit secondary event,
        # KHÔNG raise (DB đã commit, không revert được).
        dir_removed = False
        disk_error: str | None = None
        if profile_dir is not None and profile_dir.exists():
            try:
                shutil.rmtree(profile_dir)
                dir_removed = True
            except Exception as exc:  # noqa: BLE001
                disk_error = f"{type(exc).__name__}: {exc}"
                self._log(
                    f"delete_profile disk error apple_id={apple_id}: {exc} "
                    f"(DB đã commit, profile_dir còn lại trên disk — admin xoá tay)"
                )
                try:
                    self._audit_repo.write(
                        event_type="profile_delete_fail",
                        apple_id=apple_id,
                        payload={
                            "apple_id": apple_id,
                            "reason": "disk_cleanup_fail",
                            "error": disk_error,
                            "profile_dir": str(profile_dir),
                        },
                    )
                except Exception as audit_exc:  # noqa: BLE001
                    self._log(
                        f"delete_profile audit secondary fail: {audit_exc}"
                    )

        return ProfileDeleteResult(
            apple_id=apple_id,
            deleted=True,
            profile_dir_removed=dir_removed,
            hme_count_at_delete=existing.hme_count,
            reason="disk_error" if disk_error else None,
        )

    # =====================================================================
    # status_report (R7)
    # =====================================================================

    def status_report(self) -> PoolStatusReport:
        """Aggregate report: by_status, profiles, emails_by_status,
        quota_remaining, low_capacity, quota_full_count (R7.1, R7.5)."""
        accounts = self._pool_repo.list_all()
        by_status: dict[str, int] = {s: 0 for s in _ALL_STATUSES}
        profiles: list[ProfileSnapshot] = []
        total_quota_remaining = 0
        quota_full_count = 0
        quota_full_profiles: list[dict] = []

        for acc in accounts:
            by_status[acc.status] = by_status.get(acc.status, 0) + 1
            quota_remaining = max(0, self._hme_quota_limit - acc.hme_count)
            # quota_remaining đếm cho các status còn dùng được.
            if acc.status in ("active", "limited", "quota_full"):
                total_quota_remaining += quota_remaining
            if acc.status == "quota_full":
                quota_full_count += 1
                quota_full_profiles.append(
                    {
                        "apple_id": acc.apple_id,
                        "hme_count": acc.hme_count,
                        "quota_retry_until": _format_ts(acc.quota_retry_until)
                        if acc.quota_retry_until
                        else None,
                    }
                )
            profiles.append(
                ProfileSnapshot(
                    apple_id=acc.apple_id,
                    status=acc.status,
                    hme_count=acc.hme_count,
                    quota_remaining=quota_remaining,
                    last_used_at=acc.last_used_at,
                    limited_until=acc.limited_until,
                    quota_retry_until=acc.quota_retry_until,
                    last_error=acc.last_error,
                )
            )

        # emails_by_status — đọc raw từ icloud_emails.
        emails_by_status = self._count_emails_by_status()

        low_capacity = total_quota_remaining < self._low_capacity_threshold

        return PoolStatusReport(
            by_status=by_status,
            profiles=profiles,
            emails_by_status=emails_by_status,
            quota_soft_cap_per_account=self._hme_quota_limit,
            total_quota_remaining=total_quota_remaining,
            low_capacity=low_capacity,
            quota_full_count=quota_full_count,
            quota_full_profiles=quota_full_profiles,
        )

    # =====================================================================
    # Helpers
    # =====================================================================

    def _count_by_status_in_tx(self, conn) -> dict[str, int]:
        """Count icloud_accounts theo status — chạy trong tx hiện hành."""
        rows = conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM icloud_accounts GROUP BY status"
        ).fetchall()
        result = {s: 0 for s in _ALL_STATUSES}
        for row in rows:
            result[row["status"]] = row["cnt"]
        return result

    def _count_emails_by_status(self) -> dict[str, int]:
        """Count icloud_emails theo status (R7 emails_by_status).

        Read-only single statement — KHÔNG wrap engine.transaction() vì:
            1. SQLite WAL mode đảm bảo read consistency cho 1 query.
            2. Wrap tx chỉ phục vụ multi-statement atomicity, ở đây 1 SELECT
               nên không thêm value, chỉ thêm overhead BEGIN/COMMIT.
            3. status_report() là observability path, fail-soft acceptable.

        Pattern cố tình khác `_count_by_status` (icloud_accounts) vì cả 2
        đều single-stmt read; không gọi từ trong tx khác → an toàn.
        """
        conn = self._pool_repo.engine.raw_connection()
        rows = conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM icloud_emails GROUP BY status"
        ).fetchall()
        # Initialize với 7 status enum v6 để caller có shape ổn định.
        result = {
            "created": 0,
            "reconciled": 0,
            "deactivated": 0,
            "revoked": 0,
            "deleted": 0,
            "disabled": 0,
            "used_for_chatgpt": 0,
        }
        for row in rows:
            result[row["status"]] = row["cnt"]
        return result


# ---------------------------------------------------------------------------
# Timestamp helpers — match Property 30 / Timestamp_Format
# (đồng bộ với db/repositories.py:_dt_to_iso / _iso_to_dt nhưng không tạo
# circular dependency vào repository ngoài việc dùng nó qua DI).
# ---------------------------------------------------------------------------

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _format_ts(value: "datetime | None") -> str | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime(_ISO_FORMAT)


def _parse_ts(value) -> "datetime | None":
    if value is None:
        return None
    raw = value if isinstance(value, str) else str(value)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


__all__ = ["IcloudPoolManager"]
