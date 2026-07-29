"""Reproduce nghi vấn: DongVanFB provider baseline-by-value capture code mới
làm baseline khi mail OTP về kịp vòng poll đầu → skip code mới ở vòng kế.

Cũng kiểm tra `_extract_otp` có extract đúng từ mail body OpenAI thật không.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx

from mail_providers import (
    _DONGVANFB_HEADERS,
    _DONGVANFB_URL,
    _extract_otp,
    _is_openai_sender,
    _parse_dongvanfb_date,
    OutlookCombo,
)


COMBO = "randolphzuckerberg304@hotmail.com|YfDpUU7n|M.C538_BAY.0.U.MsaArtifacts.-Cn*wadyDxhxmH1IOtGenanvhpE9kxMPG5HN36zHnoG8vAULVZBORiIUJI1lkscj**HUaJVZ*Wa0f6anLsKO37Z*8*kFtFdwBP0sM2prYz6VkfxEoHvQAf5FUnje941seeVxBskryvZ1q!UHy8iymTre2V0kojTuOT23DYFYiKgGxi0A*r9EoKNAJmJ68RO8tkZAtf6YPPpzKD299NDbGHps*FPwyXW4ZlCfy873Gw6ByxeY3bmcTJ1hoQM9SzDAN7c5JTlXRnYSCuRzTaMgGfvJj0HPt3tBeB5FmcLEZwXsajjk53D7GGoMM!9Pr3l8wBoNQa7p5RYy9BdbQyTeu!I98mZR2c82mTn1jRMJyoP7hBco5WYvvLb7ULo5aEOj9NuJvXv7UeAc5ylqqcTa2e6h5D!SxGVsY2cEjJHWWstUSltkL5bVNIjhukBR*JK!33U4owapvgq*qXq9JBLRBMVk$|9e5f94bc-e8a4-4e73-b8be-63364c29d753"


def log(s: str) -> None:
    print(s, flush=True)


async def main() -> int:
    combo = OutlookCombo.parse(COMBO)
    payload = {
        "email": combo.email,
        "pass": combo.password,
        "refresh_token": combo.refresh_token,
        "client_id": combo.client_id,
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(_DONGVANFB_URL, headers=_DONGVANFB_HEADERS, json=payload)

    data = resp.json()
    msgs = data.get("messages") or []
    log(f"[1/3] fetched {len(msgs)} messages")

    log("\n[2/3] inspect mail body / extract code")
    extracted_codes: list[tuple[str, str]] = []  # (date, code)
    for i, m in enumerate(msgs):
        sender = str(m.get("from") or "")
        subject = str(m.get("subject") or "")
        api_code = str(m.get("code") or "").strip()
        body_msg = str(m.get("message") or "")
        date_str = str(m.get("date") or "")
        msg_dt = _parse_dongvanfb_date(date_str)
        is_oai = _is_openai_sender(sender) or "openai" in subject.lower()

        log(
            f"  msg#{i}: from={sender!r}\n"
            f"    subj={subject!r}\n"
            f"    date={date_str!r} → utc={msg_dt}\n"
            f"    api.code={api_code!r}  is_openai={is_oai}\n"
            f"    body.len={len(body_msg)}  body.preview={body_msg[:200]!r}"
        )
        if is_oai:
            code = api_code if (api_code and len(api_code) == 6 and api_code.isdigit()) else (
                _extract_otp(subject, body_msg) or ""
            )
            log(f"    → extracted code = {code!r}")
            if code:
                extracted_codes.append((date_str, code))

    log(f"\n[3/3] simulate baseline-by-value bug")
    if not extracted_codes:
        log("  inbox không có code OpenAI nào trong body — bỏ qua simulate")
        return 0
    log(f"  vòng poll #1: capture baseline = {[c for _, c in extracted_codes]}")
    log(f"  → seen_codes chứa luôn code MỚI nếu mail OTP về kịp vòng đầu")
    log(f"  → vòng poll #2+: code này coi là 'cũ' → KHÔNG return → loop đến timeout")
    log(f"  KẾT LUẬN: bug confirmed nếu trong runtime mail OTP về trong <pollInterval (4s)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
