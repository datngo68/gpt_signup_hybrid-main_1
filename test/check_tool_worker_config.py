"""Debug: tool reg đang dùng worker_config (logs_url/api_key) nào trong Settings Store?

So sánh với cái user nhập trên web (icloud-email.linhtd.com + token 1481...).
Rồi poll thử đúng email bằng CHÍNH config tool đang dùng để xem có ra OTP không.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GSH_DB_PATH", str(ROOT / "runtime" / "data.db"))

import httpx  # noqa: E402

EMAIL = "pitches_pump_0a+ypsieo@icloud.com"
WEB_LOGS_URL = "https://icloud-email.linhtd.com/logs"
WEB_TOKEN = "1481d2b23a24f5c7e9cd4a2cdbdb6af7ed03309f97b195b5"

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def dump_settings() -> dict:
    from db import get_engine, get_settings_repo
    repo = get_settings_repo(get_engine())
    keys = [
        "mail_mode.current",
        "mail_mode.worker_config",
        "autoreg.logs_url",
        "autoreg.api_key",
        "reg_mode.current",
    ]
    out = {}
    for k in keys:
        try:
            out[k] = repo.get(k)
        except Exception as exc:  # noqa: BLE001
            out[k] = f"<err: {exc}>"
    return out


def resolve_worker_config_like_autoreg(all_settings_get) -> dict:
    """Mirror AutoRegRunner._resolve_worker_config + icloud_routes start."""
    # autoreg.start: logs_url/api_key từ body (None) → Settings mail_mode.worker_config
    wc = all_settings_get("mail_mode.worker_config")
    logs_url = ""
    api_key = ""
    if isinstance(wc, dict):
        logs_url = wc.get("logs_url", "")
        api_key = wc.get("api_key", "")
    # runner fallback
    resolved_logs = (
        logs_url
        or os.environ.get("HYBRID_WORKER_LOGS_URL", "")
        or "https://icloud-cf-mail.n5pskgzs9g.workers.dev/logs"
    )
    resolved_key = api_key or os.environ.get("HYBRID_WORKER_API_KEY", "")
    return {"logs_url": resolved_logs, "api_key": resolved_key}


async def poll(label: str, logs_url: str, api_key: str) -> None:
    mailbox = EMAIL.strip().lower()
    headers = {"Accept": "application/json", "User-Agent": _BROWSER_UA}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = f"{logs_url}?mail={quote(mailbox)}"
    print(f"\n=== {label} ===", flush=True)
    print(f"[GET] {url}", flush=True)
    print(f"[api_key] {api_key!r}", flush=True)
    try:
        async with httpx.AsyncClient(verify=True, timeout=20.0, follow_redirects=True) as c:
            r = await c.get(url, headers=headers)
        print(f"[HTTP] {r.status_code}", flush=True)
        body = r.text
        print(f"[BODY <=600] {body[:600]}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {type(exc).__name__}: {exc!r}", flush=True)


async def main() -> None:
    print(f"now(utc)={datetime.now(timezone.utc).isoformat()}", flush=True)
    print(f"DB={os.environ['GSH_DB_PATH']}", flush=True)

    settings = dump_settings()
    print("\n--- Settings Store (giá trị tool đang dùng) ---", flush=True)
    for k, v in settings.items():
        print(f"  {k} = {v!r}", flush=True)

    tool_cfg = resolve_worker_config_like_autoreg(lambda k: settings.get(k))
    print(f"\n--- Config tool RESOLVE ra ---\n  {tool_cfg}", flush=True)

    # 1. Poll bằng config tool đang dùng
    await poll("TOOL config", tool_cfg["logs_url"], tool_cfg["api_key"])
    # 2. Poll bằng config user nhập trên web (đối chứng)
    await poll("WEB config (đối chứng)", WEB_LOGS_URL, WEB_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
