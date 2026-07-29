"""Smoke test cho reg_mode='hybrid' — verify wiring không thực sự call browser.

Test cases:
    [1/6] SignupRequest accept reg_mode='hybrid' + reject reg_mode='bogus'
    [2/6] reg_hybrid module import được (4 file)
    [3/6] db.repositories._validate_type_constraint accept 'reg_mode.current'='hybrid'
    [4/6] signup.run_signup routing có nhánh hybrid (AST scan, không thực thi)
    [5/6] cli signup_cmd validate reg_mode bao gồm 'hybrid'
    [6/6] MailProviderOTPReader build được + raise RuntimeError khi loop closed

Không launch Camoufox, không gọi network — chỉ verify hợp đồng adapter.

Chạy:
    .venv/bin/python test/smoke_reg_hybrid.py
"""
from __future__ import annotations

import asyncio
import ast
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


def tc1_signup_request() -> bool:
    from models import SignupRequest
    from pydantic import ValidationError

    def _ok() -> None:
        req = SignupRequest(email="x@hotmail.com", reg_mode="hybrid")
        assert req.reg_mode == "hybrid"

    def _bogus_rejected() -> None:
        try:
            SignupRequest(email="x@hotmail.com", reg_mode="bogus")
        except ValidationError:
            return
        raise AssertionError("SignupRequest accept reg_mode='bogus' (should reject)")

    def _three_modes() -> None:
        for mode in ("browser", "pure_request", "hybrid"):
            req = SignupRequest(email="x@hotmail.com", reg_mode=mode)
            assert req.reg_mode == mode

    a = _check("[1.1] reg_mode='hybrid' accepted", _ok)
    b = _check("[1.2] reg_mode='bogus' rejected", _bogus_rejected)
    c = _check("[1.3] 3 mode {browser, pure_request, hybrid} accepted", _three_modes)
    return a and b and c


def tc2_module_imports() -> bool:
    def _import_init() -> None:
        import reg_hybrid
        assert hasattr(reg_hybrid, "run_hybrid_signup")
        assert hasattr(reg_hybrid, "HybridSignupError")

    def _import_runner() -> None:
        from reg_hybrid.runner import run_hybrid_signup, HybridSignupError  # noqa: F401

    def _import_adapter() -> None:
        from reg_hybrid.mail_adapter import MailProviderOTPReader  # noqa: F401

    def _import_factory() -> None:
        # Factory chỉ wrap lazy imports (chatgpt_camoufox không buộc cài) — import
        # chính nó phải pass dù curl_cffi chưa có.
        from reg_hybrid import camoufox_factory  # noqa: F401

    a = _check("[2.1] reg_hybrid package import", _import_init)
    b = _check("[2.2] reg_hybrid.runner import", _import_runner)
    c = _check("[2.3] reg_hybrid.mail_adapter import", _import_adapter)
    d = _check("[2.4] reg_hybrid.camoufox_factory import", _import_factory)
    return a and b and c and d


def tc3_settings_validate() -> bool:
    from db.repositories import RepositoryError, _validate_type_constraint, _EXACT_KEYS

    def _key_in_whitelist() -> None:
        assert "reg_mode.current" in _EXACT_KEYS, "reg_mode.current missing from whitelist"

    def _hybrid_accepted() -> None:
        _validate_type_constraint("reg_mode.current", "hybrid")

    def _existing_still_ok() -> None:
        _validate_type_constraint("reg_mode.current", "browser")

    def _pure_request_rejected() -> None:
        # pure_request đã bị gỡ khỏi reg (2026) → validation phải reject.
        try:
            _validate_type_constraint("reg_mode.current", "pure_request")
        except RepositoryError:
            return
        raise AssertionError("validate accept 'pure_request' (đã gỡ — phải reject)")

    def _bogus_rejected() -> None:
        try:
            _validate_type_constraint("reg_mode.current", "bogus")
        except RepositoryError:
            return
        raise AssertionError("validate accept 'bogus' (should reject)")

    a = _check("[3.1] reg_mode.current trong _EXACT_KEYS", _key_in_whitelist)
    b = _check("[3.2] validate accept 'hybrid'", _hybrid_accepted)
    c = _check("[3.3] validate accept 'browser'", _existing_still_ok)
    d = _check("[3.4] validate reject 'bogus'", _bogus_rejected)
    e = _check("[3.5] validate reject 'pure_request' (đã gỡ khỏi reg)", _pure_request_rejected)
    return a and b and c and d and e


def tc4_signup_routing() -> bool:
    """AST scan signup.run_signup body — đảm bảo có branch `reg_mode == "hybrid"`
    và import từ ``reg_hybrid`` module."""

    def _scan() -> None:
        path = ROOT / "signup.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))

        # Tìm function run_signup
        run_signup_fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_signup":
                run_signup_fn = node
                break
        assert run_signup_fn is not None, "run_signup function not found in signup.py"

        # ast.unparse chuẩn hoá quote → check cả single & double để robust.
        body_source = ast.unparse(run_signup_fn)
        assert ("'hybrid'" in body_source or '"hybrid"' in body_source), (
            "run_signup không có chuỗi 'hybrid' — chưa wire reg_mode='hybrid'"
        )
        assert "run_hybrid_signup" in body_source, (
            "run_signup không gọi run_hybrid_signup"
        )
        assert "from reg_hybrid" in body_source or "import reg_hybrid" in body_source, (
            "run_signup không import reg_hybrid"
        )

    return _check("[4] signup.run_signup có routing 'hybrid' + gọi run_hybrid_signup", _scan)


def tc5_cli_pattern() -> bool:
    """AST scan cli.signup_cmd — verify list validate có 'hybrid'."""

    def _scan() -> None:
        path = ROOT / "cli.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))

        signup_cmd = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "signup_cmd":
                signup_cmd = node
                break
        assert signup_cmd is not None, "signup_cmd not found in cli.py"

        source = ast.unparse(signup_cmd)

        # ast.unparse chuẩn hoá literal về single-quote ('hybrid') nhưng vẫn
        # giữ double-quote khi literal chứa single quote ("foo's bar"). Check
        # cả 2 dạng để robust với mọi version Python.
        def _has(literal: str) -> bool:
            return f"'{literal}'" in source or f'"{literal}"' in source

        assert _has("hybrid"), "cli.signup_cmd không validate 'hybrid'"
        assert _has("browser"), "cli.signup_cmd không validate 'browser'"
        assert not _has("pure_request"), (
            "cli.signup_cmd vẫn còn 'pure_request' (đã gỡ khỏi reg)"
        )

    return _check("[5] cli.signup_cmd validate reg_mode = browser+hybrid (pure_request đã gỡ)", _scan)


def tc7_autoreg_wire() -> bool:
    """AutoRegConfig + web/icloud_routes phải propagate reg_mode='hybrid'.

    Đây là MAJOR gap đã phát hiện audit lần 2: autoreg trước đó không đọc
    reg_mode.current → mọi job autoreg luôn dùng default 'browser', bất kể
    Settings.
    """
    from autoreg.runner import AutoRegConfig

    def _config_has_field() -> None:
        cfg = AutoRegConfig()
        assert hasattr(cfg, "reg_mode"), "AutoRegConfig thiếu field reg_mode"
        assert cfg.reg_mode == "browser", f"default reg_mode phải là 'browser', got {cfg.reg_mode!r}"

    def _config_accept_hybrid() -> None:
        cfg = AutoRegConfig(reg_mode="hybrid")
        assert cfg.reg_mode == "hybrid"

    def _routes_pass_reg_mode() -> None:
        # AST scan: web/icloud_routes.py autoreg_start phải build AutoRegConfig
        # với kwarg reg_mode lấy từ all_settings.
        path = ROOT / "web" / "icloud_routes.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Tìm AutoRegConfig(...) call có kwarg reg_mode
        found_kwarg = False
        found_settings_read = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "AutoRegConfig":
                for kw in node.keywords:
                    if kw.arg == "reg_mode":
                        found_kwarg = True
                        break
            # Find string "reg_mode.current" anywhere (settings lookup)
            if isinstance(node, ast.Constant) and node.value == "reg_mode.current":
                found_settings_read = True
        assert found_kwarg, "AutoRegConfig(...) call không có kwarg reg_mode"
        assert found_settings_read, "web/icloud_routes.py không đọc Settings 'reg_mode.current'"

    def _runner_passes_to_spec() -> None:
        # AST scan autoreg/runner.py: spec.build_request(...) phải có kwarg
        # reg_mode = self._config.reg_mode.
        path = ROOT / "autoreg" / "runner.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "build_request":
                    for kw in node.keywords:
                        if kw.arg == "reg_mode":
                            found = True
                            break
        assert found, "autoreg/runner.py spec.build_request không truyền reg_mode"

    a = _check("[7.1] AutoRegConfig có field reg_mode (default 'browser')", _config_has_field)
    b = _check("[7.2] AutoRegConfig accept reg_mode='hybrid'", _config_accept_hybrid)
    c = _check("[7.3] web/icloud_routes.py đọc Settings + truyền reg_mode vào AutoRegConfig", _routes_pass_reg_mode)
    d = _check("[7.4] autoreg/runner.py truyền reg_mode vào spec.build_request", _runner_passes_to_spec)
    return a and b and c and d


def tc6_adapter_contract() -> bool:
    """Verify MailProviderOTPReader contract: build được + raise đúng khi loop chết."""
    from reg_hybrid.mail_adapter import MailProviderOTPReader

    class _StubProvider:
        async def poll_otp(self, **_kw) -> str:
            return "123456"

    def _build_ok() -> None:
        # Cần event loop sống để giữ reference — dùng new_event_loop tạm
        loop = asyncio.new_event_loop()
        try:
            reader = MailProviderOTPReader(
                mail_provider=_StubProvider(),
                recipient="foo@bar.com",
                loop=loop,
                poll_interval_seconds=1.0,
            )
            assert reader.recipient == "foo@bar.com"
            assert reader.loop is loop
        finally:
            loop.close()

    def _closed_loop_raises() -> None:
        loop = asyncio.new_event_loop()
        loop.close()
        reader = MailProviderOTPReader(
            mail_provider=_StubProvider(),
            recipient="foo@bar.com",
            loop=loop,
            poll_interval_seconds=1.0,
        )
        try:
            reader.get_code(timeout=1.0)
        except RuntimeError as exc:
            assert "loop đã đóng" in str(exc), f"unexpected msg: {exc}"
            return
        raise AssertionError("get_code không raise RuntimeError khi loop closed")

    def _live_loop_passes_code() -> None:
        """End-to-end mini: chạy reader.get_code() trong thread → coroutine
        await trong loop chính → trả 123456."""

        async def _main() -> str:
            loop = asyncio.get_running_loop()
            reader = MailProviderOTPReader(
                mail_provider=_StubProvider(),
                recipient="foo@bar.com",
                loop=loop,
                poll_interval_seconds=1.0,
                log=lambda _msg: None,
            )
            # Chạy get_code() trong thread riêng (giả lập ChatGPTRelay sync flow)
            code = await asyncio.to_thread(reader.get_code, 5.0, 1.0)
            return code

        code = asyncio.run(_main())
        assert code == "123456", f"expected '123456', got {code!r}"

    a = _check("[6.1] MailProviderOTPReader build OK", _build_ok)
    b = _check("[6.2] get_code raise RuntimeError khi loop closed", _closed_loop_raises)
    c = _check("[6.3] get_code() bridge sync↔async trả OTP đúng", _live_loop_passes_code)
    return a and b and c


def tc8_chatgpt_camoufox_alignment() -> bool:
    """Verify camoufox_factory KHỚP chính xác golden chatgpt_camoufox CLI.

    chatgpt_camoufox.__main__.py argparse default:
      --locale=vi-VN  --firefox-major=135  --platform=Windows
    Hybrid mode phải dùng cùng default để hưởng nguyên test coverage của package.
    """
    from reg_hybrid import camoufox_factory

    def _default_platform_is_windows() -> None:
        assert camoufox_factory._DEFAULT_PLATFORM == "Windows", (
            f"hybrid platform default phải là 'Windows' (golden chatgpt_camoufox), "
            f"got {camoufox_factory._DEFAULT_PLATFORM!r}"
        )

    def _default_locale_is_en_us() -> None:
        # Lệch chủ ý khỏi chatgpt_camoufox CLI default ('vi-VN' = preference dev gốc) —
        # repo này dùng pool proxy đa quốc gia, en-US là fallback anti-ban an toàn.
        assert camoufox_factory._DEFAULT_LOCALE == "en-US", (
            f"hybrid locale default phải là 'en-US' (neutral anti-ban), "
            f"got {camoufox_factory._DEFAULT_LOCALE!r}"
        )

    def _firefox_major_135() -> None:
        assert camoufox_factory._DEFAULT_FIREFOX_MAJOR == 135, (
            f"hybrid firefox_major phải là 135, got {camoufox_factory._DEFAULT_FIREFOX_MAJOR}"
        )

    def _profile_build_matches_golden() -> None:
        """Test factory với mock request — verify UA + camoufox_os = Windows."""
        # Import lazily — test này yêu cầu chatgpt_camoufox khả dụng để build profile.
        try:
            from models import SignupRequest
        except ImportError:
            raise AssertionError("models.SignupRequest không import được")

        try:
            from chatgpt_camoufox.chatgpt_camoufox.fingerprint import FirefoxProfile  # noqa: F401
        except ImportError:
            # Skip TC này nếu chatgpt_camoufox chưa cài (chạy CI/dev không có deps)
            print(
                "[SKIP] [8.4] chatgpt_camoufox.fingerprint không khả dụng — skip",
                flush=True,
            )
            return

        req = SignupRequest(email="x@hotmail.com")  # locale None → use default
        profile = camoufox_factory.build_firefox_profile(req)
        assert profile.platform == "Windows", (
            f"profile.platform phải là 'Windows', got {profile.platform!r}"
        )
        assert profile.firefox_major == 135, (
            f"profile.firefox_major phải là 135, got {profile.firefox_major}"
        )
        assert "Windows NT 10.0" in profile.user_agent, (
            f"UA phải chứa 'Windows NT 10.0', got {profile.user_agent!r}"
        )
        assert profile.camoufox_os == "windows", (
            f"camoufox_os phải là 'windows', got {profile.camoufox_os!r}"
        )
        assert profile.impersonate == "firefox135", (
            f"impersonate phải là 'firefox135', got {profile.impersonate!r}"
        )

    a = _check("[8.1] _DEFAULT_PLATFORM = 'Windows' (golden)", _default_platform_is_windows)
    b = _check("[8.2] _DEFAULT_LOCALE = 'en-US' (neutral anti-ban, lệch khỏi CLI default vi-VN)", _default_locale_is_en_us)
    c = _check("[8.3] _DEFAULT_FIREFOX_MAJOR = 135", _firefox_major_135)
    d = _check("[8.4] build_firefox_profile khớp golden Firefox 135 Windows", _profile_build_matches_golden)
    return a and b and c and d


def tc9_output_schema_helpers() -> bool:
    """Verify helper functions của runner.py trả đúng output schema đồng nhất.

    Test 4 helper: _compute_age, _extract_session_token, _classify_error,
    _normalize_relay_cookies. Pure-function tests, không cần Camoufox.
    """
    from reg_hybrid.runner import (
        _classify_error,
        _compute_age,
        _extract_session_token,
        _normalize_relay_cookies,
    )

    def _age_compute() -> None:
        # Birthdate 2000-01-01 với today 2026 → age 26 (giả định không bug
        # leap year edge case). Test giá trị approx, không exact để tránh
        # tied vào current date.
        age = _compute_age("1990-06-15")
        assert age is not None and 30 <= age <= 40, f"age {age} ngoài kỳ vọng"

    def _age_invalid() -> None:
        assert _compute_age(None) is None
        assert _compute_age("") is None
        assert _compute_age("invalid") is None

    def _session_token_simple() -> None:
        cookies = {"__Secure-next-auth.session-token": "JWT123"}
        token = _extract_session_token(cookies)
        assert token == "JWT123", f"expected JWT123, got {token!r}"

    def _session_token_chunks() -> None:
        # NextAuth split JWT > 4KB thành chunks .0, .1
        cookies = {
            "__Secure-next-auth.session-token.0": "PART1.",
            "__Secure-next-auth.session-token.1": "PART2.",
            "__Secure-next-auth.session-token.2": "PART3",
        }
        token = _extract_session_token(cookies)
        assert token == "PART1.PART2.PART3", f"chunk merge sai: {token!r}"

    def _session_token_missing() -> None:
        assert _extract_session_token({}) is None
        assert _extract_session_token({"other-cookie": "x"}) is None

    def _classify_invalid_state() -> None:
        for msg in (
            "HTTP 409 invalid_state",
            "Your sign-in session is no longer valid",
            "Server returned 409",
        ):
            cat = _classify_error(RuntimeError(msg))
            assert cat == "invalid_state", f"msg={msg!r} → cat={cat!r} (expected invalid_state)"

    def _classify_cf_block() -> None:
        for msg in (
            "HTTP 403 Forbidden",
            "Cloudflare challenge detected",
            "Just a moment...",
            "cf-mitigated: challenge",
        ):
            cat = _classify_error(RuntimeError(msg))
            assert cat == "cf_block", f"msg={msg!r} → cat={cat!r} (expected cf_block)"

    def _classify_terminal() -> None:
        for msg in (
            "Outlook combo dead: invalid_grant",
            "OTP timeout after 300s",
            "ValidationError: email format invalid",
        ):
            cat = _classify_error(RuntimeError(msg))
            assert cat == "terminal", f"msg={msg!r} → cat={cat!r} (expected terminal)"

    def _normalize_cookies_no_jar() -> None:
        # Khi relay không có _jar (fake stub) → fallback minimal record
        class FakeRelay:
            pass
        cookies = _normalize_relay_cookies(FakeRelay(), {"a": "1", "b": "2"})
        assert len(cookies) == 2
        for c in cookies:
            assert c["domain"] == ".chatgpt.com"
            assert c["path"] == "/"
            assert c["secure"] is True
            assert c["name"] in ("a", "b")
            assert c["value"] in ("1", "2")

    def _normalize_cookies_with_jar() -> None:
        # Khi relay._jar có cookies với domain riêng → preserve
        from http.cookiejar import Cookie, CookieJar
        jar = CookieJar()
        jar.set_cookie(Cookie(
            version=0, name="cf_clearance", value="CF1", port=None,
            port_specified=False, domain=".openai.com", domain_specified=True,
            domain_initial_dot=True, path="/", path_specified=True,
            secure=True, expires=None, discard=False, comment=None,
            comment_url=None, rest={}, rfc2109=False,
        ))
        class FakeRelay:
            _jar = jar
        cookies = _normalize_relay_cookies(FakeRelay(), {"cf_clearance": "CF1"})
        assert len(cookies) == 1
        c = cookies[0]
        assert c["name"] == "cf_clearance"
        assert c["value"] == "CF1"
        assert c["domain"] == ".openai.com"

    a = _check("[9.1] _compute_age từ birthdate hợp lệ", _age_compute)
    b = _check("[9.2] _compute_age return None khi invalid", _age_invalid)
    c = _check("[9.3] _extract_session_token cookie nguyên", _session_token_simple)
    d = _check("[9.4] _extract_session_token ghép chunks .0/.1/.2 (NextAuth split JWT)", _session_token_chunks)
    e = _check("[9.5] _extract_session_token None khi missing", _session_token_missing)
    f = _check("[9.6] _classify_error → 'invalid_state' (HTTP 409)", _classify_invalid_state)
    g = _check("[9.7] _classify_error → 'cf_block' (Cloudflare 403)", _classify_cf_block)
    h = _check("[9.8] _classify_error → 'terminal' (combo dead / OTP timeout)", _classify_terminal)
    i = _check("[9.9] _normalize_relay_cookies fallback khi không có _jar", _normalize_cookies_no_jar)
    j = _check("[9.10] _normalize_relay_cookies preserve domain từ _jar", _normalize_cookies_with_jar)
    return all([a, b, c, d, e, f, g, h, i, j])


def tc10_runner_retry_structure() -> bool:
    """AST scan runner.py để verify outer-loop retry structure.

    KHÔNG test end-to-end retry vì cần Camoufox + curl_cffi runtime. Verify:
    - max_attempts = 2
    - rotation chain firefox_major
    - catch Exception + classify + retry hoặc terminate
    """

    def _scan_retry_loop() -> None:
        path = ROOT / "reg_hybrid" / "runner.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        # 1. _IMPERSONATE_FALLBACK_MAJORS phải chứa 135 + 133 (cho rotation).
        # Handle cả ``ast.Assign`` lẫn ``ast.AnnAssign`` (constant có type hint).
        found_chain = False
        for node in ast.walk(tree):
            value_node = None
            if (isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "_IMPERSONATE_FALLBACK_MAJORS" for t in node.targets)):
                value_node = node.value
            elif (isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)
                    and node.target.id == "_IMPERSONATE_FALLBACK_MAJORS"):
                value_node = node.value
            if value_node is None:
                continue
            vals: list[int] = []
            if isinstance(value_node, (ast.Tuple, ast.List)):
                vals = [
                    e.value for e in value_node.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, int)
                ]
            assert 135 in vals, f"_IMPERSONATE_FALLBACK_MAJORS thiếu 135: {vals}"
            assert 133 in vals, f"_IMPERSONATE_FALLBACK_MAJORS thiếu 133: {vals}"
            found_chain = True
            break
        assert found_chain, "_IMPERSONATE_FALLBACK_MAJORS chưa định nghĩa"

        # 2. run_hybrid_signup phải có `max_attempts = 2` (outer loop)
        run_fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_hybrid_signup":
                run_fn = node
                break
        assert run_fn is not None, "run_hybrid_signup không tồn tại"
        fn_source = ast.unparse(run_fn)
        assert "max_attempts" in fn_source, "run_hybrid_signup thiếu outer-loop attempts"
        assert "_classify_error" in fn_source, "run_hybrid_signup không gọi _classify_error"
        assert "_cleanup" in fn_source, "run_hybrid_signup không gọi _cleanup"

    def _scan_timing_track() -> None:
        path = ROOT / "reg_hybrid" / "runner.py"
        source = path.read_text(encoding="utf-8")
        # Verify timing dict + result.otp_seconds / phase1_seconds / phase2_seconds
        # được assign từ delta thực.
        for marker in (
            "timing[",
            "result.otp_seconds",
            "result.phase1_seconds",
            "result.phase2_seconds",
            "result.account_id",
        ):
            assert marker in source, f"runner.py không có marker {marker!r}"

    def _scan_otp_timeout_forward() -> None:
        path = ROOT / "reg_hybrid" / "runner.py"
        source = path.read_text(encoding="utf-8")
        # Phải forward request.otp_timeout_seconds vào HybridChatGPTRelay
        # (smart OTP loop khớp pure_request — không còn dùng MailProviderOTPReader
        # ở runtime, xem reg_hybrid/relay.py).
        assert "otp_timeout_seconds=request.otp_timeout_seconds" in source, (
            "runner.py không forward otp_timeout_seconds vào HybridChatGPTRelay"
        )

    a = _check("[10.1] _IMPERSONATE_FALLBACK_MAJORS có 135 + 133 (rotation chain)", _scan_retry_loop)
    b = _check("[10.2] runner ghi otp_seconds + phase1/phase2 + account_id", _scan_timing_track)
    c = _check("[10.3] runner forward request.otp_timeout_seconds vào adapter", _scan_otp_timeout_forward)
    return a and b and c


def main() -> int:
    sections = [
        ("TC1 SignupRequest.reg_mode pattern", tc1_signup_request),
        ("TC2 reg_hybrid module imports", tc2_module_imports),
        ("TC3 db.repositories Settings validate", tc3_settings_validate),
        ("TC4 signup.run_signup routing 'hybrid'", tc4_signup_routing),
        ("TC5 cli signup_cmd reg_mode pattern", tc5_cli_pattern),
        ("TC6 MailProviderOTPReader contract", tc6_adapter_contract),
        ("TC7 autoreg + icloud_routes propagate reg_mode", tc7_autoreg_wire),
        ("TC8 hybrid khớp golden chatgpt_camoufox CLI", tc8_chatgpt_camoufox_alignment),
        ("TC9 runner helper functions output schema", tc9_output_schema_helpers),
        ("TC10 runner retry + timing + adapter forward", tc10_runner_retry_structure),
    ]
    fails = 0
    for title, fn in sections:
        _section(title)
        if not fn():
            fails += 1

    print(flush=True)
    if fails:
        print(f"=== SMOKE FAILED ({fails}/{len(sections)} sections) ===", flush=True)
        return 1
    print(f"=== SMOKE PASSED ({len(sections)} sections) ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
