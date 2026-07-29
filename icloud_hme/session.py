"""Camoufox session launcher cho iCloud HME.

Mỗi Apple ID dùng 1 thư mục profile cố định (persistent_context). Camoufox
sẽ:
  - Lưu cookies + IndexedDB + localStorage qua các session.
  - Generate 1 fingerprint cố định cho profile (saved trong profile dir).

Profile path: <runtime_dir>/icloud_profiles/<apple_id_safe>/

Module cũng expose ``extract_session_bundle`` — entry point chính cho
HME_Generator / Profile_Checker / HME_Manager để lấy ``SessionBundle``
in-memory từ profile_dir mà không thao tác UI (R12.3-R12.7, R12.11, R12.15).

Architectural note (refactor B — May 2026):
    Apple webapp đã gỡ ``window.webAuth`` global khỏi ``icloud.com``. Empirical
    test (``test/check_session_extract_diagnose.py``) cho thấy
    ``page.evaluate('window.webAuth')`` luôn ``undefined`` cả ở
    ``/settings/`` lẫn ``/mail/`` sau 10s settle. Trong khi đó cookies
    ``X-APPLE-WEBAUTH-*`` vẫn được flush đầy đủ vào profile_dir.

    HME REST API (verified rtunazzz/hidemyemail-generator + nội bộ
    ``test/check_hme_minimal_call.py``) chỉ require:
      - cookies ``X-APPLE-WEBAUTH-*`` (header ``Cookie:``);
      - host cố định ``p68-maildomainws.icloud.com``;
      - ``dsid`` / ``clientId`` query param có thể rỗng.

    Vì vậy ``SessionBundle`` mới chỉ chứa ``cookies`` + ``apple_id`` +
    ``extracted_at``. Validate đơn giản: cookies non-empty + có ÍT NHẤT 1
    marker login (``X-APPLE-WEBAUTH-USER`` / ``-TOKEN`` / ``-PCS-Mail``).
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from .exceptions import ProfileLockError, SessionExtractError
from .models import SessionBundle
from .profile_lock import ProfileLock

# Marker chứng minh profile đã login + 2FA xong. Cần ÍT NHẤT 1 — Apple một
# số account chỉ set USER+TOKEN, không set PCS-Mail (Advanced Data Protection
# / region). Khớp logic ``_LOGIN_COOKIE_MARKERS`` ở ``add_profile.py`` /
# ``open_profile.py`` / ``bootstrap.py``.
_LOGIN_COOKIE_MARKERS: tuple[str, ...] = (
    "X-APPLE-WEBAUTH-USER",
    "X-APPLE-WEBAUTH-TOKEN",
    "X-APPLE-WEBAUTH-PCS-Mail",
)


def _safe_apple_id(apple_id: str) -> str:
    """Apple ID là email → sanitize cho dùng làm directory name."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", apple_id.strip().lower())
    if not safe:
        raise ValueError(f"apple_id rỗng sau sanitize: {apple_id!r}")
    return safe


def _validate_and_build_bundle(
    *,
    apple_id: str,
    cookies: dict[str, str],
    audit_repo: Any,
) -> SessionBundle:
    """Validate cookies + build immutable ``SessionBundle`` (R12.4, R12.5, R12.7).

    Pure function — KHÔNG launch Camoufox, không I/O ngoài audit_repo.write.
    Dùng cho cả runtime path (gọi từ ``_do_extract_session_bundle``) lẫn
    test path (Property 16 — fake context object).

    Validation rules (refactor B):
      - ``cookies`` non-empty (dict truthy).
      - Có ÍT NHẤT 1 cookie thuộc ``_LOGIN_COOKIE_MARKERS``.

    Raises:
        SessionExtractError: thiếu cookies hoặc không có marker login.
            Audit ``session_extract_fail`` với
            ``payload.missing_fields=['cookies']`` (semantic mới: 1 field
            duy nhất 'cookies' đại diện cho cả validation gate).
    """
    if not isinstance(cookies, dict) or not cookies:
        audit_repo.write(
            event_type="session_extract_fail",
            apple_id=apple_id,
            payload={"missing_fields": ["cookies"], "reason": "cookies_empty"},
            error="cookies dict empty",
        )
        raise SessionExtractError(
            apple_id=apple_id, missing_fields=["cookies"]
        )

    has_marker = any(name in cookies for name in _LOGIN_COOKIE_MARKERS)
    if not has_marker:
        audit_repo.write(
            event_type="session_extract_fail",
            apple_id=apple_id,
            payload={
                "missing_fields": ["cookies"],
                "reason": "login_marker_missing",
                "available_cookie_names": sorted(cookies.keys()),
            },
            error="no X-APPLE-WEBAUTH-* cookie found",
        )
        raise SessionExtractError(
            apple_id=apple_id, missing_fields=["cookies"]
        )

    bundle = SessionBundle(
        apple_id=apple_id,
        cookies=dict(cookies),
        extracted_at=datetime.now(timezone.utc),
    )
    # Audit success — payload meta-only, KHÔNG log raw cookie values (R12.7).
    audit_repo.write(
        event_type="session_extract",
        apple_id=apple_id,
        payload={
            "has_pcs_mail_cookie": "X-APPLE-WEBAUTH-PCS-Mail" in cookies,
            "has_user_cookie": "X-APPLE-WEBAUTH-USER" in cookies,
            "has_token_cookie": "X-APPLE-WEBAUTH-TOKEN" in cookies,
            "cookie_count": len(cookies),
            "extracted_at": bundle.extracted_at.isoformat(),
        },
    )
    return bundle


def profile_dir_for(runtime_dir: Path, apple_id: str) -> Path:
    """Trả về thư mục profile cố định cho 1 Apple ID."""
    return runtime_dir / "icloud_profiles" / _safe_apple_id(apple_id)


def ensure_profile_dir(runtime_dir: Path, apple_id: str) -> Path:
    path = profile_dir_for(runtime_dir, apple_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


@asynccontextmanager
async def launch_camoufox(
    *,
    profile_dir: Path,
    headless: bool,
    proxy: str | None = None,
    viewport: tuple[int, int] = (1440, 900),
) -> AsyncIterator[Any]:
    """Async context manager — launch Camoufox với persistent profile.

    Yields BrowserContext (Camoufox đã enter persistent context).
    """
    try:
        from camoufox.async_api import AsyncCamoufox
    except ImportError as exc:
        raise RuntimeError(
            "Camoufox chưa cài. Chạy: .venv/bin/pip install camoufox && "
            ".venv/bin/python -m camoufox fetch"
        ) from exc

    profile_dir.mkdir(parents=True, exist_ok=True)
    w, h = viewport

    kwargs: dict[str, Any] = {
        "headless": headless,
        "humanize": True,
        "persistent_context": True,
        "user_data_dir": str(profile_dir),
        "viewport": {"width": w, "height": h},
        "i_know_what_im_doing": True,
        "config": {
            "window.innerWidth": w,
            "window.innerHeight": h,
            "window.outerWidth": w,
            "window.outerHeight": h + 85,
            "screen.width": w,
            "screen.height": h + 85,
            "screen.availWidth": w,
            "screen.availHeight": h + 85,
        },
    }
    if proxy:
        kwargs["proxy"] = {"server": proxy}

    cf = AsyncCamoufox(**kwargs)
    ctx = await cf.__aenter__()
    try:
        yield ctx
    finally:
        try:
            await cf.__aexit__(None, None, None)
        except Exception:
            pass


# ------------------------------------------------------------- session bundle


async def _do_extract_session_bundle(
    *,
    profile_dir: Path,
    apple_id: str,
    audit_repo: Any,
    proxy: str | None,
    log: Any,
    settle_seconds: float,
) -> SessionBundle:
    """Inner extract — caller PHẢI giữ Profile_Lock read mode trước khi gọi.

    Tách riêng khỏi ``extract_session_bundle`` để dễ test và để lock acquisition
    nằm gọn 1 nơi (Fail-Fast nếu lock fail thì không launch Camoufox).

    Flow (cookies-only — refactor B):
        1. Launch Camoufox HEADLESS với profile_dir.
        2. Navigate ``https://www.icloud.com/settings/`` (Apple webapp gọi
           ``/setup/ws/1/validate`` flush cookies post-validate vào
           BrowserContext — page này consistent nhất với
           ``X-APPLE-WEBAUTH-PCS-Mail`` cookie).
        3. Sleep ``settle_seconds`` (default 5s) — đợi response set thêm
           cookies session (vd ``X-APPLE-WEBAUTH-PCS-Mail`` chỉ xuất hiện
           sau khi webapp authenticate xong).
        4. Đọc ``BrowserContext.cookies('https://www.icloud.com/')``.
        5. Đóng browser ngay (R12.11).
        6. Validate qua ``_validate_and_build_bundle``.
    """
    import asyncio

    cookies: dict[str, str] = {}
    async with launch_camoufox(
        profile_dir=profile_dir, headless=True, proxy=proxy
    ) as ctx:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            # /settings/ là page Apple webapp gọi /setup/ws/1/validate
            # consistent nhất (xác nhận qua test/check_session_extract_*).
            # Khớp pattern bootstrap.py / add_profile.py / open_profile.py.
            await page.goto(
                "https://www.icloud.com/settings/",
                wait_until="domcontentloaded",
            )
            # Apple webapp init (auth/validate) thường ~3-5s. settle 5s là
            # điểm cân bằng giữa fail-fast và độ tin cậy. Test override = 0.
            if settle_seconds > 0:
                await asyncio.sleep(settle_seconds)

            cookies_list = await ctx.cookies("https://www.icloud.com/")
            cookies = {
                c["name"]: c["value"] for c in cookies_list if "name" in c
            }
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass

    if log is not None:
        log(
            f"extract_session_bundle apple_id={apple_id} "
            f"cookie_count={len(cookies)} "
            f"webauth_markers={[n for n in cookies if n.startswith('X-APPLE-WEBAUTH')]}"
        )

    return _validate_and_build_bundle(
        apple_id=apple_id, cookies=cookies, audit_repo=audit_repo,
    )


async def extract_session_bundle(
    *,
    profile_dir: Path,
    apple_id: str,
    audit_repo: Any,
    proxy: str | None = None,
    log: Any = None,
    settle_seconds: float = 5.0,
) -> SessionBundle:
    """Extract Session_Bundle từ Camoufox profile_dir (R12.3-R12.7, R12.11, R12.15).

    Refactor B — cookies-only. Xem docstring module + ``_validate_and_build_bundle``
    để hiểu rationale bỏ ``window.webAuth`` extraction.

    Flow:
        1. Acquire Profile_Lock mode ``read`` (timeout 60s) — block khi
           Bootstrap/Recorder đang giữ ``write`` lock.
        2. Launch Camoufox HEADLESS với profile_dir, navigate
           ``https://www.icloud.com/settings/``, settle ``settle_seconds``
           để Apple webapp gọi ``/setup/ws/1/validate`` flush cookies.
        3. Đọc ``BrowserContext.cookies('https://www.icloud.com/')`` rồi
           đóng browser ngay (R12.11).
        4. Validate cookies non-empty + có marker login (R12.5).
        5. Audit ``session_extract`` payload meta-only (R12.7) + return
           ``SessionBundle`` in-memory.

    Args:
        profile_dir: Path tới Camoufox persistent profile của Apple_ID.
        apple_id: Apple ID raw — dùng cho audit + lock filename.
        audit_repo: ``AuditLogRepository`` (db.repositories) — dùng cho
            ``session_extract`` / ``session_extract_fail``.
        proxy: Proxy URL truyền cho Camoufox (optional).
        log: Logger callable (``log(message: str)``) hoặc ``None``.
        settle_seconds: Thời gian sleep sau ``goto`` để Apple webapp flush
            cookies. Test override = 0 để chạy nhanh.

    Returns:
        ``SessionBundle`` immutable, scoped process-lifetime, KHÔNG persist disk.

    Raises:
        SessionExtractError: cookies empty hoặc không có marker login,
            hoặc Profile_Lock acquire fail (reason = ``profile_locked_by_bootstrap``).
            Caller (Generator/Checker/Manager) SHALL switch profile khác qua
            Pool_Manager (R12.15).
    """
    lock_dir = profile_dir / ".lock"
    lock = ProfileLock(lock_dir, apple_id)

    try:
        with lock.read_lock(timeout=60.0):
            return await _do_extract_session_bundle(
                profile_dir=profile_dir,
                apple_id=apple_id,
                audit_repo=audit_repo,
                proxy=proxy,
                log=log,
                settle_seconds=settle_seconds,
            )
    except ProfileLockError as exc:
        # R12.15: lock conflict — audit fail rồi raise SessionExtractError
        # với reason `profile_locked_by_bootstrap`. Không có missing_fields
        # vì chưa extract.
        audit_repo.write(
            event_type="session_extract_fail",
            apple_id=apple_id,
            payload={"reason": "profile_locked_by_bootstrap", "lock_mode": exc.mode},
            error=str(exc),
        )
        raise SessionExtractError(
            apple_id=apple_id,
            missing_fields=[],
            reason="profile_locked_by_bootstrap",
        ) from exc
    except SessionExtractError:
        # Validation fail — audit đã được ghi trong _validate_and_build_bundle,
        # raise nguyên không double-audit.
        raise
    except Exception as exc:  # noqa: BLE001
        # Bất kỳ exception khác (Camoufox launch fail, page.goto timeout,
        # browser crash mid-extract) — audit fail với reason 'launch_error'
        # rồi wrap thành SessionExtractError để caller có exception path
        # đồng nhất (A20 fix — trước đây bubble Exception lên Generator gây
        # fail-fast crash thay vì mark_session_expired).
        if log is not None:
            log(
                f"extract_session_bundle launch error apple_id={apple_id}: "
                f"{type(exc).__name__}: {exc}"
            )
        try:
            audit_repo.write(
                event_type="session_extract_fail",
                apple_id=apple_id,
                payload={
                    "reason": "launch_error",
                    "exc_class": type(exc).__name__,
                },
                error=str(exc),
            )
        except Exception as audit_exc:  # noqa: BLE001
            if log is not None:
                log(f"audit session_extract_fail secondary fail: {audit_exc}")
        raise SessionExtractError(
            apple_id=apple_id,
            missing_fields=[],
            reason="launch_error",
        ) from exc
