#!/usr/bin/env python3
"""Smoke: import web.upi_runner dạng package + verify field/func mới tồn tại."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from web.upi_runner import (
        UpiQrResult, _find_hosted_instructions_url, run_upi_qr_probe,
    )
    import inspect

    # Field payment_link có trong dataclass + to_dict.
    r = UpiQrResult(ok=True, email="x@y.com", payment_link="https://payments.stripe.com/upi/instructions/ABC")
    assert r.payment_link == "https://payments.stripe.com/upi/instructions/ABC", "field thiếu"
    assert "payment_link" in r.to_dict(), "to_dict thiếu payment_link"
    print("[PASS] UpiQrResult.payment_link + to_dict OK", flush=True)

    # Matcher callable.
    assert callable(_find_hosted_instructions_url)
    print("[PASS] _find_hosted_instructions_url tồn tại", flush=True)

    # run_upi_qr_probe signature giữ nguyên (không vỡ caller).
    sig = inspect.signature(run_upi_qr_probe)
    for p in ("email", "password", "proxy_pool", "approve_retries", "qr_out_path", "log"):
        assert p in sig.parameters, f"thiếu param {p}"
    print("[PASS] run_upi_qr_probe signature OK", flush=True)

    # manager import OK + UpiJob.payment_link.
    from web.manager import UpiJob
    j = UpiJob(id="t", email="x@y.com", password="p", payment_link="L")
    assert "payment_link" in j.to_dict(), "UpiJob.to_dict thiếu payment_link"
    print("[PASS] web.manager + UpiJob.payment_link OK", flush=True)

    print("\n[summary] all import/field checks passed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
