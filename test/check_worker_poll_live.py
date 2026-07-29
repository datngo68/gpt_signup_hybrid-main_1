"""Verify WorkerMailProvider (code HIỆN TẠI) có vớt được OTP của email này không.

Chạy 2 case:
  A) started_at = quá khứ xa  -> phải tìm thấy mã (chứng minh provider + parse OK).
  B) started_at = bây giờ      -> mô phỏng reg mới: mã cũ trong worker bị loại
                                  (đúng logic), trừ khi có mã mới gửi gần đây.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GSH_DB_PATH", str(ROOT / "runtime" / "data.db"))

from mail_providers import WorkerMailProvider  # noqa: E402

EMAIL = "pitches_pump_0a+ypsieo@icloud.com"
LOGS_URL = "https://icloud-email.linhtd.com/logs"
API_KEY = "1481d2b23a24f5c7e9cd4a2cdbdb6af7ed03309f97b195b5"


def log(msg: str) -> None:
    print(msg, flush=True)


async def run_case(label: str, started_at: datetime) -> None:
    log(f"\n=== {label} | started_at={started_at.isoformat()} ===")
    provider = WorkerMailProvider(logs_url=LOGS_URL, api_key=API_KEY)
    try:
        code = await provider.poll_otp(
            recipient=EMAIL,
            started_at=started_at,
            timeout_seconds=12.0,   # ngắn: chỉ kiểm tra mã CÓ SẴN, không chờ lâu
            poll_interval_seconds=3.0,
            log=log,
        )
        log(f"[RESULT] OTP = {code}")
    except Exception as exc:  # noqa: BLE001
        log(f"[RESULT] {type(exc).__name__}: {exc}")


async def main() -> None:
    now = datetime.now(timezone.utc)
    log(f"now(utc)={now.isoformat()}")
    # A: chấp nhận mã có date sau mốc quá khứ xa (10 phút trước) -> nên tìm thấy mã 11:50:09 nếu còn
    await run_case("A: started_at = now-15m (chấp nhận mã cũ)", now - timedelta(minutes=15))
    # B: mô phỏng reg mới bây giờ -> mã cũ bị loại (đúng), timeout 12s
    await run_case("B: started_at = now (reg mới)", now)


if __name__ == "__main__":
    asyncio.run(main())
