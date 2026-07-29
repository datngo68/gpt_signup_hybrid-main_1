"""Verify sentinel cache TTL + create_account retry logic.

Test cases:
    [1/4] cache fresh (< TTL) → mint_token HIT, không gọi original.
    [2/4] cache stale (> TTL) → invalidated + remint qua original.
    [3/4] mint_so cũng tuân thủ TTL.
    [4/4] run() gọi create_account golden ĐÚNG 1 lần — KHÔNG helper retry double-POST
          (Task 4.1 xóa _create_account_with_retry; acceptance criteria 2.2).

Chạy:
    .venv/bin/python test/check_hybrid_sentinel_ttl.py
"""
from __future__ import annotations

import sys
import time
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


class _FakeTokens:
    def __init__(self) -> None:
        self.mint_token_calls: list[str] = []
        self.mint_so_calls: list[str] = []

    def mint_token(self, flow: str) -> str:
        self.mint_token_calls.append(flow)
        return f"REAL-token-{flow}"

    def mint_so(self, flow: str) -> str:
        self.mint_so_calls.append(flow)
        return f"REAL-so-{flow}"


def tc1_cache_fresh_hit() -> bool:
    from reg_hybrid.runner import _patch_tokens_cache

    def _run() -> None:
        tokens = _FakeTokens()
        cache = _patch_tokens_cache(tokens, log=lambda _m: None)
        # Pre-fill cache với data fresh.
        cache["token"] = "CACHED-token"
        cache["so"] = "CACHED-so"
        cache["minted_at"] = time.monotonic()  # ngay bây giờ

        # 2 call liên tiếp → cả 2 phải HIT (không gọi original).
        assert tokens.mint_token("oauth_create_account") == "CACHED-token"
        assert tokens.mint_token("oauth_create_account") == "CACHED-token"
        assert tokens.mint_token_calls == [], (
            f"original_mint_token KHÔNG được gọi khi cache fresh: "
            f"got {tokens.mint_token_calls}"
        )

    return _check("[1/4] cache fresh → mint_token HIT", _run)


def tc2_cache_stale_remint() -> bool:
    from reg_hybrid.runner import _SENTINEL_CACHE_TTL_SECONDS, _patch_tokens_cache

    def _run() -> None:
        tokens = _FakeTokens()
        cache = _patch_tokens_cache(tokens, log=lambda _m: None)
        # Pre-fill cache với data STALE (mint cách đây quá TTL).
        cache["token"] = "STALE-token"
        cache["so"] = "STALE-so"
        cache["minted_at"] = time.monotonic() - _SENTINEL_CACHE_TTL_SECONDS - 5.0

        result = tokens.mint_token("oauth_create_account")
        assert result == "REAL-token-oauth_create_account", (
            f"Stale cache phải remint, got {result!r}"
        )
        assert tokens.mint_token_calls == ["oauth_create_account"], (
            f"original_mint_token phải được gọi: {tokens.mint_token_calls}"
        )
        # Cache phải bị invalidate (token=None) — gọi mint_so sau cũng remint.
        assert cache["token"] is None, (
            f"Cache token phải bị invalidate sau stale detect: {cache['token']}"
        )
        assert cache["so"] is None, (
            f"Cache so phải bị invalidate cùng: {cache['so']}"
        )

    return _check("[2/4] cache stale → invalidated + remint", _run)


def tc3_mint_so_ttl() -> bool:
    from reg_hybrid.runner import _SENTINEL_CACHE_TTL_SECONDS, _patch_tokens_cache

    def _run() -> None:
        tokens = _FakeTokens()
        cache = _patch_tokens_cache(tokens, log=lambda _m: None)
        cache["token"] = "STALE-token"
        cache["so"] = "STALE-so"
        cache["minted_at"] = time.monotonic() - _SENTINEL_CACHE_TTL_SECONDS - 5.0

        # mint_so trước mint_token → token cache còn (nhưng stale).
        result = tokens.mint_so("oauth_create_account")
        # Mint_so check TTL trên so cache, vì age > TTL → fallback gọi original.
        assert result == "REAL-so-oauth_create_account", (
            f"Stale so phải fallback original: {result!r}"
        )
        assert tokens.mint_so_calls == ["oauth_create_account"]

    return _check("[3/4] mint_so cũng tuân thủ TTL", _run)


def tc4_create_account_single_post_shape() -> bool:
    """Verify run() gọi create_account golden ĐÚNG 1 lần — KHÔNG helper retry double-POST.

    UPDATED (Task 4.1): spec reg-hybrid-deactivated-after-signup xác định
    ``_create_account_with_retry`` là dead-path drift (double-POST
    ``/api/accounts/create_account`` trên cùng OAuth session — pattern golden
    không bao giờ phát, tín hiệu deferred ban). Helper đã bị xóa hẳn; ``run()``
    giờ gọi method golden kế thừa ``self.create_account()`` đúng 1 lần/session.

    Acceptance criteria 2.2: createAccountPostCount(result) = 1 như golden.
    Test cũ (assert helper TỒN TẠI) đã lỗi thời và được thay bằng assert
    hành vi mới đúng — KHÔNG nới lỏng, phản ánh đúng spec.
    """
    import ast

    def _run() -> None:
        src = (ROOT / "reg_hybrid" / "relay.py").read_text(encoding="utf-8")
        # 1. Helper retry double-POST phải bị xóa HẲN (dead-path).
        assert "_create_account_with_retry" not in src, (
            "_create_account_with_retry phải bị xóa (dead-path double-POST) "
            "— vẫn còn trong relay.py"
        )
        # 2. run() phải gọi self.create_account() golden ĐÚNG 1 lần.
        tree = ast.parse(src)
        run_node = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "run"
            ),
            None,
        )
        assert run_node is not None, "relay.py thiếu method run()"
        create_calls = [
            sub for sub in ast.walk(run_node)
            if isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "create_account"
        ]
        assert len(create_calls) == 1, (
            f"run() phải gọi self.create_account() ĐÚNG 1 lần (golden), "
            f"got {len(create_calls)} call(s)"
        )
        # 3. Call phải qua self → method golden kế thừa, không re-POST.
        call = create_calls[0]
        assert (
            isinstance(call.func.value, ast.Name)
            and call.func.value.id == "self"
        ), "create_account phải gọi qua self.create_account() (golden kế thừa)"

    return _check("[4/4] run() gọi create_account golden đúng 1 lần (no double-POST)", _run)


def main() -> int:
    results = [
        tc1_cache_fresh_hit(),
        tc2_cache_stale_remint(),
        tc3_mint_so_ttl(),
        tc4_create_account_single_post_shape(),
    ]
    passed = sum(results)
    total = len(results)
    print(flush=True)
    if passed == total:
        print(f"=== HYBRID SENTINEL TTL PASSED ({passed}/{total}) ===", flush=True)
        return 0
    print(f"=== HYBRID SENTINEL TTL FAILED ({passed}/{total}) ===", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
