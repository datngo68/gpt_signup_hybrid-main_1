"""Kiểm tra xem có job nào gửi OTP 2 lần không.

Cách check:
- Đọc job_logs từ SQLite, group theo job_id
- Đếm số dòng chứa pattern "OTP send", "Resend", "/email-otp/send", "polling OTP"
- In ra job có > 1 lần trigger send
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "runtime" / "data.db"


def main() -> int:
    if not DB.exists():
        print(f"[FAIL] không thấy DB: {DB}", flush=True)
        return 1

    print(f"[INFO] đọc {DB}", flush=True)
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row

    # In schema 1 phát để biết bảng jobs có cột gì
    cols = conn.execute("PRAGMA table_info(jobs)").fetchall()
    print(f"[INFO] cột bảng jobs: {[c['name'] for c in cols]}", flush=True)

    # Lấy 50 jobs gần nhất
    rows = conn.execute(
        """
        SELECT * FROM jobs
        ORDER BY created_at DESC
        LIMIT 50
        """
    ).fetchall()
    print(f"[INFO] tìm thấy {len(rows)} reg jobs gần nhất", flush=True)

    # Patterns gợi ý cho việc gửi OTP
    patterns = {
        "register_post": "POST /api/accounts/user/register",
        "otp_send": "OTP send triggered",
        "otp_send_lower": "/email-otp/send",
        "polling_otp": "polling OTP",
        "click_resend": "clicked 'Resend",
        "register_ok": "register OK → continue_url",
        "submit_otp": "[browser] typing OTP",
        "otp_form_ready": "OTP input ready",
        "screen_otp": "screen=otp",
        "screen_password_create": "screen=password_create",
        "screen_continue": "screen=continue",
    }

    suspect_jobs = []
    for row in rows:
        job_id = row["id"]
        log_rows = conn.execute(
            "SELECT line FROM job_logs WHERE job_id = ? ORDER BY created_at ASC",
            (job_id,),
        ).fetchall()
        if not log_rows:
            continue
        counts: dict[str, int] = defaultdict(int)
        for r in log_rows:
            line = r["line"] or ""
            for k, p in patterns.items():
                if p in line:
                    counts[k] += 1

        # Heuristic: tổng OTP-send-trigger > 1 → suspect
        send_total = (
            counts["register_ok"]
            + counts["click_resend"]
        )
        if send_total >= 2 or counts["register_ok"] >= 2 or counts["polling_otp"] >= 2:
            suspect_jobs.append((job_id, row["email"], row["status"], dict(counts), len(log_rows)))

        # In summary cho mọi job (1 dòng / job)
        print(
            f"[JOB] {job_id[:24]:24s} email={row['email'][:30]:30s} "
            f"status={row['status']:8s} "
            f"reg_post={counts['register_post']} reg_ok={counts['register_ok']} "
            f"otp_send_trig={counts['otp_send']} resend={counts['click_resend']} "
            f"poll={counts['polling_otp']} submit={counts['submit_otp']} "
            f"sc_otp={counts['screen_otp']} sc_cont={counts['screen_continue']}",
            flush=True,
        )

    print("", flush=True)
    print(f"[SUMMARY] suspect jobs (>=2 send/poll): {len(suspect_jobs)}", flush=True)
    for jid, email, status, counts, n in suspect_jobs:
        print(f"  - {jid} {email} status={status} log_lines={n}", flush=True)
        for k, v in counts.items():
            if v > 0:
                print(f"      {k}={v}", flush=True)

    # Lấy 1 suspect job để in toàn bộ log liên quan OTP
    if suspect_jobs:
        target = suspect_jobs[0][0]
        print("", flush=True)
        print(f"[DETAIL] job {target} — các dòng liên quan OTP:", flush=True)
        log_rows = conn.execute(
            "SELECT line FROM job_logs WHERE job_id = ? ORDER BY created_at ASC",
            (target,),
        ).fetchall()
        keywords = ("OTP", "otp", "Resend", "register", "screen=", "continue_url", "email-verification")
        for r in log_rows:
            line = r["line"] or ""
            if any(k in line for k in keywords):
                print(f"    {line}", flush=True)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
