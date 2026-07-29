"""Mimic the import chain that cli.web_cmd uses — must succeed without SyntaxError."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    # Order from the traceback:
    #   cli.web_cmd → web.server → web.manager → session_phase
    chain = [
        "session_phase",
        "request_phase",
        "sentinel_sidecar",
        "browser_phase",
        "web.manager",
        "web.server",
        "web",
    ]
    bad = []
    for name in chain:
        try:
            __import__(name)
            print(f"OK:   import {name}")
        except SyntaxError as exc:
            print(f"FAIL: import {name} — SyntaxError: {exc}")
            bad.append(name)
        except Exception as exc:
            # ImportError / ModuleNotFoundError other than syntax — list but
            # don't fail (may be due to missing runtime deps not relevant
            # for syntax check)
            print(f"WARN: import {name} — {type(exc).__name__}: {exc}")
    if bad:
        print(f"\n{len(bad)} module(s) have SyntaxError")
        return 1
    print("\nNo SyntaxError in chain")
    return 0


if __name__ == "__main__":
    sys.exit(main())
