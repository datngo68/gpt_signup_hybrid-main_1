"""Smoke test reproduce bug "Playwright Sync API inside asyncio loop" + verify fix.

Cách reproduce mà KHÔNG launch Camoufox thật:
    1. Monkey-patch `reg_hybrid.runner._build_pipeline` → check thread chạy.
    2. Monkey-patch `reg_hybrid.relay.HybridChatGPTRelay` → fake relay no-op.
    3. Monkey-patch helpers (`_patch_tokens_cache`, `_spawn_premint_thread`).
    4. Gọi `run_hybrid_signup` qua `asyncio.run(...)` — giả lập đúng case caller.
    5. Verify:
       - `_build_pipeline` chạy off event-loop thread.
       - `_cleanup` (fire-and-forget) chạy off event-loop thread.
       - End-to-end success path không raise Playwright asyncio guard.

Chạy:
    .venv/bin/python test/smoke_hybrid_asyncio_safe.py
"""
from __future__ import annotations

import asyncio
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


def _make_request(*, mfa_inline: bool = False):
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
        # Default False — test này không verify MFA (riêng test smoke_hybrid_mfa_inline).
        mfa_inline=mfa_inline,
    )


class _FakeProfile:
    user_agent = "Mozilla/5.0 (Windows NT 10.0; rv:135.0) Firefox/135.0"
    impersonate = "firefox135"
    platform = "Windows"
    language = "en-US"


class _FakeTokens:
    def mint_token(self, flow: str): return f"tok-{flow}"
    def mint_so(self, flow: str): return f"so-{flow}"
    def close(self) -> None: ...


class _FakeSession:
    def close(self) -> None: ...


class _FakeRelayResult:
    def __init__(self) -> None:
        self.session_json = {"accessToken": "fake-tok", "user": {"id": "u-1"}}
        self.device_id = "dev-1"
        self.cookies = {
            "_account": "acc-1",
            "__Secure-next-auth.session-token": "tok-1",
        }
        self.steps = ["GET /csrf -> 200"]


class _FakeHybridRelay:
    """Stub thay HybridChatGPTRelay — không launch curl/Camoufox."""
    def __init__(self, **_kw) -> None:
        self._jar = []
        self.session = _FakeSession()

    def run(self) -> _FakeRelayResult:
        return _FakeRelayResult()


class _FakeMailProvider:
    async def poll_otp(self, **_kw) -> str:
        return "123456"


def tc1_build_pipeline_runs_off_loop() -> bool:
    """`_build_pipeline` phải chạy ở thread khác event loop thread."""
    captured: dict = {}

    def _stub_build_pipeline(request, *, firefox_major, log):
        captured["thread_id"] = threading.get_ident()
        captured["thread_name"] = threading.current_thread().name
        captured["has_running_loop"] = False
        try:
            asyncio.get_running_loop()
            captured["has_running_loop"] = True
        except RuntimeError:
            captured["has_running_loop"] = False
        return (_FakeProfile(), _FakeSession(), _FakeTokens(), None, None)

    async def _run() -> None:
        captured["loop_thread_id"] = threading.get_ident()
        with patch("reg_hybrid.runner._build_pipeline", _stub_build_pipeline), \
             patch("reg_hybrid.runner._patch_tokens_cache",
                   lambda *_a, **_kw: {}), \
             patch("reg_hybrid.relay.HybridChatGPTRelay", _FakeHybridRelay), \
             patch("reg_hybrid.runner.enable_2fa_in_session",
                   lambda *a, **k: {"activated": False}, create=True):
            from reg_hybrid.runner import run_hybrid_signup
            return await run_hybrid_signup(
                _make_request(),
                mail_provider=_FakeMailProvider(),
                log=lambda _m: None,
            )

    def _go() -> None:
        asyncio.run(_run())
        assert captured.get("thread_id") is not None, "stub not called"
        assert captured["thread_id"] != captured["loop_thread_id"], (
            f"_build_pipeline chạy Ở EVENT LOOP THREAD "
            f"(thread_id={captured['thread_id']} == loop_thread_id) — bug NOT fixed"
        )
        assert captured["has_running_loop"] is False, (
            "_build_pipeline có asyncio.get_running_loop() khả dụng — "
            "Playwright sync API sẽ raise. Phải chạy ở thread KHÔNG có loop."
        )

    return _check("[1/3] _build_pipeline chạy off event-loop thread", _go)


def tc2_cleanup_runs_off_loop() -> bool:
    """`_cleanup` (fire-and-forget) phải chạy off event-loop thread."""
    captured = {"call_tids": [], "loop_tid": None}

    def _stub_cleanup(session, tokens, log) -> None:
        captured["call_tids"].append(threading.get_ident())

    def _stub_build_pipeline(request, *, firefox_major, log):
        return (_FakeProfile(), _FakeSession(), _FakeTokens(), None, None)

    async def _run() -> None:
        captured["loop_tid"] = threading.get_ident()
        with patch("reg_hybrid.runner._build_pipeline", _stub_build_pipeline), \
             patch("reg_hybrid.runner._cleanup", _stub_cleanup), \
             patch("reg_hybrid.runner._patch_tokens_cache",
                   lambda *_a, **_kw: {}), \
             patch("reg_hybrid.relay.HybridChatGPTRelay", _FakeHybridRelay):
            from reg_hybrid.runner import run_hybrid_signup, wait_pending_cleanups
            await run_hybrid_signup(
                _make_request(),
                mail_provider=_FakeMailProvider(),
                log=lambda _m: None,
            )
            # Fire-and-forget cleanup chạy sau return → wait để có data.
            await wait_pending_cleanups(timeout=5.0)

    def _go() -> None:
        asyncio.run(_run())
        assert len(captured["call_tids"]) >= 1, (
            f"_cleanup chưa được gọi: {captured}"
        )
        for tid in captured["call_tids"]:
            assert tid != captured["loop_tid"], (
                f"_cleanup chạy Ở EVENT LOOP THREAD (tid={tid}) — "
                f"sync ops sẽ block event loop + Playwright sync API fail"
            )

    return _check("[2/3] _cleanup chạy off event-loop thread", _go)


def tc3_no_playwright_asyncio_error() -> bool:
    """End-to-end: build_pipeline KHÔNG được thấy event loop running."""

    def _stub_build_pipeline(request, *, firefox_major, log):
        try:
            asyncio.get_running_loop()
            raise NotImplementedError(
                "It looks like you are using Playwright Sync API inside the "
                "asyncio loop. Please use the Async API instead."
            )
        except RuntimeError:
            pass  # No loop → ok
        return (_FakeProfile(), _FakeSession(), _FakeTokens(), None, None)

    async def _run():
        with patch("reg_hybrid.runner._build_pipeline", _stub_build_pipeline), \
             patch("reg_hybrid.runner._patch_tokens_cache",
                   lambda *_a, **_kw: {}), \
             patch("reg_hybrid.relay.HybridChatGPTRelay", _FakeHybridRelay), \
             patch("reg_hybrid.runner.enable_2fa_in_session",
                   lambda *a, **k: {"activated": False}, create=True):
            from reg_hybrid.runner import run_hybrid_signup
            return await run_hybrid_signup(
                _make_request(),
                mail_provider=_FakeMailProvider(),
                log=lambda _m: None,
            )

    def _go() -> None:
        result = asyncio.run(_run())
        assert result.success is True, (
            f"Pipeline fail không phải vì Playwright guard: error={result.error}"
        )
        assert result.access_token == "fake-tok", f"access_token={result.access_token}"
        assert result.user_id == "u-1", f"user_id={result.user_id}"
        assert result.account_id == "acc-1", f"account_id={result.account_id}"

    return _check(
        "[3/3] End-to-end: run_hybrid_signup không trigger Playwright asyncio guard",
        _go,
    )


def main() -> int:
    results = [
        tc1_build_pipeline_runs_off_loop(),
        tc2_cleanup_runs_off_loop(),
        tc3_no_playwright_asyncio_error(),
    ]
    passed = sum(results)
    total = len(results)
    print(flush=True)
    if passed == total:
        print(f"=== ASYNCIO-SAFE SMOKE PASSED ({passed}/{total}) ===", flush=True)
        return 0
    print(f"=== ASYNCIO-SAFE SMOKE FAILED ({passed}/{total}) ===", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
