"""Debug 1 lần signup ChatGPT cho 1 email iCloud (worker mail mode).

Mirror đúng AutoRegRunner._process_email:
- worker_config = {logs_url, api_key} (user cung cấp)
- headless / job_timeout đọc từ Settings store
- proxy lấy từ pool qua _resolve_job_proxy (giống production)
- reg_mode = mặc định "browser" (autoreg không truyền reg_mode)

In log realtime từng bước qua log_fn (flush=True) để biết kẹt ở đâu.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

EMAIL = "broader_bravura.8u+7kzfk7@icloud.com"
LOGS_URL = "https://icloud-email.linhtd.com/logs"
API_KEY = "1481d2b23a24f5c7e9cd4a2cdbdb6af7ed03309f97b195b5"


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def log_fn(msg: str) -> None:
    print(f"[{_ts()}] {msg}", flush=True)


async def main() -> None:
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
    log_fn(f"[cfg] headless={headless} job_timeout={job_timeout} reg_mode=browser(default)")

    # 2. Proxy từ pool (giống production)
    try:
        from web.manager import _resolve_job_proxy
        proxy, proxy_line = await _resolve_job_proxy()
        log_fn(f"[proxy] resolved={'<set>' if proxy else 'DIRECT (pool rỗng)'}")
    except Exception as exc:  # noqa: BLE001
        proxy = None
        log_fn(f"[proxy] resolve fail: {type(exc).__name__}: {exc} → DIRECT")

    # 3. Build request qua worker spec
    worker_config = {"logs_url": LOGS_URL, "api_key": API_KEY}
    spec = get_spec("worker")
    parsed = spec.parse_line(EMAIL)
    request = spec.build_request(
        parsed,
        worker_config=worker_config,
        password=password,
        headless=headless,
        proxy=proxy,
    )
    log_fn(
        f"[req] email={request.email} provider={request.mail_provider} "
        f"reg_mode={request.reg_mode} logs_url={request.email_logs_url} "
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
    log_fn(f"email={result.email} password={result.password}")
    log_fn(f"session_token={'<set>' if result.session_token else None}")
    log_fn(f"access_token={'<set>' if result.access_token else None}")
    log_fn(f"elapsed={dt:.1f}s phase1={result.phase1_seconds} otp={result.otp_seconds} phase2={result.phase2_seconds}")


if __name__ == "__main__":
    asyncio.run(main())
