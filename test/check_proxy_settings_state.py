"""Dump trạng thái proxy/reg trong Settings Store để chẩn đoán root cause
batch reg fail (OTP timeout liên tiếp, max_check_attempts) — kiểm tra xem
autoreg có thực sự chạy qua proxy hay chạy direct.

Chỉ đọc DB, không ghi. Run: .venv/bin/python test/check_proxy_settings_state.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GSH_DB_PATH", str(ROOT / "runtime" / "data.db"))


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    from db import get_engine
    from db.repositories import SettingsRepository
    from web.proxy_format import mask_proxy

    engine = get_engine()
    settings = SettingsRepository(engine)

    # ── 1. Reg toggle proxy ────────────────────────────────────────────
    use_proxy = settings.get("reg.use_proxy")
    log(f"reg.use_proxy           = {use_proxy!r}")

    # ── 2. Proxy pool config ───────────────────────────────────────────
    raw_pool = settings.get("proxy.pool") or []
    mode = settings.get("proxy.rotation_mode") or "round_robin"
    log(f"proxy.rotation_mode     = {mode!r}")
    log(f"proxy.pool count        = {len(raw_pool)}")

    if raw_pool:
        log("proxy.pool entries (masked):")
        for i, line in enumerate(raw_pool):
            log(f"  [{i}] {mask_proxy(line)}")
    else:
        log("  (rỗng — autoreg chạy DIRECT từ IP máy host)")

    # ── 3. Probe knobs (nếu mode=probe) ────────────────────────────────
    if mode == "probe":
        for k in ("proxy.probe_endpoint", "proxy.probe_timeout", "proxy.max_tries",
                  "proxy.sid_len", "proxy.sid_retry_per_line", "proxy.probe_concurrency"):
            log(f"  {k:<35} = {settings.get(k)!r}")

    # ── 4. Reg concurrency + retry ─────────────────────────────────────
    log("")
    log("=== Reg runtime ===")
    for k in ("reg.headless", "reg.job_timeout", "reg.max_concurrent",
              "reg.auto_retry", "reg.auto_retry_max", "reg.auto_retry_delay"):
        log(f"  {k:<30} = {settings.get(k)!r}")

    # ── 5. Verdict ─────────────────────────────────────────────────────
    log("")
    log("=== Chẩn đoán ===")
    if not raw_pool:
        log("[ROOT CAUSE] proxy.pool rỗng → autoreg chạy DIRECT (cùng IP host).")
        log("  → N email signup liên tiếp = N hit từ 1 IP → OAI rate-limit + flag.")
        log("  → Triệu chứng: OTP timeout 300s, max_check_attempts, SO token empty.")
        log("  FIX: vào CMS Settings → Proxy Pool, paste danh sách proxy + Save.")
        return 1
    if use_proxy is False:
        log("[WARN] reg.use_proxy=False → pool có data nhưng autoreg vẫn chạy direct.")
        log("  FIX: vào CMS Settings → Reg → bật Use Proxy.")
        return 2
    log("[OK] pool có entries + use_proxy=True → autoreg sẽ qua proxy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
