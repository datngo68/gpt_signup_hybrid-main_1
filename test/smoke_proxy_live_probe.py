"""Live probe 2 proxy user cần test — verify connectivity thực.

Run: .venv/bin/python test/smoke_proxy_live_probe.py
"""
from __future__ import annotations

import asyncio
import sys
import time

sys.path.insert(0, ".")

from web.proxy_format import materialize_proxy


async def probe_one(label: str, raw_line: str) -> None:
    """Probe 1 proxy line — materialize + curl_cffi GET ipify."""
    print(f"\n[{label}] raw = {raw_line}", flush=True)
    try:
        url = materialize_proxy(raw_line)
    except ValueError as e:
        print(f"[{label}] MATERIALIZE FAIL: {e}", flush=True)
        return
    # Mask creds cho log
    from web.proxy_format import mask_proxy
    masked = mask_proxy(url)
    print(f"[{label}] materialized = {masked}", flush=True)

    from curl_cffi.requests import AsyncSession
    endpoint = "https://api64.ipify.org"
    timeout = 10
    t0 = time.monotonic()
    try:
        async with AsyncSession(impersonate="chrome145") as sess:
            r = await sess.get(
                endpoint, proxies={"http": url, "https": url}, timeout=timeout
            )
        elapsed = time.monotonic() - t0
        if r.status_code // 100 == 2:
            ip = r.text.strip()
            print(f"[{label}] ✅ OK  ip={ip}  latency={elapsed:.1f}s  http={r.status_code}", flush=True)
        elif r.status_code == 407:
            print(f"[{label}] ❌ AUTH FAIL  http=407  latency={elapsed:.1f}s", flush=True)
        else:
            print(f"[{label}] ❌ IP-LEVEL FAIL  http={r.status_code}  latency={elapsed:.1f}s", flush=True)
    except Exception as exc:
        elapsed = time.monotonic() - t0
        print(f"[{label}] ❌ EXCEPTION  latency={elapsed:.1f}s  error={exc!r}", flush=True)


async def main():
    print("=== smoke_proxy_live_probe ===", flush=True)

    # Proxy 1: CliProxy (user:pass@host:port form — từng bị parse sai)
    await probe_one(
        "cliproxy",
        "9g5r1200918-region-IN-sid-TsM3NwMX-t-120:kpra1kp6@sg.cliproxy.io:3010",
    )

    # Proxy 2: SOCKS5 (URL form — đã OK)
    await probe_one(
        "socks5",
        "socks5://103.61.122.229:1080",
    )

    print("\n=== done ===", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
