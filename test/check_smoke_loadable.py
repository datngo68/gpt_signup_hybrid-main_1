"""Verify test/smoke_reg_icloud_v3.py load được + có LINES + parse hợp lệ.

Không chạy reg thật (cần proxy + browser + sentinel sidecar). Chỉ verify:
  - File import được (không import error)
  - LINES có >= 1 dòng
  - Mỗi dòng parse được qua MailModeSpec.parse_line('icloud_v3')
  - build_request trên dòng đầu trả SignupRequest hợp lệ

Run: python3 test/check_smoke_loadable.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    smoke_path = ROOT / "test" / "smoke_reg_icloud_v3.py"
    if not smoke_path.exists():
        print(f"FAIL: missing {smoke_path}")
        return 1

    try:
        mod = _load_module(smoke_path, "smoke_reg_icloud_v3")
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: import smoke module raised {type(exc).__name__}: {exc}")
        return 1

    lines = getattr(mod, "LINES", None)
    if not isinstance(lines, list) or not lines:
        print(f"FAIL: LINES không phải list non-empty (got {type(lines).__name__})")
        return 1
    print(f"[PASS] smoke module imports OK, LINES={len(lines)}")

    from web.mail_modes import get_spec

    spec = get_spec("icloud_v3")
    fail = 0
    for i, line in enumerate(lines):
        try:
            parsed = spec.parse_line(line)
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] line {i}: parse raised {type(exc).__name__}: {exc}")
            fail += 1
            continue
        if "@" not in parsed.email or " " in parsed.email:
            print(f"[FAIL] line {i}: bad email {parsed.email!r}")
            fail += 1
            continue
        print(f"[PASS] line {i}: email={parsed.email}")

    # build_request trên dòng đầu
    parsed = spec.parse_line(lines[0])
    try:
        req = spec.build_request(parsed, password="Test#Reg2026", headless=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] build_request raised {type(exc).__name__}: {exc}")
        return 1
    if req.mail_provider != "icloud_v3" or not req.icloud_v3_url:
        print(
            f"[FAIL] build_request: provider={req.mail_provider!r} "
            f"url_set={bool(req.icloud_v3_url)}"
        )
        return 1
    print(
        f"[PASS] build_request OK provider={req.mail_provider} "
        f"reg_mode={req.reg_mode} otp_timeout={req.otp_timeout_seconds}s"
    )

    if fail:
        print(f"=== {fail} line(s) FAILED parse ===")
        return 1
    print("=== ALL OK ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
