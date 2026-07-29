"""Live test: chạy reg hybrid 5 email iCloud HME, đo timing chi tiết.

Mục tiêu:
    - Verify fix Playwright Sync API trong asyncio loop chạy thực.
    - Đo phase1/otp/phase2/total/cold-launch timing từng email.
    - Detect bug regression / bottleneck flow hybrid.

Chạy:
    .venv/bin/python test/run_hybrid_live_5emails.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Silence noise — chỉ giữ runner/pool log từ print().
logging.basicConfig(level=logging.WARNING)


EMAILS = (
    "accents_jurist.0t+ormcb@icloud.com",
    "balks_haze.4c+y2ozybp@icloud.com",
    "gazer.benign-8g+u41qs9y@icloud.com",
    "kappas-nobler-9s+ws02sr@icloud.com",
    "refit_garble.6c+y2sgbra@icloud.com",
)

WORKER_LOGS_URL = "https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/logs"
WORKER_API_KEY = "12345678@"

OUT_DIR = ROOT / "runtime" / "live_hybrid_5emails"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def _make_logger(prefix: str):
    def _log(msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}][{prefix}] {msg}", flush=True)
    return _log


async def _run_one(email: str, idx: int, total: int) -> dict:
    """Chạy 1 signup hybrid, return dict timing + status."""
    from models import SignupRequest
    from random_profile import random_profile_for_locale
    from mail_providers import WorkerMailProvider
    from reg_hybrid import run_hybrid_signup

    prefix = f"{idx}/{total} {email.split('@')[0][:24]}"
    log = _make_logger(prefix)

    # Gen profile random (password, name, age, birthdate) — đồng nhất pipeline
    # signup.run_signup làm gì cho mode hybrid (random_profile_for_locale).
    profile = random_profile_for_locale("en-US")

    request = SignupRequest(
        email=email,
        password=profile["password"],
        name=profile["name"],
        birthdate=profile["birthdate"],
        reg_mode="hybrid",
        mail_provider="worker",
        email_logs_url=WORKER_LOGS_URL,
        email_api_key=WORKER_API_KEY,
        email_insecure_tls=False,
        headless=True,
        proxy=None,
        tls_insecure=False,
        # Bump OTP timeout + resend cycle để đợi mail HME tới (thực tế relay có
        # thể delay 5-10 phút). Hybrid OTP loop auto-resend mỗi [base*0.5, base]
        # giây — base=120 → resend mỗi 60-120s, max_resends=20 (~20 min total).
        otp_timeout_seconds=1800.0,      # 30 phút
        otp_poll_interval_seconds=4.0,
        otp_resend_after_seconds=120.0,  # resend mỗi 60-120s (random)
        sentinel_cookie_timeout_seconds=30.0,
        locale="en-US",
        persona="firefox_mac",
        # MFA inline: enroll + activate 2FA tự động bằng curl session sống
        # sau khi reg success → result.two_factor.secret → xuất email|password|secret.
        mfa_inline=True,
    )

    mail_provider = WorkerMailProvider(
        logs_url=WORKER_LOGS_URL,
        api_key=WORKER_API_KEY,
        insecure_tls=False,
    )

    log(
        f"START email={email} name={profile['name']!r} "
        f"birthdate={profile['birthdate']} pw_len={len(profile['password'])}"
    )

    t_start = time.monotonic()
    checkpoint_otp_at: list[float] = []

    def on_checkpoint(stage: str) -> None:
        if stage == "otp":
            checkpoint_otp_at.append(time.monotonic())

    try:
        result = await run_hybrid_signup(
            request,
            mail_provider=mail_provider,
            log=log,
            on_checkpoint=on_checkpoint,
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - t_start
        log(f"FATAL: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        return {
            "email": email,
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
            "total_seconds": round(elapsed, 2),
            "phase1_seconds": 0.0,
            "otp_seconds": 0.0,
            "phase2_seconds": 0.0,
            "otp_checkpoint_seconds": (
                round(checkpoint_otp_at[0] - t_start, 2)
                if checkpoint_otp_at else None
            ),
            "session_token_len": 0,
            "access_token_len": 0,
            "cookies_count": 0,
        }

    elapsed = time.monotonic() - t_start

    log(
        f"END success={result.success} "
        f"phase1={result.phase1_seconds:.2f}s "
        f"otp={result.otp_seconds:.2f}s "
        f"phase2={result.phase2_seconds:.2f}s "
        f"total={elapsed:.2f}s "
        f"error={result.error or '<none>'}"
    )

    payload = result.model_dump()
    payload["__elapsed_seconds"] = round(elapsed, 2)
    payload["__otp_checkpoint_seconds"] = (
        round(checkpoint_otp_at[0] - t_start, 2)
        if checkpoint_otp_at else None
    )
    out_path = OUT_DIR / f"{idx:02d}_{email.replace('@', '_at_').replace('+', '_plus_')}.json"
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return {
        "email": email,
        "success": result.success,
        "error": result.error,
        "total_seconds": round(elapsed, 2),
        "phase1_seconds": round(result.phase1_seconds or 0.0, 2),
        "otp_seconds": round(result.otp_seconds or 0.0, 2),
        "phase2_seconds": round(result.phase2_seconds or 0.0, 2),
        "otp_checkpoint_seconds": (
            round(checkpoint_otp_at[0] - t_start, 2)
            if checkpoint_otp_at else None
        ),
        "session_token_len": len(result.session_token or ""),
        "access_token_len": len(result.access_token or ""),
        "cookies_count": len(result.cookies or []),
        "user_id": result.user_id,
        "account_id": result.account_id,
    }


async def _main() -> int:
    summary: list[dict] = []
    grand_start = time.monotonic()

    # ── Pre-warm Camoufox pool (đo cold launch lần đầu tách khỏi signup #1) ──
    from reg_hybrid.browser_pool import get_pool
    pool = get_pool()
    t_warm = time.monotonic()
    print("[pool] pre-warming Camoufox...", flush=True)
    try:
        pool.warm_up(
            proxy=None, headless=True, insecure=False,
            log=_make_logger("warm"),
        )
        print(
            f"[pool] warm_up done in {time.monotonic() - t_warm:.2f}s",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[pool] warm_up failed (vẫn tiếp tục, sẽ cold launch ở signup #1): "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )

    for idx, email in enumerate(EMAILS, 1):
        line = "─" * 78
        print(f"\n{line}\n[{idx}/{len(EMAILS)}] {email}\n{line}", flush=True)
        row = await _run_one(email, idx, len(EMAILS))
        summary.append(row)
    grand_total = time.monotonic() - grand_start

    # ── Đợi background cleanup tasks xong + shutdown pool ──
    from reg_hybrid.runner import wait_pending_cleanups
    print("[pool] waiting pending cleanups...", flush=True)
    await wait_pending_cleanups(timeout=10.0)
    pool.shutdown_all()

    # ── Summary table ──
    print(f"\n{'═' * 78}", flush=True)
    print(f"SUMMARY ({len(EMAILS)} emails, grand total {grand_total:.2f}s)", flush=True)
    print("═" * 78, flush=True)
    header = (
        f"{'#':<3}{'email':<45}{'ok':<4}"
        f"{'phase1':>9}{'otp':>8}{'phase2':>9}{'total':>9}"
    )
    print(header, flush=True)
    print("─" * 78, flush=True)
    success_count = 0
    sum_phase1 = sum_otp = sum_phase2 = sum_total = 0.0
    for i, r in enumerate(summary, 1):
        ok = "✓" if r["success"] else "✗"
        if r["success"]:
            success_count += 1
        sum_phase1 += r["phase1_seconds"]
        sum_otp += r["otp_seconds"]
        sum_phase2 += r["phase2_seconds"]
        sum_total += r["total_seconds"]
        email_short = r["email"][:43] + ".." if len(r["email"]) > 45 else r["email"]
        print(
            f"{i:<3}{email_short:<45}{ok:<4}"
            f"{r['phase1_seconds']:>9.2f}{r['otp_seconds']:>8.2f}"
            f"{r['phase2_seconds']:>9.2f}{r['total_seconds']:>9.2f}",
            flush=True,
        )
        if not r["success"] and r.get("error"):
            print(f"     └─ error: {r['error']}", flush=True)
    print("─" * 78, flush=True)
    print(
        f"AVG (n={len(summary)})                                "
        f"   {sum_phase1/len(summary):>9.2f}{sum_otp/len(summary):>8.2f}"
        f"{sum_phase2/len(summary):>9.2f}{sum_total/len(summary):>9.2f}",
        flush=True,
    )
    print(
        f"Success: {success_count}/{len(EMAILS)} | "
        f"Grand total: {grand_total:.2f}s | "
        f"Sum-of-totals: {sum_total:.2f}s "
        f"(pool savings: {sum_total - grand_total:.2f}s)",
        flush=True,
    )

    (OUT_DIR / "summary.json").write_text(
        json.dumps({
            "emails": EMAILS,
            "grand_total_seconds": round(grand_total, 2),
            "success_count": success_count,
            "rows": summary,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return 0 if success_count == len(EMAILS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
