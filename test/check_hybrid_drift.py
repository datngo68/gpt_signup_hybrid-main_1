"""Exploration test — reproduce drift hybrid vs golden (TRƯỚC khi fix).

Spec: .kiro/specs/reg-hybrid-deactivated-after-signup (bugfix).

Mục đích (bug condition methodology):
    Test này ENCODE hành vi ĐÚNG (Property 1) và phải FAIL / surface
    counterexample trên code CHƯA fix — fail = xác nhận drift tồn tại.

Setup offline (không network, không Camoufox thật):
    - FakeCurlSession: recorder ghi method+url+body+headers, trả canned
      response theo URL. Cho phép script chuỗi response của create_account.
    - FakeTokenGenerator: stub mint_token/mint_so, đếm số lần gọi.
    - FakeMailProvider: poll_otp / poll_all_codes async trả OTP theo kịch bản,
      chạy trên 1 asyncio loop nền (relay.run bridge sync↔async qua
      run_coroutine_threadsafe).
    - Golden ChatGPTRelay + HybridChatGPTRelay dùng cùng kiểu fake session.

4 case (design.md → Exploratory Bug Condition Checking):
    A1 double-POST   : create_account parse-fail lần 1 → assert recorder ghi
                       create_account POST >1 lần (counterexample nhánh A).
    A2 step diff     : OTP về ngay → so relay.steps hybrid vs golden ngoài
                       OTP loop (xác nhận happy path khớp golden).
    B1 synthetic feed: assert đường mint `so` của browser_pool có inject
                       _OBSERVER_FEEDER_JS/_OBSERVER_BURST_JS (counterexample B).
    B2 shared browser: assert pool tái dùng cùng browser instance cho 2 lần
                       acquire khác signup (counterexample cluster fingerprint).

Chạy:  python3 test/check_hybrid_drift.py
EXPECTED OUTCOME (code CHƯA fix): FAIL — A1/B1/B2 surface counterexample.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import threading
import time
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Golden step skeleton (ngoài OTP loop) — endpoint path, không kèm query/status.
GOLDEN_STEP_PATHS = [
    "https://chatgpt.com/api/auth/csrf",
    "https://chatgpt.com/api/auth/signin/openai",
    "https://auth.openai.com/authorize",
    "https://auth.openai.com/api/accounts/user/register",
    "https://auth.openai.com/api/accounts/email-otp/send",
    "https://auth.openai.com/api/accounts/email-otp/validate",
    "https://auth.openai.com/api/accounts/create_account",
    "https://chatgpt.com/api/auth/callback/openai",
    "https://chatgpt.com/api/auth/session",
]

# Delta hợp lệ duy nhất NGOÀI OTP loop (intentional, design.md + relay.run docstring):
# ``_visit_promo_landing()`` GET promo URL ở ĐẦU flow (gắn campaign). FakeCurlSession
# strip query/fragment qua ``url.split("?")[0]`` → recorded path = "https://chatgpt.com/".
# Promo landing được chốt là intentional (GIỮ) → happy path hybrid = [promo] + golden.
PROMO_LANDING_PATH = "https://chatgpt.com/"


# ─────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────


class FakeResp:
    """Minimal curl_cffi-like response. headers=None → relay bỏ qua absorb."""

    def __init__(self, status_code: int, json_body: dict, text: str = ""):
        self.status_code = status_code
        self._json = json_body
        self.text = text or ""
        self.headers = None  # _absorb_response_cookies sẽ return sớm

    def json(self) -> dict:
        return self._json


_PARSE_FAIL_BODY = {"error": "no_continue_url"}  # parse_callback raise ValueError
_SUCCESS_CALLBACK = {
    "continue_url": (
        "https://chatgpt.com/api/auth/callback/openai?code=CODE123&state=STATE-XYZ"
    )
}


class FakeCurlSession:
    """Recorder + canned responder. Không chạm network."""

    def __init__(self, *, validate_outcomes=None, create_account_responses=None):
        self.records: list[dict] = []
        # validate: list[int] status codes (200 = ok). Default 1× ok.
        self._validate = deque(validate_outcomes or [200])
        # create_account: list[dict] json bodies, pop theo thứ tự POST.
        self._create = deque(create_account_responses or [_SUCCESS_CALLBACK])

    # curl_cffi Session API mà relay dùng: .get / .post
    def get(self, url, headers=None, **kw):
        return self._handle("GET", url, headers, None, None)

    def post(self, url, headers=None, json=None, data=None, **kw):
        return self._handle("POST", url, headers, json, data)

    def _handle(self, method, url, headers, json_body, data):
        self.records.append({
            "method": method, "url": url.split("?")[0],
            "json": json_body, "data": data,
            "headers": dict(headers or {}),
        })
        return self._route(method, url)

    def _route(self, method, url) -> FakeResp:
        if "/api/auth/csrf" in url:
            return FakeResp(200, {"csrfToken": "csrf-abc"})
        if "/api/auth/signin/openai" in url:
            return FakeResp(200, {
                "url": "https://auth.openai.com/authorize?state=STATE-XYZ&client_id=x"
            })
        if "/authorize" in url:
            return FakeResp(200, {})
        if "/api/accounts/user/register" in url:
            return FakeResp(200, {"ok": True})
        if "/api/accounts/email-otp/send" in url:
            return FakeResp(200, {})
        if "/api/accounts/email-otp/validate" in url:
            status = self._validate.popleft() if self._validate else 200
            body = {"ok": True} if status == 200 else {"error": "wrong_email_otp_code"}
            return FakeResp(status, body, text=str(body))
        if "/api/accounts/create_account" in url:
            body = self._create.popleft() if self._create else _SUCCESS_CALLBACK
            return FakeResp(200, body, text=str(body))
        if "/api/auth/callback/openai" in url:
            return FakeResp(200, {})
        if "/api/auth/session" in url:
            return FakeResp(200, {
                "user": {"id": "u-1", "email": "a@b.c"},
                "accessToken": "acc-tok", "expires": "2099-01-01",
            })
        return FakeResp(200, {})

    def create_account_post_count(self) -> int:
        return sum(
            1 for r in self.records
            if r["method"] == "POST"
            and r["url"].endswith("/api/accounts/create_account")
        )

    def step_paths(self) -> list[str]:
        return [r["url"] for r in self.records]


class _FakeToken:
    p = "fake-p"
    t = "fake-t"
    c = "fake-c"


class FakeTokenGenerator:
    """Stub CamoufoxTokenGenerator — đếm số lần mint, không launch browser."""

    def __init__(self):
        self.mint_token_calls = 0
        self.mint_so_calls = 0

    def set_device_id(self, device_id: str) -> None:
        pass

    def export_cookies(self) -> list:
        return []

    def mint_token(self, flow: str):
        self.mint_token_calls += 1
        return _FakeToken()

    def mint_so(self, flow: str) -> str:
        self.mint_so_calls += 1
        return "fake-so"


class FakeMailProvider:
    """Async mail provider trả OTP theo kịch bản."""

    def __init__(self, codes):
        self._codes = list(codes)
        self.poll_otp_calls = 0
        self.poll_all_calls = 0

    async def poll_otp(self, *, recipient, started_at, timeout_seconds,
                       poll_interval_seconds, log):
        self.poll_otp_calls += 1
        return self._codes[0] if self._codes else ""

    async def poll_all_codes(self, *, recipient, started_at, log):
        self.poll_all_calls += 1
        return list(self._codes)


class FakeOTPReader:
    """Golden ChatGPTRelay otp_reader.get_code()."""

    def __init__(self, code: str):
        self._code = code

    def get_code(self, timeout: float = 120.0, poll: float = 5.0) -> str:
        return self._code


# ─────────────────────────────────────────────────────────────────────
# Builders
# ─────────────────────────────────────────────────────────────────────


def _build_account():
    from chatgpt_camoufox.chatgpt_camoufox.client import Account
    return Account(email="probe@example.com", password="Passw0rd!xyz", api="manual")


def _build_hybrid(session, mail_loop, mail_provider):
    from reg_hybrid.relay import HybridChatGPTRelay
    return HybridChatGPTRelay(
        _build_account(),
        session=session,
        tokens=FakeTokenGenerator(),
        mail_provider=mail_provider,
        mail_loop=mail_loop,
        recipient="probe@example.com",
        otp_timeout_seconds=30.0,
        otp_poll_interval_seconds=5.0,
        otp_resend_after_seconds=20.0,
        max_resends=2,
        log=lambda _m: None,
    )


def _build_golden(session, code="123456"):
    from chatgpt_camoufox.chatgpt_camoufox.client import ChatGPTRelay
    return ChatGPTRelay(
        _build_account(),
        otp_reader=FakeOTPReader(code),
        session=session,
        tokens=FakeTokenGenerator(),
    )


# ─────────────────────────────────────────────────────────────────────
# Test cases — mỗi case trả (passed, detail, counterexample)
# ─────────────────────────────────────────────────────────────────────


def case_a1_double_post(mail_loop):
    """A1 — create_account parse-fail lần 1 → double-POST trên code cũ."""
    mail = FakeMailProvider(["123456"])
    # create_account: lần 1 parse-fail (thiếu continue_url) → lần 2 success.
    session = FakeCurlSession(
        validate_outcomes=[200],
        create_account_responses=[_PARSE_FAIL_BODY, _SUCCESS_CALLBACK],
    )
    relay = _build_hybrid(session, mail_loop, mail)
    # Sau fix 4.1 (bỏ double-POST), create_account gọi đúng 1 lần; parse-fail
    # lần đầu → propagate ValueError giống golden (KHÔNG re-POST). Bắt đối xứng
    # với nhánh golden bên dưới để đọc post-count đúng hành vi golden-matching.
    try:
        relay.run()
    except ValueError:
        pass
    n_hybrid = session.create_account_post_count()

    # Golden cùng kịch bản: chỉ POST 1 lần rồi raise ValueError (không retry).
    gsession = FakeCurlSession(
        validate_outcomes=[200],
        create_account_responses=[_PARSE_FAIL_BODY, _SUCCESS_CALLBACK],
    )
    golden = _build_golden(gsession)
    try:
        golden.run()
    except ValueError:
        pass
    n_golden = gsession.create_account_post_count()

    detail = f"hybrid create_account POST = {n_hybrid}, golden = {n_golden}"
    # Hành vi ĐÚNG (Property 1 / 2.2): hybrid phải POST đúng 1 lần như golden.
    passed = (n_hybrid == 1)
    ce = None if passed else (
        f"createAccountPostCount(hybrid)={n_hybrid} > 1 "
        f"(golden={n_golden}) — DOUBLE-POST drift nhánh A"
    )
    return passed, detail, ce


def case_a2_step_diff(mail_loop):
    """A2 — happy path: so relay.steps hybrid vs golden ngoài OTP loop."""
    mail = FakeMailProvider(["123456"])
    hsession = FakeCurlSession(validate_outcomes=[200],
                               create_account_responses=[_SUCCESS_CALLBACK])
    hybrid = _build_hybrid(hsession, mail_loop, mail)
    hybrid.run()

    gsession = FakeCurlSession(validate_outcomes=[200],
                               create_account_responses=[_SUCCESS_CALLBACK])
    golden = _build_golden(gsession)
    golden.run()

    h_paths = hsession.step_paths()
    g_paths = gsession.step_paths()
    # Sau khi BỎ promo landing: hybrid happy path == golden skeleton thuần.
    # Trước đây hybrid = [promo landing] + golden (intentional). Promo đã bị
    # gỡ vì Camoufox launch qua proxy timeout 90s khi có thêm GET / extra.
    expected_hybrid = list(GOLDEN_STEP_PATHS)
    extra = [p for p in h_paths if p not in expected_hybrid]
    missing = [p for p in expected_hybrid if p not in h_paths]
    detail = (
        f"hybrid steps={len(h_paths)}, golden steps={len(g_paths)}, "
        f"extra={extra}, missing={missing}"
    )
    # Hành vi ĐÚNG (sau bỏ promo): golden == skeleton; happy path hybrid ==
    # golden skeleton thuần. Bất kỳ bước-sequence ngoài-OTP nào khác golden
    # đều là drift (guard chi tiết ở test/check_run_step_guard.py).
    passed = (g_paths == GOLDEN_STEP_PATHS and h_paths == expected_hybrid)
    ce = None
    if not passed:
        ce = (
            f"stepSequence(hybrid, exclude=OTP_LOOP) != golden — "
            f"extra step(s) {extra or '∅'}, missing {missing or '∅'} "
            f"(drift nhánh A: bước ngoài OTP loop lệch golden skeleton)"
        )
    return passed, detail, ce


def case_b1_synthetic_feeder():
    """B1 — đường mint `so` của browser_pool inject synthetic feeder events."""
    from reg_hybrid import browser_pool

    acquire_src = inspect.getsource(
        browser_pool._CamoufoxRunner._acquire_context_in_thread
    )
    mint_so_src = inspect.getsource(
        browser_pool.HybridContextHandle._mint_so_in_thread
    )
    feeder_injected = (
        "_OBSERVER_FEEDER_JS" in acquire_src
        and "page.evaluate(_OBSERVER_FEEDER_JS)" in acquire_src
    )
    burst_injected = "_OBSERVER_BURST_JS" in mint_so_src
    detail = (
        f"feeder_in_acquire={feeder_injected}, burst_in_mint_so={burst_injected}"
    )
    # Hành vi ĐÚNG (Property 1 / NOT soTokenFromSyntheticEvents): KHÔNG inject.
    passed = (not feeder_injected) and (not burst_injected)
    ce = None
    if not passed:
        ce = (
            "browser_pool inject synthetic Observer events "
            "(_OBSERVER_FEEDER_JS/_OBSERVER_BURST_JS) → `so` token sinh từ "
            "event giả — drift nhánh B (soTokenFromSyntheticEvents)"
        )
    return passed, detail, ce


def case_b2_shared_browser():
    """B2 — hybrid DEFAULT phải no-pool: mỗi signup launch generator riêng.

    Sau fix task 5.2: pool là OPT-IN (Settings ``reg.hybrid_pool_enabled``).
    Default (env unset + key vắng) → ``build_token_generator`` trả
    ``_NoPoolThreadAffinityWrapper`` bọc ``CamoufoxTokenGenerator`` golden RIÊNG
    mỗi signup → mỗi account browser riêng (khớp golden), KHÔNG share fingerprint.
    KHÔNG nới lỏng: test assert đúng hành vi đúng (Property 1 / NOT
    browserFingerprintReusedAcrossSignups).
    """
    import os
    from reg_hybrid import browser_pool
    from reg_hybrid.camoufox_factory import build_token_generator
    from models import SignupRequest

    saved = os.environ.get("HYBRID_POOL_DISABLED")
    gens = []
    try:
        # 1) Quyết định DEFAULT: env unset → pool OPT-IN, default no-pool.
        os.environ.pop("HYBRID_POOL_DISABLED", None)
        default_no_pool = (browser_pool.pool_enabled() is False)

        # 2) No-pool routing: mỗi signup → generator riêng (browser riêng).
        #    Ép no-pool deterministic qua env override (không phụ thuộc DB ambient)
        #    để cô lập đúng hành vi factory routing.
        os.environ["HYBRID_POOL_DISABLED"] = "1"
        req = SignupRequest(email="probe@example.com")
        gen1 = build_token_generator(req, profile=None)
        gen2 = build_token_generator(req, profile=None)
        gens = [gen1, gen2]
        is_nopool = (
            type(gen1).__name__ == "_NoPoolThreadAffinityWrapper"
            and type(gen2).__name__ == "_NoPoolThreadAffinityWrapper"
        )
        # Mỗi signup là instance riêng + inner CamoufoxTokenGenerator riêng →
        # 2 browser process độc lập khi launch (lazy), KHÔNG share.
        distinct = (
            gen1 is not gen2
            and getattr(gen1, "_inner", None) is not getattr(gen2, "_inner", None)
        )
    finally:
        for g in gens:
            try:
                g.close()  # stop daemon worker; inner.close no-op (chưa launch)
            except Exception:
                pass
        if saved is None:
            os.environ.pop("HYBRID_POOL_DISABLED", None)
        else:
            os.environ["HYBRID_POOL_DISABLED"] = saved

    detail = (
        f"default_no_pool(pool_enabled=False)={default_no_pool}, "
        f"nopool_wrappers={is_nopool}, distinct_generators={distinct}"
    )
    # Hành vi ĐÚNG: default no-pool + mỗi signup generator/browser riêng.
    passed = default_no_pool and is_nopool and distinct
    ce = None
    if not passed:
        ce = (
            f"hybrid default vẫn có nguy cơ share browser xuyên signup "
            f"(default_no_pool={default_no_pool}, nopool={is_nopool}, "
            f"distinct={distinct}) → cluster fingerprint — drift nhánh B "
            f"(browserFingerprintReusedAcrossSignups)"
        )
    return passed, detail, ce


# ─────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────


def _patch_sleep_fast():
    """No-op time.sleep trong relay + otp_loop để test chạy nhanh (giữ logic)."""
    import reg_hybrid.relay as _relay
    import reg_hybrid.otp_loop as _otp
    _relay.time.sleep = lambda *_a, **_k: None
    _otp.time.sleep = lambda *_a, **_k: None


def main() -> int:
    _patch_sleep_fast()

    # asyncio loop nền cho mail provider bridge.
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()

    results = []
    try:
        results.append(("A1 double-POST", case_a1_double_post(loop)))
        results.append(("A2 step diff happy path", case_a2_step_diff(loop)))
        results.append(("B1 synthetic feeder", case_b1_synthetic_feeder()))
        results.append(("B2 shared browser", case_b2_shared_browser()))
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5.0)

    print("=" * 70)
    print("DRIFT EXPLORATION — hybrid vs golden (code CHƯA fix)")
    print("=" * 70)
    n_fail = 0
    counterexamples = []
    for name, (passed, detail, ce) in results:
        tag = "PASS" if passed else "FAIL"
        print(f"[{tag}] {name} — {detail}")
        if not passed:
            n_fail += 1
            if ce:
                counterexamples.append((name, ce))

    if counterexamples:
        print("\n--- COUNTEREXAMPLES OBSERVED ---")
        for name, ce in counterexamples:
            print(f"  * {name}: {ce}")

    print("\n" + "=" * 70)
    if n_fail == 0:
        print("RESULT: tất cả PASS — drift đã được fix (hành vi khớp golden).")
        return 0
    print(
        f"RESULT: {n_fail}/{len(results)} case FAIL — drift tồn tại "
        f"(EXPECTED trên code CHƯA fix)."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
