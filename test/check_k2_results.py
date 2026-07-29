"""Phase 11.4 — Summary of K2 real-world test results.

Parses recent log files in test/ for K2 success/fail markers and reports.

Run: python3 test/check_k2_results.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST = ROOT / "test"

# Patterns
K2_OK = re.compile(r"\[sentinel\] K2 page-form intercept OK \(len=(\d+)")
K2_SYNTH = re.compile(r"\[sentinel\] K2 synthesized auth_session_logging_id=")
K2_FAIL_TOK = re.compile(r"sentinel-token not in captured headers")
K2_FAIL_ROUTE = re.compile(r"route never fired in \d+s \(no POST /register seen\)")
K2_FAIL_PWD = re.compile(r"no password input on")
K2_FAIL_BTN = re.compile(r"'Continue with password' button never visible")
K2_FAIL_SUBMIT = re.compile(r"could not submit form")
QUICKJS_OK = re.compile(r"\[sentinel\] QuickJS OK \(p=(\d+)")
SUCCESS = re.compile(r'"success":\s*true')
USER_ID = re.compile(r'"user_id":\s*"(user-[^"]+)"')

def main() -> int:
    # Collect log files from this run (recent k2v* files only)
    candidates = sorted(TEST.glob("_k2*.log"))
    if not candidates:
        print("No K2 log files found")
        return 1

    rows: list[dict] = []
    for path in candidates:
        text = path.read_text(encoding="utf-8", errors="ignore")

        k2_ok_m = K2_OK.search(text)
        k2_ok = bool(k2_ok_m)
        token_len = int(k2_ok_m.group(1)) if k2_ok_m else 0

        synthesized = bool(K2_SYNTH.search(text))

        fail_reasons = []
        for pat, label in (
            (K2_FAIL_TOK, "no-token-header"),
            (K2_FAIL_ROUTE, "route-never-fired"),
            (K2_FAIL_PWD, "no-pwd-input"),
            (K2_FAIL_BTN, "no-pwd-btn"),
            (K2_FAIL_SUBMIT, "no-submit"),
        ):
            if pat.search(text):
                fail_reasons.append(label)

        success = bool(SUCCESS.search(text))
        user_m = USER_ID.search(text)
        user_id = user_m.group(1) if user_m else ""

        rows.append({
            "log": path.name,
            "k2_ok": k2_ok,
            "token_len": token_len,
            "synthesized": synthesized,
            "fail_reasons": fail_reasons,
            "e2e_success": success,
            "user_id": user_id,
        })

    # Summary
    total = len(rows)
    k2_count = sum(1 for r in rows if r["k2_ok"])
    e2e_count = sum(1 for r in rows if r["e2e_success"])

    print(f"=== K2 Phase 11.4 Real-World Results ({total} runs) ===\n")
    for r in rows:
        status = "✓" if r["e2e_success"] else "✗"
        k2_status = "K2-OK" if r["k2_ok"] else "K2-FAIL"
        synth = "synth-asli" if r["synthesized"] else ""
        token_info = f"len={r['token_len']}" if r["token_len"] else ""
        fail = ",".join(r["fail_reasons"]) if r["fail_reasons"] else ""
        line = f"{status} {r['log']:36s} {k2_status:8s} {token_info:10s} {synth:12s}"
        if fail:
            line += f" [{fail}]"
        if r["user_id"]:
            line += f" → {r['user_id']}"
        print(line)

    print()
    print(f"K2 intercept success: {k2_count}/{total} ({100*k2_count/total:.0f}%)")
    print(f"E2E signup success:   {e2e_count}/{total} ({100*e2e_count/total:.0f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
