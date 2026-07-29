"""Phase 1: Browser signup — register (email+pass) → OTP → /about-you → session.

Flow (theo HAR mới):
  1. chatgpt.com → bootstrap NextAuth (csrf + signin/openai) → authorize URL
  2. Navigate authorize → /email-verification page load
  3. Click "Continue with password" → /create-account/password
  4. Fill password → submit → POST /api/accounts/user/register {username, password}
  5. Server trigger OTP (GET /email-otp/send) → redirect /email-verification (OTP form)
  6. Poll OTP → submit → POST /email-otp/validate
  7. /about-you → fill name+age → POST /create_account
  8. Đợi session-token cookie (đã login)
  9. Exfil cookies → BrowserHandoff

Retry (account đã tồn tại):
  - Register trả lỗi "already exists" → fallback OTP-only login
  - HOẶC: OTP → login → chatgpt.com

Kết quả: BrowserHandoff đủ context để Phase 2 extract session/access_token.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from config import PROMO_LANDING_URL, Settings, ensure_runtime_dirs, prepare_profile_dir
from mail_providers import MailProvider, OutlookComboError
from models import BrowserHandoff, SignupRequest
from _nextauth_bootstrap import bootstrap_authorize_url
from _browser_retry import (
    DRIVER_DEAD_MARKERS as _DRIVER_DEAD_MARKERS,
    LAUNCH_RETRY_BACKOFF as _LAUNCH_RETRY_BACKOFF,
    LAUNCH_RETRY_MAX as _LAUNCH_RETRY_MAX,
    is_driver_dead_error as _is_driver_dead_error,
    is_navigation_timeout as _is_navigation_timeout,
    is_network_error as _is_network_error,
    parse_proxy_for_playwright as _parse_proxy,
)
from user_agent_profile import CAMOUFOX_OS as _CAMOUFOX_OS


class BrowserPhaseError(Exception):
    """Phase 1 failed."""


class AccountAlreadyExistsError(BrowserPhaseError):
    """Server trả ``error_code: user_already_exists`` trên ``/about-you``.

    Fatal: account đã tồn tại trong hệ thống OpenAI — KHÔNG retry submit
    nữa, caller (signup runner) bỏ luôn account này, chuyển combo kế tiếp.
    Dùng subclass để caller có thể phân biệt nếu cần (vd mark "duplicate"
    riêng thay vì gộp chung "error"); mặc định caller chỉ catch
    ``BrowserPhaseError`` → tự nhiên propagate.
    """


# Các error_code của /about-you mà server commit là vĩnh viễn (retry không
# bao giờ pass). Detect → raise fatal, dừng retry submit ngay.
_ABOUT_YOU_FATAL_ERROR_CODES: tuple[str, ...] = (
    "user_already_exists",
)


# Cookies bắt buộc cho Phase 2 (chatgpt.com session).
_REQUIRED_AUTH_COOKIES = (
    "oai-did",
    "__cf_bm",
    "cf_clearance",
)


# ─────────────────────────────────────────────────────────────────────
# JS helpers — REMOVED 2026-06-25 (anti-ban Phase 2 Task 2.1)
# ─────────────────────────────────────────────────────────────────────
#
# Trước fix có 2 JS constant:
#   - _REGISTER_USER_JS:       fetch /api/accounts/user/register
#   - _PAGE_CREATE_ACCOUNT_JS: fetch /api/accounts/create_account
#
# Cả 2 dùng `page.evaluate(fetch)` để bypass form UI. Sentinel SDK của OpenAI
# build so-token (Session Observer) bằng cách track DOM events trên form input
# (focus / keydown / blur / mousemove). Bypass form = so-token rỗng = server
# flag bot.
#
# Thay thế:
#   - Register password: state machine `password_create` branch dùng
#     ``human_type`` + ``human_click`` (helper trong ``_human_input.py``).
#   - About-you: ``_fill_about_you`` đã dùng UI typing từ trước.
#
# Xem journal `260625-1224-reg-anti-ban-master-plan.md` Task 2.1.


# ─────────────────────────────────────────────────────────────────────
# Helper functions
# ─────────────────────────────────────────────────────────────────────


def _browser_descendant_pids(root_pid: int) -> set[int]:
    """PID con cháu của ``root_pid`` mà command là browser (camoufox / firefox /
    playwright node driver).

    Dùng để force-kill chống leak khi ``cf.__aexit__`` không reap hết tiến trình
    con (firefox/persistent-context đôi khi sống sót sau close). Best-effort qua
    ``ps`` (macOS/Linux); trả set rỗng nếu lỗi. Lọc theo command để KHÔNG bao giờ
    kill nhầm tiến trình con không phải browser.
    """
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:  # noqa: BLE001 — best-effort
        return set()
    children: dict[int, list[int]] = {}
    cmd_of: dict[int, str] = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
        cmd_of[pid] = parts[2] if len(parts) > 2 else ""
    # BFS xuống toàn bộ cây con của root_pid.
    seen: set[int] = set()
    stack = [root_pid]
    while stack:
        cur = stack.pop()
        for ch in children.get(cur, []):
            if ch not in seen:
                seen.add(ch)
                stack.append(ch)
    markers = ("camoufox", "/firefox", "playwright", "geckodriver")
    return {
        pid for pid in seen
        if any(m in cmd_of.get(pid, "").lower() for m in markers)
    }


def _pid_alive(pid: int) -> bool:
    """True nếu tiến trình ``pid`` còn sống (signal 0 = chỉ kiểm tra)."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _force_kill_pids(pids, *, log) -> None:
    """SIGKILL từng PID (best-effort). Log số lượng đã kill."""
    killed = 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except OSError:
            pass
    if killed:
        log(f"[browser] anti-leak: force-killed {killed} tiến trình browser còn sót")


def _browser_health(ctx, page) -> str:
    """Non-blocking snapshot trạng thái browser/context/page để log debug.

    Trả về chuỗi short, không raise — dùng trước/sau thao tác có thể fail
    do target closed (Plan D: observability).

    Format: 'page=open ctx_pages=2 browser=connected'
            'page=CLOSED ctx_pages=0 browser=disconnected'
    """
    try:
        page_closed = page.is_closed()
        page_state = "CLOSED" if page_closed else "open"
    except Exception as exc:
        page_state = f"ERR({type(exc).__name__})"

    try:
        pages = list(getattr(ctx, "pages", []) or [])
        live_pages = sum(1 for p in pages if not _safe_is_closed(p))
        ctx_state = f"{live_pages}/{len(pages)}"
    except Exception as exc:
        ctx_state = f"ERR({type(exc).__name__})"

    try:
        browser = getattr(ctx, "browser", None)
        if browser is None:
            br_state = "n/a"
        else:
            br_state = "connected" if browser.is_connected() else "DISCONNECTED"
    except Exception as exc:
        br_state = f"ERR({type(exc).__name__})"

    return f"page={page_state} ctx_pages_live={ctx_state} browser={br_state}"


def _safe_is_closed(p) -> bool:
    try:
        return bool(p.is_closed())
    except Exception:
        return True


async def _dump_debug_artifacts(page, out_dir, job_id, *, reason: str, log) -> None:
    """Dump full HTML + screenshot + URL của page hiện tại để debug khi lỗi/stuck.

    Dùng kèm HAR + Playwright trace (bật qua --har) để có đủ context tối ưu khi
    flow lỗi hoặc treo. Best-effort — không raise.
    """
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = out_dir / f"debug-{ts}-{job_id}-{reason}"
    try:
        url = page.url
        log(f"[browser] debug dump (reason={reason}) URL={url}")
    except Exception:
        pass
    try:
        html = await page.content()
        base.with_suffix(".html").write_text(html, encoding="utf-8")
        log(f"[browser] dumped HTML → {base.with_suffix('.html')}")
    except Exception as exc:  # noqa: BLE001
        log(f"[browser] dump HTML failed: {exc}")
    try:
        await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        log(f"[browser] dumped screenshot → {base.with_suffix('.png')}")
    except Exception as exc:  # noqa: BLE001
        log(f"[browser] dump screenshot failed: {exc}")


_GEOIP_CACHE_MAX_AGE = 86400  # 24h


def _ensure_geoip_cache(runtime_dir: Path, *, log) -> None:
    """Cache GeoIP mmdb locally so camoufox doesn't re-download every launch."""
    try:
        from camoufox.locale import MMDB_FILE, download_mmdb
    except ImportError:
        return
    if MMDB_FILE.exists():
        return
    cache = runtime_dir / "geoip" / "GeoLite2-City.mmdb"
    if cache.exists() and (time.time() - cache.stat().st_mtime) < _GEOIP_CACHE_MAX_AGE:
        cache.parent.mkdir(parents=True, exist_ok=True)
        MMDB_FILE.parent.mkdir(parents=True, exist_ok=True)
        import shutil as _shutil
        _shutil.copy2(cache, MMDB_FILE)
        log(f"[geoip] restored from cache ({cache})")
        return
    log("[geoip] downloading GeoIP database (cached for 24h)...")
    download_mmdb()
    cache.parent.mkdir(parents=True, exist_ok=True)
    import shutil as _shutil
    _shutil.copy2(MMDB_FILE, cache)
    log(f"[geoip] cached to {cache}")


async def _bootstrap_oauth_url(page, *, email: str, device_id: str, logging_id: str, log) -> str:
    """Gọi /api/auth/csrf + POST /signin/openai trong page context chatgpt.com."""
    log("[browser] bootstrapping NextAuth (csrf + signin)...")
    url = await bootstrap_authorize_url(
        page,
        email=email,
        device_id=device_id,
        logging_id=logging_id,
    )
    log(f"[browser] authorize URL ready: {url[:120]}...")
    return url


# REMOVED 2026-06-25 (anti-ban Phase 2 Task 2.1):
#   ``_register_with_password`` — legacy helper dùng ``page.evaluate(fetch)``
#   bypass form. Đã được thay bằng UI form submit thật trong state machine
#   ``password_create`` branch của ``_drive_signup_flow``. Không có caller khác.


async def _wait_otp_form(page, *, timeout_seconds: float, log) -> str:
    """Đợi OTP form xuất hiện. Return selector."""
    selectors = (
        'input[name="code"]',
        'input[autocomplete="one-time-code"]',
        'input[inputmode="numeric"]',
    )
    for sel in selectors:
        try:
            await page.wait_for_selector(sel, state="visible", timeout=int(timeout_seconds * 1000))
            log(f"[browser] OTP input ready ({sel})")
            return sel
        except Exception:
            continue
    raise BrowserPhaseError(f"OTP input không xuất hiện sau {timeout_seconds}s. URL: {page.url}")


async def _submit_otp(ctx, page, *, otp_code: str, otp_selector: str, log) -> tuple[str | None, str, int]:
    """Submit OTP qua UI (human-like type + Enter), wrap trong expect_response
    để detect server validate sớm. Fallback: gọi validate API qua
    ``context.request``.

    Strategy (theo HAR manual user thật):
      - User mở /email-verification (OTP form), gõ 6 ký số → form autoSubmit
        khi đủ độ dài HOẶC user nhấn Enter trên input.
      - Code cũ ``page.fill + click button[type="submit"]`` không trigger
        handler React (button có thể không có type=submit hoặc handler nằm
        trên form.onSubmit chứ không onClick).
      - Cách user-like: gõ per-char qua ``human_type`` (Gaussian delay) →
        ``locator.press("Enter")``. Wrap trong ``expect_response`` 12s; nếu
        UI không trigger POST → fallback ``_submit_otp_via_api`` ngay.

    Returns:
        Tuple ``(continue_url, source, status)``:
          - ``continue_url`` (str) khi server validate OK + trả continue_url
            trong body, ``None`` khi 4xx hoặc body thiếu.
          - ``source``: ``"ui"`` khi UI POST đi qua form thật của page (page
            **sẽ tự navigate** sau response — caller KHÔNG cần goto, sẽ
            trigger NS_BINDING_ABORTED nếu cố tình). ``"api"`` khi submit
            qua ``ctx.request.post`` fallback — page KHÔNG biết navigate,
            caller phải ``page.goto(continue_url)`` thủ công.
          - ``status``: HTTP status thật từ server (200 = code đã consume +
            validated, 400/401 = wrong/expired code, 0 = không observe được
            response vd page bị crash trước khi POST đi). Caller dùng status
            để quyết định resubmit hay re-poll — KHÔNG được resubmit code đã
            consume (200) vì OpenAI sẽ trả 401 "wrong_email_otp_code".
    """
    log(f"[browser] typing OTP {otp_code} ({_browser_health(ctx, page)})")

    if _safe_is_closed(page):
        log(f"[browser] page closed before OTP fill — fallback API ({_browser_health(ctx, page)})")
        cu, st = await _submit_otp_via_api(ctx, otp_code=otp_code, log=log)
        return (cu, "api", st)

    from _human_input import human_type, dwell

    otp_input = page.locator(otp_selector).first

    # Wrap toàn bộ submission trong expect_response để biết UI có trigger
    # POST /email-otp/validate hay không trong 12s. Đa số UI trigger sau
    # Enter; một số autoSubmit ngay khi length == 6.
    try:
        async with page.expect_response(
            lambda r: (
                "/api/accounts/email-otp/validate" in r.url
                and r.request.method == "POST"
            ),
            timeout=12000,
        ) as resp_info:
            # 1. Human-type OTP (Gaussian per-char). human_type tự click force
            #    + fill("") trước khi gõ.
            try:
                await human_type(
                    otp_input, otp_code,
                    delay_min_ms=50, delay_max_ms=120, log=log,
                )
            except Exception as exc:
                if _is_driver_dead_error(exc):
                    log(
                        f"[browser] human_type OTP — driver dead "
                        f"({type(exc).__name__}: {exc}) — fallback API "
                        f"({_browser_health(ctx, page)})"
                    )
                    cu, st = await _submit_otp_via_api(ctx, otp_code=otp_code, log=log)
                    return (cu, "api", st)
                # Non-driver-dead: vẫn cố submit (input có thể đã có một phần)
                log(f"[browser] human_type OTP non-fatal: {type(exc).__name__}: {exc}")

            # 2. Dwell ngắn trước submit (user-like)
            await dwell(0.25, 0.6)

            # 3. Submit: ưu tiên Enter trên OTP input (user thật làm thế).
            submitted_via: str | None = None
            try:
                await otp_input.press("Enter", timeout=2000)
                submitted_via = "Enter"
            except Exception as exc:
                if _is_driver_dead_error(exc):
                    log(
                        f"[browser] Enter — driver dead "
                        f"({type(exc).__name__}: {exc}) — fallback API "
                        f"({_browser_health(ctx, page)})"
                    )
                    cu, st = await _submit_otp_via_api(ctx, otp_code=otp_code, log=log)
                    return (cu, "api", st)
                log(f"[browser] OTP Enter failed: {type(exc).__name__}: {exc} — thử click button")

            if submitted_via is None:
                for btn in (
                    'button[type="submit"]',
                    'button:has-text("Continue")',
                    'button:has-text("Verify")',
                ):
                    try:
                        await page.click(btn, timeout=1500)
                        submitted_via = btn
                        break
                    except Exception as exc:
                        if _is_driver_dead_error(exc):
                            log(
                                f"[browser] click {btn} — driver dead — fallback API "
                                f"({_browser_health(ctx, page)})"
                            )
                            cu, st = await _submit_otp_via_api(ctx, otp_code=otp_code, log=log)
                            return (cu, "api", st)
                        continue
            log(f"[browser] OTP submitted via {submitted_via or '(no UI trigger fired)'}")

        # expect_response context closed → POST đã observed
        resp = await resp_info.value
        status = resp.status
        log(f"[browser] OTP validate UI → HTTP {status}")
        if status >= 400:
            # OTP sai (incorrect/expired) — caller resend / pop pending code
            return (None, "ui", status)
        # 2xx → parse continue_url. Schema (từ HAR manual):
        #   {"continue_url": "...", "method": "GET", "page": {...}}
        try:
            body = await resp.text()
        except Exception as exc:
            log(f"[browser] OTP response body read failed: {exc}")
            return (None, "ui", status)
        if not body:
            return (None, "ui", status)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return (None, "ui", status)
        if not isinstance(data, dict):
            return (None, "ui", status)
        cu = (data.get("continue_url") or "").strip()
        return (cu or None, "ui", status)

    except Exception as exc:
        # expect_response timeout (UI không trigger POST trong 12s) hoặc lỗi context.
        # Fallback API — server-side state có thể đã consume input value khác,
        # cứ thử qua API để xác định.
        log(
            f"[browser] OTP UI submit không trigger POST trong 12s "
            f"({type(exc).__name__}: {str(exc)[:80]}) — fallback API"
        )
        cu, st = await _submit_otp_via_api(ctx, otp_code=otp_code, log=log)
        return (cu, "api", st)


async def _submit_otp_via_api(ctx, *, otp_code: str, log) -> tuple[str | None, int]:
    """Submit OTP qua context.request — không phụ thuộc page sống.

    Dùng cookies từ context (đã chia sẻ với page) để giữ session.
    KHÔNG raise trên 4xx (trước đây raise BrowserPhaseError che mất status
    để caller phân biệt "code consumed" vs "code wrong"). Caller phải kiểm
    tra status return.

    Returns:
        Tuple ``(continue_url, status)``:
          - ``continue_url`` (str) khi server trả 200 + body JSON có field
            này (vd ``https://auth.openai.com/about-you``). Caller dùng để
            ``page.goto(continue_url)`` cho SPA navigation thật.
          - ``None`` khi server trả 4xx hoặc 2xx nhưng body không parse
            được/thiếu ``continue_url``.
          - ``status``: HTTP status thật (200, 400, 401, 500…) hoặc 0 khi
            network fail trước khi nhận response.

    Raises:
        BrowserPhaseError khi network fail (chưa biết status) hoặc
        ``ctx.request`` không khả dụng. KHÔNG raise trên 4xx.
    """
    request_ctx = getattr(ctx, "request", None)
    if request_ctx is None:
        raise BrowserPhaseError(
            "OTP fallback failed: context.request không khả dụng "
            "(Camoufox/Playwright version cũ?)"
        )
    url = "https://auth.openai.com/api/accounts/email-otp/validate"
    try:
        resp = await request_ctx.post(
            url,
            data={"code": otp_code},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://auth.openai.com",
                "Referer": "https://auth.openai.com/email-verification",
            },
        )
    except Exception as exc:
        raise BrowserPhaseError(
            f"OTP fallback API request failed: {type(exc).__name__}: {exc}"
        ) from exc

    status = resp.status
    try:
        body_text = await resp.text()
    except Exception:
        body_text = ""
    log(f"[browser] OTP fallback API → HTTP {status} body={body_text[:120]}")
    if status >= 400:
        # KHÔNG raise — return status để caller phân biệt 401 (wrong code)
        # vs 200-consumed (caller cần re-poll, KHÔNG resubmit cùng code).
        return (None, status)

    # Parse continue_url từ JSON body. Server trả schema (HAR manual record):
    #   {"continue_url": "https://auth.openai.com/about-you",
    #    "method": "GET",
    #    "page": {"type": "about_you", ...}}
    if not body_text:
        log("[browser] OTP fallback API: empty body, không có continue_url")
        return (None, status)
    try:
        data = json.loads(body_text)
    except json.JSONDecodeError:
        log("[browser] OTP fallback API: body không phải JSON, không có continue_url")
        return (None, status)
    if not isinstance(data, dict):
        log(f"[browser] OTP fallback API: body không phải object (got {type(data).__name__})")
        return (None, status)
    continue_url = (data.get("continue_url") or "").strip()
    if not continue_url:
        log("[browser] OTP fallback API: response thiếu continue_url field")
        return (None, status)
    return (continue_url, status)


async def _wait_after_login(page, *, timeout_seconds: float, log) -> str:
    """Sau submit login password, đợi:
    - chatgpt.com (login OK, không cần OTP)
    - /email-verification (cần OTP)
    - error
    Returns: 'chatgpt' hoặc 'otp_required'.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        cur = page.url
        if "chatgpt.com" in cur and "auth.openai.com" not in cur and "/auth/error" not in cur:
            log("[browser] login OK — redirected to chatgpt.com")
            return "chatgpt"
        if "/email-verification" in cur or "/email-otp" in cur:
            log("[browser] login requires OTP")
            return "otp_required"
        if "/auth/error" in cur:
            raise BrowserPhaseError(f"login error page: {cur}")
        # Detect OTP form xuất hiện (SPA case)
        try:
            otp_input = page.locator('input[name="code"], input[autocomplete="one-time-code"]').first
            if await otp_input.is_visible(timeout=300):
                log("[browser] login OTP form detected (SPA)")
                return "otp_required"
        except Exception:
            pass
        # Detect login error (sai password)
        try:
            err_el = page.locator('[role="alert"], [class*="error"]').first
            err_text = await err_el.text_content(timeout=300)
            if err_text and ("incorrect" in err_text.lower() or "wrong password" in err_text.lower() or "invalid" in err_text.lower()):
                raise BrowserPhaseError(f"login error: {err_text.strip()}")
        except BrowserPhaseError:
            raise
        except Exception:
            pass
        await asyncio.sleep(0.5)
    raise BrowserPhaseError(f"timeout {timeout_seconds}s after login submit. URL: {page.url}")


async def _detect_screen(page) -> str:
    """Detect màn hình hiện tại từ URL + DOM. Return 1 trong:
      - 'chatgpt'              : đã login xong, page ở chatgpt.com
      - 'about_you'            : form name+age (auth.openai.com/about-you)
      - 'mfa_challenge'        : account có 2FA → cần TOTP code từ authenticator
      - 'turnstile_challenge'  : Cloudflare Turnstile challenge visible
      - 'otp'                  : OTP input visible (/email-verification or SPA)
      - 'password_create'      : /create-account/password (form set password mới)
      - 'password_login'       : /log-in/password (form login với account đã tồn tại)
      - 'continue'             : /email-verification trang chọn 'Continue with password'
      - 'auth_error'           : page lỗi /auth/error
      - 'unknown'              : không nhận diện được
    """
    cur = page.url
    if "/auth/error" in cur:
        return "auth_error"
    if "chatgpt.com" in cur and "auth.openai.com" not in cur:
        return "chatgpt"
    if "auth.openai.com/about-you" in cur:
        return "about_you"
    if "passkey" in cur.lower():
        return "passkey_enroll"
    # Nội dung SPA có thể đã render /about-you mà URL chưa đổi
    try:
        name_el = page.locator('input[name="name"], input[autocomplete="name"]').first
        if await name_el.is_visible(timeout=200):
            return "about_you"
    except Exception:
        pass

    # MFA challenge — phải check TRƯỚC OTP vì input selector trùng nhau
    # (cả MFA và OTP đều dùng input[name="code"] / inputmode=numeric).
    # Phân biệt qua URL pattern hoặc text marker đặc trưng MFA.
    if "/mfa" in cur or "/totp" in cur or "/two-factor" in cur:
        return "mfa_challenge"
    try:
        # Text marker: "authenticator app", "two-factor", "Enter the 6-digit code from your authenticator"
        mfa_text = page.locator(
            'text=/authenticator app/i, text=/two[- ]factor/i, text=/from your authenticator/i'
        ).first
        if await mfa_text.is_visible(timeout=200):
            return "mfa_challenge"
    except Exception:
        pass

    if "/create-account/password" in cur:
        return "password_create"
    if "/log-in/password" in cur:
        # SPA case: URL vẫn là /log-in/password nhưng content đã chuyển sang OTP form
        # hoặc "Check your inbox" page (email verification sau login)
        try:
            otp_input = page.locator('input[name="code"], input[autocomplete="one-time-code"]').first
            if await otp_input.is_visible(timeout=200):
                return "otp"
        except Exception:
            pass
        try:
            inbox_el = page.locator(
                'text="Check your inbox", text="Check your email", text="Enter the verification code"'
            ).first
            if await inbox_el.is_visible(timeout=200):
                return "otp"
        except Exception:
            pass
        return "password_login"
    # /email-verification: ƯU TIÊN button "password" để bắt buộc set password
    # Nếu cả OTP input và password button cùng visible, password button thắng
    #
    # Anti-flaky: timeout 800ms cũ quá ngắn → SPA chưa kịp render password button
    # → flow rớt vào nhánh OTP-only (passwordless) → password KHÔNG được set. Tăng
    # lên 3000ms để khớp với latency SPA render thật (manual trace 1-2.5s).
    _PWD_BTN_SELECTOR = (
        'button:has-text("password"), a:has-text("password"), '
        '[role="button"]:has-text("password")'
    )
    if "/email-verification" in cur or "/email-otp" in cur or "/identifier" in cur:
        try:
            pwd_btn = page.locator(_PWD_BTN_SELECTOR).first
            if await pwd_btn.is_visible(timeout=3000):
                return "continue"
        except Exception:
            pass
    # Broad check: trên bất kỳ auth.openai.com page nào có nút password → ưu tiên click
    if "auth.openai.com" in cur:
        try:
            pwd_btn = page.locator(_PWD_BTN_SELECTOR).first
            if await pwd_btn.is_visible(timeout=800):
                return "continue"
        except Exception:
            pass
    # Turnstile / Cloudflare challenge — check trước OTP vì có thể overlay trên OTP form
    try:
        turnstile = page.locator(
            'iframe[src*="challenges.cloudflare.com"], '
            'iframe[src*="turnstile"], '
            '#cf-turnstile, .cf-turnstile, '
            '[data-turnstile-callback]'
        ).first
        if await turnstile.is_visible(timeout=200):
            return "turnstile_challenge"
    except Exception:
        pass
    # OTP form (URL có thể là /email-verification, /email-otp, /log-in/email-verification, ...)
    try:
        otp_input = page.locator('input[name="code"], input[autocomplete="one-time-code"]').first
        if await otp_input.is_visible(timeout=200):
            return "otp"
    except Exception:
        pass
    if "/email-verification" in cur or "/email-otp" in cur:
        return "otp"  # fallback: chỉ có OTP form, không có password button
    return "unknown"


async def _skip_passkey(page, *, log, leave_timeout: float = 10.0) -> bool:
    """Skip passkey enrollment page. Returns True khi đã rời khỏi passkey URL.

    Strategy:
      1. Click explicit skip/dismiss buttons (text-based)
      2. Click any secondary/non-primary button or link
    Sau khi click, ĐỢI URL không còn chứa "passkey" (timeout `leave_timeout`s).
    Nếu click rồi mà page vẫn ở passkey → return False (caller xử lý).
    KHÔNG fallback goto chatgpt.com — sẽ cướp navigation OAuth callback inflight,
    làm Set-Cookie session-token bị abort.
    """
    async def _wait_leave_passkey() -> bool:
        try:
            await page.wait_for_url(
                lambda u: "passkey" not in (u or "").lower(),
                timeout=int(leave_timeout * 1000),
            )
            return True
        except Exception:
            return False

    # 1. Explicit skip buttons
    for sel in (
        'button:has-text("Skip")',
        'button:has-text("Maybe later")',
        'button:has-text("Do this later")',
        'button:has-text("Not now")',
        'button:has-text("I\'ll do this later")',
        'a:has-text("Skip")',
        'a:has-text("Maybe later")',
        'a:has-text("Do this later")',
        'a:has-text("Not now")',
        'a:has-text("I\'ll do this later")',
        '[data-testid*="skip" i]',
        '[data-testid*="dismiss" i]',
    ):
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=800):
                await btn.click(timeout=3000)
                log(f"[browser] clicked skip passkey: {sel}")
                if await _wait_leave_passkey():
                    log("[browser] passkey page left after skip click")
                    return True
                log("[browser] click landed but URL still passkey — continue trying")
                break  # đừng click thêm selector khác, page đã transition
        except Exception:
            continue

    # 2. Log page content for debugging
    try:
        buttons_info = await page.evaluate(r"""
            () => {
                const els = [...document.querySelectorAll('button, a[href], [role="button"]')];
                return els.slice(0, 10).map(e => ({
                    tag: e.tagName, text: (e.textContent || '').trim().substring(0, 60),
                    cls: (e.className || '').substring(0, 40),
                }));
            }
        """)
        log(f"[browser] passkey page elements: {json.dumps(buttons_info, ensure_ascii=False)}")
    except Exception:
        pass

    # 3. Try clicking non-primary buttons (secondary/tertiary)
    try:
        all_buttons = page.locator('button, a[role="button"]')
        count = await all_buttons.count()
        for i in range(count):
            btn = all_buttons.nth(i)
            text = ((await btn.text_content()) or "").strip().lower()
            if any(k in text for k in ("create", "set up", "enable", "passkey")):
                continue
            if text and await btn.is_visible(timeout=500):
                await btn.click(timeout=3000)
                log(f"[browser] clicked non-primary button on passkey page: {text!r}")
                if await _wait_leave_passkey():
                    log("[browser] passkey page left after non-primary click")
                    return True
                break
    except Exception:
        pass

    log("[browser] could not leave passkey page after click attempts")
    return False


# Khi đã LẤY ĐƯỢC OTP, đảm bảo còn tối thiểu ngần này giây để hoàn tất các bước
# ngắn còn lại (submit OTP + /about-you + chờ session) — KHÔNG để wall-clock kill
# job ngay sau khi đã có OTP (lãng phí email + code). Áp cho cả deadline nội bộ
# của flow lẫn watchdog bên ngoài (qua on_checkpoint).
_POST_OTP_GRACE_SECONDS = 150.0
# Budget cho các bước TRƯỚC khi có OTP (load trang + send + submit ban đầu),
# cộng thêm vào otp_timeout để overall flow deadline phủ trọn thời gian chờ mail.
_PRE_OTP_MARGIN_SECONDS = 60.0


async def _drive_signup_flow(
    *, ctx, page, request, mail_provider, callback_holder, otp_started_at, log,
    overall_timeout: float = 240.0,
    on_checkpoint=None,
    post_otp_grace: float = _POST_OTP_GRACE_SECONDS,
    debug_capture: bool = False,
    debug_dir=None,
    job_id: str = "",
) -> tuple[str, float]:
    """State machine: check URL/DOM hiện tại, dispatch handler tương ứng.
    Lặp đến khi đến được chatgpt.com (có session) hoặc gặp lỗi không phục hồi.

    on_checkpoint: callback(stage:str) gọi khi vượt mốc quan trọng (đã lấy được
        OTP) để watchdog bên ngoài gia hạn deadline tương ứng.

    Returns: (callback_url, otp_seconds).
    """
    deadline = time.monotonic() + overall_timeout
    otp_seconds_total = 0.0
    otp_already_polled = False  # tránh poll OTP nhiều lần trong cùng batch
    register_attempted = False
    register_succeeded = False  # True chỉ sau POST /register trả 200 (password đã set)
    login_attempted = False
    continue_clicked = False
    # Đếm số lần force goto /create-account/password — tránh loop vô hạn khi server
    # cứ redirect ngược về /email-verification (rare edge case, vd account vừa
    # registered nửa chừng bị stuck giữa flow).
    force_pwd_goto_count = 0
    _FORCE_PWD_GOTO_MAX = 3
    # OTP submit state — track HTTP status thật từ _submit_otp để decision tree:
    #   200 + continue_url → reset (page tự nav hoặc manual goto theo source)
    #   200 + no continue_url → manual goto /about-you (server validated nhưng quên trả URL)
    #   4xx → click Resend + re-poll code mới ngay (KHÔNG resubmit cùng code → 401)
    _otp_last_status: int = 0
    # Wait 10s trước poll OTP lần đầu — mail iCloud HME có delay forward 2-10s.
    # Poll quá sớm dễ bắt code stale từ session cũ hoặc trả None gây resend sớm.
    _otp_first_poll_wait_done: bool = False
    # Sau submit OTP UI status 200 (code consumed) mà page kẹt — chỉ goto fallback
    # /about-you 1 lần để tránh loop khi /about-you cũng kẹt.
    _otp_force_about_you_done: bool = False
    otp_submitted = False
    _otp_submit_ts: float | None = None
    _otp_last_code: str | None = None  # code đang chờ confirm (cho debug log)
    tried_codes: set[str] = set()  # codes đã submit + bị reject
    pending_codes: list[str] = []  # codes chưa submit (mail delay catch)
    last_screen = None
    same_screen_count = 0

    while time.monotonic() < deadline:
        screen = await _detect_screen(page)

        if screen != last_screen:
            log(f"[flow] screen={screen} url={page.url.split('?')[0]}")
            last_screen = screen
            same_screen_count = 0
        else:
            same_screen_count += 1

        # Stuck detector: 1 màn hình lặp quá lâu (~25 vòng) → dump HTML+screenshot
        # 1 lần để debug mà không phải đợi hết overall_timeout. Trace (nếu bật)
        # vẫn ghi liên tục toàn bộ.
        if debug_capture and debug_dir is not None and same_screen_count == 25:
            await _dump_debug_artifacts(
                page, debug_dir, job_id, reason=f"stuck_{screen}", log=log,
            )

        if screen == "chatgpt":
            await _wait_chatgpt_session(ctx, page, timeout_seconds=30.0, log=log)
            # Hard policy (2026-06-28): bắt buộc password phải được set qua
            # /create-account/password POST 200. Nếu flow chạm chatgpt.com mà
            # KHÔNG đi qua register POST (register_succeeded=False) VÀ cũng
            # KHÔNG phải login flow (login_attempted=False) → account tạo
            # passwordless → vi phạm policy, fail-fast.
            if not register_succeeded and not login_attempted:
                raise BrowserPhaseError(
                    f"password chưa được set: flow đi vào chatgpt.com mà KHÔNG "
                    f"qua /create-account/password (passwordless OTP path) — "
                    f"URL: {page.url}"
                )
            # Verify /api/auth/session trả accessToken + user.id (confirm login OK).
            await _verify_account_session(ctx, page, log=log)
            return callback_holder.get("url") or page.url, otp_seconds_total

        if screen == "auth_error":
            raise BrowserPhaseError(f"auth error page: {page.url}")

        if screen == "turnstile_challenge":
            if same_screen_count == 0:
                log("[flow] Turnstile/Cloudflare challenge detected — waiting for auto-solve")
            if same_screen_count > 60:
                raise BrowserPhaseError(
                    f"Turnstile challenge stuck >60 iterations. URL: {page.url}"
                )
            await asyncio.sleep(1.0)
            continue

        if screen == "mfa_challenge":
            # Account đã enable 2FA từ trước (combo đã từng dùng signup + 2FA).
            # Signup flow KHÔNG có TOTP secret để pass — fail-fast với message
            # rõ ràng để user biết dùng "Get Session" flow (cung cấp secret) thay vì retry signup.
            raise BrowserPhaseError(
                f"account đã có 2FA enabled — signup flow không có TOTP secret. "
                f"Dùng Get Session flow với combo email|password|secret. URL: {page.url}"
            )

        if screen == "continue":
            if continue_clicked:
                # Đã click rồi mà page chưa chuyển → đợi thêm rồi retry detect
                await asyncio.sleep(1.0)
                continue
            _pwd_sel = (
                'button:has-text("password"), a:has-text("password"), '
                '[role="button"]:has-text("password")'
            )
            pwd_btn = page.locator(_pwd_sel).first
            try:
                btn_text = await pwd_btn.text_content(timeout=1000)
            except Exception:
                btn_text = ""
            # Click thường → nếu bị chặn (overlay/Turnstile gây "performing click
            # action" timeout, từng làm KẸT account mới ở run test 08:53) →
            # thử force=True (bỏ qua pointer-intercept) → fallback đọc href rồi
            # goto. Nút này là <a href="/create-account/password" | "/log-in/password">.
            clicked = False
            try:
                await pwd_btn.click(timeout=3000)
                clicked = True
            except Exception as exc:
                log(f"[flow] click password button (normal) failed: {exc} — thử force")
                try:
                    await pwd_btn.click(timeout=2000, force=True)
                    clicked = True
                except Exception as exc2:
                    log(f"[flow] click password button (force) failed: {exc2} — thử goto href")
                    try:
                        href = await pwd_btn.get_attribute("href")
                        if href:
                            full = href if href.startswith("http") else f"https://auth.openai.com{href}"
                            await page.goto(full, wait_until="domcontentloaded", timeout=15000)
                            clicked = True
                    except Exception as exc3:
                        log(f"[flow] goto password href failed: {exc3}")
            if clicked:
                log(f"[flow] clicked password button: {(btn_text or '').strip()[:60]}")
                continue_clicked = True
            # Dwell jitter (anti-ban Task 2.3) thay sleep cố định — sentinel
            # observer thấy reading time realistic giữa state transitions.
            from _human_input import dwell as _dwell_t
            await _dwell_t(1.0, 2.2)
            continue

        if screen == "password_create":
            if register_attempted:
                await asyncio.sleep(1.0)
                continue

            # ── UI form submit thật (anti-ban Task 2.1 + 2.4 + 2.5) ──
            # Trace tay xác nhận browser thật submit qua FORM thật, KHÔNG phải
            # `fetch('/register')`. Sentinel SDK monitor input/keydown events
            # trên password input để build so-token. Bypass form = so-token
            # rỗng → server flag bot → ban.
            #
            # Thứ tự (đảo lại P2 — fix 32s gap detect input):
            #   1. Tìm password input (max 15s) — page SPA có thể đang render.
            #      Đảo lên đầu vì code cũ chạy mouse_wander + page.evaluate
            #      block trên page chưa stable → tốn 30s+ trước khi detect input.
            #   2. Wait `oai-sc` cookie (8s) — sentinel SDK đã chạy ready.
            #   3. Dwell ngắn (user-like reading) → human_type password.
            #   4. human_click submit (mousemove → click).
            #   5. expect_response /register để capture HTTP status.
            #
            # Bỏ ``random_mouse_wander`` ở đây — manual user không wander
            # trước khi gõ password; ``human_type`` per-char đã đủ keydown
            # events cho sentinel build so-token. Wander chỉ tốn wall-clock
            # và risk block trên page chưa load xong.

            from _human_input import human_type, human_click, dwell

            # 1. Find password input (đảo lên đầu)
            pwd_input = None
            pwd_selector_used = None
            for idx, sel in enumerate((
                'input[type="password"]',
                'input[name="password"]',
                'input[autocomplete*="password"]',
            )):
                try:
                    loc = page.locator(sel).first
                    # Selector đầu chờ tối đa 15s (SPA render); fallback 2s
                    sel_timeout = 15000 if idx == 0 else 2000
                    if await loc.is_visible(timeout=sel_timeout):
                        pwd_input = loc
                        pwd_selector_used = sel
                        break
                except Exception:
                    continue
            if pwd_input is None:
                raise BrowserPhaseError(
                    f"không tìm thấy password input trên /create-account/password. URL: {page.url}"
                )
            log(f"[flow] password input detected: {pwd_selector_used}")

            # 2. Wait oai-sc cookie (giảm timeout 15→8s — sentinel set rất sớm
            #    khi page bắt đầu render; nếu 8s không có thì page có vấn đề).
            try:
                await _wait_oai_sc(ctx, timeout_seconds=8.0, log=log)
            except BrowserPhaseError as exc:
                log(f"[flow] oai-sc wait timeout (continue anyway): {exc}")

            # 3. Pre-typing dwell — user-like (đọc form rồi gõ), đã giảm
            await dwell(0.3, 0.7)

            # 4. Human-type password (Task 2.2)
            from _human_input import DEFAULT_DELAY_MIN_MS, DEFAULT_DELAY_MAX_MS
            delay_min = DEFAULT_DELAY_MIN_MS
            delay_max = DEFAULT_DELAY_MAX_MS
            log(f"[flow] human-type password (delay {delay_min}-{delay_max}ms Gaussian)")
            await human_type(
                pwd_input, request.password,
                delay_min_ms=delay_min, delay_max_ms=delay_max,
                log=log,
            )
            await dwell(0.3, 0.7)  # cursor pause sau khi gõ — tab/look at button

            # 5. Submit form + capture response
            register_attempted = True
            log("[flow] submit form → expect /api/accounts/user/register response")
            try:
                async with page.expect_response(
                    lambda r: (
                        "/api/accounts/user/register" in r.url
                        and r.request.method == "POST"
                    ),
                    timeout=30000,
                ) as resp_info:
                    submitted = False
                    for sel in (
                        'button[type="submit"]',
                        'button:has-text("Continue")',
                        'button:has-text("Sign up")',
                    ):
                        try:
                            btn = page.locator(sel).first
                            # is_enabled timeout 2000ms (tăng từ 500): button có
                            # thể đang disabled vì password validator chưa pass
                            # (sentinel observer build so-token). 500ms quá ngắn
                            # → loop qua selector kế (cũng disabled) → mất time
                            # tổng. 2000ms cho phép button enable bình thường.
                            if await btn.is_visible(timeout=1500) \
                                    and await btn.is_enabled(timeout=2000):
                                await human_click(page, btn, log=log)
                                log(f"[flow] clicked submit: {sel}")
                                submitted = True
                                break
                        except Exception:
                            continue
                    if not submitted:
                        log("[flow] no submit button found — fallback Enter key")
                        await pwd_input.press("Enter")
                resp = await resp_info.value
            except Exception as exc:
                # expect_response timeout — page có thể đã navigate tới screen mới
                # (vd account đã tồn tại → SPA chuyển login mà không POST register).
                log(
                    f"[flow] expect_response /register timeout/error: "
                    f"{type(exc).__name__}: {exc} — continue detect screen"
                )
                await asyncio.sleep(1.5)
                continue

            status = resp.status
            try:
                body_text = await resp.text()
                body = json.loads(body_text) if body_text else {}
            except Exception:
                body = {}
                body_text = ""

            if status == 200:
                register_succeeded = True
                log("[flow] register OK (HTTP 200) — password set, page tự navigate qua redirect")
                # Page tự follow continue_url (server trả 302 → /email-verification).
                # KHÔNG manual goto — để form submit redirect chain tự nhiên (sentinel
                # observer sẽ thấy navigation hoàn chỉnh).
                # Dwell jitter (Task 2.3) — page đang redirect, observer cần thời gian.
                from _human_input import dwell as _dwell_reg_ok
                await _dwell_reg_ok(1.2, 2.5)
                continue

            body_str = json.dumps(body) if isinstance(body, dict) else (body_text or "")

            # ── Detailed debug log khi register fail (status != 200) ──────
            # Capture đầy đủ: URL, status, body, request headers, page state
            # để post-mortem analysis khi invalid_auth_step/4xx xảy ra.
            try:
                _req = resp.request
                _req_url = getattr(_req, "url", "?")
                _req_method = getattr(_req, "method", "?")
            except Exception:
                _req_url = "?"
                _req_method = "?"
            try:
                _resp_headers = dict(getattr(resp, "headers", {}))
            except Exception:
                _resp_headers = {}
            log(
                f"[flow] REGISTER FAIL DEBUG: status={status} "
                f"req={_req_method} {_req_url} "
                f"page_url={page.url} "
                f"set_cookie={'Set-Cookie' in _resp_headers or 'set-cookie' in _resp_headers}"
            )
            log(f"[flow] REGISTER FAIL body: {body_str[:400]}")

            # ── Detect "invalid_auth_step" (400) ──────────────────────────
            # OpenAI trả code "invalid_auth_step" khi:
            #   - Email đã có account (đã hoàn tất signup từ trước) → server từ
            #     chối /register vì auth state machine không cho phép
            #   - State machine bị out-of-sync (rare: session expired giữa load
            #     và submit, force goto bypass step)
            #
            # request_phase.py line 1625 đã treat case này là "email đã đăng ký"
            # cho HTTP flow. Browser flow mirror: fail-fast với message rõ ràng
            # để autoreg mark email disabled (KHÔNG retry vô ích).
            if status == 400 and "invalid_auth_step" in body_str.lower():
                raise BrowserPhaseError(
                    f"email {request.email} đã được đăng ký "
                    f"(invalid_auth_step) — server từ chối /register vì auth "
                    f"state không cho phép. Cần email mới để reg. URL: {page.url}"
                )

            if "already" in body_str.lower() or "exists" in body_str.lower() or status == 409:
                log(f"[flow] account already exists (HTTP {status}) — page sẽ chuyển login")
                from _human_input import dwell as _dwell_exists
                await _dwell_exists(1.2, 2.5)
                continue
            raise BrowserPhaseError(f"register failed HTTP {status}: {body_str[:200]}")

        if screen == "password_login":
            # User yêu cầu (2026-06-27): CẤM 'Log in with a one-time code'.
            # Tài khoản MỚI đi nhánh password_create (điền password + Continue).
            # Vào được /log-in/password = account ĐÃ tồn tại → điền password +
            # Continue 1 lần; password random của signup không khớp account cũ
            # → sẽ sai → KHÔNG dùng OTC, bỏ account (không phải tài khoản mới).
            if login_attempted:
                if same_screen_count >= 2:
                    raise BrowserPhaseError(
                        "account đã tồn tại — password không khớp, CẤM one-time "
                        f"code → bỏ (không phải tài khoản mới). URL: {page.url}"
                    )
                await asyncio.sleep(1.0)
                continue

            log("[flow] login: điền password + Continue (account đã tồn tại?)")
            pwd_input = None
            for sel in ('input[type="password"]', 'input[name="password"]'):
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=2000):
                        pwd_input = loc
                        break
                except Exception:
                    continue
            if not pwd_input:
                raise BrowserPhaseError(f"login page but no password input. URL: {page.url}")

            from _human_input import human_type as _hp_login, dwell as _dw_login
            await _hp_login(pwd_input, request.password, delay_min_ms=40, delay_max_ms=100, log=log)
            await _dw_login(0.1, 0.3)
            submitted = False
            for btn in (
                'button[type="submit"]',
                'button:has-text("Continue")',
                'button:has-text("Log in")',
            ):
                try:
                    btn_loc = page.locator(btn).first
                    if await btn_loc.is_visible(timeout=1500):
                        await btn_loc.click(timeout=5000)
                        submitted = True
                        break
                except Exception:
                    continue
            if not submitted:
                try:
                    await pwd_input.press("Enter")
                except Exception:
                    pass
            log("[flow] submitted login password (Continue, no OTC)")
            login_attempted = True
            await asyncio.sleep(1.5)
            continue

        if screen == "otp":
            # ── GUARD: pre-register passwordless trap ─────────────────────
            # OpenAI đôi khi render /email-verification chỉ có OTP form (không
            # có nút "Continue with password") — passwordless trend. Nếu rớt
            # vào nhánh này TRƯỚC khi register POST → account tạo thành công
            # NHƯNG KHÔNG có password (passwordless OTP-only).
            #
            # User yêu cầu (2026-06-28): bắt buộc password phải được set.
            # → Force goto /create-account/password để buộc OpenAI render form
            # password input. Giới hạn 3 lần để tránh loop (server có thể
            # redirect ngược nếu screen_hint chỉ định OTP-only).
            #
            # CRITICAL: KHÔNG fire khi login_attempted=True. Account ĐÃ tồn tại
            # đi qua nhánh password_login → server ở state "login OTP verify",
            # force goto /create-account/password sẽ làm server trả HTTP 400
            # invalid_auth_step (state machine không cho phép signup từ login).
            # Trong scenario này, OTP screen là HỢP LỆ — đây là login OTP.
            if not register_attempted and not login_attempted and "auth.openai.com" in page.url:
                if force_pwd_goto_count < _FORCE_PWD_GOTO_MAX:
                    # Thử đợi thêm 4s xem password button có render trễ không
                    # (race rare: SPA chunk JS late) trước khi force goto.
                    _PWD_SEL = (
                        'button:has-text("password"), a:has-text("password"), '
                        '[role="button"]:has-text("password")'
                    )
                    try:
                        late_btn = page.locator(_PWD_SEL).first
                        if await late_btn.is_visible(timeout=4000):
                            log(
                                "[flow] pre-register OTP screen: password button "
                                "rendered trễ → re-loop để click 'Continue with password'"
                            )
                            same_screen_count = 0
                            last_screen = None
                            await asyncio.sleep(0.5)
                            continue
                    except Exception:
                        pass
                    force_pwd_goto_count += 1
                    log(
                        f"[flow] OTP form hiện trước khi register POST "
                        f"(passwordless path) — force goto /create-account/password "
                        f"để set password ({force_pwd_goto_count}/{_FORCE_PWD_GOTO_MAX})"
                    )
                    try:
                        await page.goto(
                            "https://auth.openai.com/create-account/password",
                            wait_until="domcontentloaded",
                            timeout=15000,
                        )
                        await asyncio.sleep(1.0)
                        last_screen = None
                        same_screen_count = 0
                    except Exception as exc:
                        log(
                            f"[flow] goto /create-account/password failed: "
                            f"{type(exc).__name__}: {exc}"
                        )
                        await asyncio.sleep(1.0)
                    continue
                # Hết quota force goto → fail-fast, không cho qua nhánh OTP để
                # tạo account passwordless (vi phạm user policy).
                raise BrowserPhaseError(
                    f"không vào được /create-account/password sau "
                    f"{_FORCE_PWD_GOTO_MAX} lần goto — OpenAI cứ redirect "
                    f"passwordless. URL: {page.url}"
                )
            # ── End guard ──

            # ── OTP submit state machine — status-driven decision tree ──
            # Architecture (2026-06-28 refactor):
            #   - _submit_otp trả (continue_url, source, status). Caller dùng
            #     status để biết server trạng thái thật của code:
            #       200 → code consumed + validated → đợi page nav HOẶC manual
            #             goto continue_url/about-you (KHÔNG resubmit).
            #       4xx → wrong/expired → click Resend + re-poll code mới NGAY
            #             (KHÔNG resubmit cùng code → server trả 401).
            #   - Bỏ escalation chain cũ (>10s re-click, >18s JS submit, >25s
            #     API resubmit). Logic này resubmit code đã consume → 401
            #     "wrong_email_otp_code" → confuse re-poll, lãng phí 35s.
            #
            # State flow:
            #   1. otp_submitted=False → poll new code → submit → branch theo status
            #   2. otp_submitted=True + status=200 → đợi page nav (≤15s). Nếu kẹt,
            #      force goto /about-you 1 lần (next step trong signup flow).
            #   3. otp_submitted=True + status≥400 → xử lý ngay đầu vòng tới
            #      (click Resend hoặc pop pending code).

            # Step A: nếu vừa submit xong với status ≥ 400 → wrong code, xử lý ngay
            if otp_submitted and _otp_last_status >= 400:
                log(
                    f"[flow] OTP {_otp_last_code or '?'} rejected HTTP "
                    f"{_otp_last_status} — click Resend + poll code mới"
                )
                # Clear input để gõ code mới
                try:
                    otp_inp = page.locator('input[name="code"]').first
                    await otp_inp.fill("")
                except Exception:
                    pass
                # Nếu còn pending code → thử ngay, không cần resend
                if pending_codes:
                    log(f"[flow] còn {len(pending_codes)} pending code(s) — thử code kế trước khi resend")
                else:
                    # Click Resend để server gửi mail OTP mới
                    try:
                        resend_btn = page.locator(
                            'button:has-text("Resend"), a:has-text("Resend")'
                        ).first
                        await resend_btn.click(timeout=3000)
                        log("[flow] clicked 'Resend email' (sau wrong code)")
                    except Exception as exc:
                        log(f"[flow] resend button not found: {type(exc).__name__}: {exc}")
                # Reset state để vòng tới re-poll
                otp_submitted = False
                _otp_submit_ts = None
                _otp_last_status = 0
                same_screen_count = 0
                await asyncio.sleep(2.0)
                continue

            # Step B: nếu đã submit + status=200 → đợi page nav HOẶC force goto fallback
            if otp_submitted:
                # status=200 (hoặc 0 = chưa observe response): page đang nav hoặc kẹt
                if _otp_submit_ts is None:
                    _otp_submit_ts = time.monotonic()
                _otp_wait_elapsed = time.monotonic() - _otp_submit_ts

                # 15s không nav → force goto /about-you (next step sau OTP trong
                # signup flow). KHÔNG resubmit code (đã consume). Chỉ thử 1 lần
                # để tránh loop khi /about-you cũng kẹt.
                if _otp_wait_elapsed > 15.0 and not _otp_force_about_you_done:
                    _otp_force_about_you_done = True
                    log(
                        f"[flow] OTP submitted (status={_otp_last_status}) nhưng "
                        f"page kẹt {_otp_wait_elapsed:.0f}s ở {page.url.split('?')[0]} — "
                        f"force goto /about-you (code đã consume, KHÔNG resubmit)"
                    )
                    try:
                        await page.goto(
                            "https://auth.openai.com/about-you",
                            wait_until="domcontentloaded",
                            timeout=15000,
                        )
                        await asyncio.sleep(1.0)
                        # Reset state để state machine detect screen mới
                        otp_submitted = False
                        _otp_submit_ts = None
                        _otp_last_code = None
                        _otp_last_status = 0
                        last_screen = None
                        same_screen_count = 0
                    except Exception as exc:
                        log(
                            f"[flow] goto /about-you failed: "
                            f"{type(exc).__name__}: {exc}"
                        )
                    continue

                # 30s vẫn kẹt sau force goto → re-poll code mới (server có thể
                # đã invalidate state, cần code mới hoàn toàn).
                if _otp_wait_elapsed > 30.0:
                    log(
                        f"[flow] OTP kẹt >{_otp_wait_elapsed:.0f}s sau force goto — "
                        f"re-poll code mới (click Resend)"
                    )
                    try:
                        resend_btn = page.locator(
                            'button:has-text("Resend"), a:has-text("Resend")'
                        ).first
                        await resend_btn.click(timeout=3000)
                        log("[flow] clicked 'Resend email' (sau stuck timeout)")
                    except Exception as exc:
                        log(f"[flow] resend button not found: {type(exc).__name__}: {exc}")
                    # Clear input + reset state
                    try:
                        otp_inp = page.locator('input[name="code"]').first
                        await otp_inp.fill("")
                    except Exception:
                        pass
                    otp_submitted = False
                    _otp_submit_ts = None
                    _otp_last_code = None
                    _otp_last_status = 0
                    _otp_force_about_you_done = False
                    same_screen_count = 0
                    await asyncio.sleep(2.0)
                    continue

                # Chưa tới mốc — chỉ đợi page nav natively
                await asyncio.sleep(0.5)
                continue

            # Step C: chưa submit → poll OTP + submit
            # Đợi OTP input fully ready
            try:
                otp_selector = await _wait_otp_form(page, timeout_seconds=10.0, log=log)
            except BrowserPhaseError:
                await asyncio.sleep(0.5)
                continue

            # Wait 10s trước poll lần đầu — mail iCloud HME forward delay 2-10s.
            # Poll ngay sau register POST sẽ trả None (chưa có mail) → trigger
            # resend sớm + lãng phí 60s mini_timeout. Chỉ chạy 1 lần / session.
            if not _otp_first_poll_wait_done:
                _otp_first_poll_wait_done = True
                log("[flow] đợi 10s cho mail OTP về trước khi poll lần đầu...")
                await asyncio.sleep(10.0)

            await asyncio.sleep(1.0)
            # Reset timestamp khi sắp poll — bỏ qua code cũ trước thời điểm này
            poll_started = datetime.now(timezone.utc).replace(microsecond=0)
            t_otp = time.monotonic()
            recipient = request.source_email or request.email
            log(f"[flow] polling OTP (recipient={recipient}) since {poll_started.isoformat()}")
            
            # Poll OTP, skip codes đã thử.
            # Nếu đợi >resend_after_seconds chưa có code mới → click Resend rồi poll tiếp.
            # iCloud có thể gửi mail mới trước, mail cũ delay → lấy nhiều codes
            # rồi thử lần lượt trước khi resend.
            # Ngưỡng resend cap 60s (yêu cầu user): icloud_v3 mail đôi khi không
            # về / delay; chờ 90s mới resend là quá lâu → 1 phút không có mail
            # thì resend 1 phát.
            resend_after_seconds = min(float(request.otp_resend_after_seconds), 60.0)
            resend_count = 0
            # Resend tối đa 2 lần (tăng từ 1): scenario Step A đã click Resend
            # trước khi vào re-poll, mailbox HME có thể stuck stale → cần force
            # Resend lần 2 sau ~30s stale. Spam >2 Resend trigger OpenAI rate limit.
            max_resends = 2
            # Cap stale_poll_count: sau N polls chỉ thấy code cũ → force Resend.
            # Mailbox HME đôi khi cache stale; force Resend reset trạng thái server.
            _STALE_RESEND_THRESHOLD = 6  # ~30s với poll_interval=5s
            _STALE_FATAL_THRESHOLD = 18  # ~90s — 3x threshold safety net, raise
            stale_poll_count = 0  # đếm lần poll chỉ nhận code cũ
            while True:
                # Nếu có codes pending chưa submit → thử từng cái
                if pending_codes:
                    otp_code = pending_codes.pop(0)
                    break
                remaining = request.otp_timeout_seconds - (time.monotonic() - t_otp)
                if remaining <= 0:
                    raise BrowserPhaseError(f"OTP timeout {request.otp_timeout_seconds}s, chỉ nhận được codes cũ")
                # Poll với mini-timeout = min(resend_after_seconds, remaining)
                mini_timeout = min(resend_after_seconds, remaining)
                try:
                    otp_code = await mail_provider.poll_otp(
                        recipient=recipient,
                        started_at=poll_started,
                        timeout_seconds=mini_timeout,
                        poll_interval_seconds=request.otp_poll_interval_seconds,
                        log=log,
                    )
                except OutlookComboError:
                    raise
                except Exception:
                    otp_code = None
                if otp_code and otp_code not in tried_codes:
                    # Nhận code mới → fetch lại tất cả codes để catch mail delay
                    await asyncio.sleep(3.0)
                    all_codes: list[str] = []
                    if hasattr(mail_provider, 'poll_all_codes'):
                        all_codes = await mail_provider.poll_all_codes(
                            recipient=recipient,
                            started_at=poll_started,
                            log=log,
                        )
                    # Lọc codes chưa thử, giữ thứ tự
                    new_codes = [c for c in all_codes if c not in tried_codes]
                    if not new_codes:
                        new_codes = [otp_code]
                    elif otp_code not in new_codes:
                        new_codes.insert(0, otp_code)
                    if len(new_codes) > 1:
                        log(f"[flow] got {len(new_codes)} OTP codes: {', '.join(new_codes)}")
                    pending_codes = new_codes
                    continue  # loop lại → pop từ pending_codes
                if otp_code and otp_code in tried_codes:
                    stale_poll_count += 1
                    log(f"[flow] OTP={otp_code} đã thử rồi, chờ code mới... (lần {stale_poll_count})")

                    # Safety net: stale quá lâu (~90s) → mailbox/server stuck,
                    # raise để autoreg/manager pick email kế thay vì kẹt 5 phút.
                    if stale_poll_count >= _STALE_FATAL_THRESHOLD:
                        raise BrowserPhaseError(
                            f"mailbox stuck với code cũ {otp_code} sau "
                            f"{stale_poll_count} polls ({stale_poll_count * request.otp_poll_interval_seconds:.0f}s) — "
                            f"server không gửi code mới hoặc HME relay hỏng. "
                            f"Đã resend {resend_count}/{max_resends} lần."
                        )

                    # Trigger Resend khi stale lâu (force server gửi code mới).
                    # Quota max_resends áp dụng — nếu hết, chỉ wait + retry timeout.
                    if stale_poll_count >= _STALE_RESEND_THRESHOLD and resend_count < max_resends:
                        resend_count += 1
                        log(
                            f"[flow] {stale_poll_count} polls liên tục chỉ thấy "
                            f"code cũ — force Resend ({resend_count}/{max_resends})"
                        )
                        try:
                            resend_btn = page.locator(
                                'button:has-text("Resend"), a:has-text("Resend")'
                            ).first
                            await resend_btn.click(timeout=3000)
                            log("[flow] clicked 'Resend email' (force sau stale)")
                        except Exception as exc:
                            log(f"[flow] resend button not found: {type(exc).__name__}: {exc}")
                        # Reset stale counter + poll_started để chỉ nhận code mới
                        stale_poll_count = 0
                        poll_started = datetime.now(timezone.utc).replace(microsecond=0)
                        await asyncio.sleep(2.0)
                        continue

                    await asyncio.sleep(request.otp_poll_interval_seconds)
                    continue
                # otp_code is None → hết resend_after_seconds mà KHÔNG có mail nào về
                if resend_count < max_resends:
                    resend_count += 1
                    log(f"[flow] OTP chưa nhận sau {mini_timeout:.0f}s — click Resend ({resend_count}/{max_resends})")
                    try:
                        resend_btn = page.locator('button:has-text("Resend"), a:has-text("Resend")').first
                        await resend_btn.click(timeout=3000)
                        log("[flow] clicked 'Resend email'")
                    except Exception as exc:
                        log(f"[flow] resend button not found: {exc}")
                    # Reset poll_started để chỉ nhận code mới sau resend
                    await asyncio.sleep(2.0)
                    poll_started = datetime.now(timezone.utc).replace(microsecond=0)
                else:
                    # Đã resend hết hạn mức — không resend thêm, tiếp tục poll tới
                    # khi code về hoặc hết otp_timeout (tránh spam resend).
                    log(f"[flow] OTP chưa về sau {mini_timeout:.0f}s — đã resend {resend_count} lần, tiếp tục chờ...")
            
            otp_seconds_total += time.monotonic() - t_otp
            log(f"[flow] OTP={otp_code} got in {time.monotonic() - t_otp:.1f}s")
            # Đã LẤY ĐƯỢC OTP → gia hạn deadline nội bộ + báo watchdog ngoài, đảm bảo
            # đủ thời gian submit + /about-you + session, không bị kill giữa chừng.
            _grace_deadline = time.monotonic() + post_otp_grace
            if _grace_deadline > deadline:
                deadline = _grace_deadline
                log(f"[flow] OTP secured — gia hạn flow +{post_otp_grace:.0f}s để hoàn tất")
            if on_checkpoint is not None:
                try:
                    on_checkpoint("otp")
                except Exception:
                    pass
            tried_codes.add(otp_code)
            otp_continue_url, otp_source, otp_status = await _submit_otp(
                ctx, page, otp_code=otp_code, otp_selector=otp_selector, log=log,
            )
            otp_submitted = True
            _otp_submit_ts = time.monotonic()
            _otp_last_code = otp_code
            _otp_last_status = otp_status
            _otp_force_about_you_done = False
            otp_already_polled = True

            # ── Status-driven branching ──────────────────────────────
            # 200 + continue_url + source="api": page KHÔNG biết nav, manual goto
            # 200 + continue_url + source="ui":  page tự nav, KHÔNG goto (race)
            # 200 + no continue_url:             server validated OK nhưng quên trả
            #                                    URL — fallback manual goto /about-you
            # 4xx (wrong/expired):               vòng lặp tiếp sẽ xử lý (Step A)
            #                                    → click Resend + re-poll
            #
            # GIỮ otp_submitted=True sau status=200 để Step B có thể trigger force
            # goto /about-you nếu page kẹt không nav. State machine sẽ tự exit OTP
            # branch khi screen đổi sang about_you / chatgpt (URL change). Nếu kẹt,
            # Step B sau 15s force goto, sau 30s re-poll code mới (failsafe).
            if otp_status == 200 and otp_continue_url and otp_source == "api":
                log(f"[flow] OTP OK (API fallback path) → goto {otp_continue_url}")
                try:
                    await page.goto(
                        otp_continue_url,
                        wait_until="domcontentloaded",
                        timeout=15000,
                    )
                except Exception as exc:
                    log(f"[flow] goto continue_url failed: {type(exc).__name__}: {exc}")
                # goto đã nav → state machine vòng tới sẽ detect screen mới
                # (about_you). Keep otp_submitted=True làm fallback nếu goto fail.
                same_screen_count = 0
            elif otp_status == 200 and otp_continue_url and otp_source == "ui":
                log(
                    f"[flow] OTP OK (UI form path) — page tự nav tới "
                    f"{otp_continue_url.split('?')[0]}, KHÔNG goto manual"
                )
                # Page tự nav natively. KHÔNG goto vì sẽ race với natural nav
                # (NS_BINDING_ABORTED). Keep otp_submitted=True; Step B sẽ force
                # goto /about-you nếu page kẹt >15s không nav.
                same_screen_count = 0
            elif otp_status == 200 and not otp_continue_url:
                # Server validated 200 nhưng body thiếu continue_url — rare.
                # Code đã consume, KHÔNG resubmit. Fallback goto /about-you.
                log(
                    f"[flow] OTP validated HTTP 200 nhưng body thiếu continue_url — "
                    f"fallback goto /about-you"
                )
                try:
                    await page.goto(
                        "https://auth.openai.com/about-you",
                        wait_until="domcontentloaded",
                        timeout=15000,
                    )
                except Exception as exc:
                    log(f"[flow] goto /about-you failed: {type(exc).__name__}: {exc}")
                same_screen_count = 0
            # status >= 400 hoặc 0 (no response observed): để vòng lặp tới (Step A/B)
            # xử lý — Step A handle 4xx, Step B handle 200 đang chờ nav.

            # Dwell jitter (anti-ban Task 2.3) sau submit OTP — page transition
            # tới /about-you cần realistic delay (sentinel observer record).
            from _human_input import dwell as _dwell_otp_done
            await _dwell_otp_done(1.5, 3.0)
            continue

        if screen == "passkey_enroll":
            log("[flow] passkey enrollment page — skipping")
            if await _skip_passkey(page, log=log):
                await asyncio.sleep(2.0)
            else:
                log("[flow] no skip button found on passkey page — waiting for page change")
                await asyncio.sleep(1.5)
            continue

        if screen == "about_you":
            try:
                await _wait_oai_sc(ctx, timeout_seconds=15, log=log)
            except BrowserPhaseError:
                pass  # cookie có thể chưa cần thiết, thử fill xem có pass không
            callback_url = await _fill_about_you(
                page, name=request.name, birthdate=request.birthdate,
                timeout_seconds=60.0, log=log,
            )
            # Sau /about-you có thể vẫn còn step (rare), tiếp tục loop để chờ chatgpt.com
            await _wait_chatgpt_session(ctx, page, timeout_seconds=60.0, log=log)
            # Hard policy: bắt buộc password phải được set (xem branch chatgpt
            # ở trên). About-you chỉ chạy sau khi đã qua register POST trong
            # signup flow → register_succeeded phải True; nếu False = bug nội bộ.
            if not register_succeeded and not login_attempted:
                raise BrowserPhaseError(
                    f"password chưa được set: flow chạm /about-you mà KHÔNG "
                    f"qua /create-account/password POST 200 — "
                    f"URL: {page.url}"
                )
            await _verify_account_session(ctx, page, log=log)
            return callback_url, otp_seconds_total

        # screen == 'unknown' → đợi page settle
        await asyncio.sleep(0.7)

    raise BrowserPhaseError(f"flow timeout {overall_timeout}s. last URL: {page.url}, last screen: {last_screen}")


async def _handle_login_after_password(
    *, ctx, page, request, mail_provider, callback_holder, log,
) -> tuple[str, float]:
    """Sau khi submit login password, xử lý cả 2 case:
    - Login thẳng → chatgpt.com
    - Cần OTP → poll OTP → submit → /about-you HOẶC chatgpt.com
    Returns: (callback_url, otp_seconds).
    """
    otp_seconds = 0.0
    login_branch = await _wait_after_login(page, timeout_seconds=20.0, log=log)
    if login_branch == "chatgpt":
        await _wait_chatgpt_session(ctx, page, timeout_seconds=30.0, log=log)
        await _verify_account_session(ctx, page, log=log)
        return callback_holder.get("url") or page.url, otp_seconds

    # Cần OTP cho login (hoặc account chưa hoàn thành onboarding)
    otp_selector = await _wait_otp_form(page, timeout_seconds=15.0, log=log)
    await asyncio.sleep(2.0)
    otp_started_at = datetime.now(timezone.utc).replace(microsecond=0)

    t_otp = time.monotonic()
    recipient = request.source_email or request.email
    log(f"[browser] polling OTP for login (recipient={recipient})")
    otp_code = await mail_provider.poll_otp(
        recipient=recipient,
        started_at=otp_started_at,
        timeout_seconds=request.otp_timeout_seconds,
        poll_interval_seconds=request.otp_poll_interval_seconds,
        log=log,
    )
    otp_seconds = time.monotonic() - t_otp
    log(f"[browser] login OTP={otp_code} in {otp_seconds:.1f}s")
    otp_continue_url, otp_source, otp_status = await _submit_otp(
        ctx, page, otp_code=otp_code, otp_selector=otp_selector, log=log,
    )
    if otp_status >= 400:
        raise BrowserPhaseError(
            f"login OTP rejected HTTP {otp_status} — account combo có thể đã đổi "
            f"password/2FA hoặc OTP expired. URL: {page.url}"
        )
    # Chỉ goto khi API fallback path. UI path → page tự nav.
    if otp_continue_url and otp_source == "api":
        log(f"[browser] login OTP OK (API path) → goto {otp_continue_url}")
        try:
            await page.goto(
                otp_continue_url, wait_until="domcontentloaded", timeout=15000,
            )
        except Exception as exc:
            log(f"[browser] goto continue_url failed: {type(exc).__name__}: {exc}")
    elif otp_continue_url and otp_source == "ui":
        log(
            f"[browser] login OTP OK (UI path) — page tự nav tới "
            f"{otp_continue_url.split('?')[0]}, đợi state machine detect"
        )

    # Sau OTP có 2 case:
    # 1. /about-you (account chưa onboard) → fill name+age → callback
    # 2. chatgpt.com (login bình thường) → wait session-token
    otp_branch = await _wait_after_otp(page, ctx=ctx, timeout_seconds=60.0, log=log)
    if otp_branch == "signup":
        await _wait_oai_sc(ctx, timeout_seconds=15, log=log)
        callback_url = await _fill_about_you(
            page,
            name=request.name,
            birthdate=request.birthdate,
            timeout_seconds=30.0,
            log=log,
        )
    else:
        callback_url = callback_holder.get("url") or page.url

    await _wait_chatgpt_session(ctx, page, timeout_seconds=60.0, log=log)
    # Verify /api/auth/session — login flow phải có accessToken + user.id (xem
    # _verify_account_session). Áp dụng cho cả login lẫn signup-after-login.
    await _verify_account_session(ctx, page, log=log)
    return callback_url, otp_seconds


async def _wait_after_otp(page, *, ctx, timeout_seconds: float, log) -> str:
    """Sau submit OTP, đợi navigation: /about-you (signup) hoặc chatgpt.com (login).

    Returns: "signup" hoặc "login".
    Escalation: 10s re-click → 18s JS submit → 25s API fallback → timeout.
    """
    deadline = time.monotonic() + timeout_seconds
    start_ts = time.monotonic()
    _reclick_done = False
    _js_done = False
    _api_done = False
    while time.monotonic() < deadline:
        cur = page.url
        if "auth.openai.com/about-you" in cur:
            log("[browser] reached /about-you (signup)")
            return "signup"
        if "chatgpt.com" in cur and "auth.openai.com" not in cur and "/auth/error" not in cur:
            log("[browser] redirected to chatgpt.com (login — account exists)")
            return "login"
        if "auth/error" in cur:
            raise BrowserPhaseError(f"error page: {cur}")
        # SPA case: URL vẫn /email-verification nhưng form /about-you đã render
        try:
            name_el = page.locator('input[name="name"], input[autocomplete="name"]').first
            if await name_el.is_visible(timeout=300):
                log("[browser] detected /about-you form (SPA, URL unchanged)")
                return "signup"
        except Exception:
            pass
        # Check OTP error message (wrong code)
        try:
            err_el = page.locator('[role="alert"], [class*="error"]').first
            err_text = await err_el.text_content(timeout=300)
            if err_text and ("wrong" in err_text.lower() or "invalid" in err_text.lower() or "incorrect" in err_text.lower()):
                raise BrowserPhaseError(f"OTP wrong code: {err_text.strip()}")
        except BrowserPhaseError:
            raise
        except Exception:
            pass
        # Escalation: re-click → JS submit → API fallback
        elapsed = time.monotonic() - start_ts
        if elapsed > 10.0 and not _reclick_done:
            try:
                otp_input = page.locator('input[name="code"]').first
                if await otp_input.is_visible(timeout=500):
                    val = await otp_input.input_value()
                    if val and len(val) == 6:
                        log(f"[browser] OTP form still visible after {elapsed:.0f}s — retrying submit (url={cur})")
                        for btn in ('button[type="submit"]', 'button:has-text("Continue")'):
                            try:
                                await page.click(btn, timeout=2000)
                                log(f"[browser] re-clicked {btn}")
                                break
                            except Exception:
                                continue
            except Exception:
                pass
            _reclick_done = True
        elif elapsed > 18.0 and not _js_done:
            log("[browser] OTP UI click không work — thử form.submit() qua JS")
            try:
                await page.evaluate("() => { const f = document.querySelector('form'); if (f) f.submit(); }")
            except Exception as exc:
                log(f"[browser] JS form.submit() failed: {type(exc).__name__}: {exc}")
            _js_done = True
        elif elapsed > 25.0 and not _api_done:
            log("[browser] OTP UI+JS không work — thử validate qua API")
            try:
                otp_input = page.locator('input[name="code"]').first
                otp_val = await otp_input.input_value()
                if otp_val and len(otp_val) == 6:
                    api_cu, api_status = await _submit_otp_via_api(
                        ctx, otp_code=otp_val, log=log
                    )
                    if api_status >= 400:
                        # Code đã consume từ submit UI trước đó → resubmit luôn
                        # trả 401. Bail-out để caller fail rõ ràng thay vì
                        # continue loop chờ vô ích.
                        log(
                            f"[browser] API fallback HTTP {api_status} — code "
                            f"đã consume (UI submit trước có thể đã 200 nhưng "
                            f"page kẹt). KHÔNG resubmit cùng code."
                        )
                    elif api_cu:
                        log(f"[browser] API fallback OK → goto {api_cu}")
                        try:
                            await page.goto(api_cu, wait_until="domcontentloaded", timeout=15000)
                        except Exception as exc:
                            log(f"[browser] goto api_cu failed: {type(exc).__name__}: {exc}")
            except Exception as exc:
                log(f"[browser] API fallback failed: {type(exc).__name__}: {exc}")
            _api_done = True
        await asyncio.sleep(0.5)
    raise BrowserPhaseError(f"timeout {timeout_seconds}s after OTP submit. URL: {page.url}")


async def _check_about_you_extras(page, *, log) -> None:
    """Check + handle các element bổ sung trên /about-you (checkbox TOS, select, etc.)."""
    # Checkbox — check tất cả unchecked checkboxes (TOS, marketing opt-in, etc.)
    try:
        checkboxes = page.locator('input[type="checkbox"]')
        count = await checkboxes.count()
        for i in range(count):
            cb = checkboxes.nth(i)
            if await cb.is_visible(timeout=300) and not await cb.is_checked():
                await cb.check(timeout=2000)
                label = ""
                try:
                    parent = cb.locator("xpath=ancestor::label")
                    label = (await parent.text_content(timeout=500) or "").strip()[:60]
                except Exception:
                    pass
                log(f"[browser] /about-you checked checkbox: {label or f'#{i}'}")
    except Exception:
        pass

    # Select dropdowns — nếu có select chưa chọn, chọn option đầu tiên có value
    try:
        selects = page.locator("select")
        count = await selects.count()
        for i in range(count):
            sel = selects.nth(i)
            if await sel.is_visible(timeout=300):
                val = await sel.input_value()
                if not val:
                    # Chọn option đầu tiên có value thực
                    first_option = await sel.evaluate("""
                        (el) => {
                            const opts = [...el.options].filter(o => o.value && o.value !== '');
                            return opts.length > 0 ? opts[0].value : null;
                        }
                    """)
                    if first_option:
                        await sel.select_option(first_option, timeout=2000)
                        log(f"[browser] /about-you selected option: {first_option}")
    except Exception:
        pass


async def _click_submit_about_you(page, *, log) -> None:
    """Click submit button trên /about-you form."""
    for btn in (
        'button:has-text("Finish creating account")',
        'button:has-text("Finish")',
        'button[type="submit"]',
        'button:has-text("Continue")',
        'button:has-text("Agree")',
        'button:has-text("Next")',
        'button:has-text("Submit")',
    ):
        try:
            btn_el = page.locator(btn).first
            if await btn_el.is_visible(timeout=800) and await btn_el.is_enabled(timeout=500):
                await btn_el.click(timeout=3000)
                log(f"[browser] clicked {btn}")
                return
        except Exception:
            continue
    # Fallback: click bất kỳ button nào visible + enabled (trừ modal dismiss)
    try:
        all_btns = page.locator("button")
        count = await all_btns.count()
        for i in range(count):
            b = all_btns.nth(i)
            if await b.is_visible(timeout=300) and await b.is_enabled(timeout=300):
                text = ((await b.text_content(timeout=500)) or "").strip().lower()
                if text and not any(k in text for k in ("cancel", "back", "sign out", "log out")):
                    await b.click(timeout=3000)
                    log(f"[browser] fallback clicked button: {text[:40]}")
                    return
    except Exception:
        pass


async def _confirm_birthday_dialog(page, *, log) -> bool:
    """Bấm "OK" trên dialog confirm ngày sinh nếu nó đang hiển thị.

    Flow /about-you (variant date-picker): sau khi submit, OpenAI bật dialog
    "You're setting your birthday to <ngày> … it won't be shared" với 2 nút
    "OK" và "Cancel". Phải bấm OK để xác nhận, KHÔNG được bấm Cancel.

    Dùng ``:text-is`` (exact match) để chỉ bắt đúng nút "OK" — tránh
    ``:has-text`` vì nó match substring (vd "Okay"/"Cancel" lẫn lộn).

    Tối ưu: gộp 4 selector vào 1 locator + chỉ 1 lần ``is_visible`` ngắn
    (~120ms) thay vì 4 probe × 200ms — hàm này được gọi mỗi vòng lặp chờ
    callback nên phải rẻ. Trả True khi đã bấm, False khi không có dialog.
    """
    ok_btn = page.locator(
        'button:text-is("OK"), button:text-is("Ok"), '
        '[role="button"]:text-is("OK"), button:has-text("Confirm")'
    ).first
    try:
        if await ok_btn.is_visible(timeout=120):
            await ok_btn.click(timeout=2000)
            log("[browser] /about-you confirm birthday: clicked OK")
            return True
    except Exception:
        pass
    return False


async def _detect_about_you_form_error(page) -> str | None:
    """Detect validation error message trên /about-you form. Return message hoặc None."""
    try:
        for sel in (
            '[role="alert"]',
            '[class*="error"]',
            '[class*="Error"]',
            '[aria-invalid="true"]',
            '.field-error',
            '[data-testid*="error"]',
        ):
            el = page.locator(sel).first
            if await el.is_visible(timeout=200):
                text = (await el.text_content(timeout=500) or "").strip()
                if text:
                    return text[:200]
    except Exception:
        pass
    return None


async def _refill_about_you_birth(page, *, birthdate: str, log) -> bool:
    """Điền lại field ngày sinh/năm sinh trên /about-you với giá trị ĐÚNG.

    Gọi khi form báo lỗi validation năm sinh ("Enter a valid year of birth"):
    submit lại cùng giá trị sai là vô nghĩa — phải sửa input trước. Đọc
    min/max để biết field cần NĂM SINH hay TUỔI. Trả True nếu re-fill được.
    """
    from _human_input import (
        human_type as _human_type,
        resolve_birth_field_value,
    )

    # Date input → fill atomic
    try:
        date_loc = page.locator('input[type="date"]').first
        if await date_loc.is_visible(timeout=200):
            await page.fill('input[type="date"]', birthdate)
            log(f"[browser] re-filled birthday={birthdate} (sau form error)")
            return True
    except Exception:
        pass

    for sel in ('input[name="age"]', 'input[type="number"]', 'input[inputmode="numeric"]'):
        try:
            loc = page.locator(sel).first
            if not await loc.is_visible(timeout=200):
                continue
        except Exception:
            continue
        try:
            value, kind = await resolve_birth_field_value(loc, birthdate, log=log)
            await loc.fill("")
            await _human_type(loc, value, delay_min_ms=40, delay_max_ms=100, log=log)
            log(f"[browser] re-filled {kind}={value} (sau form error)")
            return True
        except Exception as exc:
            log(f"[browser] re-fill birth field failed ({sel}): {exc}")
    return False


async def _log_about_you_dom(page, *, log) -> None:
    """Log DOM snapshot nhẹ của /about-you form khi hết retry — giúp debug."""
    try:
        snapshot = await page.evaluate("""
            () => {
                const form = document.querySelector('form');
                if (!form) return {form: null, buttons: [], inputs: []};
                const inputs = [...form.querySelectorAll('input, select, textarea')].map(el => ({
                    tag: el.tagName, type: el.type || '', name: el.name || '',
                    value: el.value ? el.value.substring(0, 30) : '',
                    valid: el.validity ? el.validity.valid : true,
                    validationMsg: el.validationMessage || '',
                }));
                const buttons = [...form.querySelectorAll('button')].map(el => ({
                    text: (el.textContent || '').trim().substring(0, 40),
                    type: el.type || '', disabled: el.disabled,
                }));
                return {inputs, buttons};
            }
        """)
        log(f"[browser] /about-you DOM snapshot: {json.dumps(snapshot, ensure_ascii=False)[:500]}")
    except Exception as exc:
        log(f"[browser] /about-you DOM snapshot failed: {exc}")


async def _fill_about_you(page, *, name: str, birthdate: str, timeout_seconds: float, log) -> str:
    """Điền form /about-you (name + age), submit, return callback URL.

    CALLBACK CAPTURE STRATEGY (thay đổi 2026-05):
      - Dùng RESPONSE listener thay vì REQUEST listener để xác nhận
        callback đã thật sự thành công (có Set-Cookie session-token).
      - Lý do: request listener fire NGAY khi request đi ra, chưa biết
        server có response 200/302 hay không, có set cookie hay chưa.
        Đây là root cause của bug "callback URL captured" rồi vẫn
        timeout waiting session-token (page kẹt /about-you).
      - Fail-fast: nếu response status >= 400 → raise (account creation failed).
    """
    log(f"[browser] /about-you: fill name={name!r}")

    # Capture callback URL via RESPONSE listener (xác nhận server đã commit cookie)
    callback_holder: dict[str, Any] = {}

    def _on_resp(response):
        url = response.url
        if "chatgpt.com/api/auth/callback/openai" not in url or "code=" not in url:
            return
        # Đã capture rồi — bỏ qua (chỉ giữ lần đầu)
        if "url" in callback_holder:
            return
        status = response.status
        callback_holder["status"] = status
        # Status 2xx/3xx → callback OK. NextAuth thường trả 302 redirect.
        if 200 <= status < 400:
            callback_holder["url"] = url
            # Probe Set-Cookie từ headers (best-effort, có thể không thấy
            # do Playwright không expose Set-Cookie trên cross-origin redirect).
            try:
                set_cookie = response.headers.get("set-cookie", "")
                has_session = "next-auth.session-token" in set_cookie
                callback_holder["has_session_in_setcookie"] = has_session
                log(
                    f"[browser] callback response OK: HTTP {status} "
                    f"set-cookie-has-session={has_session}"
                )
            except Exception:
                log(f"[browser] callback response OK: HTTP {status}")
        else:
            # 4xx/5xx — log để debug, không raise ngay (background event)
            callback_holder["error_status"] = status
            log(f"[browser] callback response FAILED: HTTP {status}")

    page.on("response", _on_resp)
    try:
        # ── Human typing helpers (anti-ban Task 2.2) ──
        from _human_input import (
            human_type as _human_type,
            dwell as _dwell,
            resolve_birth_field_value,
        )

        # Name input
        name_input = None
        for sel in ('input[name="name"]', 'input[autocomplete="name"]', 'input[id*="name" i]'):
            try:
                await page.wait_for_selector(sel, state="visible", timeout=5000)
                name_input = sel
                break
            except Exception:
                continue
        if not name_input:
            raise BrowserPhaseError("không tìm thấy name input trên /about-you")

        # Human-type name (Gaussian delay, đã giảm tốc độ — gõ nhanh)
        name_loc = page.locator(name_input).first
        await _human_type(name_loc, name, delay_min_ms=40, delay_max_ms=100, log=log)
        await _dwell(0.2, 0.5)  # tab/look at next field

        # Birth field (parse from birthdate). Validate format fail-fast; giá
        # trị thực (năm sinh vs tuổi) quyết định lúc điền theo min/max của input.
        if len(birthdate.split("-")) != 3:
            raise BrowserPhaseError(f"birthdate format sai: {birthdate}")

        # Try date input first, fallback to age number input. Probe ngắn 600ms:
        # khi đã gõ xong name thì form render xong rồi, không cần chờ 1.5s.
        date_input = None
        try:
            date_input = await page.wait_for_selector('input[type="date"]', state="visible", timeout=600)
        except Exception:
            pass

        if date_input:
            # Date input không cần per-key typing (browser native picker); fill atomic.
            await page.fill('input[type="date"]', birthdate)
            log(f"[browser] filled birthday={birthdate}")
        else:
            age_input = None
            for sel in ('input[name="age"]', 'input[type="number"]', 'input[inputmode="numeric"]'):
                try:
                    await page.wait_for_selector(sel, state="visible", timeout=1500)
                    age_input = sel
                    break
                except Exception:
                    continue
            if age_input:
                age_loc = page.locator(age_input).first
                # OpenAI A/B test: field number có thể là NĂM SINH (min 1896,
                # validation "Enter a valid year of birth") HOẶC TUỔI — đọc
                # min/max để điền đúng (resolve_birth_field_value).
                value, kind = await resolve_birth_field_value(age_loc, birthdate, log=log)
                await _human_type(age_loc, value, delay_min_ms=40, delay_max_ms=100, log=log)
                log(f"[browser] human-typed {kind}={value}")
            else:
                # Fallback: Tab + keyboard type. Không có loc để đọc min/max →
                # điền NĂM SINH (UI /about-you hiện tại là year-of-birth).
                await page.keyboard.press("Tab")
                await _dwell(0.2, 0.5)
                # Keyboard type with random delay per char (giảm tốc độ)
                import random as _rand
                birth_year = birthdate.split("-")[0]
                for ch in birth_year:
                    await page.keyboard.type(
                        ch, delay=_rand.randint(40, 100),
                    )
                log(f"[browser] Tab + human-typed year={birth_year}")

        # Handle unchecked checkboxes/TOS trước submit — OpenAI có thể thêm field mới
        await _check_about_you_extras(page, log=log)

        # Dwell ngắn trước submit (giảm fake human action — bỏ mouse wander,
        # max 1s). Sentinel observer đã có đủ keydown events từ human_type.
        await asyncio.sleep(0.3)

        # Submit ("Finish creating account")
        await _click_submit_about_you(page, log=log)

        # Sau submit, OpenAI bật dialog confirm ngày sinh ("You're setting your
        # birthday to …") → bấm OK để xác nhận tuổi.
        await _dwell(0.2, 0.5)
        if await _confirm_birthday_dialog(page, log=log):
            await _dwell(0.2, 0.4)

        # Đợi callback URL hoặc navigate đến chatgpt.com
        deadline = time.monotonic() + timeout_seconds
        next_retry_at = time.monotonic() + 8.0
        submit_attempts = 1
        max_submit_attempts = 5
        passkey_skip_attempted = False
        dom_logged = False
        while time.monotonic() < deadline:
            # Fail-fast nếu response callback trả error
            if "error_status" in callback_holder and "url" not in callback_holder:
                raise BrowserPhaseError(
                    f"callback /api/auth/callback/openai failed: "
                    f"HTTP {callback_holder['error_status']}"
                )
            if "url" in callback_holder:
                # Sleep ngắn để cookie jar commit (response → cookie store ghi)
                # trước khi return cho caller poll cookies.
                await asyncio.sleep(0.4)
                log(
                    f"[browser] callback URL captured "
                    f"(HTTP {callback_holder.get('status', '?')})"
                )
                return callback_holder["url"]
            cur = page.url
            if "auth/error" in cur:
                raise BrowserPhaseError(f"error page: {cur}")
            # Nếu page đã navigate ra khỏi /about-you → chatgpt.com
            if "chatgpt.com" in cur:
                log("[browser] navigated to chatgpt.com (no explicit callback)")
                return callback_holder.get("url") or cur
            # Birthday confirm dialog ("You're setting your birthday to …") → OK.
            # Dialog có thể render trễ sau submit nên check mỗi vòng lặp.
            if await _confirm_birthday_dialog(page, log=log):
                await asyncio.sleep(0.4)
                continue
            # Detect consent/modal buttons mới — 1 probe gộp (trước đây 5 probe
            # × is_visible 200ms = ~1s/vòng, trong lúc page điều hướng OAuth làm
            # chậm phát hiện callback ~7s). Gộp + timeout ngắn.
            try:
                modal_btn = page.locator(
                    'button:has-text("Okay"), button:has-text("I agree"), '
                    'button:has-text("Accept"), button:has-text("Got it"), '
                    'button:has-text("Let")'
                ).first
                if await modal_btn.is_visible(timeout=150):
                    await modal_btn.click(timeout=2000)
                    log("[browser] clicked modal button (consent)")
            except Exception:
                pass
            # Passkey enrollment — skip
            if "passkey" in cur.lower():
                if not passkey_skip_attempted:
                    passkey_skip_attempted = True
                    if await _skip_passkey(page, log=log):
                        await asyncio.sleep(1.0)
                        continue
                    log("[browser] passkey skip failed — waiting for natural navigation")
                await asyncio.sleep(1.0)
                continue
            if passkey_skip_attempted and "passkey" not in cur.lower():
                passkey_skip_attempted = False
            # Retry submit nếu vẫn stuck /about-you — mỗi 8s, tối đa max_submit_attempts
            if "about-you" in cur and time.monotonic() > next_retry_at:
                if submit_attempts < max_submit_attempts:
                    submit_attempts += 1
                    # Detect form validation errors trước khi retry
                    form_err = await _detect_about_you_form_error(page)
                    if form_err:
                        log(f"[browser] /about-you form error: {form_err}")
                        # Fatal error_code (user_already_exists, …) → dừng luôn,
                        # KHÔNG retry. Server đã commit kết quả, retry vô ích.
                        err_lower = form_err.lower()
                        for fatal_code in _ABOUT_YOU_FATAL_ERROR_CODES:
                            if fatal_code in err_lower:
                                if fatal_code == "user_already_exists":
                                    raise AccountAlreadyExistsError(
                                        f"/about-you: user_already_exists — "
                                        f"account đã tồn tại, bỏ"
                                    )
                                raise BrowserPhaseError(
                                    f"/about-you fatal error_code: {fatal_code}"
                                )
                        # Validation field ngày sinh ("Enter a valid year of
                        # birth") → re-fill ĐÚNG giá trị (năm sinh/tuổi) thay vì
                        # spam submit cùng input sai (root cause timeout 60s).
                        if any(k in err_lower for k in (
                            "year of birth", "date of birth", "birth",
                            "valid age", "your age",
                        )):
                            await _refill_about_you_birth(page, birthdate=birthdate, log=log)
                    # Re-check extras (checkbox/TOS xuất hiện sau render)
                    await _check_about_you_extras(page, log=log)
                    # Thử submit lại với chiến thuật escalating
                    if submit_attempts <= 3:
                        await _click_submit_about_you(page, log=log)
                        # Confirm dialog ngày sinh nếu nó bật sau submit
                        await asyncio.sleep(0.4)
                        await _confirm_birthday_dialog(page, log=log)
                    else:
                        # Escalate: Enter key + JS dispatch
                        log(f"[browser] /about-you submit attempt {submit_attempts} — trying Enter + JS")
                        try:
                            await page.keyboard.press("Enter")
                        except Exception:
                            pass
                        await asyncio.sleep(1.0)
                        if "about-you" in page.url:
                            try:
                                await page.evaluate("""
                                    () => {
                                        const form = document.querySelector('form');
                                        if (form) {
                                            form.requestSubmit
                                                ? form.requestSubmit()
                                                : form.submit();
                                        }
                                    }
                                """)
                                log("[browser] JS form.requestSubmit() dispatched")
                            except Exception as exc:
                                log(f"[browser] JS submit failed: {exc}")
                    next_retry_at = time.monotonic() + 8.0
                elif not dom_logged:
                    # Hết retry — log DOM snapshot 1 lần để debug
                    dom_logged = True
                    await _log_about_you_dom(page, log=log)
            await asyncio.sleep(0.3)

        # Fallback: page.url nếu đã navigate qua callback hoặc chatgpt.com
        if "chatgpt.com" in page.url:
            return callback_holder.get("url") or page.url
        if "callback" in page.url and "code=" in page.url:
            return page.url

        raise BrowserPhaseError(f"timeout {timeout_seconds}s waiting callback URL. URL: {page.url}")
    finally:
        try:
            page.remove_listener("response", _on_resp)
        except Exception:
            pass


async def _verify_account_session(
    ctx,
    page,
    *,
    log,
    timeout_seconds: float = 20.0,
) -> dict:
    """Gọi `/api/auth/session` qua fetch() trong page context để confirm acc
    đã login thành công sau reg.

    Yêu cầu cứng (fail-fast nếu thiếu):
      - accessToken (JWT): bắt buộc — chứng minh server đã issue token.
      - user.id: bắt buộc — chứng minh tài khoản có thật trong DB.

    Nếu page chưa ở chatgpt.com → goto trước (relative fetch /api/auth/session
    cần đúng origin). Retry tối đa 4 lần (5s/lần) trong window timeout_seconds
    vì NextAuth có thể commit cookies trễ vài giây sau callback.

    Trả về session dict đầy đủ. Raise BrowserPhaseError nếu hết retry.
    """
    if "chatgpt.com" not in page.url:
        log(f"[verify] page đang ở {page.url.split('?')[0]} — goto chatgpt.com trước")
        try:
            await page.goto(
                "https://chatgpt.com/",
                wait_until="domcontentloaded",
                timeout=20_000,
            )
        except Exception as exc:
            log(f"[verify] goto chatgpt.com fail: {type(exc).__name__}: {exc}")

    deadline = time.monotonic() + timeout_seconds
    attempt = 0
    last_error = "no attempt"
    while time.monotonic() < deadline:
        attempt += 1
        try:
            result = await page.evaluate(
                """
                async () => {
                    try {
                        const r = await fetch('/api/auth/session', {
                            credentials: 'include',
                            headers: {'Accept': 'application/json'},
                        });
                        const text = await r.text();
                        return {ok: r.ok, status: r.status, body: text};
                    } catch (e) {
                        return {ok: false, status: 0, error: String(e)};
                    }
                }
                """
            )
        except Exception as exc:
            last_error = f"page.evaluate fail: {type(exc).__name__}: {exc}"
            log(f"[verify] attempt {attempt}: {last_error}")
            await asyncio.sleep(2.0)
            continue

        status = result.get("status", 0)
        if not result.get("ok"):
            last_error = (
                f"HTTP {status}: {result.get('error') or (result.get('body') or '')[:120]}"
            )
            log(f"[verify] attempt {attempt}: /api/auth/session {last_error}")
            await asyncio.sleep(2.0)
            continue

        body_text = result.get("body") or ""
        try:
            data = json.loads(body_text) if body_text else {}
        except Exception as exc:
            last_error = f"JSON parse fail: {exc} body={body_text[:120]!r}"
            log(f"[verify] attempt {attempt}: {last_error}")
            await asyncio.sleep(2.0)
            continue

        access_token = data.get("accessToken") or ""
        user = data.get("user") or {}
        user_id = user.get("id") or ""

        if access_token and user_id:
            log(
                f"[verify] /api/auth/session OK "
                f"(user_id={str(user_id)[:24]}..., access_token len={len(access_token)}, "
                f"expires={data.get('expires', '?')})"
            )
            return data

        last_error = (
            f"session thiếu accessToken/user.id "
            f"(has_token={bool(access_token)}, has_user_id={bool(user_id)}, "
            f"keys={list(data.keys())[:8]})"
        )
        log(f"[verify] attempt {attempt}: {last_error} — chờ NextAuth commit cookies")
        await asyncio.sleep(2.0)

    raise BrowserPhaseError(
        f"verify /api/auth/session FAIL sau {attempt} attempt "
        f"(timeout {timeout_seconds}s): {last_error}"
    )


async def _wait_chatgpt_session(ctx, page, *, timeout_seconds: float, log) -> None:
    """Đợi cookie session-token xuất hiện trên chatgpt.com.

    STRATEGY (thay đổi 2026-05):
      - Yêu cầu cứng: `__Secure-next-auth.session-token` (hoặc chunk .0).
        Đây là cookie DUY NHẤT mà Phase 2 (http_phase) cần.
      - BỎ điều kiện `_account` — cookie này chỉ được set khi browser
        navigate top-level tới chatgpt.com, KHÔNG phải lúc nào cũng tự xảy ra
        sau callback (callback OAuth chạy qua fetch background, page có thể
        vẫn ở auth.openai.com/about-you). Yêu cầu `_account` từng gây
        timeout 60s waiting session-token mặc dù callback đã OK.
      - FALLBACK: sau ~8s không thấy session-token → chủ động
        page.goto("https://chatgpt.com/") để force browser load top-level
        (server sẽ set _account + commit cookies). Chỉ goto 1 lần.
      - Sau khi có session-token → return ngay (Phase 2 self-contained).
    """
    deadline = time.monotonic() + timeout_seconds
    fallback_goto_at = time.monotonic() + 8.0
    fallback_done = False
    last_log_at = 0.0
    while time.monotonic() < deadline:
        cookies = await ctx.cookies("https://chatgpt.com/")
        names = {c["name"] for c in cookies}
        has_session = (
            "__Secure-next-auth.session-token" in names
            or "__Secure-next-auth.session-token.0" in names
        )
        if has_session:
            has_account = "_account" in names
            log(
                f"[browser] chatgpt session ready "
                f"({len(cookies)} cookies, _account={has_account})"
            )
            await asyncio.sleep(0.3)
            return

        # Fallback: force navigate top-level để server commit cookies
        if not fallback_done and time.monotonic() > fallback_goto_at:
            fallback_done = True
            log(
                f"[browser] session-token chưa có sau 8s "
                f"(URL={page.url.split('?')[0]}) — force goto chatgpt.com"
            )
            try:
                await page.goto(
                    "https://chatgpt.com/",
                    wait_until="domcontentloaded",
                    timeout=20_000,
                )
                log(f"[browser] goto chatgpt.com done (URL={page.url.split('?')[0]})")
            except Exception as exc:
                log(
                    f"[browser] goto chatgpt.com failed "
                    f"({type(exc).__name__}: {exc}) — tiếp tục poll cookies"
                )

        # Log progress mỗi 5s để debug (không spam)
        now = time.monotonic()
        if now - last_log_at > 5.0:
            last_log_at = now
            chatgpt_names = sorted(n for n in names if not n.startswith("__cf"))[:8]
            log(
                f"[browser] still waiting session-token "
                f"(URL={page.url.split('?')[0]}, "
                f"{len(cookies)} cookies, top: {chatgpt_names})"
            )

        await asyncio.sleep(0.5)
    raise BrowserPhaseError(f"timeout {timeout_seconds}s waiting session-token. URL: {page.url}")


async def _wait_oai_sc(ctx, *, timeout_seconds: float, log) -> None:
    """Đợi cookie oai-sc (Sentinel SDK fired)."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        cookies = await ctx.cookies("https://auth.openai.com/")
        if any(c["name"] == "oai-sc" for c in cookies):
            log("[browser] sentinel cookie oai-sc ready")
            return
        await asyncio.sleep(0.5)
    raise BrowserPhaseError(f"timeout {timeout_seconds}s waiting oai-sc")



def _extract_state_from_authorize(url: str) -> str | None:
    """Parse state query param từ authorize URL."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    return qs["state"][0] if "state" in qs and qs["state"] else None


async def _extract_state_from_url(page, *, log) -> str | None:
    """Lấy state từ navigation history."""
    try:
        entries = await page.evaluate(
            "() => performance.getEntriesByType('navigation').concat(performance.getEntriesByType('resource'))"
            ".map(e => e.name).filter(u => u.includes('state='))"
        )
        for entry in entries or []:
            parsed = urlparse(entry)
            qs = parse_qs(parsed.query)
            if "state" in qs and qs["state"][0]:
                return qs["state"][0]
    except Exception as exc:
        log(f"[browser] state extract failed: {exc}")
    return None


# ─────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────

async def run_browser_phase(
    *,
    request: SignupRequest,
    settings: Settings,
    mail_provider: MailProvider,
    otp_started_at: datetime,
    log,
    on_checkpoint=None,
) -> tuple[BrowserHandoff, float]:
    """Phase 1: browser signup + set password post-login.

    on_checkpoint: callback(stage:str) — gọi khi đã lấy được OTP để watchdog
        bên ngoài gia hạn deadline (tránh kill job ngay sau khi có OTP).

    Returns: (handoff, otp_seconds).
    """
    if request.tls_insecure:
        from config import warn_insecure_tls
        warn_insecure_tls("browser_phase")
        log("[security] TLS verification DISABLED — debug mode")

    engine = settings.browser_engine or "chrome"
    job_id = f"hybrid_{uuid.uuid4().hex[:10]}"

    # Profile
    if engine == "camoufox":
        profile_dir = settings.profiles_dir / f"camoufox_{job_id}"
        template_dir = settings.browser_camoufox_profile_dir
    else:
        profile_dir = settings.profile_dir_for(job_id)
        template_dir = settings.browser_profile_template_dir

    ensure_runtime_dirs(settings, extra=(profile_dir,))
    prepare_profile_dir(
        profile_dir=profile_dir,
        template_dir=template_dir,
        use_template=request.profile_template,
    )

    # HAR capture + debug artifacts (Playwright trace = actions + DOM snapshots +
    # screenshots; HTML/screenshot dump khi lỗi/stuck). Bật cùng --har để mỗi lần
    # test có đủ artifact tối ưu khi flow lỗi/treo.
    har_kwargs: dict[str, Any] = {}
    debug_capture = bool(request.har_capture)
    debug_dir = settings.runtime_dir / "har_hybrid"
    trace_path = debug_dir / f"trace-{datetime.now():%Y%m%d-%H%M%S}-{job_id}.zip"
    if request.har_capture:
        har_dir = debug_dir
        har_dir.mkdir(parents=True, exist_ok=True)
        har_path = har_dir / f"hybrid-{datetime.now():%Y%m%d-%H%M%S}-{job_id}.har"
        har_kwargs["record_har_path"] = str(har_path)
        har_kwargs["record_har_content"] = "embed"
        har_kwargs["record_har_mode"] = "full"
        log(f"[browser] HAR capture → {har_path}")
        log(f"[browser] trace + HTML dump → {debug_dir} (on stuck/error)")

    device_id = str(uuid.uuid4())
    # auth_session_logging_id: KHÔNG gen ở đây nữa — defer tới sau khi load
    # chatgpt.com để đọc cookie `oai-asli` mà sentinel SDK đã set, đảm bảo
    # query param `auth_session_logging_id` của signin/openai khớp cookie. Xem
    # journal `260625-1224-reg-anti-ban-master-plan.md` (Task 1.1, bug C2).
    log(f"[browser] device_id={device_id}")

    async def _resolve_logging_id(ctx) -> str:
        """Đọc cookie ``oai-asli`` từ ctx → dùng làm logging_id. Fallback UUID
        mới nếu cookie chưa có (page chưa render sentinel SDK)."""
        from _nextauth_bootstrap import read_oai_asli_from_ctx as _read_asli
        val = await _read_asli(ctx)
        if val:
            log(f"[browser] auth_session_logging_id={val} (from oai-asli cookie)")
            logging_id_holder["value"] = val
            return val
        new_id = str(uuid.uuid4())
        log(f"[browser] auth_session_logging_id={new_id} (gen — cookie missing)")
        logging_id_holder["value"] = new_id
        return new_id

    w, h = settings.browser_viewport_width, settings.browser_viewport_height
    viewport = {"width": w, "height": h}

    proxy_kwargs: dict[str, Any] = {}
    if request.proxy:
        proxy_kwargs["proxy"] = _parse_proxy(request.proxy)
        _ensure_geoip_cache(settings.runtime_dir, log=log)

    # ── Locale auto-detect theo proxy country (anti-ban Task 1.4) ──
    # Trace tay xác nhận server cross-check IP country ↔ navigator.language ↔
    # timezone ↔ geo. Hardcode locale en-US + proxy India → mismatch → flag.
    # Logic priority:
    #   1. request.locale (CLI explicit) > auto-detect > default en-US.
    resolved_locale = request.locale or "en-US"
    resolved_timezone = request.timezone
    resolved_geo: tuple[float, float] | None = None
    if request.proxy and (request.locale is None or request.timezone is None):
        try:
            from _geo_locale import resolve_proxy_locale as _resolve_geo
            auto_locale, auto_tz, auto_geo, auto_cc = _resolve_geo(
                request.proxy, timeout=10.0, log=log,
            )
            if request.locale is None:
                resolved_locale = auto_locale
            if request.timezone is None:
                resolved_timezone = auto_tz
            resolved_geo = auto_geo
            log(
                f"[browser] proxy locale: country={auto_cc or '?'} "
                f"locale={resolved_locale} tz={resolved_timezone} geo={resolved_geo}"
            )
        except Exception as exc:  # noqa: BLE001 — fail-safe
            log(f"[browser] proxy locale auto-detect failed: {exc}")
    elif request.locale or request.timezone:
        log(
            f"[browser] locale=explicit ({resolved_locale}, "
            f"tz={resolved_timezone or 'auto'})"
        )
    else:
        log(f"[browser] locale=default {resolved_locale} (no proxy)")

    # Camoufox locale: pass list[str] thay vì string đơn → first dùng cho Intl
    # API, others vào navigator.languages (Firefox style "en-IN,en;q=0.5").
    # Browser thật (record tay) navigator.languages = ["en-US", "en"].
    def _locale_to_list(loc: str) -> list[str]:
        out = [loc]
        if "-" in loc:
            base = loc.split("-", 1)[0]
            if base and base != loc:
                out.append(base)
        return out

    resolved_locale_list = _locale_to_list(resolved_locale)
    log(f"[browser] navigator.languages={resolved_locale_list}")

    state_param: str | None = None
    handoff_cookies: list[dict[str, Any]] = []
    authorize_url: str | None = None
    otp_seconds = 0.0
    callback_url: str | None = None

    # Kết quả enroll 2FA inline (page còn sống — CF-clean). Runner ghi vào đây
    # sau khi login OK, trước khi đóng browser. NEVER raise để không phá flow
    # (account đã create — phải trả cookies cho fallback Phase 2).
    mfa_holder: dict[str, Any] = {}

    async def _enroll_2fa_inline(page) -> None:
        """Enroll 2FA bằng page đang login. Ghi kết quả vào mfa_holder, không raise."""
        if not getattr(request, "mfa_inline", False):
            return
        from mfa_phase import MfaError, enable_2fa_in_page
        try:
            mfa_holder["two_factor"] = await enable_2fa_in_page(page, log=log)
            log("[browser] 2FA enrolled inline OK (CF-clean)")
        except MfaError as exc:
            partial = getattr(exc, "partial_state", None)
            if partial and partial.get("secret"):
                mfa_holder["two_factor_partial"] = partial
                log(f"[browser] 2FA inline: enroll OK nhưng activate fail → partial saved: {exc}")
            else:
                log(f"[browser] 2FA inline fail (fallback Phase 2): {exc}")
        except Exception as exc:
            log(f"[browser] 2FA inline lỗi bất ngờ (fallback Phase 2): {exc}")

    # Track xem có đã chạm mốc OTP poll chưa. Sau mốc này, KHÔNG retry kể cả
    # khi driver chết — vì OTP đã được gửi, retry sẽ gây gửi OTP lần 2 và
    # consume mã không cần thiết.
    flow_progress = {"otp_started": False}

    def _mark_otp_started() -> None:
        flow_progress["otp_started"] = True

    # auth_session_logging_id holder — inner runner đọc cookie oai-asli sau
    # khi load chatgpt.com (Phase 1 Task 1.1) và write vào holder. Outer scope
    # đọc giá trị này để build BrowserHandoff. Default UUID nếu inner không
    # set (vd browser launch fail trước page.goto).
    logging_id_holder: dict[str, str] = {"value": str(uuid.uuid4())}

    # ─── Inner runners (mỗi runner là 1 lần launch + drive flow) ───
    async def _run_camoufox_once() -> tuple[str, float, str, list[dict[str, Any]]]:
        from camoufox.async_api import AsyncCamoufox

        extra_config: dict = {"fonts:spacing_seed": 0} if request.off_font else {}
        screen_kwargs: dict[str, Any] = {}

        if not settings.browser_random_screen:
            from camoufox.utils import Screen as _Screen

            chrome_h = 85
            extra_config["window.innerWidth"] = w
            extra_config["window.innerHeight"] = h
            extra_config["window.outerWidth"] = w
            extra_config["window.outerHeight"] = h + chrome_h
            extra_config["screen.width"] = w
            extra_config["screen.height"] = h + chrome_h
            extra_config["screen.availWidth"] = w
            extra_config["screen.availHeight"] = h + chrome_h
            screen_kwargs["screen"] = _Screen(
                min_width=w, max_width=w, min_height=h + chrome_h, max_height=h + chrome_h
            )
            screen_kwargs["i_know_what_im_doing"] = True

        _pids_before = _browser_descendant_pids(os.getpid())
        cf = AsyncCamoufox(
            headless=request.headless,
            persistent_context=True,
            user_data_dir=str(profile_dir),
            os=list(_CAMOUFOX_OS),
            viewport=viewport,
            locale=resolved_locale_list,
            ignore_https_errors=request.tls_insecure,
            geoip=bool(request.proxy),
            # ── Anti-detect hardening (Phase 9 audit) ──
            # block_webrtc=True: chặn WebRTC mDNS leak IP thật khi dùng proxy.
            # Camoufox-Firefox mặc định KHÔNG block → IP local rò rỉ qua
            # WebRTC stun servers → server (sentinel) detect mismatch IP proxy
            # vs WebRTC IP → flag bot.
            block_webrtc=True,
            # humanize=0.4: Camoufox animate cursor mượt (anti-ban) NHƯNG cap tối
            # đa 0.4s/lần di. Mặc định humanize=True = 1.5s/lần → "di chuột rất
            # lâu" (mỗi click tốn tới 1.5s). 0.4s đủ realistic, nhanh hơn nhiều.
            humanize=0.4,
            # main_world_eval=True (Phase 11.2): cho phép page.evaluate chạy
            # main world, tránh Xray wrapper reject TypedArray của sdk.js.
            main_world_eval=True,
            # NOTE: ``fingerprint_preset=True`` removed (Phase 11.1, 2026-06-25)
            # — older Camoufox versions (current pinned in this repo's venv)
            # don't recognise this kwarg and forward it to Playwright's
            # ``BrowserType.launch()``, which then raises
            # ``TypeError: got an unexpected keyword argument 'fingerprint_preset'``.
            # Camoufox already provides BrowserForge synthetic fingerprints
            # by default; per-context persona rotation (Phase 11) re-seeds
            # canvas/audio/font/WebGL per signup via ``ctx.add_init_script``.
            config=extra_config,
            **screen_kwargs,
            **proxy_kwargs,
            **har_kwargs,
        )
        ctx = await cf.__aenter__()
        # Anti-leak: PID tiến trình browser của RUN NÀY (diff trước/sau launch).
        # Dùng để force-kill nếu cf.__aexit__ không reap hết (firefox sống sót).
        _my_browser_pids = _browser_descendant_pids(os.getpid()) - _pids_before

        if debug_capture:
            try:
                await ctx.tracing.start(screenshots=True, snapshots=True, sources=True)
                log("[browser] Playwright tracing started (actions + snapshots + screenshots)")
            except Exception as exc:  # noqa: BLE001 — best-effort debug
                log(f"[browser] tracing start failed (continue): {exc}")

        callback_holder: dict[str, str] = {}

        def _capture_callback(req) -> None:
            url = req.url
            if "chatgpt.com/api/auth/callback/openai" in url and "code=" in url:
                callback_holder.setdefault("url", url)

        ctx.on("request", _capture_callback)
        page = None
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()

            await page.goto(PROMO_LANDING_URL, wait_until="domcontentloaded")
            log("[browser] chatgpt.com loaded")
            # Fingerprint health probe (Phase 9 — anti-ban headless hardening):
            # Confirm Camoufox produced real WebGL/canvas/audio/plugins. Empty
            # values mean headless mode degraded → sdk.js so-token sẽ zero
            # fingerprint → deferred ban. Log-only (strict=False) để không
            # block account run nếu chỉ 1 vector miss; operator quyết định
            # switch headed dựa trên log.
            try:
                from sentinel_browser import verify_fingerprint_health as _vfp
                await _vfp(page, log=log)
            except Exception as _exc:
                log(f"[fingerprint] health probe exception: {_exc}")
            logging_id = await _resolve_logging_id(ctx)
            _authorize_url = await _bootstrap_oauth_url(
                page, email=request.email, device_id=device_id, logging_id=logging_id, log=log,
            )
            await page.goto(_authorize_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(1.0)

            # Mốc check-point: sắp drive flow (sẽ trigger OTP send).
            _mark_otp_started()

            _callback_url, _otp_seconds = await _drive_signup_flow(
                ctx=ctx, page=page, request=request,
                mail_provider=mail_provider,
                callback_holder=callback_holder,
                otp_started_at=otp_started_at,
                log=log,
                overall_timeout=request.otp_timeout_seconds + _PRE_OTP_MARGIN_SECONDS,
                on_checkpoint=on_checkpoint,
                debug_capture=debug_capture,
                debug_dir=debug_dir if debug_capture else None,
                job_id=job_id,
            )

            _state = (
                _extract_state_from_authorize(_authorize_url)
                or await _extract_state_from_url(page, log=log)
            )
            _cookies = await ctx.cookies()
            # 2FA inline TRƯỚC khi đóng browser (page còn login + CF-clean).
            await _enroll_2fa_inline(page)
            return _callback_url, _otp_seconds, _state or "", _cookies
        except BaseException as exc:
            # Plan D: log health snapshot trước khi propagate để debug được
            # lý do page/ctx/browser chết.
            try:
                health = _browser_health(ctx, page) if page is not None else "page=NEVER_CREATED"
            except Exception as health_exc:
                health = f"health-snapshot-failed: {type(health_exc).__name__}: {health_exc}"
            log(
                f"[browser] camoufox runner exception: "
                f"{type(exc).__name__}: {exc} ({health})"
            )
            if debug_capture and page is not None:
                await _dump_debug_artifacts(page, debug_dir, job_id, reason="error", log=log)
            raise
        finally:
            try:
                ctx.remove_listener("request", _capture_callback)
            except Exception:
                pass
            if debug_capture:
                try:
                    await ctx.tracing.stop(path=str(trace_path))
                    log(f"[browser] trace saved → {trace_path}")
                except Exception as exc:  # noqa: BLE001
                    log(f"[browser] tracing stop failed: {exc}")
            if request.keep_browser_open and not request.headless:
                log("[browser] debug: giữ browser mở — cancel job để đóng")
            else:
                # Đóng browser có TIMEOUT (cf.__aexit__ = context.close +
                # playwright.stop) — tránh treo vô hạn nếu browser wedged.
                try:
                    await asyncio.wait_for(
                        cf.__aexit__(None, None, None), timeout=20.0,
                    )
                except Exception as exc:  # noqa: BLE001 — timeout/close lỗi
                    log(
                        f"[browser] cf.__aexit__ timeout/lỗi "
                        f"({type(exc).__name__}: {exc})"
                    )
                # Anti-leak: firefox/persistent-context đôi khi sống sót sau
                # close → force-kill PID browser của RUN NÀY còn sống. Chỉ kill
                # PID đã capture của run này (an toàn khi concurrent).
                try:
                    leftover = {p for p in _my_browser_pids if _pid_alive(p)}
                    if leftover:
                        _force_kill_pids(leftover, log=log)
                except Exception as exc:  # noqa: BLE001
                    log(f"[browser] anti-leak kill lỗi (bỏ qua): {exc}")

    async def _run_chromium_once() -> tuple[str, float, str, list[dict[str, Any]]]:
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        ctx = None
        page = None
        try:
            channel = settings.browser_channel or None
            # Chrome runner: explicit timezone_id + geolocation (Playwright hỗ trợ
            # native, không có geoip auto-detect như Camoufox).
            chrome_ctx_kwargs: dict[str, Any] = {
                "user_data_dir": str(profile_dir),
                "headless": request.headless,
                "channel": channel,
                "viewport": viewport,
                "locale": resolved_locale,
                "ignore_https_errors": request.tls_insecure,
            }
            if resolved_timezone:
                chrome_ctx_kwargs["timezone_id"] = resolved_timezone
            if resolved_geo:
                lat, lon = resolved_geo
                chrome_ctx_kwargs["geolocation"] = {"latitude": lat, "longitude": lon}
                chrome_ctx_kwargs["permissions"] = ["geolocation"]
            ctx = await playwright.chromium.launch_persistent_context(
                **chrome_ctx_kwargs,
                **proxy_kwargs,
                **har_kwargs,
            )

            callback_holder: dict[str, str] = {}

            def _capture_callback(req) -> None:
                url = req.url
                if "chatgpt.com/api/auth/callback/openai" in url and "code=" in url:
                    callback_holder.setdefault("url", url)

            ctx.on("request", _capture_callback)

            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(PROMO_LANDING_URL, wait_until="domcontentloaded")
            log("[browser] chatgpt.com loaded")
            # Fingerprint health probe (Phase 9 — anti-ban headless hardening).
            try:
                from sentinel_browser import verify_fingerprint_health as _vfp
                await _vfp(page, log=log)
            except Exception as _exc:
                log(f"[fingerprint] health probe exception: {_exc}")
            logging_id = await _resolve_logging_id(ctx)
            _authorize_url = await _bootstrap_oauth_url(
                page, email=request.email, device_id=device_id, logging_id=logging_id, log=log,
            )
            await page.goto(_authorize_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(1.0)

            # Mốc check-point: sắp drive flow (sẽ trigger OTP send).
            _mark_otp_started()

            _callback_url, _otp_seconds = await _drive_signup_flow(
                ctx=ctx, page=page, request=request,
                mail_provider=mail_provider,
                callback_holder=callback_holder,
                otp_started_at=otp_started_at,
                log=log,
                overall_timeout=request.otp_timeout_seconds + _PRE_OTP_MARGIN_SECONDS,
                on_checkpoint=on_checkpoint,
                debug_capture=debug_capture,
                debug_dir=debug_dir if debug_capture else None,
                job_id=job_id,
            )

            _state = (
                _extract_state_from_authorize(_authorize_url)
                or await _extract_state_from_url(page, log=log)
            )
            _cookies = await ctx.cookies()

            # 2FA inline TRƯỚC khi đóng ctx (page còn login + CF-clean).
            await _enroll_2fa_inline(page)

            if not (request.keep_browser_open and not request.headless):
                await ctx.close()
            return _callback_url, _otp_seconds, _state or "", _cookies
        except BaseException as exc:
            # Plan D: log health snapshot trước khi propagate.
            try:
                if ctx is None:
                    health = "ctx=NEVER_CREATED"
                elif page is None:
                    health = "page=NEVER_CREATED"
                else:
                    health = _browser_health(ctx, page)
            except Exception as health_exc:
                health = f"health-snapshot-failed: {type(health_exc).__name__}: {health_exc}"
            log(
                f"[browser] chromium runner exception: "
                f"{type(exc).__name__}: {exc} ({health})"
            )
            raise
        finally:
            if request.keep_browser_open and not request.headless:
                log("[browser] debug: giữ browser mở — cancel job để đóng")
            else:
                await playwright.stop()

    runner = _run_camoufox_once if engine == "camoufox" else _run_chromium_once

    # Vòng retry: chỉ retry khi bắt được lỗi driver-pipe-dead VÀ flow chưa
    # tới mốc OTP send. Sau OTP send, lỗi driver vẫn fail-fast để tránh
    # spam mã OTP cho user.
    last_exc: BaseException | None = None
    success = False
    # B11 fix: try/finally đảm bảo profile_dir được dọn trên mọi exit path
    # (BrowserPhaseError raise giữa loop, CancelledError, KeyboardInterrupt).
    # Trừ debug mode keep_browser_open + headed (giữ profile để soi).
    try:
        for attempt in range(1, _LAUNCH_RETRY_MAX + 1):
            flow_progress["otp_started"] = False
            try:
                callback_url, otp_seconds, state_param, handoff_cookies = await runner()
                success = True
                last_exc = None
                break
            except BrowserPhaseError:
                raise
            except Exception as exc:
                last_exc = exc
                retryable = (
                    _is_driver_dead_error(exc)
                    or _is_network_error(exc)
                    or _is_navigation_timeout(exc)
                )
                if not retryable:
                    raise BrowserPhaseError(
                        f"browser launch/driver error: {type(exc).__name__}: {exc}"
                    ) from exc
                if flow_progress["otp_started"]:
                    log(
                        f"[browser] lỗi sau khi đã trigger OTP — "
                        f"không retry để tránh gửi OTP lần 2: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    raise BrowserPhaseError(
                        f"lỗi giữa flow (OTP đã gửi, không retry): {exc}"
                    ) from exc
                err_kind = (
                    "network/proxy" if _is_network_error(exc)
                    else "navigation timeout" if _is_navigation_timeout(exc)
                    else "driver pipe"
                )
                log(
                    f"[browser] {err_kind} error "
                    f"(attempt {attempt}/{_LAUNCH_RETRY_MAX}): "
                    f"{type(exc).__name__}: {exc}"
                )
                if attempt >= _LAUNCH_RETRY_MAX:
                    break
                shutil.rmtree(profile_dir, ignore_errors=True)
                prepare_profile_dir(
                    profile_dir=profile_dir,
                    template_dir=template_dir,
                    use_template=request.profile_template,
                )
                await asyncio.sleep(_LAUNCH_RETRY_BACKOFF)
    finally:
        if not (request.keep_browser_open and not request.headless):
            shutil.rmtree(profile_dir, ignore_errors=True)

    if not success:
        if last_exc is not None and (
            _is_driver_dead_error(last_exc)
            or _is_network_error(last_exc)
            or _is_navigation_timeout(last_exc)
        ):
            raise BrowserPhaseError(
                f"retryable error sau {_LAUNCH_RETRY_MAX} lần thử: {last_exc}"
            ) from last_exc
        # Defensive: không bao giờ xảy ra (đã raise trong loop)
        raise BrowserPhaseError("browser launch failed without specific error")

    if not state_param:
        raise BrowserPhaseError("không lấy được oauth state từ navigation history")

    # Sanity check required cookies
    auth_cookies = {c["name"] for c in handoff_cookies if "openai.com" in (c.get("domain") or "")}
    missing = [c for c in _REQUIRED_AUTH_COOKIES if c not in auth_cookies]
    if missing:
        raise BrowserPhaseError(f"thiếu cookies: {missing}. có: {sorted(auth_cookies)}")

    log(f"[browser] handoff: {len(handoff_cookies)} cookies, state={state_param[:20]}...")
    return (
        BrowserHandoff(
            cookies=handoff_cookies,
            state_param=state_param,
            device_id=device_id,
            auth_session_logging_id=logging_id_holder["value"],
            callback_url=callback_url,
            two_factor=mfa_holder.get("two_factor"),
            two_factor_partial=mfa_holder.get("two_factor_partial"),
        ),
        otp_seconds,
    )
