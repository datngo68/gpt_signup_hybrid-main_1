"""reg_hybrid — `reg_mode="hybrid"` runner.

Pipeline kiểu chatgpt_camoufox: pure-HTTP qua curl_cffi impersonate Firefox + Camoufox
chỉ làm oracle mint sentinel `{p,t,c}` + `so` qua sdk.js gốc. Không drive UI form, không
QuickJS/PoW fallback. So với mode ``browser`` (full Camoufox UI) và ``pure_request``
(QuickJS sentinel), mode này là điểm cân bằng: sentinel quality bằng manual user
(sdk.js chạy trong Firefox thật) + tốc độ HTTP-only + footprint thấp.

Adapter để package ``chatgpt_camoufox`` (đang được giữ nguyên ở thư mục cùng tên)
ăn khớp với:
  - SignupRequest / SignupResult (Pydantic) của repo
  - MailProvider async (worker / outlook / gmail_advanced / dongvanfb / china_icloud)
  - Settings Store + persona registry trong ``user_agent_profile``

Public API:
    run_hybrid_signup(request, mail_provider, log, on_checkpoint) -> SignupResult

Layout:
    reg_hybrid/
        __init__.py           # re-export public API
        runner.py             # orchestrator (chạy ChatGPTRelay trong thread)
        mail_adapter.py       # MailProvider async → OTPReader sync bridge
        camoufox_factory.py   # build FirefoxProfile + CamoufoxTokenGenerator
"""
from __future__ import annotations

from .runner import HybridSignupError, run_hybrid_signup

__all__ = ["run_hybrid_signup", "HybridSignupError"]
