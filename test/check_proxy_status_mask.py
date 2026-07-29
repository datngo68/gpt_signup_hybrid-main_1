"""Verify ProxyPool.status()['dead'] mask credential (F-F) — KHÔNG lộ raw line.

Run: .venv/bin/python test/check_proxy_status_mask.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from gpt_signup_hybrid_new.web.proxy_pool import ProxyPool  # noqa: E402


def t01_dead_masked() -> int:
    pool = ProxyPool()
    pool.configure(["h:1:u:realpass", "h:2:u2:p2", "h:3:user-{SID}:secret"])
    pool.mark_dead("h:1:u:realpass")
    pool.mark_dead("h:3:user-{SID}:secret")
    st = pool.status()
    dead = st["dead"]
    blob = "\n".join(dead)
    if "realpass" in blob or "secret" in blob:
        print(f"[FAIL] t01 credential leak in dead-list :: {dead}", flush=True)
        return 1
    if "***@h:1" not in dead or "***@h:3" not in dead:
        print(f"[FAIL] t01 expected masked entries :: {dead}", flush=True)
        return 1
    print(f"[PASS] t01 status().dead masked :: {dead}", flush=True)
    return 0


def main() -> int:
    print("=== check_proxy_status_mask ===", flush=True)
    rc = 0
    try:
        rc = t01_dead_masked()
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] t01 raised {type(exc).__name__}: {exc}", flush=True)
        rc = 1
    print(f"=== done :: {(1 if rc == 0 else 0)}/1 pass ===", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
