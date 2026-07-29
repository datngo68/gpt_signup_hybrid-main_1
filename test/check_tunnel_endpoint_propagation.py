"""Verify fix 502: local endpoint của Cloudflare Tunnel lan từ CLI sang app.

Bug: cli.py set_local_endpoint() trên singleton module `web.cloudflare_tunnel`,
nhưng server start tunnel trên singleton `gpt_signup_hybrid.web.cloudflare_tunnel`
(module khác). Singleton phía app giữ default 127.0.0.1:8083 → cloudflared
forward sai port → 502.

Fix: set_local_endpoint() ghi os.environ; start_async() đọc lại qua
_load_endpoint_from_env().

Run: python3 test/check_tunnel_endpoint_propagation.py
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))           # `import web.cloudflare_tunnel` (như cli.py)
sys.path.insert(0, str(ROOT.parent))    # `import gpt_signup_hybrid...` (folder name)


def main() -> int:
    import web.cloudflare_tunnel as a  # như cli.py

    try:
        b = importlib.import_module("gpt_signup_hybrid.web.cloudflare_tunnel")
    except Exception as e:  # noqa: BLE001
        print(f"[INFO] không import được gpt_signup_hybrid.web.cloudflare_tunnel: {e}")
        print("[INFO] không thể tái hiện môi trường uvicorn — skip.")
        return 0

    failures: list[str] = []

    # 2 singleton khác nhau? (điều kiện gây bug)
    ta = a.get_cloudflare_tunnel()
    tb = b.get_cloudflare_tunnel()
    same_module = a is b
    print(f"same module object?       {same_module}")
    print(f"tb default port (trước)   = {tb._local_port}")

    # CLI set endpoint trên singleton phía CLI (a) với port khác default.
    ta.set_local_endpoint("0.0.0.0", 8090)

    # App (b) đồng bộ từ env như start_async() làm.
    tb._load_endpoint_from_env()
    print(f"tb host sau load          = {tb._local_host}")
    print(f"tb port sau load          = {tb._local_port}")

    if tb._local_port == 8090:
        print("[PASS] endpoint port lan qua env → app forward đúng 8090")
    else:
        failures.append(f"app port = {tb._local_port}, expected 8090")
        print(f"[FAIL] app port = {tb._local_port}, expected 8090")

    # 0.0.0.0 phải được chuẩn hóa về loopback cho tunnel an toàn.
    if tb._local_host == "127.0.0.1":
        print("[PASS] host chuẩn hóa về 127.0.0.1 (bind 0.0.0.0)")
    else:
        failures.append(f"app host = {tb._local_host}, expected 127.0.0.1")
        print(f"[FAIL] app host = {tb._local_host}, expected 127.0.0.1")

    # Target url mà start_async sẽ spawn cloudflared --url.
    target = f"http://{tb._local_host}:{tb._local_port}"
    print(f"cloudflared --url         = {target}")
    if target == "http://127.0.0.1:8090":
        print("[PASS] target url đúng port → hết 502")
    else:
        failures.append(f"target url sai: {target}")
        print(f"[FAIL] target url sai: {target}")

    print("", flush=True)
    if failures:
        print(f"=== {len(failures)} FAILURE(S) ===", flush=True)
        for x in failures:
            print(f"  - {x}", flush=True)
        return 1
    print("=== ALL PASS ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
