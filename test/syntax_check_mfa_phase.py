"""Parse AST mfa_phase.py để verify không lỗi syntax sau khi fix CF challenge."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "mfa_phase.py"


def main() -> int:
    if not TARGET.exists():
        print(f"[FAIL] target không tồn tại: {TARGET}", flush=True)
        return 1
    src = TARGET.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(TARGET))
    except SyntaxError as exc:
        print(f"[FAIL] syntax error tại {TARGET}:{exc.lineno}:{exc.offset} — {exc.msg}", flush=True)
        return 1

    # Verify các symbol mới + cũ
    names_func = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    names_func |= {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    required = {
        "_inject_session_cookies",
        "_is_cf_challenge",
        "_refresh_access_token",
        "_enroll_totp",
        "_enroll_totp_with_retry",
        "_activate_enrollment",
        "_check_mfa_info",
        "enable_2fa",
    }
    missing = required - names_func
    if missing:
        print(f"[FAIL] thiếu function: {sorted(missing)}", flush=True)
        return 1

    # Verify constant CF backoff đã được định nghĩa
    constants = {
        n.targets[0].id
        for n in tree.body
        if isinstance(n, ast.Assign)
        and len(n.targets) == 1
        and isinstance(n.targets[0], ast.Name)
    }
    needed_const = {
        "_BACKOFF_SECONDS",
        "_BACKOFF_CF_SECONDS",
        "_CF_CHALLENGE_MARKERS",
        "_BACKEND_DOMAINS",
    }
    missing_const = needed_const - constants
    if missing_const:
        print(f"[FAIL] thiếu constant: {sorted(missing_const)}", flush=True)
        return 1

    print(f"[PASS] mfa_phase.py syntax OK — {len(src)} bytes, {len(tree.body)} top-level nodes", flush=True)
    print(f"[PASS] tất cả {len(required)} function tồn tại: {sorted(required)}", flush=True)
    print(f"[PASS] constant CF/backoff đầy đủ: {sorted(needed_const)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
