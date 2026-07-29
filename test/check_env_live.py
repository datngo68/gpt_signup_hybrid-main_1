"""Pre-flight cho live signup test — verify deps + reachability các host reg.

KHÔNG đăng ký gì. Chỉ:
    [1] import curl_cffi / camoufox (deps cho 3 mode reg).
    [2] TCP connect tới chatgpt.com / auth.openai.com / sentinel.openai.com /
        worker mail — xác định outbound có bị chặn không (tôn trọng timeout,
        KHÔNG dùng curl_cffi.get vì nó treo/crash native khi outbound blackhole).

Chạy: .venv/bin/python test/check_env_live.py
Exit 0 = mọi host reachable; 1 = có host bị chặn / thiếu dep.
"""
from __future__ import annotations

import socket

_HOSTS = (
    "chatgpt.com",
    "auth.openai.com",
    "sentinel.openai.com",
    "icloud-cf-mail-v2.n5pskgzs9g.workers.dev",
)


def main() -> int:
    # [1] deps
    try:
        import curl_cffi
        print(f"[ok] curl_cffi {getattr(curl_cffi, '__version__', '?')}")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] curl_cffi import: {exc}")
        return 1
    try:
        import camoufox  # noqa: F401
        print("[ok] camoufox import OK")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] camoufox import: {exc} (browser/hybrid mode sẽ fail)")

    # [2] network probe — TCP connect (an toàn, tôn trọng timeout)
    all_ok = True
    for host in _HOSTS:
        print(f"[..] TCP connect {host}:443 (timeout 8s)...", flush=True)
        try:
            socket.create_connection((host, 443), timeout=8).close()
            print(f"[ok] {host}:443 reachable", flush=True)
        except Exception as exc:  # noqa: BLE001
            all_ok = False
            print(f"[FAIL] {host}:443 — {type(exc).__name__}: {exc}", flush=True)

    print("\n" + "=" * 56)
    if all_ok:
        print("VERDICT: mọi host reachable (live signup khả thi từ môi trường này)")
        return 0
    print("VERDICT: có host bị chặn — KHÔNG live signup được ở đây")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
