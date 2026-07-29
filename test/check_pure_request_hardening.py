"""Verify 3 thay đổi anti-ban cho pure_request (Phase A.2 + B + D).

Phase A.2 — Selective sidecar cookie absorb:
    - ``_SIDECAR_COOKIE_ALLOWLIST`` thu hẹp: bỏ ``__cf_bm``, ``__cflb``, ``_cfuvid``
      (Cloudflare bot-management bound TLS session — copy giữa Camoufox và curl
      gây JA3 mismatch).
    - ``_CF_IP_BOUND_COOKIES`` còn ``cf_clearance`` (vẫn cần skip-if-proxy-mismatch).

Phase B — Sentinel sidecar-required mode:
    - Env ``OPENAI_SENTINEL_REQUIRE_SIDECAR=1`` → ``_get_sentinel_token`` raise
      ``RequestPhaseError`` thay vì fallback QuickJS/PoW (weak fingerprint).

Phase D — Runtime warning suggest hybrid:
    - ``signup.run_signup`` pure_request branch có log message recommend
      ``reg_mode='hybrid'``.

Chạy:
    .venv/bin/python test/check_pure_request_hardening.py
"""
from __future__ import annotations

import ast
import os
import sys
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
# Phase A.2 — Selective sidecar cookie absorb
# ─────────────────────────────────────────────────────────────────────


def tc_phase_a2_cookie_allowlist() -> bool:
    """``_SIDECAR_COOKIE_ALLOWLIST`` đã loại 3 CF bot-management cookies."""
    from request_phase import _SIDECAR_COOKIE_ALLOWLIST, _CF_IP_BOUND_COOKIES

    def _no_cf_bm() -> None:
        # 3 cookie CF bot-management bound TLS session — KHÔNG được sync.
        for forbidden in ("__cf_bm", "__cflb", "_cfuvid"):
            assert forbidden not in _SIDECAR_COOKIE_ALLOWLIST, (
                f"{forbidden!r} vẫn trong _SIDECAR_COOKIE_ALLOWLIST — "
                f"sync giữa Camoufox/curl = JA3 mismatch (Phase A.2)"
            )

    def _keep_essentials() -> None:
        # Các cookie thật sự cần sync (JS chỉ có trong Camoufox).
        for required in ("cf_clearance", "oai-sc", "oai-asli", "_dd_s", "oai-did"):
            assert required in _SIDECAR_COOKIE_ALLOWLIST, (
                f"{required!r} thiếu trong _SIDECAR_COOKIE_ALLOWLIST — "
                f"cookie JS-only cần sync từ sidecar"
            )

    def _ip_bound_narrowed() -> None:
        # Sau khi loại 3 cookie trên khỏi allowlist, _CF_IP_BOUND_COOKIES chỉ
        # cần check `cf_clearance` (cookie duy nhất còn lại bị bound IP).
        assert _CF_IP_BOUND_COOKIES == frozenset({"cf_clearance"}), (
            f"_CF_IP_BOUND_COOKIES nên chỉ còn 'cf_clearance', got {_CF_IP_BOUND_COOKIES}"
        )

    a = _check("[A.2.1] _SIDECAR_COOKIE_ALLOWLIST loại __cf_bm/__cflb/_cfuvid", _no_cf_bm)
    b = _check("[A.2.2] _SIDECAR_COOKIE_ALLOWLIST giữ cf_clearance/oai-sc/oai-asli/_dd_s/oai-did", _keep_essentials)
    c = _check("[A.2.3] _CF_IP_BOUND_COOKIES thu hẹp còn cf_clearance", _ip_bound_narrowed)
    return a and b and c


# ─────────────────────────────────────────────────────────────────────
# Phase B — Sentinel sidecar-required env knob
# ─────────────────────────────────────────────────────────────────────


def tc_phase_b_require_sidecar() -> bool:
    """``OPENAI_SENTINEL_REQUIRE_SIDECAR=1`` → _get_sentinel_token raise."""
    from request_phase import RequestPhaseError, _get_sentinel_token

    # Stub session — không cần thật vì sentinel raise trước khi gọi session.
    class _StubSession:
        pass

    _logs: list[str] = []

    def _log(msg: str) -> None:
        _logs.append(msg)

    def _raises_with_env_set() -> None:
        original = os.environ.get("OPENAI_SENTINEL_REQUIRE_SIDECAR")
        os.environ["OPENAI_SENTINEL_REQUIRE_SIDECAR"] = "1"
        try:
            _get_sentinel_token(
                _StubSession(),
                device_id="test-device",
                flow="username_password_create",
                log=_log,
            )
        except RequestPhaseError as exc:
            assert "sidecar" in str(exc).lower(), f"message thiếu 'sidecar': {exc}"
            assert "hybrid" in str(exc).lower(), f"message nên gợi ý 'hybrid': {exc}"
            return
        finally:
            if original is None:
                os.environ.pop("OPENAI_SENTINEL_REQUIRE_SIDECAR", None)
            else:
                os.environ["OPENAI_SENTINEL_REQUIRE_SIDECAR"] = original
        raise AssertionError(
            "_get_sentinel_token không raise khi OPENAI_SENTINEL_REQUIRE_SIDECAR=1"
        )

    def _warn_log_when_unset() -> None:
        # Khi env không set, hàm log warn loud rồi tiếp tục (KHÔNG raise).
        # Test này KHÔNG chạy thật vì sẽ gọi vào QuickJS/PoW (cần session
        # curl_cffi). Chỉ verify presence của warning message string trong
        # AST của function — đảm bảo developer không xoá warning về sau.
        path = ROOT / "request_phase.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found_warn = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_get_sentinel_token":
                src = ast.unparse(node)
                if "WARN" in src and "fallback path" in src:
                    found_warn = True
                break
        assert found_warn, "_get_sentinel_token thiếu warn message 'WARN: fallback path'"

    a = _check("[B.1] OPENAI_SENTINEL_REQUIRE_SIDECAR=1 → raise RequestPhaseError", _raises_with_env_set)
    b = _check("[B.2] _get_sentinel_token có warn 'fallback path' khi env không set", _warn_log_when_unset)
    return a and b


# ─────────────────────────────────────────────────────────────────────
# Phase D — signup.py recommend hybrid
# ─────────────────────────────────────────────────────────────────────


def tc_phase_d_recommend_hybrid() -> bool:
    """signup.run_signup pure_request branch log message khuyên hybrid."""

    def _scan() -> None:
        path = ROOT / "signup.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Tìm function run_signup
        run_signup_fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_signup":
                run_signup_fn = node
                break
        assert run_signup_fn is not None, "run_signup không tìm thấy"

        src = ast.unparse(run_signup_fn)
        # Phải có "consider switching to reg_mode='hybrid'" hoặc tương đương
        markers = ("consider switching", "reg_mode='hybrid'", "reg_mode=\"hybrid\"")
        found = any(m in src for m in markers)
        assert found, (
            f"signup.run_signup pure_request branch chưa có log recommend hybrid; "
            f"thiếu 1 trong: {markers}"
        )

    return _check("[D] signup.py log recommend reg_mode='hybrid' khi pure_request", _scan)


# ─────────────────────────────────────────────────────────────────────
# Phase E — Pre-mint sentinel oauth_create_account song song poll OTP
# ─────────────────────────────────────────────────────────────────────


def tc_phase_e_premint() -> bool:
    """Verify _run_request_phase_sync spawn pre-mint thread + có cache check."""

    def _scan_thread_spawn() -> None:
        path = ROOT / "request_phase.py"
        source = path.read_text(encoding="utf-8")
        # Phải có function nested _premint_create_account_sentinel
        assert "_premint_create_account_sentinel" in source, (
            "request_phase.py thiếu function _premint_create_account_sentinel"
        )
        # Phải có threading.Thread spawn cho pre-mint
        assert 'name="pure-request-premint-ca"' in source, (
            "request_phase.py thiếu Thread spawn pre-mint"
        )
        # Phải có env knob HYBRID_PREMINT_DISABLED
        assert "HYBRID_PREMINT_DISABLED" in source, (
            "request_phase.py thiếu env knob HYBRID_PREMINT_DISABLED"
        )

    def _scan_cache_check() -> None:
        path = ROOT / "request_phase.py"
        source = path.read_text(encoding="utf-8")
        # Phải có cache HIT log message
        assert "page-native cache HIT" in source, (
            "request_phase.py thiếu cache HIT branch cho sentinel_token"
        )
        assert "so-token cache HIT" in source, (
            "request_phase.py thiếu cache HIT branch cho so_token"
        )
        # Phải có device_id_unchanged check (cache stale guard)
        assert "_device_id_unchanged" in source or "device_id_unchanged" in source, (
            "request_phase.py thiếu device_id_unchanged check"
        )

    def _scan_thread_join_bounded() -> None:
        path = ROOT / "request_phase.py"
        source = path.read_text(encoding="utf-8")
        # join phải có timeout bounded để main thread không block lâu khi pre-mint hang
        assert "_premint_ca_thread.join(timeout=" in source, (
            "_premint_ca_thread.join thiếu timeout"
        )

    a = _check("[E.1] _run_request_phase_sync spawn pre-mint thread + có env knob", _scan_thread_spawn)
    b = _check("[E.2] Có cache HIT branch cho sentinel_token + so_token", _scan_cache_check)
    c = _check("[E.3] Thread join timeout bounded (không block main)", _scan_thread_join_bounded)
    return a and b and c


def main() -> int:
    sections = [
        ("Phase A.2 — Selective sidecar cookie absorb", tc_phase_a2_cookie_allowlist),
        ("Phase B — Sentinel sidecar-required env knob", tc_phase_b_require_sidecar),
        ("Phase D — signup.py recommend hybrid", tc_phase_d_recommend_hybrid),
        ("Phase E — Pre-mint sentinel oauth_create_account", tc_phase_e_premint),
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
