"""Hybrid ChatGPT signup: browser tới hết Sentinel + curl_cffi cho OTP/create_account.

Flow:
    Phase 1 (browser, ~5s): GET /api/auth/signin/openai?login_hint=<email> →
        auth.openai.com/email-verification → đợi sentinel SDK fire (cookie oai-sc).
    Phase 2 (HTTP, ~1s):    POST /api/accounts/email-otp/validate (poll OTP từ Worker logs)
                            → POST /api/accounts/create_account
                            → GET  /api/auth/callback/openai → __Secure-next-auth.session-token
"""
import sys
from pathlib import Path

__version__ = "3.5.0"

_PACKAGE_DIR = Path(__file__).resolve().parent
if str(_PACKAGE_DIR) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_DIR))

from models import SignupRequest, SignupResult, BrowserHandoff
from signup import run_signup

__all__ = ["__version__", "SignupRequest", "SignupResult", "BrowserHandoff", "run_signup"]
