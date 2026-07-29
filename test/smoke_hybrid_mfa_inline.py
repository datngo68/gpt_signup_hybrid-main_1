"""Smoke test: hybrid mode enable 2FA inline + xuất combo file.

Test cases (không launch Camoufox / curl thực):
    [1/4] runner gọi `enable_2fa_in_session` với (session, access_token, ua, activate=True)
          khi `request.mfa_inline=True` và reg success.
    [2/4] runner KHÔNG gọi `enable_2fa_in_session` khi `request.mfa_inline=False`.
    [3/4] `MfaError` từ enable_2fa_in_session → set `result.two_factor_partial`
          (KHÔNG fail reg — account đã create).
    [4/4] CLI signup_cmd có flag `--mfa/--no-mfa` (typer option) và logic append
          accounts.txt khi 2FA activated.

Chạy:
    .venv/bin/python test/smoke_hybrid_mfa_inline.py
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import threading
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _check(name: str, fn) -> bool:
    try:
        fn()
        print(f"[PASS] {name}", flush=True)
        return True
    except AssertionError as exc:
        print(f"[FAIL] {name} — {exc}", flush=True)
        return False
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"[FAIL] {name} — {type(exc).__name__}: {exc}", flush=True)
        return False


def _make_request(*, mfa_inline: bool):
    from models import SignupRequest

    return SignupRequest(
        email="test@example.com",
        password="testpass1234",
        reg_mode="hybrid",
        name="Test User",
        birthdate="2000-01-01",
        locale="en-US",
        proxy=None,
        headless=True,
        tls_insecure=False,
        otp_timeout_seconds=180.0,
        otp_poll_interval_seconds=4.0,
        mfa_inline=mfa_inline,
    )


class _FakeProfile:
    user_agent = "Mozilla/5.0 (Windows NT 10.0; rv:135.0) Gecko/20100101 Firefox/135.0"
    impersonate = "firefox135"
    platform = "Windows"
    language = "en-US"


class _FakeSession:
    def close(self) -> None: ...


class _FakeTokens:
    def mint_token(self, flow: str) -> str:
        return f"tok-{flow}"

    def mint_so(self, flow: str) -> str:
        return f"so-{flow}"

    def close(self) -> None: ...


class _FakeRelayResult:
    def __init__(self) -> None:
        self.session_json = {"accessToken": "fake-jwt-token", "user": {"id": "u-1"}}
        self.device_id = "dev-1"
        self.cookies = {"_account": "acc-1", "__Secure-next-auth.session-token": "tok-1"}
        self.steps = ["GET /csrf -> 200"]


class _FakeRelay:
    """Stub HybridChatGPTRelay — bypass curl/Camoufox."""
    def __init__(self, **_kw) -> None:
        self._jar = []
        self.session = _FakeSession()

    def run(self) -> _FakeRelayResult:
        return _FakeRelayResult()


class _FakeMailProvider:
    async def poll_otp(self, **_kw) -> str:
        return "123456"


def _stub_build_pipeline(request, *, firefox_major, log):
    """Mô phỏng `_build_pipeline` — trả profile + session + tokens + captcha + account."""
    return (_FakeProfile(), _FakeSession(), _FakeTokens(), None, None)


def _patch_runner_imports(mfa_callable):
    """Common patch để chạy run_hybrid_signup mà không touch Camoufox/curl."""
    # Patch mfa_phase.enable_2fa_in_session với callable mock.
    sys.modules.setdefault("mfa_phase", type(sys)("mfa_phase"))
    sys.modules["mfa_phase"].enable_2fa_in_session = mfa_callable

    # Đảm bảo MfaError class tồn tại (runner import).
    class _MfaError(Exception):
        def __init__(self, msg, *, partial_state=None):
            super().__init__(msg)
            self.partial_state = partial_state
    sys.modules["mfa_phase"].MfaError = _MfaError
    return _MfaError


def tc1_mfa_inline_called() -> bool:
    captured = {"called": False, "args": None, "kwargs": None}

    def _stub_enable_2fa(session, *, access_token, user_agent, activate, log):
        captured["called"] = True
        captured["args"] = (session,)
        captured["kwargs"] = {
            "access_token": access_token,
            "user_agent": user_agent,
            "activate": activate,
        }
        return {
            "secret": "B2P3OQCCXINLHGPUDIS55DHQDW5MENK5",
            "factor_id": "fac-1",
            "session_id": "ses-1",
            "provisioning_uri": "otpauth://totp/...",
            "first_code": "123456",
            "activated": True,
            "mfa_info": {},
        }

    async def _run():
        _patch_runner_imports(_stub_enable_2fa)
        with patch("reg_hybrid.runner._build_pipeline", _stub_build_pipeline), \
             patch("reg_hybrid.runner._patch_tokens_cache",
                   lambda *_a, **_kw: {}), \
             patch("reg_hybrid.relay.HybridChatGPTRelay", _FakeRelay):
            from reg_hybrid.runner import run_hybrid_signup, wait_pending_cleanups
            res = await run_hybrid_signup(
                _make_request(mfa_inline=True),
                mail_provider=_FakeMailProvider(),
                log=lambda _m: None,
            )
            await wait_pending_cleanups(timeout=2.0)
            return res

    def _go():
        result = asyncio.run(_run())
        assert result.success, f"reg fail: {result.error}"
        assert captured["called"], "enable_2fa_in_session KHÔNG được gọi khi mfa_inline=True"
        assert captured["kwargs"]["access_token"] == "fake-jwt-token", (
            f"access_token wrong: {captured['kwargs']['access_token']!r}"
        )
        assert "Firefox/135" in captured["kwargs"]["user_agent"], (
            f"user_agent wrong: {captured['kwargs']['user_agent']!r}"
        )
        assert captured["kwargs"]["activate"] is True, "activate phải = True"
        assert result.two_factor is not None, "result.two_factor phải set"
        assert result.two_factor["activated"] is True, (
            f"two_factor.activated phải True: {result.two_factor}"
        )
        assert result.two_factor["secret"] == "B2P3OQCCXINLHGPUDIS55DHQDW5MENK5", (
            f"secret wrong: {result.two_factor['secret']}"
        )

    return _check("[1/4] mfa_inline=True → enable_2fa_in_session gọi + result.two_factor set", _go)


def tc2_mfa_inline_disabled() -> bool:
    captured = {"called": False}

    def _stub_enable_2fa(*a, **kw):
        captured["called"] = True
        return {}

    async def _run():
        _patch_runner_imports(_stub_enable_2fa)
        with patch("reg_hybrid.runner._build_pipeline", _stub_build_pipeline), \
             patch("reg_hybrid.runner._patch_tokens_cache",
                   lambda *_a, **_kw: {}), \
             patch("reg_hybrid.relay.HybridChatGPTRelay", _FakeRelay):
            from reg_hybrid.runner import run_hybrid_signup, wait_pending_cleanups
            res = await run_hybrid_signup(
                _make_request(mfa_inline=False),
                mail_provider=_FakeMailProvider(),
                log=lambda _m: None,
            )
            await wait_pending_cleanups(timeout=2.0)
            return res

    def _go():
        result = asyncio.run(_run())
        assert result.success, f"reg fail: {result.error}"
        assert not captured["called"], (
            "enable_2fa_in_session BỊ GỌI dù mfa_inline=False — opt-out broken"
        )
        assert result.two_factor is None, (
            f"two_factor phải None khi mfa_inline=False: {result.two_factor}"
        )

    return _check("[2/4] mfa_inline=False → SKIP enable_2fa", _go)


def tc3_mfa_partial_state() -> bool:
    """Enroll OK + activate fail → result.two_factor_partial set, reg vẫn success."""
    PARTIAL = {
        "secret": "PARTIALSECRETABCDEFGHIJKLMNOPQRSTU",
        "factor_id": "fac-partial",
        "session_id": "ses-partial",
    }

    def _stub_enable_2fa(session, **kw):
        MfaError = sys.modules["mfa_phase"].MfaError
        raise MfaError(
            "activate failed HTTP 403: cloudflare",
            partial_state=PARTIAL,
        )

    async def _run():
        _patch_runner_imports(_stub_enable_2fa)
        with patch("reg_hybrid.runner._build_pipeline", _stub_build_pipeline), \
             patch("reg_hybrid.runner._patch_tokens_cache",
                   lambda *_a, **_kw: {}), \
             patch("reg_hybrid.relay.HybridChatGPTRelay", _FakeRelay):
            from reg_hybrid.runner import run_hybrid_signup, wait_pending_cleanups
            res = await run_hybrid_signup(
                _make_request(mfa_inline=True),
                mail_provider=_FakeMailProvider(),
                log=lambda _m: None,
            )
            await wait_pending_cleanups(timeout=2.0)
            return res

    def _go():
        result = asyncio.run(_run())
        assert result.success, (
            f"reg phải success dù activate fail (account đã create): {result.error}"
        )
        assert result.two_factor is None, "two_factor (full) phải None khi activate fail"
        assert result.two_factor_partial == PARTIAL, (
            f"two_factor_partial sai: {result.two_factor_partial}"
        )

    return _check("[3/4] MfaError activate fail → two_factor_partial set + reg vẫn success", _go)


def tc4_cli_flag_and_combo_logic() -> bool:
    """Verify CLI signup_cmd có flag --mfa và logic append accounts.txt qua AST."""
    import ast

    def _check_ast() -> None:
        src = (ROOT / "cli.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        # Tìm function signup_cmd.
        signup_fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "signup_cmd":
                signup_fn = node
                break
        assert signup_fn is not None, "không tìm thấy hàm signup_cmd trong cli.py"
        arg_names = [a.arg for a in signup_fn.args.args]
        assert "mfa_inline" in arg_names, (
            f"signup_cmd thiếu param `mfa_inline`: {arg_names}"
        )
        # Verify build SignupRequest có mfa_inline= (KHÔNG hardcode False).
        # Walk body, tìm Call → SignupRequest(...) → keyword mfa_inline.
        found_kw = False
        hardcoded_false = False
        for node in ast.walk(signup_fn):
            if isinstance(node, ast.Call):
                func_name = getattr(node.func, "id", None) or getattr(
                    node.func, "attr", None
                )
                if func_name == "SignupRequest":
                    for kw in node.keywords:
                        if kw.arg == "mfa_inline":
                            found_kw = True
                            if isinstance(kw.value, ast.Constant) and kw.value.value is False:
                                hardcoded_false = True
        assert found_kw, "SignupRequest(...) call thiếu keyword mfa_inline trong signup_cmd"
        assert not hardcoded_false, (
            "mfa_inline=False bị hardcode — phải dùng biến từ Typer option (cho phép user opt-out)"
        )
        # Verify có logic append accounts.txt.
        assert "accounts.txt" in src, "signup_cmd thiếu logic append accounts.txt"
        assert 'result.two_factor["secret"]' in src or "two_factor['secret']" in src, (
            "signup_cmd thiếu ghi `result.two_factor[\"secret\"]` vào accounts.txt"
        )

    return _check("[4/4] CLI signup_cmd có --mfa flag + logic append accounts.txt", _check_ast)


def main() -> int:
    results = [
        tc1_mfa_inline_called(),
        tc2_mfa_inline_disabled(),
        tc3_mfa_partial_state(),
        tc4_cli_flag_and_combo_logic(),
    ]
    passed = sum(results)
    total = len(results)
    print(flush=True)
    if passed == total:
        print(f"=== HYBRID MFA INLINE PASSED ({passed}/{total}) ===", flush=True)
        return 0
    print(f"=== HYBRID MFA INLINE FAILED ({passed}/{total}) ===", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
