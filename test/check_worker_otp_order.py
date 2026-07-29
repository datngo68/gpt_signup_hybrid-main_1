"""Verify Worker OTP: sort mới→cũ + extract mã kể cả date 'cũ' (không lọc thời gian).

Chạy: .venv/bin/python test/check_worker_otp_order.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mail_providers import _sort_messages_newest_first, _extract_otp  # noqa: E402


def _p(ok: bool, tc: str, desc: str, detail: str) -> bool:
    print(f"{'[PASS]' if ok else '[FAIL]'} {tc} — {desc} :: {detail}", flush=True)
    return ok


def main() -> int:
    now = datetime.now(timezone.utc)
    results: list[bool] = []

    # TC-01: sort mới→cũ theo date (input lộn xộn)
    msgs = [
        {"to": "x", "date": (now - timedelta(seconds=200)).isoformat(), "subject": "code 111111"},
        {"to": "x", "date": now.isoformat(), "subject": "code 222222"},
        {"to": "x", "date": (now - timedelta(seconds=90)).isoformat(), "subject": "code 333333"},
    ]
    _sort_messages_newest_first(msgs)
    order = [_extract_otp(m["subject"], "") for m in msgs]
    results.append(_p(order == ["222222", "333333", "111111"], "TC-01",
                      "sort mới→cũ", f"order={order}"))

    # TC-02: mã có date 'cũ' (200s trước) VẪN extract được (không bị lọc thời gian)
    old_msg = {"to": "x", "date": (now - timedelta(seconds=600)).isoformat(),
               "subject": "Your verification code is 654321"}
    code = _extract_otp(old_msg["subject"], "")
    results.append(_p(code == "654321", "TC-02",
                      "mã date cũ vẫn lấy được", f"code={code}"))

    # TC-03: messages không có date → giữ nguyên thứ tự API (không crash)
    no_date = [
        {"to": "x", "subject": "code 444444"},
        {"to": "x", "subject": "code 555555"},
    ]
    _sort_messages_newest_first(no_date)
    order2 = [_extract_otp(m["subject"], "") for m in no_date]
    results.append(_p(order2 == ["444444", "555555"], "TC-03",
                      "thiếu date giữ nguyên order", f"order={order2}"))

    # TC-04: epoch ms timestamp cũng sort đúng
    msgs2 = [
        {"to": "x", "date": int((now - timedelta(seconds=50)).timestamp() * 1000), "subject": "code 777777"},
        {"to": "x", "date": int(now.timestamp() * 1000), "subject": "code 888888"},
    ]
    _sort_messages_newest_first(msgs2)
    order3 = [_extract_otp(m["subject"], "") for m in msgs2]
    results.append(_p(order3 == ["888888", "777777"], "TC-04",
                      "epoch ms sort đúng", f"order={order3}"))

    print("", flush=True)
    n = sum(results)
    print(f"[SUMMARY] {n}/{len(results)} PASS", flush=True)
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
