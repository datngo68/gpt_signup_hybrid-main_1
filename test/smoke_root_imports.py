#!/usr/bin/env python3
"""Smoke test: import mọi root + sub-package module sau khi convert
relative→absolute. Phát hiện sớm:
  - syntax error (đã không còn vì AST parse OK trong migrate, nhưng vẫn double check)
  - missing name khi resolve absolute (vd module tên trùng stdlib)
  - circular import bị ảnh hưởng bởi đổi resolution order

Mỗi import in [PASS]/[FAIL] ngay khi xong (flush=True) để xác định bước kẹt.

Run: python3 test/smoke_root_imports.py
Exit 0 nếu tất cả pass, exit 1 nếu có FAIL.
"""
from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Inject root vào sys.path để absolute import resolve.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Thứ tự không quan trọng — Python tự handle dependency. Chỉ cần list đầy đủ.
# Loại trừ các module có side-effect mạnh (vd kéo Playwright/uvicorn) có thể
# fail môi trường — nhưng đây là spec target nên vẫn phải import OK trên Mac
# nếu requirements.txt đã cài.
ROOT_MODULES = [
    "_browser_retry",
    "_expire_check",
    "_nextauth_bootstrap",
    "browser_phase",
    "cli",
    "config",
    "http_phase",
    "mail_providers",
    "mfa_phase",
    "models",
    "outlook_pool",
    "pay_upi_http",
    "payment_link",
    "random_profile",
    "record_india",
    "record_pay_upi",
    "request_phase",
    "sentinel_pow",
    "sentinel_quickjs",
    "session_phase",
    "signup",
    "stripe_token",
    "totp_helper",
    "user_agent_profile",
    "web_recorder",
]

SUB_MODULES = [
    "autoreg",
    "autoreg.runner",
    "autoreg.schemas",
    "codex_auth",
    "codex_auth.browser",
    "codex_auth.errors",
    "codex_auth.oauth",
    "codex_auth.pkce",
    "codex_auth.runner",
    "db",
    "db.engine",
    "db.migrate",
    "db.repositories",
    "db.schema",
    "icloud_hme",
    "icloud_hme.add_profile",
    "icloud_hme.bootstrap",
    "icloud_hme.checker",
    "icloud_hme.cli",
    "icloud_hme.client",
    "icloud_hme.exceptions",
    "icloud_hme.generator",
    "icloud_hme.manager",
    "icloud_hme.models",
    "icloud_hme.open_profile",
    "icloud_hme.pool",
    "icloud_hme.profile_lock",
    "icloud_hme.recorder",
    "icloud_hme.repository",
    "icloud_hme.runner",
    "icloud_hme.runner_lock",
    "icloud_hme.session",
    "icloud_hme.web",
    "icloud_hme.web.auth",
    "icloud_hme.web.log_buffer",
    "icloud_hme.web.router",
    "icloud_hme.web.schemas",
    "web",
    "web.auth",
    "web.icloud_routes",
    "web.mail_modes",
    "web.manager",
    "web.proxy_format",
    "web.proxy_health",
    "web.proxy_pool",
    "web.runner_config_store",
    "web.server",
    "web.sse_mux",
    "web.telegram_notifier",
    "web.upi_runner",
]

ALL_MODULES = ROOT_MODULES + SUB_MODULES


def main() -> int:
    failed: list[tuple[str, str]] = []
    total = len(ALL_MODULES)
    for i, name in enumerate(ALL_MODULES, 1):
        try:
            importlib.import_module(name)
            print(f"[PASS] [{i:3}/{total}] {name}", flush=True)
        except Exception as e:  # noqa: BLE001 — smoke test bắt mọi exception
            tb = traceback.format_exc(limit=3)
            print(f"[FAIL] [{i:3}/{total}] {name} :: {type(e).__name__}: {e}",
                  flush=True)
            print(tb, flush=True)
            failed.append((name, f"{type(e).__name__}: {e}"))

    print("", flush=True)
    print(f"=== Smoke import: {total - len(failed)}/{total} pass ===", flush=True)
    if failed:
        print("Failed modules:", flush=True)
        for name, err in failed:
            print(f"  {name}: {err}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
