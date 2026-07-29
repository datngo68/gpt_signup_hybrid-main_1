"""Verify fix bug write-through `reg.max_concurrent` không clamp.

Bug cũ: payload.max_concurrent lớn → manager clamp nhưng settings_dict ghi raw
→ SettingsRepository validate fail. Fix: clamp 1 nguồn duy nhất, dùng cho cả
manager + write-through. Reg cap hiện tại = [1, 5].

TC-01  AST parse web/server.py OK.
TC-02  set_config dùng max_concurrent_clamped cho settings_dict.
TC-03  SettingsRepository.set("reg.max_concurrent", 5) accept (clamped).
TC-04  SettingsRepository.set("reg.max_concurrent", 50) reject (giữ guard rail).
TC-05  Logic clamp: 50→5, 0→1, 5→5, None→None.

Chạy: python3 test/check_reg_config_clamp.py
"""
from __future__ import annotations

import ast
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

_FAILS = 0


def _log(ok: bool, tc: str, desc: str, detail: str = "") -> None:
    global _FAILS
    tag = "[PASS]" if ok else "[FAIL]"
    if not ok:
        _FAILS += 1
    print(f"{tag} {tc} — {desc} :: {detail}", flush=True)


_SERVER = _ROOT / "web" / "server.py"


def tc01_ast() -> None:
    src = _SERVER.read_text(encoding="utf-8")
    try:
        ast.parse(src)
        _log(True, "TC-01", "AST parse server.py", "syntax OK")
    except SyntaxError as exc:
        _log(False, "TC-01", "AST parse server.py", f"{exc}")


def tc02_settings_dict_uses_clamped() -> None:
    """Đọc raw text — đảm bảo settings_dict["reg.max_concurrent"] dùng
    biến clamped, KHÔNG dùng payload.max_concurrent raw nữa."""
    src = _SERVER.read_text(encoding="utf-8")
    bad = 'settings_dict["reg.max_concurrent"] = payload.max_concurrent'
    good = 'settings_dict["reg.max_concurrent"] = max_concurrent_clamped'
    has_good = good in src
    has_bad = bad in src
    _log(has_good and not has_bad, "TC-02",
         "settings_dict dùng clamped", f"good={has_good} bad={has_bad}")


def tc03_tc04_repo_validate() -> None:
    from db.engine import DatabaseEngine
    from db.repositories import SettingsRepository, RepositoryError

    with tempfile.TemporaryDirectory() as td:
        dbp = Path(td) / "t.db"
        engine = DatabaseEngine(dbp)
        # DatabaseEngine.__init__ auto-init schema (ALL_DDL on fresh DB).
        repo = SettingsRepository(engine)

        # TC-03 — accept clamped (5).
        try:
            repo.set("reg.max_concurrent", 5)
            got = repo.get("reg.max_concurrent")
            _log(got == 5, "TC-03", "set 5 accept", f"got={got}")
        except RepositoryError as exc:
            _log(False, "TC-03", "set 5 accept", f"raised: {exc}")

        # TC-04 — reject 50 (guard rail).
        try:
            repo.set("reg.max_concurrent", 50)
            _log(False, "TC-04", "set 50 phải reject", "không raise")
        except RepositoryError as exc:
            ok = "must be in [1, 5]" in str(exc)
            _log(ok, "TC-04", "set 50 reject đúng",
                 f"err={str(exc)[:80]}")

        engine.close() if hasattr(engine, "close") else None


def tc05_clamp_logic() -> None:
    """Reproduce logic clamp inline (Reg cap = 5)."""
    def clamp(v):
        return max(1, min(v, 5)) if v is not None else None

    cases = {50: 5, 0: 1, 5: 5, 1: 1, 3: 3, 6: 5, None: None}
    fails = []
    for inp, expect in cases.items():
        got = clamp(inp)
        if got != expect:
            fails.append(f"{inp}→{got} (expect {expect})")
    _log(not fails, "TC-05", "clamp logic",
         "all OK" if not fails else "; ".join(fails))


def main() -> int:
    print("=== reg.max_concurrent clamp fix verification ===", flush=True)
    tc01_ast()
    tc02_settings_dict_uses_clamped()
    tc03_tc04_repo_validate()
    tc05_clamp_logic()
    print(f"=== done — {_FAILS} fail(s) ===", flush=True)
    return 1 if _FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
