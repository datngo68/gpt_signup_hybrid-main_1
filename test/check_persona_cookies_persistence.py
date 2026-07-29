"""Task 3.3 verify — outlook_combos.persona_cookies + repository methods.

Mục tiêu:
    - DDL_OUTLOOK_COMBOS có cột persona_cookies (cho fresh DB).
    - MIGRATIONS[12] ALTER TABLE ADD COLUMN persona_cookies.
    - CURRENT_VERSION = 12.
    - ComboRepository methods: get_persona_cookies + set_persona_cookies.
    - Round-trip: ensure_exists → set → get → set None → get None.

Chạy: .venv/bin/python3 test/check_persona_cookies_persistence.py
"""
from __future__ import annotations

import inspect
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    failures: list[str] = []

    # ── TC-01 Schema constants ──
    from db import schema

    if schema.CURRENT_VERSION == 12:
        print("[PASS] CURRENT_VERSION = 12")
    else:
        failures.append(f"CURRENT_VERSION = {schema.CURRENT_VERSION}, expect 12")

    if "persona_cookies" in schema.DDL_OUTLOOK_COMBOS:
        print("[PASS] DDL_OUTLOOK_COMBOS includes persona_cookies column")
    else:
        failures.append("DDL_OUTLOOK_COMBOS thiếu persona_cookies")

    if 12 in schema.MIGRATIONS:
        m = schema.MIGRATIONS[12]
        if any("persona_cookies" in stmt for stmt in m):
            print("[PASS] MIGRATIONS[12] ADD COLUMN persona_cookies")
        else:
            failures.append("MIGRATIONS[12] không có ADD COLUMN persona_cookies")
    else:
        failures.append("MIGRATIONS[12] missing")

    # ── TC-02 Repository signatures ──
    from db.repositories import ComboRepository

    methods = inspect.getmembers(ComboRepository, predicate=inspect.isfunction)
    method_names = [m[0] for m in methods]
    for name in ("get_persona_cookies", "set_persona_cookies"):
        if name in method_names:
            print(f"[PASS] ComboRepository.{name} exists")
        else:
            failures.append(f"ComboRepository.{name} missing")

    # ── TC-03 Round-trip với temp DB ──
    with tempfile.TemporaryDirectory() as tmp:
        from db.engine import DatabaseEngine
        from db.repositories import ComboRepository

        engine = DatabaseEngine(Path(tmp) / "test.db")
        try:
            repo = ComboRepository(engine)

            # Ensure combo row tồn tại trước (PK email)
            email = "test@example.com"
            repo.ensure_exists({
                "email": email,
                "password": "p",
                "refresh_token": "t",
                "client_id": "c",
            })
            print("[PASS] ensure_exists combo OK")

            # get persona_cookies → None (chưa set)
            got = repo.get_persona_cookies(email)
            if got is None:
                print("[PASS] get_persona_cookies → None (chưa set)")
            else:
                failures.append(f"initial get returns {got!r}, expect None")

            # set list → get list back
            cookies = [
                {
                    "name": "oai-did",
                    "value": "abc-123",
                    "domain": ".chatgpt.com",
                    "path": "/",
                    "expires": 1782364031.5,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                },
                {
                    "name": "oaicom-stable-id",
                    "value": "stable-uuid",
                    "domain": ".chatgpt.com",
                    "path": "/",
                    "expires": None,
                    "httpOnly": False,
                    "secure": True,
                    "sameSite": "None",
                },
            ]
            repo.set_persona_cookies(email, cookies)
            got = repo.get_persona_cookies(email)
            if got == cookies:
                print(f"[PASS] set_persona_cookies → get round-trip ({len(cookies)} cookies)")
            else:
                failures.append(f"round-trip mismatch:\nset={cookies}\nget={got}")

            # set None → get None (clear)
            repo.set_persona_cookies(email, None)
            got = repo.get_persona_cookies(email)
            if got is None:
                print("[PASS] set_persona_cookies(None) → clear (get returns None)")
            else:
                failures.append(f"after clear, get returns {got!r}")

            # set [] → get None (empty list treat as None)
            repo.set_persona_cookies(email, [])
            got = repo.get_persona_cookies(email)
            if got is None:
                print("[PASS] set_persona_cookies([]) → cleared (treat as None)")

            # set on missing email → fail-fast
            try:
                repo.set_persona_cookies("nonexistent@x.com", cookies)
                failures.append("set_persona_cookies on missing email phải raise")
            except Exception as exc:
                if "no row found" in str(exc).lower():
                    print("[PASS] set_persona_cookies missing email → RepositoryError")
                else:
                    failures.append(f"unexpected error: {exc}")
        finally:
            engine.close()

    print()
    if failures:
        print(f"[FAIL] {len(failures)} failures:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("[OK] All Task 3.3 persona_cookies persistence invariants pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
