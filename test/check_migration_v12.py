"""Phase 6 Task 6.4 — Migration v12 test.

Verify schema migration v11 → v12:
    - Tạo DB ở version 11 (mock pre-anti-ban schema).
    - Insert vài combo rows.
    - Run migration → bump tới v12.
    - Assert: column persona_cookies tồn tại + nullable + data cũ giữ nguyên.
    - Assert: ComboRepository.set/get_persona_cookies hoạt động trên DB migrated.

Chạy: .venv/bin/python3 test/check_migration_v12.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    failures: list[str] = []

    # ── Step 1: build mock v11 DB (KHÔNG có persona_cookies column) ──
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "v11.db"

        # DDL v11 (pre-Phase 3 schema) — outlook_combos thiếu persona_cookies
        v11_ddl = """
        CREATE TABLE _schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now')),
            description TEXT
        );
        INSERT INTO _schema_version (version, description) VALUES (11, 'mock v11');

        CREATE TABLE outlook_combos (
            email TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            refresh_token TEXT NOT NULL,
            client_id TEXT NOT NULL,
            used_for_signup INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            last_failed_at TEXT,
            used_at TEXT,
            last_refresh_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """

        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript(v11_ddl)
            conn.execute(
                "INSERT INTO outlook_combos (email, password, refresh_token, client_id) "
                "VALUES (?, ?, ?, ?)",
                ("user1@x.com", "p1", "rt1", "c1"),
            )
            conn.execute(
                "INSERT INTO outlook_combos (email, password, refresh_token, client_id, used_for_signup) "
                "VALUES (?, ?, ?, ?, ?)",
                ("user2@x.com", "p2", "rt2", "c2", 1),
            )
            conn.commit()

            # Verify pre-migration schema
            cols = [r[1] for r in conn.execute("PRAGMA table_info(outlook_combos)").fetchall()]
            if "persona_cookies" in cols:
                failures.append("v11 mock DB đã có persona_cookies — test setup wrong")
                return 1
            print("[PASS] v11 mock DB built (no persona_cookies column)")
            print(f"  cols: {cols}")
        finally:
            conn.close()

        # ── Step 2: open via DatabaseEngine → trigger migration ──
        from db.engine import DatabaseEngine

        engine = DatabaseEngine(db_path)
        try:
            # DatabaseEngine init runs migration in __init__
            with engine.get_connection() as c:
                ver = c.execute("SELECT MAX(version) FROM _schema_version").fetchone()[0]
            if ver == 12:
                print(f"[PASS] schema version bumped to {ver}")
            else:
                failures.append(f"version not 12 after migration: {ver}")

            # Check column added
            with engine.get_connection() as c:
                cols = [r[1] for r in c.execute(
                    "PRAGMA table_info(outlook_combos)"
                ).fetchall()]
            if "persona_cookies" in cols:
                print(f"[PASS] persona_cookies column added: cols={cols}")
            else:
                failures.append(f"persona_cookies missing after migration: {cols}")

            # Check data preserved
            with engine.get_connection() as c:
                rows = c.execute(
                    "SELECT email, password, used_for_signup FROM outlook_combos "
                    "ORDER BY email"
                ).fetchall()
            if len(rows) == 2 and rows[0]["email"] == "user1@x.com" \
                    and rows[1]["used_for_signup"] == 1:
                print("[PASS] existing data preserved (2 rows + used_for_signup intact)")
            else:
                failures.append(f"data lost: {rows}")

            # ── Step 3: ComboRepository methods work on migrated DB ──
            from db.repositories import ComboRepository

            repo = ComboRepository(engine)
            cookies = [
                {"name": "oai-did", "value": "abc-123", "domain": ".chatgpt.com",
                 "path": "/", "expires": None, "httpOnly": True,
                 "secure": True, "sameSite": "Lax"},
            ]
            repo.set_persona_cookies("user1@x.com", cookies)
            got = repo.get_persona_cookies("user1@x.com")
            if got == cookies:
                print("[PASS] set/get persona_cookies on migrated DB")
            else:
                failures.append(f"round-trip fail: {got}")

            # ensure_exists vẫn work (legacy code path)
            repo.ensure_exists({
                "email": "user3@x.com",
                "password": "p3",
                "refresh_token": "rt3",
                "client_id": "c3",
            })
            got_user3 = repo.get_persona_cookies("user3@x.com")
            if got_user3 is None:
                print("[PASS] new combo via ensure_exists has persona_cookies=NULL")
            else:
                failures.append(f"new combo persona_cookies != NULL: {got_user3}")
        finally:
            engine.close()

    print()
    if failures:
        print(f"[FAIL] {len(failures)} failures:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("[OK] All Task 6.4 migration v11→v12 invariants pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
