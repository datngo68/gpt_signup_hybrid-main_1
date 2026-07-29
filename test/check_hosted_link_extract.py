#!/usr/bin/env python3
"""Verify _find_hosted_instructions_url trích đúng payments.stripe.com/upi/
instructions/... từ next_action thật (research log đã capture)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from web.upi_runner import (  # noqa: E402
    _find_matches, _find_hosted_instructions_url, _find_qr_image_url, _find_qr_expires_at,
)

PREFIX = "https://payments.stripe.com/upi/instructions/"


def _ok(tc, msg):
    print(f"[PASS] {tc} — {msg}", flush=True)


def _fail(tc, msg):
    print(f"[FAIL] {tc} — {msg}", flush=True)


def main() -> int:
    fails = 0

    # TC-01 — extract từ next_action thật trong research log.
    print("[1/3] TC-01 — extract từ research log thật", flush=True)
    log = ROOT / "runtime" / "research_logs" / "upi_qr_probe_20260616-215951.json"
    if log.exists():
        data = json.loads(log.read_text(encoding="utf-8"))
        matches = _find_matches(data, source="log")
        url = _find_hosted_instructions_url(matches)
        if url and url.startswith(PREFIX):
            _ok("TC-01", f"hosted link = {url[:60]}...")
        else:
            _fail("TC-01", f"không trích được link (got {url!r})")
            fails += 1
    else:
        print("[skip] TC-01 — không có research log", flush=True)

    # TC-02 — synthetic next_action shape (direct key value).
    print("[2/3] TC-02 — synthetic next_action shape", flush=True)
    na = {
        "next_action": {
            "type": "upi_handle_redirect_or_display_qr_code",
            "upi_handle_redirect_or_display_qr_code": {
                "hosted_instructions_url": PREFIX + "ABC123xyz",
                "qr_code": {
                    "expires_at": 1781622292,
                    "image_url_png": "https://qr.stripe.com/live_abc.png",
                    "image_url_svg": "https://qr.stripe.com/live_abc.svg",
                },
            },
        }
    }
    matches = _find_matches(na, source="confirm")
    url = _find_hosted_instructions_url(matches)
    png = _find_qr_image_url(matches)
    exp = _find_qr_expires_at(matches)
    if url == PREFIX + "ABC123xyz":
        _ok("TC-02", "link OK")
    else:
        _fail("TC-02", f"link sai: {url!r}")
        fails += 1
    if png == "https://qr.stripe.com/live_abc.png":
        _ok("TC-02", "qr png vẫn trích OK (không regression)")
    else:
        _fail("TC-02", f"qr png sai: {png!r}")
        fails += 1
    if exp == 1781622292:
        _ok("TC-02", "expires_at OK")
    else:
        _fail("TC-02", f"expires_at sai: {exp!r}")
        fails += 1

    # TC-03 — không có link → None (không false-positive).
    print("[3/3] TC-03 — no link → None", flush=True)
    matches = _find_matches({"foo": {"bar": "https://example.com/x"}}, source="x")
    url = _find_hosted_instructions_url(matches)
    if url is None:
        _ok("TC-03", "None khi không có instructions link")
    else:
        _fail("TC-03", f"false positive: {url!r}")
        fails += 1

    print(f"\n[summary] fails={fails}", flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
