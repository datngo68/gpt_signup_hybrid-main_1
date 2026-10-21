from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "runtime" / "data.db"


def main() -> None:
    job_id = sys.argv[1] if len(sys.argv) > 1 else ""
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        cols = [row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()]
        log_candidates = [c for c in ("log_lines", "logs", "log", "events") if c in cols]
        if not log_candidates:
            print("jobs columns:", ", ".join(cols))
            return
        log_col = log_candidates[0]
        if job_id:
            rows = conn.execute(
                f"SELECT id, email, status, error, {log_col} AS logs FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT id, email, status, error, {log_col} AS logs FROM jobs "
                "WHERE kind = 'session' ORDER BY updated_at DESC LIMIT 8"
            ).fetchall()
    for row in rows:
        print(f"=== {row['id']} {row['email']} {row['status']} ===")
        if row["error"]:
            print(f"ERROR: {row['error']}")
        try:
            lines = json.loads(row["logs"] or "[]")
        except json.JSONDecodeError:
            lines = []
        for line in lines[-80:]:
            print(line)


if __name__ == "__main__":
    main()
