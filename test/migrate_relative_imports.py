#!/usr/bin/env python3
"""Convert relative imports → absolute imports cho PyInstaller flat-bundle.

Quy tắc:
  - File ở root project: convert MỌI `from .xxx`, `from ..xxx`, `from . import`
    sang absolute.
  - File ở sub-package (web/, db/, autoreg/, codex_auth/, icloud_hme/):
    chỉ convert level >= 2 (`from ..xxx` → `from xxx`). Level=1 (nội bộ
    sub-package) GIỮ NGUYÊN.

Cú pháp xử lý:
  1. `from .module import X`     → `from module import X`
  2. `from ..module import X`    → `from module import X`
  3. `from . import name`        → `import name`
  4. `from .. import name`       → `import name`

Multi-line import giữ nguyên format (chỉ touch line đầu tiên có `from `).

Run: python3 test/migrate_relative_imports.py
Idempotent: chạy lại không đổi gì sau lần đầu thành công.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SUB_PACKAGES = {"web", "db", "autoreg", "codex_auth", "icloud_hme"}
EXCLUDE_DIRS = {".venv", ".git", "__pycache__", "test", "scripts", "docs",
                "build", "dist", ".gitnexus", ".kiro", ".planning", ".vscode"}


def _classify(rel: Path) -> str:
    parts = rel.parts
    if len(parts) == 1:
        return "ROOT"
    if parts[0] in SUB_PACKAGES:
        return "SUB"
    return "OTHER"


def _collect_edits(tree: ast.AST, kind: str) -> list[tuple[int, int, str | None, list[str]]]:
    """Trả list (lineno, level, module, names) cần edit."""
    edits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        level = node.level or 0
        if level == 0:
            continue
        if kind == "SUB" and level == 1:
            continue  # SUB-IN giữ nguyên
        edits.append((node.lineno, level, node.module, [a.name for a in node.names]))
    return edits


def _transform_line(line: str, level: int, module: str | None, file_pos: str) -> str:
    """Replace 'from <dots><module>' prefix trên line đầu của ImportFrom.

    Raise RuntimeError nếu pattern không match (chứng tỏ giả định AST→text
    sai — fail fast để user thấy ngay).
    """
    dots = r"\." * level
    if module:
        # from .x import → from x import   /   from ..x.y import → from x.y import
        pattern = re.compile(rf"\bfrom\s+{dots}{re.escape(module)}\b")
        new_prefix = f"from {module}"
        new_line, n = pattern.subn(new_prefix, line, count=1)
        if n != 1:
            raise RuntimeError(
                f"{file_pos}: không match pattern 'from {('.' * level) + module}' "
                f"trong line: {line!r}"
            )
        return new_line
    else:
        # from . import X → import X   /   from .. import X → import X
        pattern = re.compile(rf"\bfrom\s+{dots}\s+import\b")
        new_line, n = pattern.subn("import", line, count=1)
        if n != 1:
            raise RuntimeError(
                f"{file_pos}: không match pattern 'from {'.' * level} import' "
                f"trong line: {line!r}"
            )
        return new_line


def _transform_file(path: Path, kind: str) -> int:
    """Convert tại chỗ. Trả số dòng đã đổi."""
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:
        print(f"[SKIP-SYNTAX] {path}: {e}", flush=True)
        return 0

    edits = _collect_edits(tree, kind)
    if not edits:
        return 0

    lines = src.split("\n")
    # Sort descending để index không lệch (tuy không đổi line count nhưng an toàn).
    edits.sort(key=lambda x: x[0], reverse=True)
    changed = 0
    rel = path.relative_to(PROJECT_ROOT)
    for lineno, level, module, _names in edits:
        idx = lineno - 1
        original = lines[idx]
        new_line = _transform_line(original, level, module, f"{rel}:{lineno}")
        if new_line != original:
            lines[idx] = new_line
            changed += 1

    if changed:
        path.write_text("\n".join(lines), encoding="utf-8")
    return changed


def _iter_files() -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    # Root .py (top-level only — không đệ quy)
    for p in sorted(PROJECT_ROOT.glob("*.py")):
        out.append((p, "ROOT"))
    # Sub-package .py (đệ quy)
    for sub in sorted(SUB_PACKAGES):
        base = PROJECT_ROOT / sub
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.py")):
            rel = p.relative_to(PROJECT_ROOT)
            if any(part in EXCLUDE_DIRS for part in rel.parts):
                continue
            out.append((p, "SUB"))
    return out


def main() -> int:
    total_changed = 0
    files_touched = 0
    for path, kind in _iter_files():
        rel = path.relative_to(PROJECT_ROOT)
        try:
            n = _transform_file(path, kind)
        except RuntimeError as e:
            print(f"[FAIL] {rel}: {e}", flush=True)
            return 1
        if n > 0:
            files_touched += 1
            total_changed += n
            print(f"[OK   ] {rel}  ({n} import statements)", flush=True)
        else:
            print(f"[skip ] {rel}", flush=True)

    print("", flush=True)
    print(f"=== Done: {total_changed} imports converted across {files_touched} files ===",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
