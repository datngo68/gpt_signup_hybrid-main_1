"""Isolate: import web.mail_modes + build 1 icloud_v3 request với mfa_inline."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

print("[1] import web.mail_modes ...", flush=True)
from web.mail_modes import get_spec
print("[1] OK", flush=True)

print("[2] get_spec(icloud_v3) ...", flush=True)
spec = get_spec("icloud_v3")
print("[2] OK", flush=True)

LINE = (
    "blog.pod_36+8pjb9p@icloud.com|"
    "https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/readmail/rbH7JDLf1uMxsEpqQfuQ-9GclVPddc2M/data"
)
print("[3] parse_line + build_request ...", flush=True)
parsed = spec.parse_line(LINE)
req = spec.build_request(parsed, password="Autogen#2026Xy", headless=True, proxy=None, reg_mode="hybrid")
print(f"[3] OK reg_mode={req.reg_mode} mfa_inline={req.mfa_inline} url={'SET' if req.icloud_v3_url else 'MISSING'}", flush=True)

print("[4] model_copy update mfa_inline ...", flush=True)
if not req.mfa_inline:
    req = req.model_copy(update={"mfa_inline": True})
print(f"[4] OK mfa_inline={req.mfa_inline}", flush=True)
print("ALL OK", flush=True)
