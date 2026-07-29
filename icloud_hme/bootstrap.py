"""Bootstrap_Flow — entry point DUY NHẤT chạm UI Apple ID (R12.1, R12.2, R12.10).

Refs:
    requirements.md R12.1, R12.2, R12.10, R12.14, R12.17
    design.md §Components / 1. Bootstrap_Flow
    tasks.md task 14, 14.1

Flow (async):
  1. Validate apple_id (phải là email).
  2. Acquire ``ProfileLock.write_lock(timeout=30)`` TRƯỚC KHI launch Camoufox
     (R12.14). Timeout → ``BootstrapError(reason='profile_locked_by_another_process')``
     + audit ``profile_bootstrap_fail`` rồi raise.
  3. Retry loop tối đa 3 attempt (R12.17): launch Camoufox HEADED →
     navigate ``https://www.icloud.com/mail/`` → đợi user nhấn Enter →
     verify cookies có ÍT NHẤT 1 marker (X-APPLE-WEBAUTH-USER /
     X-APPLE-WEBAUTH-TOKEN / X-APPLE-WEBAUTH-PCS-Mail).
       - Pass → break.
       - Fail → audit ``profile_bootstrap_fail`` với ``attempt`` count, sleep 5s
         giữa các attempt.
       - User gõ 'q' → audit ``profile_bootstrap_fail`` với reason ``user_cancelled``
         và raise ngay (KHÔNG retry).
  4. Sau 3 attempt fail → raise ``BootstrapError(reason='cookie_verify_failed_after_retry')``.
  5. Pass: trong CÙNG 1 outer transaction:
       - ``pool_repo.upsert(apple_id, profile_dir)``.
       - ``pool_repo.update_status(status='active', clear_error=True,
         clear_limited_until=True, clear_quota_retry_until=True)``.
       - ``audit_repo.write(event_type=...)`` với:
           - ``profile_bootstrap`` nếu là lần đầu (account chưa tồn tại HOẶC
             status đang ``active`` lần đầu).
           - ``profile_reactivate`` nếu account từng ở status
             ``session_expired`` / ``disabled`` (R12.10).
  6. Return ``BootstrapResult``.

Bootstrap_Flow là entry point DUY NHẤT chạm UI login Apple ID — KHÔNG bao giờ
được tự động trigger từ HME_Generator / Profile_Checker / HME_Manager.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .exceptions import BootstrapError, ProfileLockError
from .models import BootstrapResult
from .profile_lock import ProfileLock
from .session import ensure_profile_dir, launch_camoufox

if TYPE_CHECKING:
    from db.repositories import AuditLogRepository, IcloudPoolRepository


# Cookie marker — Apple set ÍT NHẤT 1 trong các cookie này sau khi login + 2FA
# pass thành công (R12.2, R12.4). Cần ÍT NHẤT 1 marker để session dùng được
# cho HME API ở các flow sau (Generator/Checker/Manager).
_LOGIN_COOKIE_MARKERS: tuple[str, ...] = (
    "X-APPLE-WEBAUTH-USER",
    "X-APPLE-WEBAUTH-TOKEN",
    "X-APPLE-WEBAUTH-PCS-Mail",
)

# Status enum (R2) — set status đang được coi là "phải reactivate" khi
# bootstrap lại thành công (R12.10). Nếu account đang ``active`` thì
# bootstrap chỉ refresh cookies, audit là ``profile_bootstrap``; nếu account
# đang ``session_expired`` / ``disabled`` (hoặc các status non-active khác)
# → audit ``profile_reactivate``.
_NEEDS_REACTIVATE_STATUSES: frozenset[str] = frozenset(
    {"session_expired", "disabled", "limited", "quota_full"}
)

# Retry policy (R12.17): tối đa 2 retry → 3 attempt. Pause 5 giây giữa các
# attempt để Camoufox / Apple kịp settle nếu lần đầu user nhấn Enter quá sớm.
_MAX_ATTEMPTS = 3
_RETRY_PAUSE_SEC = 5.0

# Profile_Lock timeout (R12.14): 30 giây.
_LOCK_TIMEOUT_SEC = 30.0


def _utc_now() -> datetime:
    """UTC naive datetime — match Timestamp_Format ở repository layer."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _has_login_cookies(ctx) -> tuple[bool, set[str]]:
    """Check cookies ``icloud.com`` trên context. Return (ok, set tên marker hit)."""
    try:
        cookies = await ctx.cookies("https://www.icloud.com/")
    except Exception:
        return False, set()
    names = {c["name"] for c in cookies}
    matched = names & set(_LOGIN_COOKIE_MARKERS)
    return bool(matched), matched


def _blocking_input(prompt: str) -> str:
    """Stdin read trên thread khác (không block event loop). 'q' khi EOF."""
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return "q"


async def _wait_for_enter(prompt: str) -> str:
    return await asyncio.to_thread(_blocking_input, prompt)


def _normalize_apple_id(apple_id: str) -> str:
    """Validate + normalize apple_id (lowercase, strip, must contain '@')."""
    if apple_id is None:
        raise BootstrapError("apple_id rỗng")
    norm = apple_id.strip().lower()
    if "@" not in norm:
        raise BootstrapError(f"apple_id phải là email: {apple_id!r}")
    return norm


async def _attempt_login_once(
    *,
    apple_id: str,
    profile_dir: Path,
    proxy: str | None,
    log,
    attempt: int,
) -> set[str]:
    """1 attempt login flow. Return set marker hit; raise BootstrapError nếu fail.

    Raises:
        BootstrapError: User cancel ('q') hoặc cookies verify fail.
    """
    async with launch_camoufox(
        profile_dir=profile_dir,
        headless=False,  # R12.1, R12.11: HEADED bắt buộc cho Bootstrap_Flow
        proxy=proxy,
    ) as ctx:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto(
                "https://www.icloud.com/mail/", wait_until="domcontentloaded"
            )
        except Exception as exc:
            log(f"navigation warning (attempt {attempt}): {exc}")

        # Hướng dẫn user — in stdout để hiện rõ trên terminal.
        print()
        print("=" * 70)
        print(f" iCloud login flow — {apple_id} (attempt {attempt}/{_MAX_ATTEMPTS})")
        print("=" * 70)
        print(" 1. Camoufox đã mở. Login Apple ID + nhập 2FA tay.")
        print(" 2. Đợi cho đến khi vào được iCloud Mail (hiển thị inbox).")
        print(" 3. Quay lại terminal nhấn Enter để LƯU profile.")
        print("    Hoặc gõ 'q' + Enter để HỦY (không lưu).")
        print("=" * 70)

        answer = await _wait_for_enter(
            f"\n[bootstrap] Enter sau khi đã login xong (q để hủy): "
        )
        if answer == "q":
            raise BootstrapError(f"user_cancelled apple_id={apple_id}")

        ok, matched = await _has_login_cookies(ctx)
        if not ok:
            raise BootstrapError(
                f"cookie_verify_failed apple_id={apple_id} "
                f"expected_any={list(_LOGIN_COOKIE_MARKERS)}"
            )

        log(f"login OK attempt={attempt} cookies={sorted(matched)}")
        # Đợi 2s cho browser flush state vào profile_dir trước khi đóng.
        await asyncio.sleep(2.0)
        return matched


def _persist_bootstrap_atomic(
    *,
    pool_repo: "IcloudPoolRepository",
    audit_repo: "AuditLogRepository",
    apple_id: str,
    profile_dir: Path,
    matched_cookies: list[str],
    log,
) -> str:
    """Atomic outer-tx: upsert + reset status='active' + audit. Return event_type ghi.

    Single outer ``engine.transaction()`` reentrant — repository nested call
    sẽ reuse cùng transaction (R6.3). Decision ``profile_bootstrap`` vs
    ``profile_reactivate`` dựa trên status hiện tại của account (R12.10).
    """
    engine = pool_repo.engine

    # Đọc state cũ NGOÀI tx ghi để decide event_type. Đọc lại trong tx có thể
    # block do BEGIN IMMEDIATE — đọc trước, decision đơn giản, không race vì
    # bootstrap acquire write_lock per apple_id (R12.14).
    existing = pool_repo.get(apple_id)
    if existing is None:
        event_type = "profile_bootstrap"
    elif existing.status in _NEEDS_REACTIVATE_STATUSES:
        event_type = "profile_reactivate"
    else:
        # active / deleted → coi như bootstrap refresh cookies bình thường.
        # 'deleted' không nên xảy ra vì bootstrap không re-create row deleted,
        # nhưng nếu user cố tình bootstrap lại profile đã delete thì coi như
        # bootstrap mới.
        event_type = "profile_bootstrap"

    payload = {
        "apple_id": apple_id,
        "profile_dir": str(profile_dir),
        "matched_cookies": sorted(matched_cookies),
        "previous_status": existing.status if existing else None,
    }

    with engine.transaction() as _conn:
        # 1. upsert profile_dir (insert nếu mới, update profile_dir nếu cũ).
        pool_repo.upsert(apple_id, profile_dir)
        # 2. reset status='active' + clear flags (R12.10).
        pool_repo.update_status(
            apple_id,
            status="active",
            clear_error=True,
            clear_limited_until=True,
            clear_quota_retry_until=True,
        )
        # 3. audit ghi cùng tx (R6.3).
        audit_repo.write(
            event_type=event_type,
            apple_id=apple_id,
            payload=payload,
        )

    log(f"DB persisted apple_id={apple_id} audit_event={event_type}")
    return event_type


def _audit_failure(
    *,
    audit_repo: "AuditLogRepository",
    apple_id: str,
    attempt: int,
    reason: str,
) -> None:
    """Best-effort: ghi audit ``profile_bootstrap_fail``; nuốt mọi lỗi DB.

    Không re-raise vì failure path cần ưu tiên báo lỗi gốc cho caller
    (BootstrapError); audit fail không nên làm mất context lỗi gốc.
    """
    try:
        audit_repo.write(
            event_type="profile_bootstrap_fail",
            apple_id=apple_id,
            payload={"apple_id": apple_id, "attempt": attempt, "reason": reason},
        )
    except Exception:
        pass


async def bootstrap(
    apple_id: str,
    *,
    runtime_dir: Path,
    pool_repo: "IcloudPoolRepository",
    audit_repo: "AuditLogRepository",
    proxy: str | None = None,
    log,
) -> BootstrapResult:
    """Bootstrap 1 Apple_ID — entry point duy nhất chạm UI login.

    Args:
        apple_id: Apple ID (email).
        runtime_dir: Runtime root, profile sẽ là
            ``runtime_dir/icloud_profiles/<safe_apple_id>/``.
        pool_repo: ``IcloudPoolRepository`` để upsert + reset status.
        audit_repo: ``AuditLogRepository`` để ghi audit event trong cùng tx.
        proxy: Proxy URL optional (vd ``http://user:pass@host:port``).
        log: Callable ``(msg: str) -> None`` cho logging.

    Returns:
        ``BootstrapResult`` — apple_id, profile_dir, status='active',
        matched_cookies, bootstrapped_at.

    Raises:
        BootstrapError: User huỷ, hoặc cookies verify fail sau 3 attempt,
            hoặc profile_dir đã bị process khác giữ write lock (R12.14).
    """
    apple_id_norm = _normalize_apple_id(apple_id)
    profile_dir = ensure_profile_dir(runtime_dir, apple_id_norm)
    lock_dir = profile_dir / ".lock"

    log(f"apple_id={apple_id_norm}")
    log(f"profile_dir={profile_dir}")

    profile_lock = ProfileLock(lock_dir, apple_id_norm)

    # ── Acquire write lock TRƯỚC KHI launch Camoufox (R12.14). ─────────────
    # Pattern: ExitStack thay manual __enter__/__exit__ (A3 fix). ExitStack
    # đảm bảo lock release đúng cả khi exception xảy ra giữa acquire và
    # finally block (vd asyncio.CancelledError, KeyboardInterrupt). Manual
    # pattern cũ dễ bị skip __exit__ nếu future code thêm raise giữa
    # write_lock(...) và __enter__().
    with ExitStack() as stack:
        try:
            stack.enter_context(
                profile_lock.write_lock(timeout=_LOCK_TIMEOUT_SEC)
            )
        except ProfileLockError as exc:
            _audit_failure(
                audit_repo=audit_repo,
                apple_id=apple_id_norm,
                attempt=0,
                reason="profile_locked_by_another_process",
            )
            raise BootstrapError(
                f"profile_locked_by_another_process apple_id={apple_id_norm} "
                f"detail={exc}"
            ) from exc

        # ── Retry loop (R12.17): max 3 attempt, pause 5s giữa các attempt. ───
        last_error: BootstrapError | None = None
        matched_set: set[str] = set()

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                matched_set = await _attempt_login_once(
                    apple_id=apple_id_norm,
                    profile_dir=profile_dir,
                    proxy=proxy,
                    log=log,
                    attempt=attempt,
                )
                last_error = None
                break  # success
            except BootstrapError as exc:
                last_error = exc
                msg = str(exc)
                # User cancel → KHÔNG retry, raise ngay (R12.17 chỉ áp dụng cho
                # cookie verify fail, không cho user cancel).
                if "user_cancelled" in msg:
                    _audit_failure(
                        audit_repo=audit_repo,
                        apple_id=apple_id_norm,
                        attempt=attempt,
                        reason="user_cancelled",
                    )
                    raise

                # Cookie verify fail → audit + retry nếu còn attempt.
                _audit_failure(
                    audit_repo=audit_repo,
                    apple_id=apple_id_norm,
                    attempt=attempt,
                    reason="cookie_verify_failed",
                )
                log(f"attempt {attempt}/{_MAX_ATTEMPTS} fail: {exc}")
                if attempt < _MAX_ATTEMPTS:
                    log(f"retrying in {_RETRY_PAUSE_SEC}s...")
                    await asyncio.sleep(_RETRY_PAUSE_SEC)

        if last_error is not None:
            # Đã hết 3 attempt mà vẫn fail.
            _audit_failure(
                audit_repo=audit_repo,
                apple_id=apple_id_norm,
                attempt=_MAX_ATTEMPTS,
                reason="cookie_verify_failed_after_retry",
            )
            raise BootstrapError(
                f"cookie_verify_failed_after_retry apple_id={apple_id_norm} "
                f"attempts={_MAX_ATTEMPTS} last_error={last_error}"
            )

        # ── Pass: persist atomic (upsert + reset status + audit). ────────────
        matched_sorted = sorted(matched_set)
        bootstrapped_at = _utc_now()
        _persist_bootstrap_atomic(
            pool_repo=pool_repo,
            audit_repo=audit_repo,
            apple_id=apple_id_norm,
            profile_dir=profile_dir,
            matched_cookies=matched_sorted,
            log=log,
        )

        return BootstrapResult(
            apple_id=apple_id_norm,
            profile_dir=profile_dir,
            status="active",
            matched_cookies=matched_sorted,
            bootstrapped_at=bootstrapped_at,
        )


__all__ = ["BootstrapError", "bootstrap"]
