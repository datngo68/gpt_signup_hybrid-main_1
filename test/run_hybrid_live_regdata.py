"""Live test reg hybrid + POST-REG DEACTIVATION RECHECK cho bugfix nhánh A.

Spec: reg-hybrid-deactivated-after-signup (nhánh A — bỏ double-POST
create_account trong HybridChatGPTRelay.run()). Bug: account reg hybrid báo
success=True (callback + /api/auth/session 200) nhưng bị server vô hiệu hóa NGAY
SAU reg (deferred ban). Test này reg 5 account rồi đợi một khoảng và GỌI LẠI
session endpoint để phân loại ACTIVE vs DEACTIVATED.

Tái dùng MẪU CHUẨN production (giống test/smoke_reg_icloud_v3.py):
  - Settings Store: db.get_settings_repo(get_engine()).list() → headless /
    job_timeout / default_password (reg.headless / reg.job_timeout /
    reg.default_password).
  - Proxy: web.manager._resolve_job_proxy() (giống autoreg/production).
  - Mail: web.mail_modes.get_spec("icloud_v3") → spec.parse_line / build_request.
  - Chạy signup.run_signup(request, log=...).

KHÁC smoke:
  1. 5 dòng data (hằng LINES).
  2. ÉP reg_mode="hybrid" mỗi request (smoke mặc định browser).
  3. Chạy TUẦN TỰ 5 account, lưu JSON ra runtime/live_hybrid_regdata/.
  4. POST-REG DEACTIVATION RECHECK: sau reg success đợi HYBRID_RECHECK_DELAY giây
     rồi GET /api/auth/session (cookies) + /backend-api/me (Bearer access_token)
     → ACTIVE / DEACTIVATED / recheck_skipped.
  5. Summary cuối: bảng success + active/deactivated từng account.

ENV:
  - HYBRID_RECHECK_DELAY : số giây đợi trước recheck (mặc định 60). =0 → recheck ngay.
  - HYBRID_RECHECK       : "0"/"false" → tắt recheck hẳn (mặc định bật).

Cách dùng:
  - Validate OFFLINE (KHÔNG signup, KHÔNG network):
        .venv/bin/python test/run_hybrid_live_regdata.py --dry-run
  - Chạy THẬT (reg network + recheck):
        .venv/bin/python test/run_hybrid_live_regdata.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ─── Reg data fallback (email|api_url icloud_v3) ─────────────────────
# LINES được nạp động: ưu tiên file input_lines.txt (mỗi dòng `email|url`),
# fallback hằng _FALLBACK_LINES dưới đây nếu file không tồn tại / rỗng.
_FALLBACK_LINES: list[str] = [
    "blog.pod_36+8pjb9p@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/rbH7JDLf1uMxsEpqQfuQ-9GclVPddc2M/data",
    "blog.pod_36+aupcv@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/_HbsUPnYX7EbOfrPhC2HLUY7ymO2gqQA/data",
    "blog.pod_36+gl48bl@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/eXOshjYH5fDmI-nV1IZGaQkvMwm1h13C/data",
    "blog.pod_36+i7drxc@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/DXGt1ZRPf3quuMToOH3UbDpzigebAZqK/data",
    "blog.pod_36+j0uw70g@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/dOGBlSw7sAWrSgIcFKhE92FB7v3944D3/data",
]

REG_MODE = "hybrid"
MAIL_MODE = "icloud_v3"

OUT_DIR = ROOT / "runtime" / "live_hybrid_regdata"
ACCOUNTS_FILE = OUT_DIR / "accounts.txt"
INPUT_LINES_FILE = OUT_DIR / "input_lines.txt"
_NO_2FA_MARKER = "<NO_2FA>"


def _load_lines() -> list[str]:
    """Nạp danh sách dòng reg.

    Ưu tiên đọc INPUT_LINES_FILE nếu tồn tại: mỗi dòng non-empty là 1 account,
    bỏ qua dòng trống và dòng comment bắt đầu bằng '#'. Nếu file không tồn tại
    hoặc rỗng → fallback hằng _FALLBACK_LINES.
    """
    if INPUT_LINES_FILE.exists():
        raw = INPUT_LINES_FILE.read_text(encoding="utf-8")
        parsed: list[str] = []
        for ln in raw.splitlines():
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            parsed.append(s)
        if parsed:
            return parsed
    return list(_FALLBACK_LINES)


# LINES nạp tại import-time; main() sẽ refresh lại trước khi build entries.
LINES: list[str] = _load_lines()

# Recheck endpoints
_CHATGPT_BASE = "https://chatgpt.com"
_SESSION_URL = f"{_CHATGPT_BASE}/api/auth/session"
_ME_URL = f"{_CHATGPT_BASE}/backend-api/me"


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _make_logger(prefix: str):
    def _log(msg: str) -> None:
        print(f"[{_ts()}][{prefix}] {msg}", flush=True)
    return _log


def _recheck_delay_seconds() -> float:
    raw = os.environ.get("HYBRID_RECHECK_DELAY", "60").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 60.0


def _recheck_enabled() -> bool:
    raw = os.environ.get("HYBRID_RECHECK", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _select_entries(line_index: int | None) -> list[tuple[int, str]]:
    """Chọn các dòng reg cần chạy.

    Trả list[(orig_idx_1based, line)]. line_index=None → toàn bộ LINES.
    line_index (1-based) → chỉ dòng đó. ValueError nếu out-of-range.
    """
    if line_index is None:
        return list(enumerate(LINES, 1))
    if line_index < 1 or line_index > len(LINES):
        raise ValueError(
            f"--line/LINE_INDEX={line_index} ngoài phạm vi 1..{len(LINES)}"
        )
    return [(line_index, LINES[line_index - 1])]


def _resolve_line_index(cli_line: int | None) -> int | None:
    """Ưu tiên --line; fallback env LINE_INDEX; None → chạy tất cả."""
    if cli_line is not None:
        return cli_line
    raw = os.environ.get("LINE_INDEX", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"LINE_INDEX không phải số nguyên: {raw!r}")


# ─── Build request từ mail_modes spec (mẫu chuẩn) + ÉP reg_mode=hybrid ──
def _build_request(line: str, *, password: str | None, headless: bool, proxy: str | None):
    from web.mail_modes import get_spec

    spec = get_spec(MAIL_MODE)
    parsed = spec.parse_line(line)
    request = spec.build_request(
        parsed,
        password=password,
        headless=headless,
        proxy=proxy,
        reg_mode=REG_MODE,  # spec hỗ trợ truyền reg_mode trực tiếp
    )
    # ÉP cứng lại lần nữa (đảm bảo dù spec đổi default) — đây là điểm khác smoke.
    updates: dict = {}
    if request.reg_mode != REG_MODE:
        updates["reg_mode"] = REG_MODE
    # BẬT 2FA INLINE: enroll + activate 2FA ngay trong context vừa tạo account
    # (CF-clean). spec icloud_v3.build_request KHÔNG nhận tham số mfa_inline nên
    # set qua model_copy. Sau reg success → result.two_factor['secret'].
    if not request.mfa_inline:
        updates["mfa_inline"] = True
    if updates:
        request = request.model_copy(update=updates)
    return request


# ─── POST-REG DEACTIVATION RECHECK ───────────────────────────────────
def _recheck_sync(*, request, result, log) -> dict:
    """GET /api/auth/session (cookies) + /backend-api/me (Bearer) để phân loại.

    Trả dict: {status, session_http, me_http, has_user, reason}.
    status ∈ {ACTIVE, DEACTIVATED, UNKNOWN, recheck_skipped}.
    """
    from curl_cffi import requests as curl_requests
    from user_agent_profile import (
        SEC_CH_UA,
        SEC_CH_UA_MOBILE,
        SEC_CH_UA_PLATFORM,
    )

    session_token = result.session_token
    access_token = result.access_token
    cookies = result.cookies or []

    if not session_token and not access_token and not cookies:
        return {
            "status": "recheck_skipped",
            "reason": "không có session_token/access_token/cookies để recheck",
            "session_http": None,
            "me_http": None,
            "has_user": False,
        }

    session = curl_requests.Session(impersonate=request.impersonate)
    if request.proxy:
        session.proxies = {"http": request.proxy, "https": request.proxy}

    base_headers = {
        "User-Agent": request.user_agent,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"{_CHATGPT_BASE}/",
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": SEC_CH_UA_PLATFORM,
    }

    try:
        # Inject cookies chatgpt.com (gồm __Secure-next-auth.session-token nếu có)
        for c in cookies:
            name = c.get("name")
            value = c.get("value")
            if not name or value is None:
                continue
            domain = (c.get("domain") or "chatgpt.com").lstrip(".")
            session.cookies.set(name, value, domain=domain, path=c.get("path") or "/")
        # Bảo đảm session-token có mặt (nếu result.cookies không kèm)
        has_session_cookie = any(
            c.get("name") == "__Secure-next-auth.session-token" for c in cookies
        )
        if session_token and not has_session_cookie:
            session.cookies.set(
                "__Secure-next-auth.session-token", session_token,
                domain="chatgpt.com", path="/",
            )

        # (1) /api/auth/session — dựa trên cookies
        session_http: int | None = None
        has_user = False
        try:
            r = session.get(_SESSION_URL, headers=base_headers, timeout=30)
            session_http = r.status_code
            if session_http == 200:
                try:
                    data = r.json()
                except Exception:
                    data = {}
                user = (data.get("user") or {}) if isinstance(data, dict) else {}
                has_user = bool(data.get("accessToken")) and bool(user.get("id"))
        except Exception as exc:  # noqa: BLE001
            log(f"[recheck] /api/auth/session error: {type(exc).__name__}: {exc}")

        # (2) /backend-api/me — dựa trên Bearer access_token (nếu có)
        me_http: int | None = None
        if access_token:
            me_headers = {
                **base_headers,
                "Authorization": f"Bearer {access_token}",
            }
            try:
                rm = session.get(_ME_URL, headers=me_headers, timeout=30)
                me_http = rm.status_code
            except Exception as exc:  # noqa: BLE001
                log(f"[recheck] /backend-api/me error: {type(exc).__name__}: {exc}")

        # ── Phân loại ──
        deactivated_signal = (
            session_http in (401, 403)
            or me_http in (401, 403)
            or (session_http == 200 and not has_user)
        )
        active_signal = (
            (session_http == 200 and has_user)
            or me_http == 200
        )

        if deactivated_signal and not (me_http == 200):
            status = "DEACTIVATED"
            reason = f"session_http={session_http} me_http={me_http} has_user={has_user}"
        elif active_signal:
            status = "ACTIVE"
            reason = f"session_http={session_http} me_http={me_http} has_user={has_user}"
        else:
            status = "UNKNOWN"
            reason = f"session_http={session_http} me_http={me_http} has_user={has_user}"

        return {
            "status": status,
            "reason": reason,
            "session_http": session_http,
            "me_http": me_http,
            "has_user": has_user,
        }
    finally:
        try:
            session.close()
        except Exception:
            pass


async def _recheck(*, request, result, log) -> dict:
    return await asyncio.to_thread(_recheck_sync, request=request, result=result, log=log)


# ─── Chạy 1 account ──────────────────────────────────────────────────
async def _run_one(
    line: str, idx: int, total: int, *,
    password: str | None, headless: bool, proxy: str | None, job_timeout: float,
) -> dict:
    from models import SignupResult
    from signup import run_signup

    email_preview = line.split("|", 1)[0]
    prefix = f"{idx}/{total} {email_preview[:28]}"
    log = _make_logger(prefix)

    request = _build_request(line, password=password, headless=headless, proxy=proxy)
    log(
        f"START email={request.email} provider={request.mail_provider} "
        f"reg_mode={request.reg_mode} url_set={bool(request.icloud_v3_url)} "
        f"headless={request.headless} mfa_inline={request.mfa_inline}"
    )

    row: dict = {
        "email": request.email,
        "reg_mode": request.reg_mode,
        "success": False,
        "error": None,
        "total_seconds": 0.0,
        "phase1_seconds": 0.0,
        "otp_seconds": 0.0,
        "phase2_seconds": 0.0,
        "session_token_len": 0,
        "access_token_len": 0,
        "cookies_count": 0,
        "two_factor_state": "n/a",
        "two_factor_secret_len": 0,
        "account_line": None,
        "recheck": {"status": "not_run", "reason": "reg chưa success"},
    }

    t0 = time.monotonic()
    try:
        result: SignupResult = await asyncio.wait_for(
            run_signup(request, log=log),
            timeout=max(job_timeout, request.otp_timeout_seconds + 120.0),
        )
    except asyncio.TimeoutError:
        row["error"] = "TimeoutError"
        row["total_seconds"] = round(time.monotonic() - t0, 2)
        log(f"TIMEOUT after {row['total_seconds']}s")
        _save_json(idx, request.email, {"row": row})
        return row
    except Exception as exc:  # noqa: BLE001
        row["error"] = f"{type(exc).__name__}: {exc}"
        row["total_seconds"] = round(time.monotonic() - t0, 2)
        log(f"FATAL: {row['error']}")
        _save_json(idx, request.email, {"row": row})
        return row

    dt = time.monotonic() - t0
    row.update(
        success=result.success,
        error=result.error,
        total_seconds=round(dt, 2),
        phase1_seconds=round(result.phase1_seconds or 0.0, 2),
        otp_seconds=round(result.otp_seconds or 0.0, 2),
        phase2_seconds=round(result.phase2_seconds or 0.0, 2),
        session_token_len=len(result.session_token or ""),
        access_token_len=len(result.access_token or ""),
        cookies_count=len(result.cookies or []),
        user_id=result.user_id,
        account_id=result.account_id,
    )
    log(
        f"END success={result.success} total={dt:.1f}s "
        f"session_token={'<set>' if result.session_token else None} "
        f"access_token={'<set>' if result.access_token else None} "
        f"error={result.error or '<none>'}"
    )

    # ── XUẤT KẾT QUẢ email|password|2fa (chỉ khi reg success) ──
    if result.success:
        acc = _write_account_line(
            email=request.email, password=request.password, result=result, log=log,
        )
        secret, is_partial = _resolve_2fa_secret(result)
        row["account_line"] = acc["line"]
        row["two_factor_state"] = acc["secret_state"]
        row["two_factor_secret_len"] = len(secret or "")

    # ── POST-REG DEACTIVATION RECHECK ──
    if not result.success:
        row["recheck"] = {"status": "not_run", "reason": "reg không success"}
    elif not _recheck_enabled():
        row["recheck"] = {"status": "recheck_skipped", "reason": "HYBRID_RECHECK tắt"}
    else:
        delay = _recheck_delay_seconds()
        log(f"[recheck] đợi {delay:.0f}s trước khi recheck deactivation...")
        if delay > 0:
            await asyncio.sleep(delay)
        recheck = await _recheck(request=request, result=result, log=log)
        row["recheck"] = recheck
        log(f"[recheck] status={recheck['status']} ({recheck['reason']})")

    # Lưu JSON đầy đủ result + row
    payload = result.model_dump()
    payload["__row"] = row
    _save_json(idx, request.email, payload)
    return row


def _save_json(idx: int, email: str, payload: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    safe = email.replace("@", "_at_").replace("+", "_plus_")
    (OUT_DIR / f"{idx:02d}_{safe}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def _resolve_2fa_secret(result) -> tuple[str | None, bool]:
    """Lấy 2FA secret từ result.

    Trả (secret, is_partial). Ưu tiên two_factor (enroll+activate đầy đủ),
    fallback two_factor_partial (enroll OK nhưng activate fail).
    """
    tf = result.two_factor or {}
    secret = tf.get("secret")
    if secret:
        return secret, False
    tf_partial = result.two_factor_partial or {}
    secret = tf_partial.get("secret")
    if secret:
        return secret, True
    return None, False


def _write_account_line(*, email: str, password: str | None, result, log) -> dict:
    """Ghi 1 dòng `email|password|2fa` vào accounts.txt (append) + stdout.

    Trả dict {line, secret_state} với secret_state ∈ {full, partial, none}.
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pw = password or getattr(result, "password", None) or ""
    secret, is_partial = _resolve_2fa_secret(result)

    if secret and not is_partial:
        line = f"{email}|{pw}|{secret}"
        state = "full"
    elif secret and is_partial:
        line = f"{email}|{pw}|{secret}  # partial-2fa"
        state = "partial"
    else:
        line = f"{email}|{pw}|{_NO_2FA_MARKER}"
        state = "none"
        log("[accounts] CẢNH BÁO: reg success nhưng KHÔNG có 2FA secret (full/partial) → ghi <NO_2FA>")

    with ACCOUNTS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    log(f"[accounts] +1 dòng ({state}): {line}")
    return {"line": line, "secret_state": state}


def _reset_accounts_file(log) -> None:
    """Xóa accounts.txt đầu run để không lẫn dòng cũ.

    Nếu file đã tồn tại → backup sang accounts_<timestamp>.bak rồi truncate file
    chính về rỗng. Bảo đảm mỗi run chỉ chứa account của run hiện tại.
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if ACCOUNTS_FILE.exists() and ACCOUNTS_FILE.read_text(encoding="utf-8").strip():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = OUT_DIR / f"accounts_{stamp}.bak"
        backup.write_text(ACCOUNTS_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        log(f"[accounts] backup dòng cũ → {backup.name}")
    ACCOUNTS_FILE.write_text("", encoding="utf-8")
    log(f"[accounts] reset {ACCOUNTS_FILE.name} (rỗng) đầu run")


# ─── Load settings + proxy (mẫu chuẩn production) ────────────────────
def _load_settings() -> dict:
    from db import get_engine, get_settings_repo
    from db.repositories import RepositoryError

    try:
        repo = get_settings_repo(get_engine())
        return repo.list()
    except RepositoryError as exc:
        print(f"[settings] load fail: {exc} → dùng default", flush=True)
        return {}


async def _acquire_per_email_proxy(log) -> tuple[str | None, str | None, bool]:
    """Lease proxy PER-EMAIL mirror autoreg (Option A): least-used + no-immediate-repeat.

    Sau khi acc xong, caller PHẢI gọi _release_job_proxy_lease(line, leased) ở finally.
    """
    try:
        from web.manager import _acquire_job_proxy_lease
        from web.proxy_format import mask_proxy
        url, line, leased = await _acquire_job_proxy_lease()
        if url:
            log(f"[proxy] {mask_proxy(url)} (leased={leased})")
        else:
            log("[proxy] DIRECT (pool rỗng)")
        return url, line, leased
    except Exception as exc:  # noqa: BLE001
        log(f"[proxy] acquire fail: {type(exc).__name__}: {exc} → DIRECT")
        return None, None, False


# ─── DRY-RUN: parse + build 5 request, KHÔNG signup, KHÔNG network ────
def _dry_run(entries: list[tuple[int, str]]) -> int:
    log = _make_logger("dry-run")
    log(f"=== DRY-RUN: build {len(entries)} request (KHÔNG signup, KHÔNG network) ===")

    # Settings (local SQLite — offline) cho password/headless; proxy=None.
    all_settings = _load_settings()
    headless = bool(all_settings.get("reg.headless", True))
    password = all_settings.get("reg.default_password") or "Autogen#2026Xy"
    log(f"[cfg] headless={headless} password={'<set>' if password else '<auto>'} proxy=None(dry)")

    ok = 0
    for idx, line in entries:
        try:
            request = _build_request(line, password=password, headless=headless, proxy=None)
        except Exception as exc:  # noqa: BLE001
            log(f"[{idx}] BUILD FAIL: {type(exc).__name__}: {exc}")
            continue
        url_ok = bool(request.icloud_v3_url)
        mode_ok = request.reg_mode == REG_MODE
        mfa_ok = request.mfa_inline is True
        if url_ok and mode_ok and mfa_ok:
            ok += 1
        log(
            f"[{idx}] email={request.email} reg_mode={request.reg_mode} "
            f"provider={request.mail_provider} "
            f"icloud_v3_url={'SET' if url_ok else 'MISSING'} "
            f"mfa_inline={request.mfa_inline} "
            f"otp_timeout={request.otp_timeout_seconds}s "
            f"→ {'OK' if (url_ok and mode_ok and mfa_ok) else 'FAIL'}"
        )

    log(f"=== DRY-RUN result: {ok}/{len(entries)} request build OK "
        f"(reg_mode={REG_MODE}, url set, mfa_inline=True) ===")
    log("LƯU Ý: dry-run KHÔNG gọi run_signup, KHÔNG gọi network signup, KHÔNG recheck.")
    return 0 if ok == len(entries) else 1


# ─── Chạy thật ───────────────────────────────────────────────────────
async def _live_run(entries: list[tuple[int, str]]) -> int:
    log = _make_logger("main")
    n_total = len(entries)
    log(f"=== LIVE reg hybrid — {n_total} account (TUẦN TỰ) ===")

    all_settings = _load_settings()
    headless = bool(all_settings.get("reg.headless", True))
    job_timeout = float(all_settings.get("reg.job_timeout", 360))
    password = all_settings.get("reg.default_password") or "Autogen#2026Xy"
    log(
        f"[cfg] headless={headless} job_timeout={job_timeout}s reg_mode={REG_MODE} "
        f"recheck={'on' if _recheck_enabled() else 'off'} "
        f"recheck_delay={_recheck_delay_seconds():.0f}s "
        f"password={'<set>' if password else '<auto>'}"
    )

    # Proxy acquire chuyển vào trong loop (per-email) — mirror autoreg Option A
    # để mỗi acc 1 IP riêng (no-immediate-repeat + least-used).
    _reset_accounts_file(log)

    summary: list[dict] = []
    grand_start = time.monotonic()
    for pos, (idx, line) in enumerate(entries, 1):
        bar = "─" * 78
        print(f"\n{bar}\n[{pos}/{n_total}] (line {idx}) {line.split('|', 1)[0]}\n{bar}", flush=True)

        # Acquire proxy per-email (lease least-used) + release ở finally
        proxy_log = _make_logger(f"proxy-{idx}")
        email_proxy, email_proxy_line, _proxy_leased = await _acquire_per_email_proxy(proxy_log)
        try:
            row = await _run_one(
                line, idx, n_total,
                password=password, headless=headless, proxy=email_proxy,
                job_timeout=job_timeout,
            )
            # Audit field — không leak credential, chỉ giúp cross-reference log
            row["proxy_used_line"] = email_proxy_line

            # Mark proxy dead nếu fail vì proxy network / browser-closed —
            # mirror autoreg _note_proxy_failure. Conservative: chỉ mark khi
            # error match pattern (idempotent, không kill oan).
            if not row["success"]:
                error_payload = row.get("error") or ""
                from autoreg.runner import mark_proxy_dead_on_error
                marked = mark_proxy_dead_on_error(
                    email_proxy_line, error_payload, log=proxy_log,
                )
                row["proxy_marked_dead"] = bool(marked)
        finally:
            from web.manager import _release_job_proxy_lease
            _release_job_proxy_lease(email_proxy_line, _proxy_leased)
        summary.append(row)
    grand_total = time.monotonic() - grand_start

    # Cleanup pool nếu hybrid runner có
    try:
        from reg_hybrid.runner import wait_pending_cleanups
        from reg_hybrid.browser_pool import get_pool
        await wait_pending_cleanups(timeout=10.0)
        get_pool().shutdown_all()
    except Exception as exc:  # noqa: BLE001
        log(f"[cleanup] skip: {type(exc).__name__}: {exc}")

    # ── Summary ──
    print(f"\n{'═' * 78}", flush=True)
    print(f"SUMMARY ({len(LINES)} account, grand total {grand_total:.1f}s)", flush=True)
    print("═" * 78, flush=True)
    print(f"{'#':<3}{'email':<40}{'reg':<4}{'success':<9}{'2fa':<10}{'recheck':<14}{'total':>9}", flush=True)
    print("─" * 78, flush=True)
    n_success = n_active = n_deact = 0
    n_2fa_full = n_2fa_partial = n_2fa_none = 0
    for i, r in enumerate(summary, 1):
        ok = "✓" if r["success"] else "✗"
        if r["success"]:
            n_success += 1
        rc = (r.get("recheck") or {}).get("status", "?")
        if rc == "ACTIVE":
            n_active += 1
        elif rc == "DEACTIVATED":
            n_deact += 1
        tf_state = r.get("two_factor_state", "n/a")
        if tf_state == "full":
            n_2fa_full += 1
        elif tf_state == "partial":
            n_2fa_partial += 1
        elif tf_state == "none":
            n_2fa_none += 1
        em = r["email"][:38] + ".." if len(r["email"]) > 40 else r["email"]
        print(
            f"{i:<3}{em:<40}{'hyb':<4}{ok:<9}{tf_state:<10}{rc:<14}{r['total_seconds']:>9.1f}",
            flush=True,
        )
        if not r["success"] and r.get("error"):
            print(f"     └─ error: {r['error']}", flush=True)
        elif (r.get("recheck") or {}).get("reason"):
            print(f"     └─ recheck: {r['recheck']['reason']}", flush=True)
    print("─" * 78, flush=True)
    print(
        f"Success: {n_success}/{len(LINES)} | "
        f"ACTIVE: {n_active} | DEACTIVATED: {n_deact} | "
        f"other: {len(LINES) - n_active - n_deact}",
        flush=True,
    )
    print(
        f"2FA: full={n_2fa_full} | partial={n_2fa_partial} | none={n_2fa_none}",
        flush=True,
    )

    # ── In toàn bộ accounts.txt (email|password|2fa) ──
    print(f"\n{'═' * 78}", flush=True)
    print(f"ACCOUNTS (email|password|2fa) — {ACCOUNTS_FILE}", flush=True)
    print("═" * 78, flush=True)
    if ACCOUNTS_FILE.exists():
        content = ACCOUNTS_FILE.read_text(encoding="utf-8")
        print(content if content.strip() else "(rỗng)", flush=True)
    else:
        print("(chưa có account nào reg success → accounts.txt chưa tạo)", flush=True)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "summary.json").write_text(
        json.dumps({
            "reg_mode": REG_MODE,
            "grand_total_seconds": round(grand_total, 2),
            "success_count": n_success,
            "active_count": n_active,
            "deactivated_count": n_deact,
            "two_factor_full_count": n_2fa_full,
            "two_factor_partial_count": n_2fa_partial,
            "two_factor_none_count": n_2fa_none,
            "recheck_delay_seconds": _recheck_delay_seconds(),
            "rows": summary,
        }, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    # Pass khi tất cả success VÀ không account nào bị deactivated.
    return 0 if (n_success == len(LINES) and n_deact == 0) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Live test reg hybrid + deactivation recheck")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Chỉ parse + build request (KHÔNG signup, KHÔNG network).",
    )
    parser.add_argument(
        "--line", type=int, default=None,
        help="Chỉ chạy 1 dòng (1-based). Bỏ trống → chạy toàn bộ.",
    )
    args = parser.parse_args()

    # Refresh LINES từ input file (nếu có) trước khi build entries.
    global LINES
    LINES = _load_lines()

    line_index = _resolve_line_index(args.line)
    entries = _select_entries(line_index)

    if args.dry_run:
        return _dry_run(entries)
    return asyncio.run(_live_run(entries))


if __name__ == "__main__":
    sys.exit(main())
