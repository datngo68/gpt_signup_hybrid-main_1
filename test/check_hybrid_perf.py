"""Verify 3 optimizations cho hybrid mode (Phase A + B + C).

Phase A — Browser pool:
    HybridBrowserPool singleton + _CamoufoxRunner share Camoufox xuyên signup.
    HybridContextHandle implement đầy đủ interface CamoufoxTokenGenerator.

Phase B — Pre-mint sentinel oauth_create_account:
    _setup_premint_cache monkey-patch otp_reader.get_code + tokens.mint_token
    + tokens.mint_so. Cache hit trên flow=oauth_create_account → skip mint.

Phase C — Default concurrency bump:
    AutoRegConfig.concurrency + AutoRegStartRequest default 1 → 3.

Chạy:
    .venv/bin/python test/check_hybrid_perf.py
"""
from __future__ import annotations

import ast
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _section(title: str) -> None:
    print(f"\n--- {title} ---", flush=True)


def _check(label: str, fn) -> bool:
    try:
        fn()
        print(f"[PASS] {label}", flush=True)
        return True
    except AssertionError as exc:
        print(f"[FAIL] {label} :: AssertionError: {exc}", flush=True)
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {label} :: {type(exc).__name__}: {exc}", flush=True)
        return False


# ─────────────────────────────────────────────────────────────────────
# Phase A — Browser pool
# ─────────────────────────────────────────────────────────────────────


def tc_phase_a_pool() -> bool:
    from reg_hybrid import browser_pool

    def _singleton() -> None:
        p1 = browser_pool.get_pool()
        p2 = browser_pool.get_pool()
        assert p1 is p2, "get_pool() KHÔNG trả singleton"

    def _disabled_knob() -> None:
        # Default False (env not set / "0")
        import os
        original = os.environ.get("HYBRID_POOL_DISABLED")
        try:
            os.environ.pop("HYBRID_POOL_DISABLED", None)
            assert browser_pool.pool_disabled() is False
            for v in ("1", "true", "yes", "TRUE", "Yes"):
                os.environ["HYBRID_POOL_DISABLED"] = v
                assert browser_pool.pool_disabled() is True, f"value={v!r}"
            for v in ("0", "false", "no", ""):
                os.environ["HYBRID_POOL_DISABLED"] = v
                assert browser_pool.pool_disabled() is False, f"value={v!r}"
        finally:
            if original is None:
                os.environ.pop("HYBRID_POOL_DISABLED", None)
            else:
                os.environ["HYBRID_POOL_DISABLED"] = original

    def _context_handle_interface() -> None:
        """HybridContextHandle phải có đủ method CamoufoxTokenGenerator interface."""
        required = ("set_device_id", "export_cookies", "mint_token", "mint_so", "close")
        for name in required:
            assert hasattr(browser_pool.HybridContextHandle, name), (
                f"HybridContextHandle thiếu method {name!r}"
            )

    def _pool_keys_per_config() -> None:
        """Pool keyed by (proxy, headless, insecure) — verify dict structure."""
        pool = browser_pool.HybridBrowserPool()
        assert hasattr(pool, "acquire")
        assert hasattr(pool, "cleanup_idle")
        assert hasattr(pool, "shutdown_all")
        # _runners dict tồn tại nhưng chưa lazy init runner nào
        assert isinstance(pool._runners, dict)
        assert len(pool._runners) == 0

    a = _check("[A.1] get_pool() trả singleton", _singleton)
    b = _check("[A.2] pool_disabled() respect env HYBRID_POOL_DISABLED", _disabled_knob)
    c = _check("[A.3] HybridContextHandle implement đủ interface CamoufoxTokenGenerator", _context_handle_interface)
    d = _check("[A.4] HybridBrowserPool có acquire + cleanup_idle + shutdown_all", _pool_keys_per_config)
    return a and b and c and d


# ─────────────────────────────────────────────────────────────────────
# Phase B — Pre-mint sentinel cache
# ─────────────────────────────────────────────────────────────────────


class _FakeTokens:
    """Mock CamoufoxTokenGenerator để test pre-mint cache logic."""

    def __init__(self):
        self.mint_token_calls: list[str] = []
        self.mint_so_calls: list[str] = []
        # Mỗi mint sentinel mất 0.05s để verify time-saving qua background.
        self._latency = 0.05

    def mint_token(self, flow: str):
        time.sleep(self._latency)
        self.mint_token_calls.append(flow)
        # Trả 1 marker để verify identity (object identity hơn equality).
        return f"TOKEN[{flow}]#{len(self.mint_token_calls)}"

    def mint_so(self, flow: str):
        time.sleep(self._latency)
        self.mint_so_calls.append(flow)
        return f"SO[{flow}]#{len(self.mint_so_calls)}"


class _FakeOTPReader:
    """Mock OTPReader — get_code block 0.3s rồi return code."""

    def __init__(self):
        self.get_code_called = False

    def get_code(self, timeout: float = 120.0, poll: float = 5.0) -> str:
        self.get_code_called = True
        # Block ngắn để pre-mint thread có cơ hội mint xong trước khi return.
        time.sleep(0.3)
        return "123456"


def tc_phase_b_premint() -> bool:
    from reg_hybrid.runner import _setup_premint_cache

    def _patch_applied() -> None:
        tokens = _FakeTokens()
        otp_reader = _FakeOTPReader()
        original_get_code = otp_reader.get_code
        original_mint_token = tokens.mint_token
        original_mint_so = tokens.mint_so

        cache = _setup_premint_cache(tokens, otp_reader, log=lambda _: None)

        # Patches applied
        assert otp_reader.get_code is not original_get_code, "get_code chưa patch"
        assert tokens.mint_token is not original_mint_token, "mint_token chưa patch"
        assert tokens.mint_so is not original_mint_so, "mint_so chưa patch"
        # Cache dict initialized
        assert cache["token"] is None
        assert cache["so"] is None

    def _cache_hit_on_oauth_create() -> None:
        tokens = _FakeTokens()
        otp_reader = _FakeOTPReader()
        cache = _setup_premint_cache(tokens, otp_reader, log=lambda _: None)

        # Trigger get_code() → spawn pre-mint thread → block 0.3s → return.
        code = otp_reader.get_code(timeout=5.0, poll=1.0)
        assert code == "123456"

        # Đợi pre-mint thread hoàn thành (tối đa 1s).
        for _ in range(20):
            if cache["token"] is not None and cache["so"] is not None:
                break
            time.sleep(0.05)
        assert cache["token"] is not None, "pre-mint không cache token"
        assert cache["so"] is not None, "pre-mint không cache so"
        # Pre-mint phải gọi mint_token + mint_so cho oauth_create_account
        assert tokens.mint_token_calls == ["oauth_create_account"], (
            f"pre-mint gọi mint_token sai flow: {tokens.mint_token_calls}"
        )
        assert tokens.mint_so_calls == ["oauth_create_account"], (
            f"pre-mint gọi mint_so sai flow: {tokens.mint_so_calls}"
        )

        # Bây giờ relay call mint_token(oauth_create_account) lần thứ 2 — phải HIT cache
        t_before = time.monotonic()
        token2 = tokens.mint_token("oauth_create_account")
        elapsed = time.monotonic() - t_before
        # Cache hit → KHÔNG sleep 0.05s, < 10ms
        assert elapsed < 0.02, f"cache hit nhưng vẫn mất {elapsed:.3f}s (chưa skip mint)"
        # Token trả về phải match cache (object identity).
        assert token2 == cache["token"], "mint_token KHÔNG trả cached value"
        # Pre-mint vẫn chỉ 1 call (không tăng).
        assert tokens.mint_token_calls == ["oauth_create_account"]

        # mint_so cũng hit cache
        so2 = tokens.mint_so("oauth_create_account")
        assert so2 == cache["so"], "mint_so KHÔNG trả cached value"

    def _cache_miss_on_other_flow() -> None:
        """Flow khác oauth_create_account → KHÔNG hit cache, gọi mint thật."""
        tokens = _FakeTokens()
        otp_reader = _FakeOTPReader()
        cache = _setup_premint_cache(tokens, otp_reader, log=lambda _: None)

        # Cache có giá trị (giả lập pre-mint đã chạy)
        cache["token"] = "PRE_CACHED_TOKEN"
        cache["so"] = "PRE_CACHED_SO"

        # Gọi flow khác → mint thật, KHÔNG return cache
        token = tokens.mint_token("username_password_create")
        assert token != "PRE_CACHED_TOKEN", "cache hit sai flow"
        assert tokens.mint_token_calls == ["username_password_create"]

        so = tokens.mint_so("login_attempt")
        assert so != "PRE_CACHED_SO"

    def _premint_spawned_in_thread() -> None:
        """Pre-mint phải chạy trong daemon thread, KHÔNG block main."""
        tokens = _FakeTokens()
        # Latency nhỏ để test stable (0.05s × 2 mint = 0.1s).
        tokens._latency = 0.05
        otp_reader = _FakeOTPReader()
        _setup_premint_cache(tokens, otp_reader, log=lambda _: None)

        t_before = time.monotonic()
        code = otp_reader.get_code(timeout=5.0, poll=1.0)
        elapsed = time.monotonic() - t_before
        assert code == "123456"
        # get_code chỉ sleep 0.3s — pre-mint (0.1s) chạy song song KHÔNG block.
        # Tổng wall time ~max(0.3, 0.1) ≈ 0.3s + thread overhead < 0.6s.
        assert elapsed < 0.6, (
            f"get_code() bị block bởi pre-mint: {elapsed:.3f}s "
            f"(pre-mint phải chạy daemon thread, không block main)"
        )
        # Daemon thread sẽ tự chết khi process exit — không cần join explicit.
        # Tránh threading.enumerate() loop vì có thể gặp thread khác (pool, asyncio).

    a = _check("[B.1] _setup_premint_cache apply monkey-patch otp_reader + tokens", _patch_applied)
    b = _check("[B.2] cache hit trên flow=oauth_create_account (skip mint)", _cache_hit_on_oauth_create)
    c = _check("[B.3] cache miss trên flow khác (mint thật)", _cache_miss_on_other_flow)
    d = _check("[B.4] pre-mint chạy daemon thread, KHÔNG block get_code", _premint_spawned_in_thread)
    return a and b and c and d


# ─────────────────────────────────────────────────────────────────────
# Phase C — Default concurrency bump
# ─────────────────────────────────────────────────────────────────────


def tc_phase_c_concurrency() -> bool:
    def _autoreg_config_default() -> None:
        from autoreg.runner import AutoRegConfig
        cfg = AutoRegConfig()
        assert cfg.concurrency == 3, (
            f"AutoRegConfig.concurrency default phải = 3, got {cfg.concurrency}"
        )

    def _autoreg_schema_default() -> None:
        from autoreg.schemas import AutoRegStartRequest
        req = AutoRegStartRequest()
        assert req.concurrency == 3, (
            f"AutoRegStartRequest.concurrency default phải = 3, got {req.concurrency}"
        )

    def _autoreg_schema_range() -> None:
        from autoreg.schemas import AutoRegStartRequest
        from pydantic import ValidationError

        # Cap dưới = 1
        with_low = AutoRegStartRequest(concurrency=1)
        assert with_low.concurrency == 1
        # Cap trên = 5
        with_high = AutoRegStartRequest(concurrency=5)
        assert with_high.concurrency == 5
        # Out of range reject
        for bad in (0, -1, 6, 10):
            try:
                AutoRegStartRequest(concurrency=bad)
            except ValidationError:
                continue
            raise AssertionError(f"AutoRegStartRequest accept concurrency={bad}")

    a = _check("[C.1] AutoRegConfig.concurrency default = 3", _autoreg_config_default)
    b = _check("[C.2] AutoRegStartRequest.concurrency default = 3", _autoreg_schema_default)
    c = _check("[C.3] AutoRegStartRequest concurrency range [1, 5]", _autoreg_schema_range)
    return a and b and c


def main() -> int:
    sections = [
        ("Phase A — Browser pool", tc_phase_a_pool),
        ("Phase B — Pre-mint sentinel cache", tc_phase_b_premint),
        ("Phase C — Default concurrency bump", tc_phase_c_concurrency),
    ]
    fails = 0
    for title, fn in sections:
        _section(title)
        if not fn():
            fails += 1

    print(flush=True)
    if fails:
        print(f"=== CHECK FAILED ({fails}/{len(sections)} sections) ===", flush=True)
        return 1
    print(f"=== CHECK PASSED ({len(sections)} sections) ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
