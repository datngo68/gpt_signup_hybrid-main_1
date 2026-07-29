"""Test TLS handshake với AsyncSession (giống UPI runner dùng).

Mục đích: xác nhận curl_cffi AsyncSession có bị lỗi TLS hay không
khi connect tới Stripe API DIRECT (không proxy).
"""
import asyncio
import sys

sys.path.insert(0, "/Volumes/SSD/Developments/gpt_signup_hybrid_new")

from curl_cffi.requests import AsyncSession


TARGETS = [
    ("https://api.stripe.com/v1/payment_intents", "Stripe API"),
    ("https://js.stripe.com/v3/", "Stripe JS"),
    ("https://payments.stripe.com", "Stripe Payments"),
]

IMPERSONATES = ["chrome142", "chrome145", "chrome136"]


async def test_single(imp: str, url: str, label: str) -> str:
    try:
        async with AsyncSession(impersonate=imp) as sess:
            resp = await sess.get(url, timeout=15)
            return f"[PASS] {imp:12s} → {label:20s} status={resp.status_code}"
    except Exception as e:
        return f"[FAIL] {imp:12s} → {label:20s} {type(e).__name__}: {e}"


async def test_concurrent(imp: str, url: str, label: str, count: int = 5) -> list[str]:
    """Chạy nhiều request CONCURRENT trên CÙNG session — mô phỏng UPI runner."""
    results = []
    try:
        async with AsyncSession(impersonate=imp) as sess:
            tasks = []
            for i in range(count):
                tasks.append(sess.get(url, timeout=15))
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            for i, r in enumerate(responses):
                if isinstance(r, Exception):
                    results.append(f"[FAIL] {imp} concurrent#{i+1} → {type(r).__name__}: {r}")
                else:
                    results.append(f"[PASS] {imp} concurrent#{i+1} → status={r.status_code}")
    except Exception as e:
        results.append(f"[FAIL] {imp} session-create → {type(e).__name__}: {e}")
    return results


async def main():
    print("=" * 70)
    print("TC-01: Single request per session (DIRECT, no proxy)")
    print("=" * 70, flush=True)

    for imp in IMPERSONATES:
        for url, label in TARGETS:
            result = await test_single(imp, url, label)
            print(result, flush=True)

    print("\n" + "=" * 70)
    print("TC-02: Concurrent requests on SAME session (simulate UPI runner)")
    print("=" * 70, flush=True)

    for imp in IMPERSONATES:
        results = await test_concurrent(imp, TARGETS[0][0], TARGETS[0][1], count=5)
        for r in results:
            print(r, flush=True)

    print("\n" + "=" * 70)
    print("TC-03: Sequential sessions (simulate multiple accounts)")
    print("=" * 70, flush=True)

    for i in range(6):
        imp = IMPERSONATES[i % len(IMPERSONATES)]
        url, label = TARGETS[0]
        result = await test_single(imp, url, label)
        print(f"  account#{i+1}: {result}", flush=True)

    print("\n✓ Done", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
