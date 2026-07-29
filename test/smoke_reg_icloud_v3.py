"""Smoke test reg ChatGPT thật với provider iCloud v3.

Mirror autoreg runner flow:
  - Settings: headless / job_timeout / default_password
  - Proxy: lấy từ pool qua web.manager._resolve_job_proxy (giống production)
  - Mail mode: icloud_v3 (Worker v2, URL per-mailbox)
  - reg_mode mặc định = 'browser'

Cách dùng:
  - Sửa LINES bên dưới (1 hoặc nhiều dòng `email|api_url`) → chạy:
        python3 test/smoke_reg_icloud_v3.py
  - Hoặc truyền index để chọn dòng:
        python3 test/smoke_reg_icloud_v3.py 0
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# Danh sách input user cung cấp. Test pick 1 dòng (mặc định dòng đầu) — sửa
# index qua argv[1] nếu cần thử mailbox khác.
LINES: list[str] = [
    "petunia-boar-3d+hblx3n@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/mELlXOnuhwiUDHjc7IFc-fllJLoCeuAv/data",
    "pasties.sateen.7c+im3zd@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/g5hvaOkVuQq_THa1GONjRmzt19HaGDff/data",
    "petunia-boar-3d+wgnqkzb@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/ldi7e1d9FQBsepBTKMoFDkRcJz2Qf43d/data",
    "pasties.sateen.7c+opadzi@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/yfKfoAnIoSr6XWVXNBc9wPpLXaXCgKGh/data",
    "petunia-boar-3d+e0nxd@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/ms9zrKhjKN50ADe4P1naWlILp_K3OLjh/data",
    "pasties.sateen.7c+oqgos@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/G3kILEtAbDaymI5lC3tsV9KybuI-MO2M/data",
    "petunia-boar-3d+1003cpo@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/s2Fw9Jyb8Bmao7nKMAn9oGoNY6-L1dh_/data",
    "pasties.sateen.7c+m8u5lk@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/YCjlXv69bfRFfuqw1YSnexfnrVQRfqVN/data",
    "petunia-boar-3d+9oj8vz@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/xD4gZMXFhKNWWnYhexW4lSqnU_k4p75N/data",
    "pasties.sateen.7c+gabaj6@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/bw9nLOSDXxq52LIe7TwbPDTZOMXA8KLN/data",
    "petunia-boar-3d+4tr0z4a@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/xtejtlHZ_WcCIqgSKSfk2lKARxBdzBFG/data",
    "pasties.sateen.7c+60kk46k@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/bSXZNnXGhBVHh8Rq3T-um0jhx_btlMDM/data",
]


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def log_fn(msg: str) -> None:
    print(f"[{_ts()}] {msg}", flush=True)


async def main() -> int:
    # Late import — để PYTHONPATH đã set
    from db import get_engine, get_settings_repo
    from db.repositories import RepositoryError
    from models import SignupResult
    from signup import run_signup
    from web.mail_modes import get_spec

    # Chọn line
    idx = 0
    if len(sys.argv) >= 2:
        try:
            idx = int(sys.argv[1])
        except ValueError:
            log_fn(f"argv[1]={sys.argv[1]!r} không phải int → dùng 0")
            idx = 0
    if not (0 <= idx < len(LINES)):
        log_fn(f"idx={idx} ngoài [0, {len(LINES)})")
        return 2
    line = LINES[idx]
    email_preview = line.split("|", 1)[0]
    log_fn(f"=== SMOKE reg icloud_v3 — idx={idx} email={email_preview} ===")

    # Settings store (source of truth)
    try:
        repo = get_settings_repo(get_engine())
        all_settings = repo.list()
    except RepositoryError as exc:
        log_fn(f"[settings] load fail: {exc} → dùng default")
        all_settings = {}

    headless = bool(all_settings.get("reg.headless", True))
    job_timeout = float(all_settings.get("reg.job_timeout", 240))
    password = all_settings.get("reg.default_password") or "Autogen#2026Xy"
    log_fn(
        f"[cfg] headless={headless} job_timeout={job_timeout}s "
        f"reg_mode=browser(default) password={'<set>' if password else '<auto>'}"
    )

    # Proxy từ pool — same flow như autoreg/runner
    proxy: str | None = None
    try:
        from web.manager import _resolve_job_proxy
        proxy, proxy_line = await _resolve_job_proxy()
        log_fn(f"[proxy] resolved={'<set>' if proxy else 'DIRECT (pool rỗng)'}")
    except Exception as exc:  # noqa: BLE001
        log_fn(f"[proxy] resolve fail: {type(exc).__name__}: {exc} → DIRECT")

    # Build request qua icloud_v3 spec
    spec = get_spec("icloud_v3")
    parsed = spec.parse_line(line)
    request = spec.build_request(
        parsed,
        password=password,
        headless=headless,
        proxy=proxy,
    )
    log_fn(
        f"[req] email={request.email} provider={request.mail_provider} "
        f"reg_mode={request.reg_mode} url_set={bool(request.icloud_v3_url)} "
        f"otp_timeout={request.otp_timeout_seconds}s "
        f"poll={request.otp_poll_interval_seconds}s "
        f"resend_after={request.otp_resend_after_seconds}s"
    )

    log_fn("=== START run_signup ===")
    t0 = time.monotonic()
    try:
        result: SignupResult = await asyncio.wait_for(
            run_signup(request, log=log_fn),
            timeout=max(job_timeout, request.otp_timeout_seconds + 120.0),
        )
    except asyncio.TimeoutError:
        log_fn(f"=== TIMEOUT after {time.monotonic() - t0:.1f}s ===")
        return 1
    dt = time.monotonic() - t0

    log_fn("=== RESULT ===")
    log_fn(f"success={result.success}")
    log_fn(f"error={result.error}")
    log_fn(f"email={result.email} password={result.password}")
    log_fn(f"name={result.name} age={result.age}")
    log_fn(f"session_token={'<set>' if result.session_token else None}")
    log_fn(f"access_token={'<set>' if result.access_token else None}")
    log_fn(
        f"elapsed={dt:.1f}s phase1={result.phase1_seconds:.1f}s "
        f"otp={result.otp_seconds:.1f}s phase2={result.phase2_seconds:.1f}s"
    )
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
