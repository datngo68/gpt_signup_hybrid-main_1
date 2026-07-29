"""Manual probe thật 1 proxy line qua curl_cffi (CÓ network — không vào sweep tự động).

Usage:
    .venv/bin/python test/probe_proxy_health.py --line "host:port:user-{SID}:pass"
    .venv/bin/python test/probe_proxy_health.py --line "http://u:p@h:1" --endpoint https://api64.ipify.org
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from gpt_signup_hybrid_new.web.proxy_format import mask_proxy, materialize_proxy  # noqa: E402
from gpt_signup_hybrid_new.web.proxy_health import probe_proxy  # noqa: E402


async def _main(args) -> int:
    url = materialize_proxy(args.line, sid_len=args.sid_len)
    print(f"materialized: {mask_proxy(url)}")
    ok, reason = await probe_proxy(url, endpoint=args.endpoint, timeout=args.timeout)
    print(f"probe → ok={ok} reason={reason}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--line", required=True, help="raw proxy line/template")
    ap.add_argument("--endpoint", default="https://api64.ipify.org")
    ap.add_argument("--timeout", type=int, default=6)
    ap.add_argument("--sid-len", type=int, default=8, dest="sid_len")
    args = ap.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
