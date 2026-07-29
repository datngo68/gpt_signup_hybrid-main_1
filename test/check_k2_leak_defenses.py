"""Phase 11.7 — verify K2 has DUMMY-password leak defenses.

If ``page.route.abort()`` fails silently on a particular Camoufox build,
the sidecar's DUMMY password reaches the server which then creates the
account with DUMMY (not the real one). Later login fails with
``invalid_username_or_password``. We want 3 defenses:

  1. ``_abort_ok`` flag in captured dict — set to False if abort raises;
     caller MUST drop token in that case.
  2. ``leaked_register_requests`` audit via ``page.on("requestfinished")``
     — any /register POST that completes its full round-trip means abort
     failed; drop token.
  3. Navigate to ``about:blank`` after capture — destroys the form so the
     SPA cannot retry submission AFTER our unroute() runs.

Run: python3 test/check_k2_leak_defenses.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"OK:   {msg}")


def main() -> int:
    src = (ROOT / "sentinel_sidecar.py").read_text()

    # 1. _abort_ok flag set inside _route handler
    if "captured[\"_abort_ok\"] = True" not in src:
        fail("captured['_abort_ok']=True missing inside route handler")
    if "captured[\"_abort_ok\"] = False" not in src:
        fail("captured['_abort_ok']=False missing on exception path")
    ok("captured['_abort_ok'] flag set on both success & failure")

    # 2. Check _abort_ok after wait_for and DROP token if False
    if 'captured.get("_abort_ok") is False' not in src:
        fail(
            "K2 doesn't check _abort_ok before returning token (no defense "
            "against silent route.abort failure)"
        )
    ok("K2 checks _abort_ok and drops token if abort failed")

    # 3. leaked_register_requests audit
    if "leaked_register_requests" not in src:
        fail("leaked_register_requests audit list missing")
    if "requestfinished" not in src:
        fail("page.on('requestfinished') listener missing")
    ok("page.on('requestfinished') listener + leak audit list present")
    if "if leaked_register_requests:" not in src:
        fail("K2 doesn't check leaked_register_requests before returning")
    ok("K2 drops token if any /register POST leaked to server")

    # 4. Navigate to about:blank after capture
    if 'page.goto("about:blank"' not in src:
        fail(
            "K2 doesn't navigate to about:blank after capture — SPA can "
            "retry the form submit"
        )
    ok("K2 navigates to about:blank to prevent SPA retry")

    # 5. requestfinished listener removed in finally
    if "remove_listener" not in src and "page.removeListener" not in src:
        fail("K2 doesn't remove requestfinished listener in finally block")
    ok("K2 removes requestfinished listener in cleanup")

    print("\nAll K2 leak defenses present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
