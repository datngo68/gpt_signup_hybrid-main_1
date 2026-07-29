#!/usr/bin/env python3
"""Liệt kê tất cả relative import (level > 0) trong codebase.

Mục đích: làm baseline trước/sau khi convert sang absolute import cho
PyInstaller flat-bundle build.

Phân loại output:
  [ROOT]    file ở root project (cần sửa: . hoặc ..)
  [SUB-OUT] file trong subpackage import OUT khỏi sub-package (.. trở lên)
            → cần sửa, vì grandparent không tồn tại trong exe.
  [SUB-IN]  file trong subpackage import nội bộ sub-package (chỉ .)
            → KHÔNG cần sửa, sub-package vẫn là package trong exe.

Run: python3 test/check_relative_imports.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SUB_PACKAGES = {"web", "db", "autoreg", "codex_auth", "icloud_hme"}
SCAN_DIRS = ["", *SUB_PACKAGES]
EXCLUDE_DIRS = {".venv", ".git", "__pycache__", "test", "scripts", "docs",
                "build", "dist", ".gitnexus", ".kiro", ".planning", ".vscode"}


def classify(file_rel: Path) -> str:
    parts = file_rel.parts
    if len(parts) == 1:
        return "ROOT"
    top = parts[0]
    if top in SUB_PACKAGES:
        return "SUB"
    return "OTHER"


def scan_file(path: Path) -> list[tuple[int, int, str, list[str]]]:
    """Trả list (line, level, module, names) cho mỗi relative import."""
    try:
        src = path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[SKIP] {path}: {e}", flush=True)
        return []
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:
        print(f"[SYNTAX] {path}: {e}", flush=True)
        return []

    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.level or 0) > 0:
            mod = node.module or ""
            names = [a.name for a in node.names]
            out.append((node.lineno, node.level, mod, names))
    return out


def main() -> int:
    total = 0
    by_kind: dict[str, int] = {"ROOT": 0, "SUB-OUT": 0, "SUB-IN": 0}

    for sub in SCAN_DIRS:
        base = PROJECT_ROOT / sub if sub else PROJECT_ROOT
        if not base.is_dir():
            continue
        # Walk: root → chỉ glob *.py top-level (không đệ quy vào subdir)
        if sub == "":
            files = sorted(p for p in base.glob("*.py"))
        else:
            files = sorted(
                p for p in base.rglob("*.py")
                if not any(part in EXCLUDE_DIRS for part in p.relative_to(PROJECT_ROOT).parts)
            )

        for f in files:
            rel = f.relative_to(PROJECT_ROOT)
            kind = classify(rel)
            if kind == "OTHER":
                continue
            hits = scan_file(f)
            for lineno, level, mod, names in hits:
                if kind == "ROOT":
                    tag = "ROOT"
                elif level >= 2:
                    tag = "SUB-OUT"
                else:
                    tag = "SUB-IN"
                by_kind[tag] = by_kind.get(tag, 0) + 1
                total += 1
                dots = "." * level
                names_str = ", ".join(names)
                print(f"[{tag:7}] {rel}:{lineno}  from {dots}{mod} import {names_str}",
                      flush=True)

    print("", flush=True)
    print(f"=== Summary: {total} relative imports ===", flush=True)
    for k, v in by_kind.items():
        print(f"  [{k}] {v}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
