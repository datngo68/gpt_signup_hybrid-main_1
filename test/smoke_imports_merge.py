#!/usr/bin/env python3
"""Smoke: import thật các module đã sửa (bắt lỗi import-time, không chỉ AST).

Chạy: .venv/bin/python test/smoke_imports_merge.py
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))
PKG = "gpt_signup_hybrid_new"

rc = 0
mods = [
    "db.repositories",
    "session_phase",
    "web.upi_runner",
    "web.manager",
    "web.server",  # import nặng nhất — kéo toàn bộ route + manager + runner
]
for i, m in enumerate(mods, 1):
    full = f"{PKG}.{m}"
    try:
        __import__(full)
        print(f"[PASS] [{i}/{len(mods)}] import {m}", flush=True)
    except Exception as e:  # noqa: BLE001
        rc = 1
        import traceback
        print(f"[FAIL] [{i}/{len(mods)}] import {m}: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()

print("=== DONE ===", flush=True)
sys.exit(rc)
