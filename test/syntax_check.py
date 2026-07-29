"""AST parse mọi file Python đã chạm trong feature reg_mode=hybrid.

Không thực thi import (tránh kéo theo dependency curl_cffi/camoufox) — chỉ
verify cú pháp + AST hợp lệ. Tách khỏi smoke test để smoke có thể fail mà
syntax vẫn pass (hoặc ngược lại).

Chạy:
    .venv/bin/python test/syntax_check.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

# File trực tiếp chạm trong feature này — fail-fast nếu list lệch reality.
_TARGETS: tuple[Path, ...] = (
    ROOT / "reg_hybrid" / "__init__.py",
    ROOT / "reg_hybrid" / "runner.py",
    ROOT / "reg_hybrid" / "mail_adapter.py",
    ROOT / "reg_hybrid" / "camoufox_factory.py",
    ROOT / "reg_hybrid" / "browser_pool.py",
    ROOT / "reg_hybrid" / "relay.py",
    ROOT / "reg_hybrid" / "otp_loop.py",
    ROOT / "models.py",
    ROOT / "signup.py",
    ROOT / "cli.py",
    ROOT / "db" / "repositories.py",
    ROOT / "web" / "manager.py",
    ROOT / "web" / "server.py",
    ROOT / "web" / "icloud_routes.py",
    ROOT / "autoreg" / "runner.py",
    ROOT / "autoreg" / "schemas.py",
    ROOT / "request_phase.py",
    ROOT / "test" / "run_hybrid_live_regdata.py",
)


def main() -> int:
    failures: list[str] = []
    for path in _TARGETS:
        if not path.exists():
            failures.append(f"MISSING: {path}")
            print(f"[FAIL] {path.relative_to(ROOT)} — file not found", flush=True)
            continue
        try:
            source = path.read_text(encoding="utf-8")
            ast.parse(source, filename=str(path))
            print(f"[PASS] {path.relative_to(ROOT)}", flush=True)
        except SyntaxError as exc:
            failures.append(f"{path}: line {exc.lineno}: {exc.msg}")
            print(
                f"[FAIL] {path.relative_to(ROOT)} — SyntaxError line {exc.lineno}: {exc.msg}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 — catch-all để tiếp tục file kế
            failures.append(f"{path}: {type(exc).__name__}: {exc}")
            print(
                f"[FAIL] {path.relative_to(ROOT)} — {type(exc).__name__}: {exc}",
                flush=True,
            )

    print(flush=True)
    if failures:
        print(f"=== SYNTAX CHECK FAILED ({len(failures)}/{len(_TARGETS)}) ===", flush=True)
        for f in failures:
            print(f"  - {f}", flush=True)
        return 1
    print(f"=== SYNTAX CHECK PASSED ({len(_TARGETS)} files) ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
