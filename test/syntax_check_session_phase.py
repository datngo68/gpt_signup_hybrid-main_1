"""Verify session_phase.py parses cleanly after the await fix."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    targets = [
        ROOT / "session_phase.py",
        ROOT / "request_phase.py",
        ROOT / "sentinel_sidecar.py",
        ROOT / "browser_phase.py",
        ROOT / "web" / "server.py",
        ROOT / "web" / "manager.py",
        ROOT / "cli.py",
    ]
    bad = []
    for p in targets:
        if not p.exists():
            print(f"SKIP: {p} not found")
            continue
        try:
            ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            print(f"OK:   {p.relative_to(ROOT)}")
        except SyntaxError as exc:
            print(f"FAIL: {p.relative_to(ROOT)} — line {exc.lineno}: {exc.msg}")
            bad.append(p)
    if bad:
        print(f"\n{len(bad)} file(s) have SyntaxError")
        return 1
    print("\nAll files parse OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
