"""Verify cờ --no-auth: TẮT token auth cho /api/* (opt-in insecure).

Run: python3 test/check_no_auth.py

Kiểm:
  [1] Syntax cli.py + web/server.py.
  [2] server.py: set_disable_auth + _disable_auth_enabled + middleware dùng not _disable_auth_enabled().
  [3] cli.py: option --no-auth + set_disable_auth(no_auth).
  [4] setup.sh: parse --no-auth + truyền $NO_AUTH_FLAG.
  [5] Functional: middleware trả 401 khi auth ON (no token), KHÔNG 401 khi --no-auth.
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Set token qua env để get_token() KHÔNG chạm DB (tránh side-effect khi test).
os.environ["GPT_SIGNUP_WEB_TOKEN"] = "test-token-check-no-auth"


def _parse(p: Path) -> None:
    ast.parse(p.read_text(encoding="utf-8"), filename=str(p))


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, label: str) -> None:
        if cond:
            print(f"[PASS] {label}", flush=True)
        else:
            failures.append(label)
            print(f"[FAIL] {label}", flush=True)

    # [1] Syntax
    for f in [ROOT / "cli.py", ROOT / "web" / "server.py"]:
        try:
            _parse(f)
            print(f"[PASS] syntax {f.relative_to(ROOT)}", flush=True)
        except SyntaxError as e:
            failures.append(f"syntax {f}: {e}")
            print(f"[FAIL] syntax {f.relative_to(ROOT)} :: {e}", flush=True)

    server_src = (ROOT / "web" / "server.py").read_text(encoding="utf-8")
    cli_src = (ROOT / "cli.py").read_text(encoding="utf-8")
    setup_src = (ROOT / "setup.sh").read_text(encoding="utf-8")

    # [2] server.py wiring
    check("def set_disable_auth" in server_src, "server.py: có set_disable_auth()")
    check("def _disable_auth_enabled" in server_src,
          "server.py: có _disable_auth_enabled() (đọc env)")
    check("os.environ[_ENV_DISABLE_AUTH]" in server_src,
          "server.py: set_disable_auth ghi os.environ")
    check("not _disable_auth_enabled()" in server_src,
          "server.py: auth_middleware bỏ qua khi _disable_auth_enabled()")

    # [3] cli.py
    check('"--no-auth"' in cli_src, "cli.py: có option --no-auth")
    check("set_disable_auth(no_auth)" in cli_src, "cli.py: gọi set_disable_auth(no_auth)")

    # [4] setup.sh
    check("--no-auth) NO_AUTH=" in setup_src, "setup.sh: parse case --no-auth")
    check('NO_AUTH_FLAG="--no-auth"' in setup_src, "setup.sh: set NO_AUTH_FLAG")
    check("$HIDE_REG_FLAG $NO_AUTH_FLAG" in setup_src,
          "setup.sh: truyền $NO_AUTH_FLAG vào lệnh web")

    # [5] Functional qua TestClient (không 'with' → không chạy startup event).
    try:
        from fastapi.testclient import TestClient
        from web.server import app, set_disable_auth

        client = TestClient(app, raise_server_exceptions=False)

        # auth ON, không token → 401 (middleware chặn trước handler).
        set_disable_auth(False)
        r_on = client.get("/api/jobs")
        check(r_on.status_code == 401,
              f"auth ON + no token → 401 (got {r_on.status_code})")

        # --no-auth → KHÔNG còn 401 (middleware bypass).
        set_disable_auth(True)
        r_off = client.get("/api/jobs")
        check(r_off.status_code != 401,
              f"--no-auth → KHÔNG 401 (got {r_off.status_code})")

        set_disable_auth(False)  # cleanup
    except Exception as e:  # noqa: BLE001
        failures.append(f"functional: {type(e).__name__}: {e}")
        print(f"[FAIL] functional :: {e}", flush=True)

    print("", flush=True)
    if failures:
        print(f"=== {len(failures)} FAILURE(S) ===", flush=True)
        for x in failures:
            print(f"  - {x}", flush=True)
        return 1
    print("=== ALL PASS ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
