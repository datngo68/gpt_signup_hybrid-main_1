"""Check format log mới của upi_runner — gọi từng helper, in mẫu output.

Chạy: python3 test/check_upi_log_format.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load_module(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


u = _load_module("upi_runner_format", "web/upi_runner.py")
fmt_step = u._fmt_step
fmt_attempt = u._fmt_attempt
fmt_kv = u._fmt_kv
short = u._short

print("=== Demo log mẫu cho 1 job UPI thành công ===\n", flush=True)

# Header
print("[02:54:30]", fmt_step("upi", "account", "info", "ab***@nik.edu.pl  proxy_pool=3"))
print("[02:54:30]", fmt_step("upi", "config", "info", fmt_kv(
    ("approve_retries", 500), ("delay", "3s"), ("batch", 3),
    ("be_consec", 5), ("variants", "qr_code,empty,flow_qr,intent")
)))

# Step 1: login
print("[02:54:30]", fmt_step("1/6", "login", "start", "pure-HTTP request_phase"))
print("[02:54:34]", fmt_step("1/6", "login", "ok", "user=ab***@nik.edu.pl"))

# Step 2: checkout
print("[02:54:34]", fmt_step("2/6", "checkout", "ok",
                              f"cs={short('cs_live_a1gMta6DMF', 14)}  ui=custom"))

# Step 3: init
print("[02:54:34]", fmt_step("3/6", "init", "ok",
                              f"amount=2000  ppage={short('ppage_live_xxx', 12)}"))

# Step 4: elements
print("[02:54:34]", fmt_step("4/6", "elements", "ok",
                              f"session={short('es_live_abcdefgh', 14)}"))

# Step 5a: token-config
print("[02:54:34]", fmt_step("5a", "token-config", "ok", "shift=11  rv=e5ebd5e1"))

# Step 5b: confirm variants
print("[02:54:35]", fmt_step("5b", "confirm", "fail", "variant=qr_code  http=400  err=invalid_request"))
print("[02:54:35]", fmt_step("5b", "confirm", "ok",   "variant=empty  http=200"))

# Step 5c: page-refresh
print("[02:54:35]", fmt_step("5c", "page-refresh", "ok", "http=200  proxy=direct"))

# Step 6: approve loop
print("[02:54:35]", fmt_step("6/6", "approve loop", "start",
                              "retries=500  delay=3s  batch=3"))
for i, (http, result) in enumerate([
    (403, "unknown"), (403, "unknown"), (200, "blocked"),
    (200, "exception"), (200, "approved"),
], start=1):
    print(f"[02:54:{34+i*3:02d}]",
          fmt_attempt(idx=i, total=500, http_status=http, result=result,
                      proxy_mask="103.116.38.17:8003"))
print("[02:54:49]", fmt_step("6/6", "approve", "ok", "approved at 5/500  (14.0s)"))
print("[02:54:49]", fmt_step("5c", "page-refresh", "ok", "http=200  proxy=direct"))

# QR + Telegram
print("[02:54:49]", fmt_step("qr", "saved", "ok", "source=stripe_image  expires_at=1781622292"))
print("[02:54:49]", "[tg]   sent            ✓  QR + combo (spoiler+code)")

# Final
print("[02:54:50]", fmt_step("upi", "done", "ok", "qr=yes  total=20.0s"))

print("\n=== OK ===", flush=True)
