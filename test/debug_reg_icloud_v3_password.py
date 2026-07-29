"""Debug live 1 lần reg ChatGPT với icloud_v3 provider — verify fix:
  1. Password phải được set qua /create-account/password POST 200
  2. Sau reg, /api/auth/session trả accessToken + user.id

Dùng account thứ N từ list user cung cấp (default N=0 = đầu tiên).
Đọc proxy từ pool (giống production autoreg). Log realtime từng dòng.

Chạy: .venv/bin/python test/debug_reg_icloud_v3_password.py [INDEX]

INDEX optional, default 0 (account đầu tiên trong _ACCOUNTS).
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# Account list user cung cấp (2026-06-28) — icloud_v3 + Worker v2.
_ACCOUNTS: list[str] = [
    "splits-malt2j+kaxywo@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/BnTDVbT3cDgy0KqYRqhQXlbdaxSKYxh4/data",
    "spar.octant4o+luizl@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/h_ir7oQOTp3T_O9_A53hUrALlZlJBsMs/data",
    "seemly.kettles_6v+t7iv8@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/NllonToFc8-cU5drw0V4mo7SYfpcggKu/data",
    "did.sirens_9a+7cg5yjw@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/b6E-BBVH2Dw2SR3hG7JHEO9iW2WD9_ns/data",
    "freest.woolens.50+6w04u@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/ZejAOPW7EA_YmM9Gt8p01wcryBQhOING/data",
    "splits-malt2j+gh9sxq@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/gneo4du-W9z_reLb7GgtFJvo01o_oxKm/data",
    "spar.octant4o+epjekz@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/awcJejZZsJx81HGjqo-VAHLttEU3Oe9b/data",
    "seemly.kettles_6v+9uzpt@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/P2xqZR3exwTns6LNK9BEjVljNI4XubfO/data",
    "did.sirens_9a+nmhq2@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/LLQ0MtxPx3v4TdJmME-YlW2xXgfkHZYh/data",
    "freest.woolens.50+cs9iv@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/C2Yfn4rOxe7xLx2pyDB0q1dn44Nxf4GZ/data",
]


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def log_fn(msg: str) -> None:
    print(f"[{_ts()}] {msg}", flush=True)


async def main() -> int:
    idx = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    if not (0 <= idx < len(_ACCOUNTS)):
        log_fn(f"[ERR] index {idx} ngoài range [0, {len(_ACCOUNTS) - 1}]")
        return 1

    line = _ACCOUNTS[idx]
    log_fn(f"=== TEST CASE: account index {idx}/{len(_ACCOUNTS) - 1} ===")
    log_fn(f"line: {line}")

    from db import get_engine, get_settings_repo
    from db.repositories import RepositoryError
    from web.mail_modes import get_spec
    from signup import run_signup
    from models import SignupResult

    # 1. Settings
    try:
        repo = get_settings_repo(get_engine())
        all_settings = repo.list()
    except RepositoryError as exc:
        log_fn(f"[settings] load fail: {exc} → dùng default")
        all_settings = {}

    headless = bool(all_settings.get("reg.headless", True))
    job_timeout = float(all_settings.get("reg.job_timeout", 240))
    password = all_settings.get("reg.default_password") or "Autogen#2026Xy"
    log_fn(f"[cfg] headless={headless} job_timeout={job_timeout}s password={password!r}")

    # 2. Proxy từ pool
    try:
        from web.manager import _resolve_job_proxy
        proxy, proxy_line = await _resolve_job_proxy()
        log_fn(f"[proxy] resolved={'<set>' if proxy else 'DIRECT (pool rỗng)'}")
    except Exception as exc:
        proxy = None
        log_fn(f"[proxy] resolve fail: {type(exc).__name__}: {exc} → DIRECT")

    # 3. Build request qua icloud_v3 spec
    spec = get_spec("icloud_v3")
    parsed = spec.parse_line(line)
    request = spec.build_request(
        parsed,
        password=password,
        headless=headless,
        proxy=proxy,
        reg_mode="browser",  # explicit để verify nhánh browser
    )
    log_fn(
        f"[req] email={request.email} provider={request.mail_provider} "
        f"reg_mode={request.reg_mode} "
        f"otp_timeout={request.otp_timeout_seconds} poll={request.otp_poll_interval_seconds}"
    )

    # 4. Run signup
    log_fn("=== START run_signup ===")
    t0 = time.monotonic()
    result: SignupResult = await run_signup(request, log=log_fn)
    dt = time.monotonic() - t0

    log_fn("=== RESULT ===")
    log_fn(f"success={result.success}")
    log_fn(f"error={result.error}")
    log_fn(f"email={result.email}")
    log_fn(f"password={result.password!r}")
    log_fn(f"session_token={'<set>' if result.session_token else None}")
    log_fn(f"access_token={'<set>' if result.access_token else None}")
    log_fn(f"user_id={result.user_id}")
    log_fn(f"account_id={result.account_id}")
    log_fn(
        f"elapsed={dt:.1f}s "
        f"phase1={result.phase1_seconds} otp={result.otp_seconds} phase2={result.phase2_seconds}"
    )

    # Validation checks (verify fix yêu cầu user)
    print("", flush=True)
    log_fn("=== VALIDATION ===")
    failures = []
    if not result.success:
        failures.append(f"[FAIL] result.success=False ({result.error})")
    else:
        log_fn("[PASS] result.success=True")

    if not result.password:
        failures.append("[FAIL] result.password trống — password chưa được set")
    elif result.password != password:
        failures.append(f"[FAIL] result.password mismatch: expected={password!r}, got={result.password!r}")
    else:
        log_fn(f"[PASS] result.password đúng giá trị: {result.password!r}")

    if not result.session_token:
        failures.append("[FAIL] result.session_token rỗng — verify chatgpt session fail")
    else:
        log_fn(f"[PASS] result.session_token set ({len(result.session_token)} bytes)")

    if not result.access_token:
        failures.append("[FAIL] result.access_token rỗng — /api/auth/session fail")
    else:
        log_fn(f"[PASS] result.access_token set ({len(result.access_token)} bytes)")

    if not result.user_id:
        failures.append("[FAIL] result.user_id rỗng")
    else:
        log_fn(f"[PASS] result.user_id: {result.user_id[:24]}...")

    print("", flush=True)
    if failures:
        log_fn("=== ❌ TEST FAIL ===")
        for f in failures:
            log_fn(f)
        return 1
    log_fn("=== ✅ TEST PASS — password set chuẩn, session verified ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
