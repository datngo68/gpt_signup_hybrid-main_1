"""Thử các format username phổ biến của proxy provider để tìm format đúng.

Khi proxy CONNECT bị abort (curl exit 56) thường do:
    - IP máy chạy chưa whitelist tại dashboard provider
    - Username format không khớp với provider
    - Quota hết

Chạy:
    python3 test/check_proxy_credentials_formats.py \\
        --host 209.38.173.242 --port 31113 \\
        --base 'zp76579_2nhyyb' \\
        --pass 'iMLh6AsVLahI3cwW' \\
        --country India

Test sẽ thử cả format `host:port` direct + 5 format username phổ biến.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PARENT = ROOT.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

from curl_cffi.requests import AsyncSession  # type: ignore


async def _try_proxy(
    sess: AsyncSession,
    proxy_url: str,
    label: str,
) -> None:
    print(f"\n[{label}]", flush=True)
    print(f"   proxy: {proxy_url.replace(proxy_url.split('@')[0].split('//')[1], '***:***')}", flush=True)
    try:
        r = await sess.get(
            "https://ipinfo.io/json",
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=15,
        )
        if r.status_code == 200:
            body = (r.text or "")[:200].replace("\n", " ")
            print(f"   [PASS] HTTP {r.status_code} body={body}", flush=True)
        else:
            print(f"   [FAIL] HTTP {r.status_code}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"   [FAIL] {type(exc).__name__}: {exc}", flush=True)


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", required=True, type=int)
    ap.add_argument("--base", required=True, help="username base (vd zp76579_2nhyyb)")
    ap.add_argument("--pass", dest="passwd", required=True, help="password thuần (vd iMLh6AsVLahI3cwW)")
    ap.add_argument("--country", default="India", help="ISO country (default India)")
    args = ap.parse_args(argv[1:])

    base = args.base
    pw = args.passwd
    cc_full = args.country  # India
    cc_iso = "IN" if args.country.lower().startswith("in") else args.country[:2].upper()
    cc_lower = cc_iso.lower()  # in
    host_port = f"{args.host}:{args.port}"

    formats = [
        ("user-pass-bare (no country)",
            f"http://{base}:{pw}@{host_port}"),

        ("password-suffix-country (như user truyền)",
            f"http://{base}:{pw}_country-{cc_full}@{host_port}"),

        ("username-suffix-country (IPRoyal-style)",
            f"http://{base}_country-{cc_full}:{pw}@{host_port}"),

        ("username-dashed-country (Smartproxy-style)",
            f"http://{base}-country-{cc_lower}:{pw}@{host_port}"),

        ("username-suffix-country-iso (Webshare-style)",
            f"http://{base}-country-{cc_iso}:{pw}@{host_port}"),

        ("brightdata-style",
            f"http://brd-customer-{base}-zone-residential-country-{cc_lower}:{pw}@{host_port}"),
    ]

    async with AsyncSession(impersonate="chrome136") as sess:
        for label, url in formats:
            await _try_proxy(sess, url, label)

    print("\n→ Format nào trả PASS với country=IN trong response → format đúng.", flush=True)
    print("→ Cả 6 fail = IP máy chạy chưa whitelist HOẶC quota hết.", flush=True)
    print("  Login dashboard provider, kiểm whitelist + bandwidth.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv)))
