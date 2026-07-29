"""Batch smoke test reg browser mode với nhiều combo iCloud v3 (chạy tuần tự).

Vì sao cần script này:
    Process signup hiện KHÔNG tự thoát sau khi in result (browser Camoufox con
    treo) → chạy tay nhiều combo sẽ tích tụ zombie + treo. Script này:
      1. Chạy từng combo qua subprocess (`python __main__.py signup ...`).
      2. Đọc stdout tới khi thấy block result JSON (hoặc timeout).
      3. KILL subprocess + mọi process Camoufox để dọn sạch trước combo kế.
      4. Gom kết quả (success/fail/timeout + phase1_seconds + error) → in bảng
         tổng kết + ghi runtime/batch_reg_results.json.

Chạy: .venv/bin/python test/smoke_batch_reg.py
Chỉ Camoufox (engine=camoufox trong .env). Không đụng playwright-mcp (khác path).
"""
from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
RESULT_TIMEOUT = 170.0  # giây tối đa chờ 1 combo ra result trước khi bỏ
OTP_TIMEOUT = "90"

COMBOS = [
    "78.spaniel.brasher+oi0dw@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/pbxPc3d48Z0y9Z-TAiywCJJ-qAZSyrKn/data",
    "roadies.mead_6d+2te2poj@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/hbQVkqMtDmACMKuEqOUqbrXPBVk_yqzM/data",
    "themes_flies1b+ilavur@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/7QqrH0NsJFOLCmdqD6sqw3wx_OvwrgJf/data",
    "78.spaniel.brasher+6tylvq@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/Im6T3HK3FIHy7q3D0E-KEfvsvmK5UzU0/data",
    "roadies.mead_6d+s3092@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/hFDOrt1JKgV5twjmfu_0T9mKdLNiOHIv/data",
    "themes_flies1b+b9zvzlr@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/ZnxS2AN0BZW4oUBnf_IP9AQVvYO4glbq/data",
    "78.spaniel.brasher+gnv5eke@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/NydKUV80Xu1J9NxXaY16LzucF0JLSq7p/data",
    "roadies.mead_6d+hppjr@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/Rn9rs6q6X4UGJNguFOLKbDonxdYy_c_g/data",
    "themes_flies1b+uor34@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/_7VwSvhqLSLlC-FRkX8k2BIu7mMKZex9/data",
    "78.spaniel.brasher+x90dl@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/QoB-cJUoiECjktN2ZguWq-Lksp1yuKQS/data",
    "roadies.mead_6d+voumlgb@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/rPyXu4uBlTUDhdqI3iwd8eHs7NePqcwH/data",
    "themes_flies1b+1sjioq@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/cqxlefOIbmv5brkxRfiUB-D35pJ-Ykj1/data",
    "78.spaniel.brasher+01t567g@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/ZQiW2PaPwL4Z0weOX66iayCOZPrjj3Y7/data",
    "roadies.mead_6d+2yvk2wc@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/akt3sDcGIqXMPxGzHR0LcF419VclZwMp/data",
    "themes_flies1b+ervmqgo@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/GApUmNjJHN7F1dXvHPisvj5ksxq2mYiz/data",
    "78.spaniel.brasher+zujoeuc@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/HXMStv-tLwQ-mKGhjuonuC15MYsS_Hw3/data",
    "roadies.mead_6d+g4zec@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/hMFjvVv31o78B7eAbT9kb96tcbmdSo50/data",
    "themes_flies1b+hvd3ah@icloud.com|https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/BPtHY0KAmTsfZGslcn-ZhvW52vVsUQOV/data",
]


def _kill_camoufox() -> None:
    """Kill mọi Camoufox + playwright driver + python signup (KHÔNG đụng playwright-mcp)."""
    for pat in (
        "Caches/camoufox",
        "gpt_signup_hybrid/.venv/lib/python3.13/site-packages/playwright/driver",
        "__main__.py signup",
    ):
        try:
            subprocess.run(["pkill", "-9", "-f", pat], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def _extract_result(lines: list[str]) -> dict | None:
    """Trích block JSON result cuối cùng (in từ cli) từ stdout."""
    starts = [i for i, l in enumerate(lines) if l.strip() == "{"]
    if not starts:
        return None
    s = starts[-1]
    for e in range(s + 1, len(lines)):
        if lines[e].strip() == "}":
            try:
                return json.loads("".join(lines[s:e + 1]))
            except Exception:
                return None
    return None


def run_one(idx: int, total: int, combo: str) -> dict:
    email = combo.split("|", 1)[0]
    cmd = [
        PY, "-u", "__main__.py", "signup",
        "--reg-mode", "browser", "--headed", "--no-mfa",
        "--otp-timeout", OTP_TIMEOUT,
        "--icloud-v3", combo,
    ]
    print(f"\n[{idx}/{total}] ▶ {email}", flush=True)
    t0 = time.time()
    _env = dict(os.environ, PYTHONUNBUFFERED="1")
    p = subprocess.Popen(
        cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=_env,
    )
    lines: list[str] = []
    status = "TIMEOUT"
    try:
        while time.time() - t0 < RESULT_TIMEOUT:
            rlist, _, _ = select.select([p.stdout], [], [], 1.0)
            if rlist:
                line = p.stdout.readline()
                if line == "" and p.poll() is not None:
                    break
                if not line:
                    continue
                lines.append(line)
                if '"success":' in line:
                    status = "DONE"
                    # đọc nốt phần JSON còn lại (grace 5s)
                    g = time.time() + 5.0
                    while time.time() < g:
                        r2, _, _ = select.select([p.stdout], [], [], 0.5)
                        if not r2:
                            continue
                        l2 = p.stdout.readline()
                        if not l2:
                            break
                        lines.append(l2)
                        if l2.strip() == "}":
                            break
                    break
            elif p.poll() is not None:
                break
    finally:
        try:
            p.send_signal(signal.SIGKILL)
        except Exception:
            pass
        try:
            p.wait(timeout=5)
        except Exception:
            pass
        _kill_camoufox()
        time.sleep(2.5)

    elapsed = time.time() - t0
    res = _extract_result(lines)
    rec: dict = {"email": email, "elapsed": round(elapsed, 1)}
    if res is not None:
        rec["success"] = bool(res.get("success"))
        rec["phase1"] = res.get("phase1_seconds")
        rec["error"] = res.get("error")
        rec["user_id"] = res.get("user_id")
        tag = "✅ SUCCESS" if rec["success"] else "❌ FAIL"
        extra = "" if rec["success"] else f" — {(res.get('error') or '')[:90]}"
        p1 = res.get("phase1_seconds") or 0.0
        print(f"[{idx}/{total}] {tag} {email} (phase1={p1:.0f}s, wall={elapsed:.0f}s){extra}", flush=True)
    else:
        rec["success"] = None
        rec["error"] = "no result captured (timeout/hang)"
        print(f"[{idx}/{total}] ⏱ TIMEOUT {email} (wall={elapsed:.0f}s, không bắt được result)", flush=True)
    return rec


def main() -> int:
    _kill_camoufox()
    time.sleep(1)
    total = len(COMBOS)
    print(f"=== BATCH REG TEST: {total} combo (Camoufox, tuần tự, --no-mfa, otp-timeout={OTP_TIMEOUT}s) ===", flush=True)
    results: list[dict] = []
    t_all = time.time()
    for i, combo in enumerate(COMBOS, 1):
        results.append(run_one(i, total, combo))

    ok = sum(1 for r in results if r.get("success") is True)
    fail = sum(1 for r in results if r.get("success") is False)
    timeout = sum(1 for r in results if r.get("success") is None)

    print("\n" + "=" * 64, flush=True)
    print(f"TỔNG KẾT: {ok} success / {fail} fail / {timeout} timeout  (tổng {time.time() - t_all:.0f}s)", flush=True)
    print("=" * 64, flush=True)
    for r in results:
        tag = {True: "✅", False: "❌", None: "⏱"}[r.get("success")]
        p1 = r.get("phase1") or 0.0
        line = f"  {tag} {r['email']:<48} phase1={p1:>5.0f}s wall={r['elapsed']:>5.0f}s"
        if r.get("success") is not True and r.get("error"):
            line += f"  | {r['error'][:80]}"
        print(line, flush=True)

    out = ROOT / "runtime" / "batch_reg_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[batch] kết quả chi tiết → {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
