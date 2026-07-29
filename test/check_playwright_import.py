#!/usr/bin/env python3
"""Verify đúng cách import playwright — KHÔNG dùng .__version__ (không tồn tại).

Module ``playwright`` là namespace package, ``__version__`` không expose ở root.
Cách verify đúng:
    1. import playwright (no error → cài OK).
    2. from playwright.async_api import async_playwright (chạm sub-module thật).
    3. Đọc version từ importlib.metadata (PEP 566 standard).

Workflow CI dùng cùng pattern này thay vì sai cú pháp ``playwright.__version__``.

Chạy:
    .venv/bin/python3 test/check_playwright_import.py
"""
from __future__ import annotations

import sys


def main() -> int:
    failures = 0

    # TC-01: import root namespace
    try:
        import playwright  # noqa: F401
        print("[PASS] TC-01 import playwright (namespace package)", flush=True)
    except ImportError as exc:
        print(f"[FAIL] TC-01 import playwright :: {exc}", flush=True)
        failures += 1

    # TC-02: import sub-module thật (touch .async_api)
    try:
        from playwright.async_api import async_playwright  # noqa: F401
        print("[PASS] TC-02 from playwright.async_api import async_playwright", flush=True)
    except ImportError as exc:
        print(f"[FAIL] TC-02 import async_api :: {exc}", flush=True)
        failures += 1

    # TC-03: version từ importlib.metadata (PEP 566 standard)
    try:
        from importlib.metadata import version
        v = version("playwright")
        print(f"[PASS] TC-03 importlib.metadata.version('playwright') = {v}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] TC-03 metadata version :: {exc}", flush=True)
        failures += 1

    # TC-04: Confirm playwright.__version__ KHÔNG có (sanity — đảm bảo tao
    # KHÔNG còn dùng cách này trong CI)
    try:
        import playwright
        ver = getattr(playwright, "__version__", None)
        if ver is None:
            print("[PASS] TC-04 playwright.__version__ = None (đúng — KHÔNG dùng cách này)",
                  flush=True)
        else:
            # Một số version có thể có (lib evolve) — không fail, chỉ note.
            print(f"[INFO] TC-04 playwright.__version__ = {ver} (nice-to-have, không bắt buộc)",
                  flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] TC-04 :: {exc}", flush=True)
        failures += 1

    print(f"\n{4 - failures}/4 passed" + (f" — {failures} failures" if failures else ""),
          flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
