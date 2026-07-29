"""Verify fix: DongVanFB provider giờ filter mail theo date window thay vì
baseline-by-value. Mail OTP về kịp vòng poll đầu vẫn được accept.

Test cases:
  - TC-01: started_at = NOW (sau khi mail OTP về). Mail có date = NOW - 30s
           (trong grace 90s) → phải accept ngay vòng đầu, không capture baseline.
  - TC-02: started_at = NOW. Mail có date = NOW - 10 phút (ngoài grace) →
           phải skip, không return.
  - TC-03: Microsoft Graph filter — mail có receivedDateTime = started_at - 20s
           (trong grace 30s) → phải accept.
  - TC-04: Microsoft Graph — mail có receivedDateTime = started_at - 5 phút
           (ngoài grace) → phải skip.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mail_providers import (
    _DONGVANFB_DATE_GRACE,
    _OUTLOOK_GRAPH_DATE_GRACE,
    DongVanFBOutlookProvider,
    OutlookCombo,
    OutlookMailProvider,
)


def log(s: str) -> None:
    print(s, flush=True)


def _make_combo() -> OutlookCombo:
    return OutlookCombo(
        email="test@hotmail.com",
        password="dummy",
        refresh_token="M.C123_BAY.0.U.MsaArtifacts.dummy",
        client_id="9e5f94bc-e8a4-4e73-b8be-63364c29d753",
    )


def _vn_date_str(dt: datetime) -> str:
    """Convert UTC dt → 'HH:MM - DD/MM/YYYY' format (VN time)."""
    vn = dt.astimezone(timezone(timedelta(hours=7)))
    return vn.strftime("%H:%M - %d/%m/%Y")


def _make_dongvanfb_response(
    *, code: str, sent_at_utc: datetime
) -> dict:
    """Stub DongVanFB API response."""
    body = (
        f"<html><body>Your verification code is "
        f"<h1>{code}</h1> use this to verify.</body></html>"
    )
    return {
        "email": "test@hotmail.com",
        "status": True,
        "messages": [
            {
                "from": "noreply@tm.openai.com",
                "subject": "Your temporary ChatGPT verification code",
                "code": "",
                "message": body,
                "date": _vn_date_str(sent_at_utc),
            }
        ],
        "content": "Mail loaded successfully.",
    }


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> dict:
        return self._payload

    @property
    def text(self) -> str:
        import json as _j

        return _j.dumps(self._payload)


async def _run_dongvanfb(
    *, sent_at_utc: datetime, started_at: datetime, code: str, timeout: float = 5.0
) -> str | None:
    provider = DongVanFBOutlookProvider(combo=_make_combo())
    payload = _make_dongvanfb_response(code=code, sent_at_utc=sent_at_utc)

    async def _fake_post(*args, **kwargs):
        return _FakeResponse(payload)

    with patch("httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.post = _fake_post
        mock_cls.return_value.__aenter__.return_value = client
        try:
            return await asyncio.wait_for(
                provider.poll_otp(
                    recipient="test@hotmail.com",
                    started_at=started_at,
                    timeout_seconds=timeout,
                    poll_interval_seconds=0.5,
                    log=log,
                ),
                timeout=timeout + 1.0,
            )
        except (asyncio.TimeoutError, TimeoutError):
            return None


async def _run_graph_filter(
    *, sent_at_utc: datetime, started_at: datetime, code: str, timeout: float = 5.0
) -> str | None:
    """Test Microsoft Graph filter ngay sau refresh."""
    provider = OutlookMailProvider(
        combo=_make_combo(),
        state_dir=Path("/tmp/check_otp_state_unused"),
    )
    # Stub access token — bỏ qua refresh.
    provider._access_token = "fake-access-token"
    provider._access_expires_at = 9_999_999_999

    fake_messages = {
        "value": [
            {
                "subject": "Your temporary ChatGPT verification code",
                "from": {"emailAddress": {"address": "noreply@tm.openai.com"}},
                "receivedDateTime": sent_at_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "bodyPreview": f"Verification code: {code}",
                "body": {"content": f"<p>verification code: {code}</p>"},
            }
        ]
    }

    class _GraphFakeResponse:
        status_code = 200

        def json(self):
            return fake_messages

        def raise_for_status(self):
            pass

    async def _fake_get(url, *args, **kwargs):
        return _GraphFakeResponse()

    with patch("httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = _fake_get
        client.post = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = client
        try:
            return await asyncio.wait_for(
                provider.poll_otp(
                    recipient="test@hotmail.com",
                    started_at=started_at,
                    timeout_seconds=timeout,
                    poll_interval_seconds=0.5,
                    log=log,
                ),
                timeout=timeout + 1.0,
            )
        except (asyncio.TimeoutError, TimeoutError):
            return None


async def main() -> int:
    failures: list[str] = []

    log(f"\n=== TC-01 DongVanFB: mail mới về kịp vòng đầu (sent=NOW-30s) ===")
    log(f"  expect: return code (KHÔNG capture baseline)")
    log(f"  grace = {_DONGVANFB_DATE_GRACE.total_seconds():.0f}s")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    code = await _run_dongvanfb(
        sent_at_utc=now - timedelta(seconds=30),
        started_at=now,
        code="123456",
        timeout=3.0,
    )
    if code == "123456":
        log(f"[PASS] TC-01 — got {code} ngay vòng đầu")
    else:
        log(f"[FAIL] TC-01 — expected '123456', got {code!r}")
        failures.append("TC-01")

    log(f"\n=== TC-02 DongVanFB: mail cũ ngoài grace (sent=NOW-10min) ===")
    log(f"  expect: skip → timeout (None)")
    code = await _run_dongvanfb(
        sent_at_utc=now - timedelta(minutes=10),
        started_at=now,
        code="654321",
        timeout=2.0,
    )
    if code is None:
        log(f"[PASS] TC-02 — mail cũ bị skip đúng (timeout)")
    else:
        log(f"[FAIL] TC-02 — expected None, got {code!r}")
        failures.append("TC-02")

    log(f"\n=== TC-03 Microsoft Graph: mail trong grace (sent=NOW-20s) ===")
    log(f"  grace = {_OUTLOOK_GRAPH_DATE_GRACE.total_seconds():.0f}s")
    code = await _run_graph_filter(
        sent_at_utc=now - timedelta(seconds=20),
        started_at=now,
        code="789012",
        timeout=3.0,
    )
    if code == "789012":
        log(f"[PASS] TC-03 — got {code}")
    else:
        log(f"[FAIL] TC-03 — expected '789012', got {code!r}")
        failures.append("TC-03")

    log(f"\n=== TC-04 Microsoft Graph: mail cũ ngoài grace (sent=NOW-5min) ===")
    code = await _run_graph_filter(
        sent_at_utc=now - timedelta(minutes=5),
        started_at=now,
        code="345678",
        timeout=2.0,
    )
    if code is None:
        log(f"[PASS] TC-04 — mail cũ bị skip đúng")
    else:
        log(f"[FAIL] TC-04 — expected None, got {code!r}")
        failures.append("TC-04")

    log(f"\n=== summary: {4 - len(failures)}/4 pass ===")
    if failures:
        log(f"FAILED: {', '.join(failures)}")
        return 1
    log("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
