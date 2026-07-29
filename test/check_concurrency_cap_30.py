"""Verify Reg + Session cap đã được nâng từ 5/10 lên 30 ở mọi lớp.

Run: python3 test/check_concurrency_cap_30.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MANAGER = (ROOT / "web/manager.py").read_text(encoding="utf-8")
SERVER = (ROOT / "web/server.py").read_text(encoding="utf-8")
APP_JS = (ROOT / "web/static/app.js").read_text(encoding="utf-8")


def tc(idx: int, total: int, name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    suffix = f" :: {detail}" if detail else ""
    print(f"[{tag}] [{idx}/{total}] {name}{suffix}", flush=True)
    return ok


def main() -> int:
    cases: list[tuple[str, bool, str]] = []

    # 1. _MAX_CONCURRENT env clamp 30 (không còn 10)
    has_env_30 = bool(re.search(
        r'_MAX_CONCURRENT\s*=\s*min\(max\(int\(_env\("HYBRID_MAX_CONCURRENT",\s*"2"\)\),\s*1\),\s*30\)',
        MANAGER,
    ))
    cases.append(("TC-01 _MAX_CONCURRENT env cap 30", has_env_30, ""))

    # 2. set_max_concurrent: KHÔNG còn cap 5 (giá trị cũ Reg). Allowed values:
    # Reg=30, Session=30, Link=10 (đang ẩn tab), UPI=200.
    set_max_blocks = re.findall(
        r'def\s+set_max_concurrent\(self,\s*n:\s*int\)\s*->\s*None:\s*\n'
        r'\s*if\s+n\s*<\s*1\s*or\s*n\s*>\s*(\d+):',
        MANAGER,
    )
    has_old_5 = "5" in set_max_blocks
    cases.append(("TC-02 Reg.set_max_concurrent KHÔNG còn cap 5",
                  not has_old_5,
                  f"caps={set_max_blocks}"))

    # 3. Manager.apply_settings clamp 30
    has_apply_clamp_30 = bool(re.search(
        r'self\._max\s*=\s*max\(1,\s*min\(val,\s*30\)\)', MANAGER,
    ))
    cases.append(("TC-03 Reg.apply_settings clamp val→30",
                  has_apply_clamp_30, ""))

    # 4. Session apply_settings cap 30 (1 <= val <= 30) — đã đổi từ 10
    has_session_30_apply = bool(re.search(
        r'val\s*=\s*int\(settings\["reg\.max_concurrent"\]\)\s*\n\s*if\s+1\s*<=\s*val\s*<=\s*30',
        MANAGER,
    ))
    cases.append(("TC-04 Session.apply_settings range 1..30",
                  has_session_30_apply, ""))

    # 5. server.py set_config clamp 30 (không còn 5)
    has_old_clamp_5 = bool(re.search(
        r'max\(1,\s*min\(payload\.max_concurrent,\s*5\)\)', SERVER,
    ))
    has_new_clamp_30 = bool(re.search(
        r'max\(1,\s*min\(payload\.max_concurrent,\s*30\)\)', SERVER,
    ))
    cases.append(("TC-05 server.py set_config clamp 30 (no clamp 5)",
                  not has_old_clamp_5 and has_new_clamp_30,
                  f"old5={has_old_clamp_5} new30={has_new_clamp_30}"))

    # 6. server.py set_session_config clamp 30 (không còn 10)
    # Check có dòng: clamped = max(1, min(payload.max_concurrent, 30))
    # Trong context set_session_config (sm.set_max_concurrent ngay sau)
    session_config_block = re.search(
        r'set_session_config[\s\S]*?sm\.set_max_concurrent',
        SERVER,
    )
    block = session_config_block.group(0) if session_config_block else ""
    has_session_30 = "min(payload.max_concurrent, 30)" in block
    has_session_old_10 = "min(payload.max_concurrent, 10)" in block
    cases.append(("TC-06 set_session_config clamp 30",
                  has_session_30 and not has_session_old_10,
                  f"new30={has_session_30} old10={has_session_old_10}"))

    # 7. FE _syncRegConcurrencyToServer + flag _regModeSyncedOnLoad
    has_sync_fn = bool(re.search(
        r'async\s+function\s+_syncRegConcurrencyToServer\(mode\)', APP_JS,
    ))
    has_flag = bool(re.search(
        r'let\s+_regModeSyncedOnLoad\s*=\s*false', APP_JS,
    ))
    has_flag_guard = bool(re.search(
        r'if\s*\(\s*!_regModeSyncedOnLoad\s*\)\s*\{\s*_regModeSyncedOnLoad\s*=\s*true',
        APP_JS,
    ))
    cases.append(("TC-07 FE force-sync 1-shot khi load Reg tab",
                  has_sync_fn and has_flag and has_flag_guard,
                  f"fn={has_sync_fn} flag={has_flag} guard={has_flag_guard}"))

    # 8. UPI cap vẫn 200 (không bị động nhầm)
    has_upi_200 = "max_concurrent phải trong [1, 200]" in MANAGER
    cases.append(("TC-08 UPI cap giữ nguyên 200", has_upi_200, ""))

    total = len(cases)
    passed = sum(1 for i, (name, ok, detail) in enumerate(cases, 1)
                 if tc(i, total, name, ok, detail))
    print(f"=== Summary: {passed}/{total} PASS ===", flush=True)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
