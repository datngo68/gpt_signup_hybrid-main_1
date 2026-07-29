"""Task 1.5 verify — fresh profile mặc định.

Mục tiêu:
    - SignupRequest.profile_template default = False.
    - CLI signup `--fresh-profile` là default behavior.
    - config.Settings.browser_use_profile_template default = False.
    - Env BROWSER_USE_PROFILE_TEMPLATE missing → False.

Chạy: .venv/bin/python3 test/check_fresh_profile_default.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    failures: list[str] = []

    # 1. SignupRequest model default
    from models import SignupRequest

    req = SignupRequest(email="x@y.z")
    if req.profile_template is False:
        print("[PASS] SignupRequest.profile_template default = False")
    else:
        failures.append(
            f"SignupRequest.profile_template default = {req.profile_template} (expect False)"
        )
        print(f"[FAIL] SignupRequest.profile_template default = {req.profile_template}")

    # 2. config.load_settings — verify default qua source code (load_settings
    #    đọc .env file thật, không inject env-dict được).
    cfg_src = (ROOT / "config.py").read_text(encoding="utf-8")
    if 'BROWSER_USE_PROFILE_TEMPLATE", "false"' in cfg_src and \
       "default=False,\n        )," in cfg_src:
        print("[PASS] config.py: BROWSER_USE_PROFILE_TEMPLATE env default = false")
    else:
        failures.append("config.py BROWSER_USE_PROFILE_TEMPLATE default chưa = false")
        print("[FAIL] config.py BROWSER_USE_PROFILE_TEMPLATE default chưa = false")

    # 3. Settings dataclass field default
    if 'browser_use_profile_template: bool = False' in cfg_src:
        print("[PASS] config.py: Settings.browser_use_profile_template field default = False")
    else:
        failures.append("Settings field default chưa = False")
        print("[FAIL] Settings field default chưa = False")

    # 4. CLI source — `False` là default
    cli_src = (ROOT / "cli.py").read_text(encoding="utf-8")
    if 'False, "--profile-template/--fresh-profile"' in cli_src:
        print("[PASS] cli.py signup_cmd: profile_template default = False")
    else:
        failures.append("cli.py signup_cmd profile_template default chưa = False")
        print("[FAIL] cli.py signup_cmd profile_template default chưa = False")

    print()
    if failures:
        for f in failures:
            print(f"   - {f}")
        return 1
    print("[OK] All Task 1.5 fresh profile defaults pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
