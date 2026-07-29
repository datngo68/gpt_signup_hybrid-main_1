"""STEP 0 — Xác nhận live run hybrid dùng POOL hay NO-POOL.

Đọc Settings Store key ``reg.hybrid_pool_enabled`` từ DB live (runtime/data.db
hoặc env ``GSH_DB_PATH``) + gọi ``reg_hybrid.browser_pool.pool_enabled()`` —
in ra True/False cho cả hai để xác định path mà bug "multi-signup launch-hang"
đi theo.

Lưu ý: ``pool_enabled()`` còn bị override bởi env ``HYBRID_POOL_DISABLED=1``
(luôn no-pool). Test in cả env đó để chẩn đoán đầy đủ.

Chạy:  .venv/bin/python test/check_pool_path.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    db_path = os.environ.get("GSH_DB_PATH") or "runtime/data.db"
    print(f"[config] GSH_DB_PATH env = {os.environ.get('GSH_DB_PATH') or '<unset>'}")
    print(f"[config] resolved db_path = {Path(db_path).resolve()}")
    print(f"[config] HYBRID_POOL_DISABLED env = {os.environ.get('HYBRID_POOL_DISABLED') or '<unset>'}")
    print("=" * 70)

    # ── 1. Giá trị raw trong Settings Store ──
    raw_value: object = "<error>"
    try:
        from db import get_engine, get_settings_repo

        repo = get_settings_repo(get_engine())
        raw_value = repo.get("reg.hybrid_pool_enabled")
        print(f"[settings] reg.hybrid_pool_enabled (raw) = {raw_value!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"[settings] đọc DB FAIL: {type(exc).__name__}: {exc}")
        print("[settings] → pool_enabled() sẽ fallback no-pool (DB không mở được)")

    # ── 2. Quyết định runtime thực tế ──
    try:
        from reg_hybrid.browser_pool import pool_disabled, pool_enabled

        enabled = pool_enabled()
        disabled_override = pool_disabled()
        print(f"[runtime] pool_disabled() [env override] = {disabled_override}")
        print(f"[runtime] pool_enabled()  [quyết định cuối] = {enabled}")
        print("=" * 70)
        if enabled:
            print("[VERDICT] LIVE RUN dùng POOL — shared Camoufox xuyên multi-signup.")
            print("          => Nếu hang theo pool-mode: 1 op treo trên single")
            print("             _PlaywrightThread sẽ block MỌI signup kế tiếp.")
            if raw_value is True:
                print("          => DB đang để reg.hybrid_pool_enabled=True (đúng lý do).")
        else:
            print("[VERDICT] LIVE RUN dùng NO-POOL — mỗi signup launch")
            print("          CamoufoxTokenGenerator golden riêng qua")
            print("          _NoPoolThreadAffinityWrapper (mỗi wrapper 1 _PlaywrightThread).")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[runtime] import/eval pool_enabled FAIL: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
