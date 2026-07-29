"""Phase 4 verify — pure_request optimize.

Chạy: .venv/bin/python3 test/check_request_phase_p4.py

Tasks verified:
    4.1 — _step_signup deprecated (still exists, has WARNING log)
    4.2 — Visit /email-verification HTML thay /create-account/password
    4.3 — _step_send_otp dùng _navigate_headers (Sec-Fetch-Mode=navigate)
    4.4 — _common_headers persona-aware (CHROME_145_WIN default)
    4.6 — _step_auth_url đọc oai-asli cookie cho auth_session_logging_id
"""
from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    failures: list[str] = []

    # ── AST parse first (catch syntax errors) ──
    src = (ROOT / "request_phase.py").read_text(encoding="utf-8")
    try:
        ast.parse(src)
        print("[PASS] request_phase.py AST parse OK")
    except SyntaxError as e:
        print(f"[FAIL] AST parse error: {e}")
        return 1

    # Import module
    import request_phase

    # ── TC-01 (Task 4.4) — _common_headers persona-aware ──
    sig_common = inspect.signature(request_phase._common_headers)
    if "persona" in sig_common.parameters:
        param = sig_common.parameters["persona"]
        if param.kind == inspect.Parameter.KEYWORD_ONLY and param.default is None:
            print("[PASS] Task 4.4 — _common_headers(persona=None) keyword-only")
        else:
            failures.append(
                f"_common_headers persona param wrong kind/default"
            )
    else:
        failures.append("_common_headers missing persona param")

    # Default behavior — Chrome persona
    h = request_phase._common_headers("https://chatgpt.com/")
    expected_chrome_keys = {"User-Agent", "sec-ch-ua", "sec-ch-ua-mobile",
                            "sec-ch-ua-platform", "Accept", "Origin"}
    missing_chrome = expected_chrome_keys - set(h.keys())
    if not missing_chrome:
        print("[PASS] _common_headers default (Chrome) → 3 sec-ch-ua + UA + Accept + Origin")
    else:
        failures.append(f"missing Chrome keys: {missing_chrome}")

    # Firefox persona — KHÔNG có sec-ch-ua
    from user_agent_profile import FIREFOX_135_MAC
    h_ff = request_phase._common_headers(
        "https://chatgpt.com/", persona=FIREFOX_135_MAC,
    )
    if "sec-ch-ua" not in h_ff:
        print("[PASS] _common_headers(Firefox) — KHÔNG gửi sec-ch-ua (đặc trưng)")
    else:
        failures.append("_common_headers(Firefox) vẫn gửi sec-ch-ua")

    # Datadog headers always present
    if "traceparent" in h:
        print("[PASS] _common_headers includes Datadog traceparent")
    else:
        failures.append("_common_headers missing Datadog traceparent")

    # ── TC-02 (Task 4.3) — _navigate_headers exists ──
    if hasattr(request_phase, "_navigate_headers"):
        nav_h = request_phase._navigate_headers("https://auth.openai.com/")
        if nav_h.get("Sec-Fetch-Mode") == "navigate" \
                and nav_h.get("Sec-Fetch-Dest") == "document" \
                and nav_h.get("Upgrade-Insecure-Requests") == "1" \
                and nav_h.get("Accept", "").startswith("text/html"):
            print("[PASS] Task 4.3 — _navigate_headers (Sec-Fetch-Mode=navigate)")
        else:
            failures.append(f"_navigate_headers wrong: {nav_h}")
    else:
        failures.append("_navigate_headers missing")

    # ── TC-03 (Task 4.3) — _step_send_otp dùng _navigate_headers ──
    send_otp_src = inspect.getsource(request_phase._step_send_otp)
    if "_navigate_headers" in send_otp_src and "allow_redirects=True" in send_otp_src:
        print("[PASS] Task 4.3 — _step_send_otp dùng _navigate_headers + follow 302")
    else:
        failures.append("_step_send_otp chưa dùng _navigate_headers / follow redirect")

    # ── TC-04 (Task 4.2) — visit /email-verification HTML thay /create-account/password ──
    sync_src = inspect.getsource(request_phase._run_request_phase_sync)
    if "/email-verification" in sync_src \
            and "_navigate_headers" in sync_src:
        print("[PASS] Task 4.2 — sync flow visit /email-verification HTML (page navigate)")
    else:
        failures.append("Task 4.2 chưa wire /email-verification visit")

    # KHÔNG còn /create-account/password GET trong sync flow
    if 'GET /create-account/password' in sync_src or \
            '/create-account/password",' in sync_src:
        # OK — vẫn có thể có là Referer string. Check session.get specific
        if 'session.get(\n                    "https://auth.openai.com/create-account/password"' in sync_src:
            failures.append("Task 4.2 vẫn còn session.get /create-account/password")
        else:
            print("[PASS] Task 4.2 — KHÔNG còn session.get /create-account/password (chỉ Referer string)")
    else:
        print("[PASS] Task 4.2 — clean (no /create-account/password GET)")

    # ── TC-05 (Task 4.6) — _step_auth_url đọc oai-asli ──
    auth_url_src = inspect.getsource(request_phase._step_auth_url)
    if "read_oai_asli_from_session" in auth_url_src \
            and "auth_session_logging_id" in auth_url_src:
        print("[PASS] Task 4.6 — _step_auth_url đọc oai-asli → auth_session_logging_id")
    else:
        failures.append("Task 4.6 chưa wire oai-asli")

    # ── TC-06 (Task 4.1 + Phase 7 Task 7.1) — _step_signup XÓA HẲN ──
    # Phase 4 marked deprecated, Phase 7 xóa hẳn (no caller).
    if hasattr(request_phase, "_step_signup"):
        failures.append("Task 7.1 — _step_signup phải xóa hẳn (Phase 7)")
    else:
        print("[PASS] Task 7.1 — _step_signup deleted (was deprecated Phase 4)")

    # _run_request_phase_sync KHÔNG gọi _step_signup
    if "_step_signup(" in sync_src:
        failures.append("_run_request_phase_sync vẫn gọi _step_signup (deleted)")
    else:
        print("[PASS] Task 4.1 — sync flow KHÔNG gọi _step_signup deleted")

    # _step_authorize_continue vẫn còn (cho session_phase login)
    if hasattr(request_phase, "_step_authorize_continue"):
        print("[PASS] Task 4.1 — _step_authorize_continue giữ (cho session_phase login)")

    # ── TC-07 BrowserPersona import ──
    if "BrowserPersona" in src:
        print("[PASS] request_phase imports BrowserPersona")
    else:
        failures.append("request_phase missing BrowserPersona import")

    print()
    if failures:
        print(f"[FAIL] {len(failures)} failures:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("[OK] All Phase 4 invariants pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
