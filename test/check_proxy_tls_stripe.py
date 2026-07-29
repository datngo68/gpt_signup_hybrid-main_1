"""Test TLS handshake tới Stripe qua proxy pool từ Settings Store.

Xác minh từng proxy trong pool có connect được Stripe API qua TLS hay không.
Mục đích: isolate proxy nào đang gây SSLError curl:(35) cho UPI flow.
"""
import asyncio
import json
import sys
import sqlite3

sys.path.insert(0, "/Volumes/SSD/Developments/gpt_signup_hybrid_new")

from curl_cffi.requests import AsyncSession

DB_PATH = "/Volumes/SSD/Developments/gpt_signup_hybrid_new/runtime/data.db"
STRIPE_URL = "https://api.stripe.com/v1/payment_intents"
IMPERSONATES = ["chrome142", "chrome145", "chrome136"]


def load_proxy_pool() -> list[str]:
    """Đọc proxy.pool từ Settings Store SQLite."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT value FROM settings WHERE key = ?", ("proxy.pool",))
    row = cur.fetchone()
    conn.close()
    if not row:
        return []
    try:
        pool = json.loads(row[0])
        return pool if isinstance(pool, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def mask_proxy(proxy: str) -> str:
    """Mask credentials trong proxy URL để không leak."""
    if "@" in proxy:
        # user:pass@host:port → ***@host:port
        at_idx = proxy.rfind("@")
        return "***@" + proxy[at_idx + 1:]
    return proxy


async def test_proxy(proxy: str, imp: str) -> str:
    """Test 1 proxy + 1 impersonate → Stripe."""
    proxies = {"http": proxy, "https": proxy}
    masked = mask_proxy(proxy)
    try:
        async with AsyncSession(impersonate=imp, proxies=proxies) as sess:
            resp = await sess.get(STRIPE_URL, timeout=15)
            return f"[PASS] {imp:12s} via {masked[:40]:40s} → status={resp.status_code}"
    except Exception as e:
        err_short = str(e)[:120]
        return f"[FAIL] {imp:12s} via {masked[:40]:40s} → {type(e).__name__}: {err_short}"


async def test_direct(imp: str) -> str:
    """Test DIRECT (không proxy) → baseline."""
    try:
        async with AsyncSession(impersonate=imp) as sess:
            resp = await sess.get(STRIPE_URL, timeout=15)
            return f"[PASS] {imp:12s} DIRECT                                   → status={resp.status_code}"
    except Exception as e:
        return f"[FAIL] {imp:12s} DIRECT                                   → {type(e).__name__}: {str(e)[:80]}"


async def main():
    pool = load_proxy_pool()
    print(f"proxy.pool entries: {len(pool)}", flush=True)
    if not pool:
        print("[WARN] proxy.pool trống — UPI sẽ chạy DIRECT", flush=True)
        return

    # Baseline: DIRECT
    print("\n--- Baseline (DIRECT, no proxy) ---", flush=True)
    for imp in IMPERSONATES:
        result = await test_direct(imp)
        print(f"  {result}", flush=True)

    # Test mỗi proxy
    print(f"\n--- Proxy pool ({len(pool)} entries) ---", flush=True)
    for idx, raw_proxy in enumerate(pool, start=1):
        # Materialize nếu cần (proxy có template {SID})
        proxy = raw_proxy.strip()
        if not proxy:
            print(f"  [{idx}] EMPTY LINE — skip", flush=True)
            continue
        # Dùng materialize_proxy chính xác như UPI runner
        from web.proxy_format import materialize_proxy
        try:
            proxy = materialize_proxy(proxy)
        except ValueError as e:
            print(f"    materialize_proxy error: {e}", flush=True)
            continue

        print(f"\n  [{idx}] {mask_proxy(proxy)}", flush=True)
        for imp in IMPERSONATES:
            result = await test_proxy(proxy, imp)
            print(f"    {result}", flush=True)
            # Nếu PASS 1 impersonate → skip còn lại (proxy OK)
            if "[PASS]" in result:
                print(f"    → proxy OK, skip remaining impersonates", flush=True)
                break

    print("\n✓ Done", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
