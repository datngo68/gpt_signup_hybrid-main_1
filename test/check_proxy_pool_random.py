"""Chẩn đoán bug 'random nhưng các tk ra cùng 1 proxy'.

Đọc proxy.pool + proxy.rotation_mode thực tế từ Settings Store (DB), rồi:
1. In 3 dòng proxy đã MASK + host:port của từng dòng -> phát hiện 'cùng gateway'.
2. Chạy ProxyPool.ordered_for_job() N lần (mô phỏng N job UPI) -> in first_proxy
   mỗi lần + thống kê phân phối. Nếu phân phối lệch hẳn về 1 host -> bug code.
   Nếu 3 host vốn giống nhau -> không phải bug code (proxy config trùng gateway).

Chỉ đọc, không ghi DB. Flush từng dòng để theo dõi realtime.
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("GSH_DB_PATH", str(ROOT / "runtime" / "data.db"))


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    from db import get_engine
    from db.repositories import SettingsRepository
    from web.proxy_format import mask_proxy, materialize_proxy
    from web.proxy_pool import ProxyPool, normalize_proxies

    engine = get_engine()
    settings = SettingsRepository(engine)

    raw_pool = settings.get("proxy.pool")
    mode = settings.get("proxy.rotation_mode") or "round_robin"

    log(f"[info] proxy.rotation_mode = {mode!r}")
    if not raw_pool:
        log("[FAIL] proxy.pool rỗng/None trong DB -> không có gì để random.")
        return 1

    entries = normalize_proxies(raw_pool)
    log(f"[info] proxy.pool raw count={len(raw_pool)}  normalized count={len(entries)}")
    log("")
    log("=== Từng dòng proxy (masked) + host:port ===")
    hosts = []
    for i, line in enumerate(entries):
        try:
            mat = materialize_proxy(line)
        except Exception as exc:  # noqa: BLE001
            log(f"  [{i}] MASK={mask_proxy(line)}  materialize_ERROR={type(exc).__name__}: {exc}")
            continue
        # host:port từ URL materialized
        host_part = mat.split("://", 1)[-1].rsplit("@", 1)[-1]
        hosts.append(host_part)
        log(f"  [{i}] MASK={mask_proxy(line)}  ->  host:port={host_part}")

    uniq_hosts = set(hosts)
    log("")
    log(f"[info] số host:port DUY NHẤT = {len(uniq_hosts)}  -> {sorted(uniq_hosts)}")
    if len(uniq_hosts) <= 1:
        log("[KẾT LUẬN] 3 dòng proxy CÙNG 1 host:port (gateway). 'random' giữa chúng")
        log("           vô nghĩa về IP; IP đổi phải nhờ {SID}. => KHÔNG phải bug code.")

    # Mô phỏng N job: mỗi job 1 ProxyPool.ordered_for_job() rồi lấy [0].
    log("")
    log("=== Mô phỏng 30 job: first_proxy = ordered_for_job()[0] (host:port) ===")
    pool = ProxyPool()
    pool.configure(entries, mode=mode)
    first_hosts: Counter[str] = Counter()
    prev_line: str | None = None
    consecutive_repeats = 0
    for n in range(1, 31):
        ordered = pool.ordered_for_job()
        if not ordered:
            log(f"  [{n:02d}/30] ordered rỗng")
            continue
        cur_line = ordered[0]
        mat = materialize_proxy(cur_line)
        host_part = mat.split("://", 1)[-1].rsplit("@", 1)[-1]
        first_hosts[host_part] += 1
        repeat_flag = ""
        if prev_line is not None and cur_line == prev_line:
            consecutive_repeats += 1
            repeat_flag = "  <-- TRÙNG job trước!"
        prev_line = cur_line
        log(f"  [{n:02d}/30] first host:port = {host_part}{repeat_flag}")

    log("")
    log("=== Phân phối first host:port qua 30 job ===")
    for host, cnt in first_hosts.most_common():
        log(f"  {host:<28} : {cnt}")

    log("")
    log(f"[info] số lần 2 job LIÊN TIẾP trùng proxy = {consecutive_repeats}")
    if len(uniq_hosts) > 1 and consecutive_repeats > 0:
        log("[FAIL] vẫn còn job liên tiếp trùng IP -> no-repeat chưa hiệu lực.")
        return 3

    # ── Mô phỏng job SONG SONG qua acquire()/release() (least-used) ──────
    log("")
    log("=== Mô phỏng job SONG SONG (acquire không release) — least-used ===")
    n_live = len(entries)

    def host_of(line: str) -> str:
        mat = materialize_proxy(line)
        return mat.split("://", 1)[-1].rsplit("@", 1)[-1]

    # Case 1: số job song song = số proxy -> mỗi job 1 IP riêng.
    pool2 = ProxyPool()
    pool2.configure(entries, mode=mode)
    held = [pool2.acquire() for _ in range(n_live)]
    hosts_held = [host_of(x) for x in held if x]
    log(f"  [{n_live} job // = {n_live} proxy] hosts = {hosts_held}")
    if len(set(hosts_held)) == n_live:
        log(f"  [PASS] {n_live} job song song -> {n_live} IP DISTINCT (không trùng).")
    else:
        log("  [FAIL] job song song <= pool size mà vẫn trùng IP!")
        return 4

    # Case 2: gấp đôi số proxy job song song -> mỗi IP dùng đúng 2 lần (đều).
    pool3 = ProxyPool()
    pool3.configure(entries, mode=mode)
    held2 = [pool3.acquire() for _ in range(n_live * 2)]
    dist = Counter(host_of(x) for x in held2 if x)
    log(f"  [{n_live * 2} job // = 2x proxy] phân phối = {dict(dist)}")
    spread = max(dist.values()) - min(dist.values())
    if spread <= 1:
        log("  [PASS] vượt pool size -> rải đều, chênh lệch tải <= 1 (tối ưu).")
    else:
        log(f"  [FAIL] tải lệch {spread} > 1 -> least-used chưa tối ưu.")
        return 5

    # Case 3: release rồi acquire lại -> lease count về 0 đúng.
    for x in held2:
        pool3.release(x)
    after = pool3.acquire()
    log(f"  [release-all + acquire] lease mới host = {host_of(after) if after else None}")
    log("  [PASS] release/acquire vòng đời lease OK.")

    if len(uniq_hosts) > 1 and len(first_hosts) <= 1:
        log("")
        log("[FAIL] >1 host trong pool nhưng 30 job đều ra CÙNG 1 host -> BUG code random.")
        return 2

    log("")
    log("[OK] check xong.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
