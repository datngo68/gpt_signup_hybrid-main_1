#!/usr/bin/env python3
"""Verify proxy THỰC SỰ đi vào network layer mỗi step (không chỉ log).

Cách làm: monkeypatch `_RotatingSession.post/.get` để record (method, url,
proxies-arg) MỖI request thật, rồi delegate xuống original (vẫn chạy network
thật). Sau run, phân loại URL → step → assert proxies dict khớp policy
`proxy_from_step`.

proxy_from_step=3 → step 1 login DIRECT (session riêng, không intercept),
    step 2 checkout DIRECT, step 3 init / 4 elements / 5 confirm / 6 approve VIA PROXY.

Test cases:
    [TC-1] step 2 checkout: proxies = None (DIRECT).
    [TC-2] step 3 init: proxies != None (VIA PROXY).
    [TC-3] step 4 elements: proxies != None.
    [TC-4] step 5 confirm: proxies != None.
    [TC-5] step 6 approve: proxies != None.
    [TC-6] proxies URL khớp first_proxy materialized (cùng host).

Chạy: ACCOUNT_LINE='email|password|secret' python3 test/smoke_upi_proxy_wiring_live.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Proxy raw line user đang dùng (host:port:user:pass).
PROXY_LINE = "103.229.53.131:8525:VzchMOE66c696:t0KsQXsV"
PROXY_HOST = "103.229.53.131"


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


def _classify(url: str) -> str | None:
    """URL → step name. None = không phải step quan tâm."""
    if "/checkout/approve" in url:
        return "approve"          # step 6
    if url.endswith("/payments/checkout") or "/payments/checkout?" in url:
        return "checkout"         # step 2
    if "/payment_pages/" in url and "/init" in url:
        return "init"             # step 3
    if "/elements/sessions" in url:
        return "elements"         # step 4
    if "/payment_pages/" in url and "/confirm" in url:
        return "confirm"          # step 5
    return None


async def _run() -> int:
    account_line = os.environ.get("ACCOUNT_LINE", "").strip()
    if not account_line:
        _fail("BOOT", "thiếu ACCOUNT_LINE env")
        return 2
    email, password, secret = _parse_account(account_line)

    import web.upi_runner as upi_runner

    # ── Record buffer: list of (step, method, has_proxy, proxy_url) ──
    recorded: list[tuple[str, str, bool, str | None]] = []

    orig_post = upi_runner._RotatingSession.post
    orig_get = upi_runner._RotatingSession.get

    def _extract(args, kwargs):
        url = args[0] if args else kwargs.get("url", "")
        proxies = kwargs.get("proxies")
        proxy_url = None
        if isinstance(proxies, dict):
            proxy_url = proxies.get("https") or proxies.get("http")
        return str(url), proxies, proxy_url

    async def _rec_post(self, *args, **kwargs):
        url, proxies, proxy_url = _extract(args, kwargs)
        step = _classify(url)
        if step:
            recorded.append((step, "POST", proxies is not None, proxy_url))
        return await orig_post(self, *args, **kwargs)

    async def _rec_get(self, *args, **kwargs):
        url, proxies, proxy_url = _extract(args, kwargs)
        step = _classify(url)
        if step:
            recorded.append((step, "GET", proxies is not None, proxy_url))
        return await orig_get(self, *args, **kwargs)

    upi_runner._RotatingSession.post = _rec_post
    upi_runner._RotatingSession.get = _rec_get

    qr_path = Path(tempfile.gettempdir()) / "_smoke_proxy_wiring.png"
    print("[smoke] running run_upi_qr_probe proxy_from_step=3 ...", flush=True)
    try:
        await upi_runner.run_upi_qr_probe(
            email=email,
            password=password,
            secret=secret,
            proxy_pool=[PROXY_LINE],
            approve_retries=1,          # tới step 6 1 lần rồi thoát
            qr_out_path=qr_path,
            log=lambda s: print(s, flush=True),
            proxy_from_step=3,          # step 1-2 direct, 3-6 proxy
        )
    except Exception as exc:  # noqa: BLE001
        _fail("RUN", "runner raised", repr(exc)[:200])
        # vẫn tiếp tục assert recorded (có thể đã ghi đủ trước khi fail)
    finally:
        upi_runner._RotatingSession.post = orig_post
        upi_runner._RotatingSession.get = orig_get

    # ── Print recorded ───────────────────────────────────────────────
    print("\n" + "=" * 60, flush=True)
    print(" RECORDED NETWORK CALLS (step → proxy?)", flush=True)
    print("=" * 60, flush=True)
    from web.proxy_format import mask_proxy
    for step, method, has_proxy, proxy_url in recorded:
        tag = f"PROXY {mask_proxy(proxy_url)}" if has_proxy else "DIRECT"
        print(f"  {method:5s} step={step:10s} → {tag}", flush=True)

    # ── Assertions ───────────────────────────────────────────────────
    print("\n[smoke] assertions...", flush=True)
    by_step: dict[str, list[tuple[bool, str | None]]] = {}
    for step, _method, has_proxy, proxy_url in recorded:
        by_step.setdefault(step, []).append((has_proxy, proxy_url))

    expectations = [
        ("TC-1", "checkout", False, "step 2 < 3 → DIRECT"),
        ("TC-2", "init", True, "step 3 >= 3 → PROXY"),
        ("TC-3", "elements", True, "step 4 >= 3 → PROXY"),
        ("TC-4", "confirm", True, "step 5 >= 3 → PROXY"),
        ("TC-5", "approve", True, "step 6 >= 3 → PROXY"),
    ]
    failures = 0
    for tc, step, expect_proxy, note in expectations:
        calls = by_step.get(step)
        if not calls:
            _fail(tc, f"{step}: KHÔNG có request nào được record",
                  "step không chạy tới (login/confirm fail?)")
            failures += 1
            continue
        # Mọi call của step này phải khớp expect_proxy.
        mismatch = [c for c in calls if c[0] != expect_proxy]
        if mismatch:
            _fail(tc, f"{step}: {len(mismatch)}/{len(calls)} call sai proxy state",
                  f"expect has_proxy={expect_proxy} ({note})")
            failures += 1
            continue
        _ok(tc, f"{step}: {len(calls)} call has_proxy={expect_proxy}", note)

    # TC-6: proxy URL của step 3-6 phải trỏ đúng host PROXY_HOST.
    proxied = [
        (s, purl) for s, _m, hp, purl in recorded if hp and purl
    ]
    if not proxied:
        _fail("TC-6", "không có call nào qua proxy để verify host")
        failures += 1
    else:
        bad_host = [(s, purl) for s, purl in proxied if PROXY_HOST not in (purl or "")]
        if bad_host:
            _fail("TC-6", f"{len(bad_host)} call proxy URL sai host",
                  f"expect chứa {PROXY_HOST}")
            failures += 1
        else:
            _ok("TC-6", f"{len(proxied)} proxied call đều trỏ {PROXY_HOST}")

    print(f"\n[summary] passed={len(expectations) + 1 - failures}/{len(expectations) + 1}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
