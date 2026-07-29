"""Sinh reference output cho Stripe form encode (`_to_form` flatten dict).

Dùng để verify Rust port `stripe::forms::to_form` cho ra cùng key/value pairs
với cùng thứ tự cho cùng JSON input.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


def main() -> int:
    from gpt_signup_hybrid_new.pay_upi_http import _to_form

    test_payload = {
        "_stripe_version": "2025-03-31.basil; checkout_server_update_beta=v1",
        "expected_amount": 0,
        "expected_payment_method_type": "upi",
        "passive_captcha_token": None,  # None → skip
        "passive_captcha_ekey": None,
        "elements_session_client": {
            "client_betas": ["beta_a", "beta_b"],
            "is_aggregation_expected": "false",
            "locale": "en",
        },
        "payment_method_data": {
            "billing_details": {
                "address": {
                    "city": "Mumbai",
                    "country": "IN",
                    "postal_code": "400001",
                },
                "email": "test@example.com",
                "name": "Aarav Sharma",
            },
            "type": "upi",
            "upi": {"qr_code": {}},
        },
        "version": "e5ebd5e1e6",
    }
    pairs = _to_form(test_payload)
    print("# Reference Stripe form-encoded pairs (Python order)")
    for k, v in pairs:
        print(f"  ({k!r}, {v!r})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
