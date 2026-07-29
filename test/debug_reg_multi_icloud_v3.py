"""Multi-account reg test với icloud_v3 — deep debug invalid_auth_step bug.

Chạy reg N email lần lượt (mặc định 5 fresh emails), capture:
  - Time elapsed
  - Status outcome (success / invalid_auth_step / user_already_exists / other)
  - Error message details

In bảng tóm tắt cuối để analyze pattern.

Usage:
  .venv/bin/python test/debug_reg_multi_icloud_v3.py [START_IDX] [COUNT]

Default: START_IDX=5, COUNT=5 (test fresh emails từ index 5-9).
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Account list user cung cấp (2026-06-28). 5 base emails × 5 aliases mỗi base.
# Index 0-4 đã reg trong test trước (1 alias mỗi base) → các index khác share
# base sẽ trả user_already_exists / invalid_auth_step.
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
    "splits-malt2j+oez6hl@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/KpHKvK-m6lJNqNKIpMVRaQz34k2-2cXC/data",
    "spar.octant4o+dvdwz@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/iYWcjYCl6e1jslJqrooaV6UyIonuVZs_/data",
    "seemly.kettles_6v+27g5lz@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/7OAPSM59ot6IoVx4QYphXwHKVD2buaka/data",
    "did.sirens_9a+8t8ka8o@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/-yx068UnpcKdeokIqZ8cutlRokA-p0mH/data",
    "freest.woolens.50+92m7wd@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/Ks5h4inAEGvE4OiiNGuPsY-va36yKKNV/data",
    "splits-malt2j+xlx0r@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/hJQVK6BKW1tM3CtptiUOjlgXH7M3cQAy/data",
    "spar.octant4o+sy0p5q4@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/9lolZPQ_aNP-LxW-R3vKoTHIr1b_-DRJ/data",
    "seemly.kettles_6v+hrmc5@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/RW37dHkmgF8LxEV47tt_a177pcQjw7aA/data",
    "did.sirens_9a+5r4tc7h@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/TMY4CBTwhFwrQ-s-trsezkpeB-3HTf3O/data",
    "freest.woolens.50+eewqyt@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/NW6IQBRSdTffjP3yYBYp46jFcYt_lj5s/data",
    "splits-malt2j+m47kzm@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/gQ6xkzHQjm-wn9F6KaRKysNP8lJkHR2b/data",
    "spar.octant4o+fafp4dk@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/dTASlMYrvcJ7QslYHAKE1vT2InlayEiB/data",
    "seemly.kettles_6v+dzh7xl@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/Dzc8BX86_63df4QWRVoDb2OO-PqMLF63/data",
    "did.sirens_9a+o5b4g@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/p53hBxm01wOkY9qy3dt5IX11trSgmG_6/data",
    "freest.woolens.50+1zcq6s@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/6RLjqQk5z7hBWsg999JkiUBGchWjbOfA/data",
]


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def log_fn(prefix: str):
    def _log(msg: str) -> None:
        print(f"[{_ts()}] {prefix} {msg}", flush=True)
    return _log


def _classify_error(error_msg: str | None) -> str:
    """Phân loại error message thành category để summary."""
    if not error_msg:
        return "unknown"
    e = error_msg.lower()
    if "invalid_auth_step" in e or "đã được đăng ký" in e:
        return "invalid_auth_step (email đã reg)"
    if "user_already_exists" in e or "accountalreadyexistserror" in e:
        return "user_already_exists (/about-you)"
    if "2fa enabled" in e or "mfa_challenge" in e:
        return "2FA enabled (account đã reg + setup 2FA)"
    if "register failed http 400" in e:
        return "register HTTP 400 (other)"
    if "register failed http" in e:
        return "register HTTP non-200"
    if "timeout" in e:
        return "timeout"
    if "passwordphase" in e or "password chưa được set" in e:
        return "password not set"
    return f"other: {error_msg[:60]}"


async def _run_one(idx: int, line: str) -> dict:
    """Chạy 1 lần reg, return dict summary."""
    from db import get_engine, get_settings_repo
    from db.repositories import RepositoryError
    from web.mail_modes import get_spec
    from signup import run_signup
    from models import SignupResult

    prefix = f"[#{idx}]"
    log = log_fn(prefix)
    log(f"=== START: {line.split('|')[0]} ===")

    try:
        repo = get_settings_repo(get_engine())
        all_settings = repo.list()
    except RepositoryError:
        all_settings = {}

    headless = bool(all_settings.get("reg.headless", True))
    password = all_settings.get("reg.default_password") or "Autogen#2026Xy"

    try:
        from web.manager import _resolve_job_proxy
        proxy, _ = await _resolve_job_proxy()
    except Exception:
        proxy = None

    spec = get_spec("icloud_v3")
    parsed = spec.parse_line(line)
    request = spec.build_request(
        parsed,
        password=password,
        headless=headless,
        proxy=proxy,
        reg_mode="browser",
    )

    t0 = time.monotonic()
    result: SignupResult = await run_signup(request, log=log)
    dt = time.monotonic() - t0

    out = {
        "idx": idx,
        "email": request.email,
        "elapsed": dt,
        "success": result.success,
        "error": result.error,
        "category": _classify_error(result.error) if not result.success else "SUCCESS",
        "password_set": bool(result.password),
        "session_token": bool(result.session_token),
        "access_token": bool(result.access_token),
    }
    log(f"=== END: success={result.success} elapsed={dt:.1f}s category={out['category']} ===")
    return out


async def main() -> int:
    start_idx = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    end_idx = min(start_idx + count, len(_ACCOUNTS))
    if start_idx >= len(_ACCOUNTS):
        print(f"[ERR] start_idx {start_idx} >= total {len(_ACCOUNTS)}")
        return 1

    print(f"\n{'='*70}", flush=True)
    print(f"  MULTI-ACCOUNT REG TEST: index {start_idx}..{end_idx - 1} ({end_idx - start_idx} accounts)", flush=True)
    print(f"{'='*70}\n", flush=True)

    results: list[dict] = []
    for idx in range(start_idx, end_idx):
        try:
            r = await _run_one(idx, _ACCOUNTS[idx])
        except Exception as exc:
            r = {
                "idx": idx,
                "email": _ACCOUNTS[idx].split("|")[0],
                "elapsed": 0.0,
                "success": False,
                "error": f"unexpected: {type(exc).__name__}: {exc}",
                "category": "unexpected_exception",
                "password_set": False,
                "session_token": False,
                "access_token": False,
            }
        results.append(r)
        # Cooldown giữa các test để CF/sentinel reset
        if idx + 1 < end_idx:
            print(f"[cooldown] 5s trước test kế tiếp...\n", flush=True)
            await asyncio.sleep(5.0)

    # Summary
    print(f"\n{'='*70}", flush=True)
    print(f"  SUMMARY", flush=True)
    print(f"{'='*70}", flush=True)
    print(f"{'idx':>3} | {'email':<45} | {'elapsed':>7} | category", flush=True)
    print("-" * 100, flush=True)
    cat_counts: dict[str, int] = {}
    for r in results:
        cat = r["category"]
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
        flag = "✅" if r["success"] else "❌"
        print(f"{r['idx']:>3} | {flag} {r['email']:<42} | {r['elapsed']:>6.1f}s | {cat}", flush=True)
    print("-" * 100, flush=True)
    print(f"\nCategories:", flush=True)
    for cat, cnt in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"  {cnt:>3} × {cat}", flush=True)
    print(f"\nTotal: {len(results)} | Success: {sum(1 for r in results if r['success'])}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
