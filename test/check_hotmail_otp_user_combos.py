"""Reproduce vấn đề user: reg hotmail không lấy được OTP với 3 combo cụ thể.

Test:
  1. Parse combo → OutlookCombo (đã pass nếu format chuẩn).
  2. Gọi DongVanFB API trực tiếp → in raw response.
  3. Gọi Microsoft Graph token endpoint → in raw response.
  4. Nếu refresh token còn sống → list 5 mail mới nhất.

Mục đích: xác định combo dead, scope sai, hay endpoint từ chối.
Không gửi OTP — chỉ verify refresh + read inbox.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx

from mail_providers import (
    _DEFAULT_SCOPE,
    _DONGVANFB_HEADERS,
    _DONGVANFB_URL,
    _GRAPH_BASE,
    _TOKEN_URL,
    OutlookCombo,
    OutlookComboError,
)


COMBOS = [
    "randolphzuckerberg304@hotmail.com|YfDpUU7n|M.C538_BAY.0.U.MsaArtifacts.-Cn*wadyDxhxmH1IOtGenanvhpE9kxMPG5HN36zHnoG8vAULVZBORiIUJI1lkscj**HUaJVZ*Wa0f6anLsKO37Z*8*kFtFdwBP0sM2prYz6VkfxEoHvQAf5FUnje941seeVxBskryvZ1q!UHy8iymTre2V0kojTuOT23DYFYiKgGxi0A*r9EoKNAJmJ68RO8tkZAtf6YPPpzKD299NDbGHps*FPwyXW4ZlCfy873Gw6ByxeY3bmcTJ1hoQM9SzDAN7c5JTlXRnYSCuRzTaMgGfvJj0HPt3tBeB5FmcLEZwXsajjk53D7GGoMM!9Pr3l8wBoNQa7p5RYy9BdbQyTeu!I98mZR2c82mTn1jRMJyoP7hBco5WYvvLb7ULo5aEOj9NuJvXv7UeAc5ylqqcTa2e6h5D!SxGVsY2cEjJHWWstUSltkL5bVNIjhukBR*JK!33U4owapvgq*qXq9JBLRBMVk$|9e5f94bc-e8a4-4e73-b8be-63364c29d753",
    "pourkatlyn21@hotmail.com|avt34i2L2|M.C553_BL2.0.U.MsaArtifacts.-CrN8*e24*Kn7kLV4JRlUsqIywl8h0!OPKcFfKEzE5rZ4VHZJE52dtROza24GzldhGJeCm0va4FV6CxMncXv9x5G!Xhx6y3DiN8s0px!HRyeHtxIm6oDp4wQuf5WOcK*18sWjekzk6ZZYI4huzQ8yHkatlc3tsCa2nTCgzo4TJ5WG2xu6sKJ1B1!QGapLoftQ4SXmRoaJcEgVY3!F09mazslmFLs50jAz5yZ9z8RNq0syWLw8ZqZRLWsF*ZS1B6bwozOrI7sRXuYe5k5y!Ij0mr6Siqs*RmqrFp68MXxTj2E8FbOWQ2B4WCgp4gyqiiXSEHJa3HzD4GNDmtoc0ORh0BX9Q07JWTkaNTNgWTdLDyzUS0Z8vh7Yg2gJcaVvhqofepadVLAs4CNCv**yRR!HjNVO!KF!C5QttgLuhfMDDs8T2PcM1FP8dBnUlkGZ4zfm*Leo5l24RVghz0tBlvhvJhM$|9e5f94bc-e8a4-4e73-b8be-63364c29d753",
    "nattesterbrook46@hotmail.com|FYHNz9jzhe|M.C557_SN1.0.U.MsaArtifacts.-Cj69ZiNRCg8mxmcIkd*W7zwFOx7jVGnmgIq6tPU!zsAWpKdQYH8!iWuBplXCOn!C5MxdC7sBExsT17qGh4C2XkAByWgmg2eOVgqpFSKE2NRpQayF2uhsHW!6CZH!Zi1Q4fL0Tzdpzt666dz9jqOu8xhYqExcViY2vhKxlL!PHzO49YiqZe3SATHu3QJAUdwv6X8lsDlSsotzh0q7p9kB6Zwn1laVk1RoSvNof6gjsKqq1q7!DXdAfKY8sSSSVCzsHCsFiUrIFkUF!c8LIfwHn!OasjpcPbTF8N3qVqGyr8VLrBJxDMJFl*NjR8ThNfVbzCYAi5CGagkGcs8PjzsR3aftnzjVblhM3oGmIHe6T!9RE5GN7nP23MwgTa9sIVbdjkxJ4Kgg9IhQV!9j70XZC8V*GQ6GbCXAcSpdXMHFSoNrax5O36Yn*z3rLX6GpxYqHLE*hJL0pUfgMKspQiWfQns$|9e5f94bc-e8a4-4e73-b8be-63364c29d753",
]


def log(msg: str) -> None:
    print(msg, flush=True)


async def test_dongvanfb(combo: OutlookCombo) -> dict | None:
    log(f"  [dongvanfb] POST {_DONGVANFB_URL}")
    payload = {
        "email": combo.email,
        "pass": combo.password,
        "refresh_token": combo.refresh_token,
        "client_id": combo.client_id,
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                _DONGVANFB_URL, headers=_DONGVANFB_HEADERS, json=payload
            )
        log(f"  [dongvanfb] HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            log(f"  [dongvanfb] body (text): {resp.text[:400]}")
            return None
        log(f"  [dongvanfb] status={data.get('status')!r} content={data.get('content')!r}")
        msgs = data.get("messages") or []
        log(f"  [dongvanfb] messages count={len(msgs)}")
        for i, m in enumerate(msgs[:5]):
            log(
                f"    #{i}: from={m.get('from')!r} subj={m.get('subject')!r} "
                f"date={m.get('date')!r} code={m.get('code')!r}"
            )
        return data
    except Exception as exc:  # noqa: BLE001
        log(f"  [dongvanfb] EXCEPTION: {type(exc).__name__}: {exc!r}")
        return None


async def test_graph_refresh(combo: OutlookCombo) -> str | None:
    """Refresh access token; return access_token nếu OK."""
    log(f"  [graph-refresh] POST {_TOKEN_URL}")
    log(f"  [graph-refresh] scope={_DEFAULT_SCOPE!r}")
    log(f"  [graph-refresh] client_id={combo.client_id}")
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                _TOKEN_URL,
                data={
                    "client_id": combo.client_id,
                    "scope": _DEFAULT_SCOPE,
                    "refresh_token": combo.refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        log(f"  [graph-refresh] HTTP {resp.status_code}")
        body = resp.text
        if resp.status_code != 200:
            log(f"  [graph-refresh] body: {body[:600]}")
            return None
        data = resp.json()
        access = data.get("access_token")
        log(
            f"  [graph-refresh] OK access_token len={len(access) if access else 0} "
            f"expires_in={data.get('expires_in')} scope={data.get('scope')!r}"
        )
        return access
    except Exception as exc:  # noqa: BLE001
        log(f"  [graph-refresh] EXCEPTION: {type(exc).__name__}: {exc!r}")
        return None


async def test_graph_list(access_token: str) -> None:
    log(f"  [graph-list] GET {_GRAPH_BASE}/me/messages?$top=5")
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"{_GRAPH_BASE}/me/messages",
                params={
                    "$top": 5,
                    "$orderby": "receivedDateTime desc",
                    "$select": "subject,from,receivedDateTime,bodyPreview",
                },
                headers={"Authorization": f"Bearer {access_token}"},
            )
        log(f"  [graph-list] HTTP {resp.status_code}")
        if resp.status_code != 200:
            log(f"  [graph-list] body: {resp.text[:400]}")
            return
        msgs = resp.json().get("value", [])
        log(f"  [graph-list] messages count={len(msgs)}")
        for i, m in enumerate(msgs):
            sender = (m.get("from") or {}).get("emailAddress", {}).get("address", "")
            log(
                f"    #{i}: from={sender!r} subj={m.get('subject')!r} "
                f"date={m.get('receivedDateTime')!r}"
            )
    except Exception as exc:  # noqa: BLE001
        log(f"  [graph-list] EXCEPTION: {type(exc).__name__}: {exc!r}")


async def main() -> int:
    for idx, raw in enumerate(COMBOS, start=1):
        log(f"\n=== TC-{idx:02d} {raw[:50]}... ===")
        try:
            combo = OutlookCombo.parse(raw)
            log(f"[PASS] parse — email={combo.email} client_id={combo.client_id}")
            log(
                f"  refresh_token prefix={combo.refresh_token[:30]!r} "
                f"len={len(combo.refresh_token)}"
            )
        except OutlookComboError as exc:
            log(f"[FAIL] parse :: {exc}")
            continue

        log("--- DongVanFB ---")
        await test_dongvanfb(combo)

        log("--- Microsoft Graph ---")
        access = await test_graph_refresh(combo)
        if access:
            await test_graph_list(access)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
