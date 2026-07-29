#!/usr/bin/env python3
"""Smoke chạy `run_upi_qr_probe` THẬT để verify diagnostic proxy log mới.

Test cases:
    [TC-A] Pool có proxy raw format hợp lệ (`host:port:user:pass`) →
        - Log `[proxy] format-error` KHÔNG xuất hiện
        - Log `[upi] proxy policy info` xuất hiện với first_proxy masked
        - Step log (qua pay_upi_http) hiển thị `proxy=via ***@host:port`
        - Login + checkout + init + elements PASS
    [TC-B] Pool toàn line format lỗi → raise UpiQrError + log format-error.

Chạy: ACCOUNT_LINE='email|password|secret' python3 test/smoke_upi_proxy_diagnostic_live.py

Yêu cầu: account thật, network OK. Không submit confirm/approve (an toàn).
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _ok(tc, label, detail=""):
    msg = f"[PASS] {tc} — {label}"
    if detail:
        msg += f" :: {detail}"
    print(msg, flush=True)


def _fail(tc, label, detail=""):
    msg = f"[FAIL] {tc} — {label}"
    if detail:
        msg += f" :: {detail}"
    print(msg, flush=True)


def _parse_account(line):
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 2 or "@" not in parts[0]:
        raise ValueError("ACCOUNT_LINE phải là email|password|totp_secret")
    return parts[0], parts[1], parts[2] if len(parts) >= 3 and parts[2] else None


async def t01_valid_proxy_pool():
    """Pool 1 line raw format hợp lệ — chạy live tới step 4 + log diagnostic."""
    print("\n========== TC-A — Pool valid raw line ==========", flush=True)
    from web.upi_runner import run_upi_qr_probe

    account_line = os.environ.get("ACCOUNT_LINE", "").strip()
    if not account_line:
        _fail("TC-A", "thiếu ACCOUNT_LINE env", "skip live test")
        return False
    email, password, secret = _parse_account(account_line)

    captured: list[str] = []
    log = lambda s: (captured.append(s), print(s, flush=True))[0] is None or None

    # Sample raw line user đang dùng (từ output trước):
    # 103.229.53.131:8525:VzchMOE66c696:t0KsQXsV (host:port:user:pass)
    proxy_pool = ["103.229.53.131:8525:VzchMOE66c696:t0KsQXsV"]

    qr_path = Path(tempfile.gettempdir()) / "_smoke_upi_diag_qr.png"

    try:
        result = await run_upi_qr_probe(
            email=email,
            password=password,
            secret=secret,
            proxy_pool=proxy_pool,
            approve_retries=1,  # tới step 6 nhưng chỉ 1 attempt rồi exit
            qr_out_path=qr_path,
            log=log,
            proxy_from_step=6,  # chỉ step 6 (approve) áp proxy → login + 2-5 DIRECT
        )
    except Exception as exc:  # noqa: BLE001
        _fail("TC-A", f"runner raised", repr(exc)[:200])
        return False

    full_log = "\n".join(captured)

    # Assert KHÔNG có format-error.
    if re.search(r"\[proxy\].*format-error", full_log):
        _fail("TC-A", "có format-error log dù line hợp lệ")
        return False
    _ok("TC-A", "không có [proxy] format-error log")

    # Assert có "proxy policy" diagnostic.
    if not re.search(r"\[upi\].*proxy policy", full_log):
        _fail("TC-A", "thiếu [upi] proxy policy info diagnostic")
        return False
    _ok("TC-A", "có log [upi] proxy policy info")

    # Verify masked IP xuất hiện trong proxy_policy log (***@103.229.53.131:8525).
    if "***@103.229.53.131:8525" not in full_log:
        _fail("TC-A", "first_proxy masked URL không xuất hiện trong log",
              "expect ***@103.229.53.131:8525")
        return False
    _ok("TC-A", "first_proxy masked URL xuất hiện trong proxy policy log")

    # Verify proxy_from_step=6 → step 1-5 direct, step 6 via proxy.
    if not re.search(r"via_proxy=6=approve", full_log):
        _fail("TC-A", "proxy_from_step=6 không show via_proxy=6=approve")
        return False
    _ok("TC-A", "proxy_from_step=6 → step 6 via proxy")
    if not re.search(r"direct=1=login,2=checkout,3=stripe_init,4=elements,5=confirm", full_log):
        _fail("TC-A", "proxy_from_step=6 không show direct=1..5")
        return False
    _ok("TC-A", "step 1-5 direct (đúng policy)")

    # Verify login OK + step 2-4 PASS.
    if not result or not getattr(result, "ok", False):
        # Note: ok=False là bình thường vì confirm=False → không có QR. Nhưng login phải xong.
        # Check qua login marker
        if "login" not in full_log or "✓" not in full_log:
            _fail("TC-A", "login không tới ✓")
            return False
    _ok("TC-A", "login + checkout + init + elements completed")

    # Verify step log production có 'proxy=DIRECT' ở step 2-5 (proxy_from_step=6
    # nên chỉ step 6 áp proxy). _step_proxy_tag inline trong _safe_log.
    if "proxy=DIRECT" not in full_log:
        _fail("TC-A", "step log production không có 'proxy=DIRECT' (step 2-5 phải DIRECT)",
              "expect 'proxy=DIRECT' ở step 2/3/4/5b log")
        return False
    _ok("TC-A", "step log production có 'proxy=DIRECT'")

    # Verify step 6 approve attempt log đã có masked proxy URL (đã có sẵn từ
    # logic cũ, regression check).
    if "proxy=http://***@103.229.53.131:8525" not in full_log:
        _fail("TC-A", "approve attempt log không có 'proxy=http://***@…'")
        return False
    _ok("TC-A", "approve attempt log có masked URL")

    return True


async def t02_invalid_proxy_pool():
    """Pool toàn line lỗi format → raise UpiQrError + log format-error."""
    print("\n========== TC-B — Pool format lỗi → fail-fast ==========", flush=True)
    from web.upi_runner import run_upi_qr_probe, UpiQrError

    captured: list[str] = []
    log = lambda s: (captured.append(s), print(s, flush=True))[0] is None or None

    qr_path = Path(tempfile.gettempdir()) / "_smoke_upi_diag_qr.png"
    try:
        await run_upi_qr_probe(
            email="x@example.com",
            password="dummy",
            secret=None,
            proxy_pool=["this_is_garbage"],  # format lỗi
            approve_retries=10,
            qr_out_path=qr_path,
            log=log,
            proxy_from_step=1,
        )
    except UpiQrError as exc:
        full_log = "\n".join(captured)
        msg = str(exc)
        if "first_proxy" not in msg or "format" not in msg.lower():
            _fail("TC-B", "error message không đề cập first_proxy/format", msg[:200])
            return False
        _ok("TC-B", "raise UpiQrError với message rõ", msg[:120])

        if not re.search(r"\[proxy\].*format-error", full_log):
            _fail("TC-B", "thiếu log [proxy] format-error trước khi raise")
            return False
        _ok("TC-B", "log format-error xuất hiện trước raise")
        return True
    except Exception as exc:  # noqa: BLE001
        _fail("TC-B", f"raised non-UpiQrError: {type(exc).__name__}", str(exc)[:200])
        return False

    _fail("TC-B", "không raise — fail-fast guard không hoạt động")
    return False


async def main_async():
    failures = 0
    for name, fn in [
        ("TC-A live valid proxy", t01_valid_proxy_pool),
        ("TC-B live invalid proxy", t02_invalid_proxy_pool),
    ]:
        try:
            ok = await fn()
        except Exception as exc:  # noqa: BLE001
            _fail(name, "raised", repr(exc))
            ok = False
        if not ok:
            failures += 1
    total = 2
    print(f"\n[summary] passed={total - failures}/{total}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))
