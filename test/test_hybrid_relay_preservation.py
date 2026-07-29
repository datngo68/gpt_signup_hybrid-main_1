"""Preservation property test BASELINE (TRƯỚC khi fix).

Spec: .kiro/specs/reg-hybrid-deactivated-after-signup (bugfix), Task 3.
Property 2 (design.md): "Hành vi golden + smart OTP loop không đổi".

Mục đích (observation-first):
    Chạy ``HybridChatGPTRelay`` trên code CHƯA fix với input KHÔNG kích hoạt bug
    (``isBugCondition(X) = false`` — create_account success ngay, không double-POST,
    không feeder), QUAN SÁT hành vi thật rồi CHỐT thành assert. Đây là baseline
    để fix nhánh A (task 4) KHÔNG làm hồi quy các bất biến dưới đây.

Các bất biến được chốt (requirements 3.1–3.6):
    P1  Smart OTP loop bất biến (3.2): resend / multi-code ``poll_all_codes`` /
        ``prefer_second_code`` / verify-retry ``_otp_validate_soft`` / human-like
        delay 2–4s / ``prefer_newest_untried_otp_sync`` vẫn hoạt động.
    P2  Kế thừa bất biến (3.3): ``_dd_s`` / ``oai-did`` / ``device_id`` /
        sentinel / headers vẫn từ ``super().__init__()`` — subclass KHÔNG override
        các method flow golden.
    P3  Happy path request (3.4): chuỗi bước golden xuất hiện ĐÚNG THỨ TỰ trong
        ``relay.steps``.
        ⚠ PROMO LANDING: hiện ``run()`` có thêm 1 GET promo landing
        (``_visit_promo_landing``) TRƯỚC ``get_csrf`` — đây là DRIFT đang chờ
        user quyết (task 4 sẽ chốt bỏ/giữ). Test này CHỈ GHI NHẬN trạng thái
        hiện tại của promo landing, **KHÔNG hard-assert** theo hướng nào, để
        quyết định ở task 4 không làm baseline này sai lệch.
    P4  No-pre-mint (3.5): ``on_otp_poll_start`` KHÔNG bị gọi; sentinel
        ``oauth_create_account`` chỉ mint tại ``create_account`` (SAU OTP validate
        OK), không có pre-mint.
    P5  Schema (3.6): ``RelayResult`` đủ ``session_json`` / ``device_id`` /
        ``cookies`` / ``steps``.
    P6  Golden package bất biến (3.1): ``chatgpt_camoufox`` là golden — skeleton
        ``ChatGPTRelay.run()`` + việc subclass chỉ override ``run()`` (không đụng
        method flow) được khẳng định bằng introspection. (File-integrity byte-level
        là static, ngoài phạm vi assert tự động — xem ghi chú P6.)

Cách tiếp cận property-based (KHÔNG có hypothesis trong .venv → tự sinh kịch bản
    có seed): vòng lặp sinh nhiều kịch bản OTP (số code, số lần validate sai trong
    retry budget) và assert bất biến phổ quát giữ với MỌI input non-bug.

DRY: tái dùng fakes từ ``test/check_hybrid_drift.py`` — KHÔNG định nghĩa lại.

Chạy:  .venv/bin/python test/test_hybrid_relay_preservation.py
EXPECTED OUTCOME (code CHƯA fix): tất cả PASS (chốt baseline cần giữ).
"""
from __future__ import annotations

import asyncio
import inspect
import random
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# DRY — tái dùng fakes + helper từ test reproduce drift (task 1).
from check_hybrid_drift import (  # noqa: E402
    GOLDEN_STEP_PATHS,
    FakeCurlSession,
    FakeMailProvider,
    FakeTokenGenerator,
    _SUCCESS_CALLBACK,
    _build_account,
    _patch_sleep_fast,
)

# Golden flow methods phải được KẾ THỪA (không override trong subclass).
_GOLDEN_FLOW_METHODS = (
    "get_csrf", "signin", "authorize", "register", "otp_send",
    "otp_validate", "create_account", "callback", "get_session",
    "build_sentinel_header", "build_sentinel_and_so_headers",
    "_set_cookie", "_request", "_get", "_post", "_dump_cookies",
)


# ─────────────────────────────────────────────────────────────────────
# Instrumented fakes (subclass các fake DRY để thêm quan sát)
# ─────────────────────────────────────────────────────────────────────


class RecordingTokenGenerator(FakeTokenGenerator):
    """Ghi lại thứ tự flow của mint_token / mint_so (để kiểm no-pre-mint)."""

    def __init__(self):
        super().__init__()
        self.mint_token_flows: list[str] = []
        self.mint_so_flows: list[str] = []

    def mint_token(self, flow: str):
        self.mint_token_flows.append(flow)
        return super().mint_token(flow)

    def mint_so(self, flow: str) -> str:
        self.mint_so_flows.append(flow)
        return super().mint_so(flow)


class ScriptedMailProvider(FakeMailProvider):
    """poll_otp trả rỗng ``silent_polls`` lần đầu rồi mới trả code.

    Dùng để buộc smart OTP loop vượt ngưỡng resend (quan sát resend invariant)
    khi mail "về chậm".
    """

    def __init__(self, codes, *, silent_polls: int):
        super().__init__(codes)
        self._silent = int(silent_polls)

    async def poll_otp(self, *, recipient, started_at, timeout_seconds,
                       poll_interval_seconds, log):
        self.poll_otp_calls += 1
        if self.poll_otp_calls <= self._silent:
            return ""
        return self._codes[0] if self._codes else ""

    async def poll_all_codes(self, *, recipient, started_at, log):
        self.poll_all_calls += 1
        if self.poll_otp_calls <= self._silent:
            return []
        return list(self._codes)


class FakeClock:
    """Đồng hồ giả: ``sleep(d)`` đẩy ``monotonic`` tiến ``d`` giây (deterministic).

    Thay thế tham chiếu ``time`` trong relay + otp_loop để kịch bản resend
    (phụ thuộc thời gian) chạy nhanh và xác định, KHÔNG phụ thuộc wall-clock.
    """

    def __init__(self):
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, d: float = 0.0) -> None:
        self.t += max(0.0, float(d))


# ─────────────────────────────────────────────────────────────────────
# Builders
# ─────────────────────────────────────────────────────────────────────


def _build_hybrid(session, mail_loop, mail_provider, *, tokens=None,
                  on_otp_poll_start=None, uniform_recorder=None,
                  max_resends=3, otp_resend_after=20.0):
    from reg_hybrid.relay import HybridChatGPTRelay
    relay = HybridChatGPTRelay(
        _build_account(),
        session=session,
        tokens=tokens or RecordingTokenGenerator(),
        mail_provider=mail_provider,
        mail_loop=mail_loop,
        recipient="probe@example.com",
        otp_timeout_seconds=120.0,
        otp_poll_interval_seconds=5.0,
        otp_resend_after_seconds=otp_resend_after,
        max_resends=max_resends,
        on_otp_poll_start=on_otp_poll_start,
        log=lambda _m: None,
    )
    return relay


# ─────────────────────────────────────────────────────────────────────
# Static checks (không cần chạy relay)
# ─────────────────────────────────────────────────────────────────────


def check_p2_inheritance_structural():
    """P2 — subclass KHÔNG override method flow golden (đều kế thừa)."""
    from reg_hybrid.relay import HybridChatGPTRelay
    from chatgpt_camoufox.chatgpt_camoufox.client import ChatGPTRelay

    assert issubclass(HybridChatGPTRelay, ChatGPTRelay), "phải subclass golden"
    own = set(HybridChatGPTRelay.__dict__.keys())
    overridden = [m for m in _GOLDEN_FLOW_METHODS if m in own]
    detail = (
        f"subclass override flow methods = {overridden or '∅'}; "
        f"subclass __dict__ = {sorted(m for m in own if not m.startswith('__'))}"
    )
    # ĐÚNG: chỉ override 'run' + thêm helper, KHÔNG override flow method golden.
    passed = (overridden == [])
    ce = None if passed else (
        f"subclass override method flow golden {overridden} — vi phạm kế thừa (3.3)"
    )
    return passed, detail, ce


def check_p6_golden_skeleton():
    """P6 — golden ``ChatGPTRelay.run()`` skeleton nguyên (introspection).

    GHI CHÚ: đây KHÔNG phải file-integrity byte-level (việc đó là static, ngoài
    phạm vi assert tự động). Ta khẳng định skeleton golden bằng cách đọc source
    của ``ChatGPTRelay.run`` — bất kỳ sửa đổi nào lên chuỗi bước golden sẽ làm
    assert này fail (early-warning cho regression "đụng golden").
    """
    from chatgpt_camoufox.chatgpt_camoufox.client import ChatGPTRelay

    src = inspect.getsource(ChatGPTRelay.run)
    expected_calls = [
        "self.get_csrf()", "self.signin(", "self.authorize(", "self.register()",
        "self.otp_send()", "self.otp_reader.get_code()", "self.otp_validate(",
        "self.create_account()", "self.callback(", "self.get_session()",
    ]
    missing = [c for c in expected_calls if c not in src]
    # Thứ tự xuất hiện đúng skeleton golden.
    positions = [src.find(c) for c in expected_calls if c in src]
    ordered = positions == sorted(positions)
    detail = f"golden run() missing={missing or '∅'}, ordered={ordered}"
    passed = (missing == []) and ordered
    ce = None if passed else (
        f"golden ChatGPTRelay.run() skeleton lệch (missing={missing}, "
        f"ordered={ordered}) — package golden có thể đã bị sửa (vi phạm 3.1)"
    )
    return passed, detail, ce


# ─────────────────────────────────────────────────────────────────────
# Runtime checks (chạy relay.run trên input non-bug)
# ─────────────────────────────────────────────────────────────────────


def check_p1_smart_otp_loop(mail_loop):
    """P1 — resend / multi-code / verify-retry / human-like delay / prefer_*."""
    import reg_hybrid.relay as _relay
    import reg_hybrid.otp_loop as _otp

    # ── Kịch bản resend + multi-code: mail về chậm (silent 6 poll) → buộc resend.
    clock = FakeClock()
    saved_relay_time, saved_otp_time = _relay.time, _otp.time
    _relay.time, _otp.time = clock, clock

    # Ghi lại các lần random.uniform(2.0, 4.0) trong relay → human-like delay.
    uniform_calls: list[tuple] = []
    saved_uniform = _relay.random.uniform

    def _rec_uniform(a, b):
        uniform_calls.append((a, b))
        return (a + b) / 2.0

    # LƯU Ý: _relay.random và _otp.random là CÙNG module object → chỉ patch 1
    # lần là đủ (patch 2 lần sẽ ghi đè recorder). Recorder trả midpoint; với
    # silent_polls=6 (×5s = 30s) > mọi ngưỡng resend → resend chắc chắn xảy ra
    # mà không cần ép threshold.
    _relay.random.uniform = _rec_uniform

    try:
        mail = ScriptedMailProvider(["123456"], silent_polls=6)
        session = FakeCurlSession(validate_outcomes=[200],
                                  create_account_responses=[_SUCCESS_CALLBACK])
        relay = _build_hybrid(session, mail_loop, mail)
        relay.run()
        send_count = sum(
            1 for r in session.records
            if r["url"].endswith("/api/accounts/email-otp/send")
        )
        multi_code_used = mail.poll_all_calls >= 1
        resend_happened = send_count >= 2
        human_like = (2.0, 4.0) in uniform_calls
    finally:
        _relay.time, _otp.time = saved_relay_time, saved_otp_time
        _relay.random.uniform = saved_uniform

    # ── Kịch bản verify-retry: validate sai 2 lần rồi đúng (đủ code để thử). ──
    _patch_sleep_fast()  # no-op sleep cho kịch bản nhanh (không cần fake clock)
    saved_uniform2 = _relay.random.uniform
    uniform_calls2: list[tuple] = []
    _relay.random.uniform = lambda a, b: (uniform_calls2.append((a, b)) or (a + b) / 2.0)
    try:
        mail2 = FakeMailProvider(["111111", "222222", "333333", "444444"])
        session2 = FakeCurlSession(
            validate_outcomes=[401, 401, 200],
            create_account_responses=[_SUCCESS_CALLBACK],
        )
        relay2 = _build_hybrid(session2, mail_loop, mail2)
        relay2.run()
        validate_count = sum(
            1 for r in session2.records
            if r["url"].endswith("/api/accounts/email-otp/validate")
        )
        retry_human_like = (2.0, 4.0) in uniform_calls2
        verify_retry_ok = (validate_count == 3)  # 2 sai + 1 đúng
        ca_once = (session2.create_account_post_count() == 1)
    finally:
        _relay.random.uniform = saved_uniform2

    detail = (
        f"[resend] send_count={send_count} (resend={resend_happened}), "
        f"multi_code={multi_code_used}, human_like={human_like}; "
        f"[retry] validate_count={validate_count} (retry_ok={verify_retry_ok}), "
        f"retry_human_like={retry_human_like}, create_account_once={ca_once}"
    )
    passed = (
        resend_happened and multi_code_used and human_like
        and verify_retry_ok and retry_human_like and ca_once
    )
    ce = None if passed else (
        "smart OTP loop bất biến KHÔNG giữ: "
        f"resend={resend_happened}, multi_code={multi_code_used}, "
        f"human_like={human_like}, verify_retry={verify_retry_ok} "
        "(vi phạm 3.2)"
    )
    return passed, detail, ce


def check_p2_inheritance_runtime(mail_loop):
    """P2 — _dd_s / oai-did / device_id từ super().__init__() (runtime)."""
    mail = FakeMailProvider(["123456"])
    session = FakeCurlSession(validate_outcomes=[200],
                              create_account_responses=[_SUCCESS_CALLBACK])
    _patch_sleep_fast()
    relay = _build_hybrid(session, mail_loop, mail)

    cookies = {c.name: (c.value, c.domain) for c in relay._jar}
    dd_s = cookies.get("_dd_s")
    oai_did = cookies.get("oai-did")
    has_dd_s = dd_s is not None and dd_s[1] == ".openai.com"
    oai_did_ok = oai_did is not None and oai_did[0] == relay.device_id
    device_ok = bool(relay.device_id)
    logging_ok = bool(relay.logging_id)
    detail = (
        f"_dd_s={dd_s}, oai-did={oai_did}, device_id={relay.device_id!r}, "
        f"logging_id set={logging_ok}"
    )
    passed = has_dd_s and oai_did_ok and device_ok and logging_ok
    ce = None if passed else (
        f"kế thừa init lệch: _dd_s_ok={has_dd_s}, oai_did_ok={oai_did_ok}, "
        f"device_ok={device_ok} (vi phạm 3.3)"
    )
    return passed, detail, ce


def check_p3_happy_path_steps(mail_loop):
    """P3 — golden skeleton xuất hiện đúng thứ tự; promo landing CHỈ GHI NHẬN."""
    mail = FakeMailProvider(["123456"])
    session = FakeCurlSession(validate_outcomes=[200],
                              create_account_responses=[_SUCCESS_CALLBACK])
    _patch_sleep_fast()
    relay = _build_hybrid(session, mail_loop, mail)
    relay.run()

    paths = session.step_paths()

    # Golden skeleton phải là SUBSEQUENCE đúng thứ tự của paths thực tế.
    def _is_subsequence(sub, seq):
        it = iter(seq)
        return all(any(s == x for x in it) for s in sub)

    skeleton_ok = _is_subsequence(GOLDEN_STEP_PATHS, paths)

    # Promo landing: GHI NHẬN trạng thái hiện tại — KHÔNG hard-assert.
    promo_path = "https://chatgpt.com/"
    promo_present = (promo_path in paths) and (paths and paths[0] == promo_path)
    extra_steps = [p for p in paths if p not in GOLDEN_STEP_PATHS]

    detail = (
        f"steps={len(paths)}, golden_skeleton_in_order={skeleton_ok}; "
        f"[OBSERVED-ONLY promo landing] present={promo_present}, "
        f"extra_steps={extra_steps} "
        f"(promo landing là DRIFT chờ task 4 — KHÔNG assert giữ/bỏ)"
    )
    # CHỈ assert skeleton golden đúng thứ tự (3.4 phần bất biến). Promo landing
    # không vào điều kiện pass/fail (quyết định ở task 4).
    passed = skeleton_ok
    ce = None if passed else (
        f"golden skeleton KHÔNG xuất hiện đúng thứ tự trong steps={paths} "
        "(vi phạm 3.4)"
    )
    return passed, detail, ce


def check_p4_no_pre_mint(mail_loop):
    """P4 — on_otp_poll_start KHÔNG bị gọi; oauth_create_account mint sau validate."""
    calls = {"n": 0}

    def _spy():
        calls["n"] += 1

    mail = FakeMailProvider(["123456"])
    session = FakeCurlSession(validate_outcomes=[200],
                              create_account_responses=[_SUCCESS_CALLBACK])
    tokens = RecordingTokenGenerator()
    _patch_sleep_fast()
    relay = _build_hybrid(session, mail_loop, mail, tokens=tokens,
                          on_otp_poll_start=_spy)
    relay.run()

    poll_start_not_called = (calls["n"] == 0)

    # mint_token flows: register=username_password_create, create_account=oauth_create_account.
    flows = tokens.mint_token_flows
    oauth_mints = [i for i, f in enumerate(flows) if f == "oauth_create_account"]
    single_oauth_mint = (len(oauth_mints) == 1)

    # oauth_create_account mint phải SAU khi validate OK (no pre-mint).
    # Kiểm tra bằng thứ tự request: index của validate < index của create_account.
    paths = session.step_paths()
    try:
        idx_validate = paths.index(
            "https://auth.openai.com/api/accounts/email-otp/validate")
        idx_create = paths.index(
            "https://auth.openai.com/api/accounts/create_account")
        mint_after_validate = idx_validate < idx_create
    except ValueError:
        mint_after_validate = False

    detail = (
        f"on_otp_poll_start_calls={calls['n']}, mint_token_flows={flows}, "
        f"single_oauth_mint={single_oauth_mint}, "
        f"create_after_validate={mint_after_validate}, "
        f"mint_so_flows={tokens.mint_so_flows}"
    )
    passed = (
        poll_start_not_called and single_oauth_mint and mint_after_validate
    )
    ce = None if passed else (
        f"no-pre-mint bất biến lệch: poll_start_called={not poll_start_not_called}, "
        f"single_oauth_mint={single_oauth_mint}, "
        f"create_after_validate={mint_after_validate} (vi phạm 3.5)"
    )
    return passed, detail, ce


def check_p5_schema(mail_loop):
    """P5 — RelayResult đủ field session_json/device_id/cookies/steps."""
    mail = FakeMailProvider(["123456"])
    session = FakeCurlSession(validate_outcomes=[200],
                              create_account_responses=[_SUCCESS_CALLBACK])
    _patch_sleep_fast()
    relay = _build_hybrid(session, mail_loop, mail)
    result = relay.run()

    has_session = isinstance(getattr(result, "session_json", None), dict)
    has_device = isinstance(getattr(result, "device_id", None), str) and result.device_id
    has_cookies = isinstance(getattr(result, "cookies", None), dict)
    has_steps = isinstance(getattr(result, "steps", None), list) and result.steps
    detail = (
        f"session_json={type(getattr(result,'session_json',None)).__name__}, "
        f"device_id set={bool(has_device)}, "
        f"cookies={type(getattr(result,'cookies',None)).__name__}, "
        f"steps={len(getattr(result,'steps',[]) or [])}"
    )
    passed = bool(has_session and has_device and has_cookies and has_steps)
    ce = None if passed else (
        "RelayResult thiếu/sai field session_json/device_id/cookies/steps "
        "(vi phạm 3.6)"
    )
    return passed, detail, ce


# ─────────────────────────────────────────────────────────────────────
# Property-based: vòng lặp sinh kịch bản OTP non-bug (seed cố định)
# ─────────────────────────────────────────────────────────────────────


def check_pbt_preservation(mail_loop, *, n_cases: int = 40, seed: int = 1337):
    """For-all non-bug input: bất biến phổ quát giữ qua nhiều kịch bản OTP.

    Sinh ngẫu nhiên (seed): số lần validate sai trong retry budget + số code dư.
    Mỗi case create_account success ngay (non-bug, createAccountPostCount = 1).
    Assert: verified OK, create_account đúng 1 lần, validate_count = wrong+1,
    kế thừa init nguyên, schema hợp lệ, on_otp_poll_start không bị gọi.
    """
    _patch_sleep_fast()
    rng = random.Random(seed)
    failures: list[str] = []
    checked = 0

    for case_i in range(n_cases):
        wrong = rng.randint(0, 3)               # số validate sai (≤ retry budget)
        n_codes = wrong + 1 + rng.randint(1, 2)  # đủ code distinct + buffer
        codes = [f"{100000 + i + case_i * 10}" for i in range(n_codes)]
        outcomes = [401] * wrong + [200]

        calls = {"n": 0}
        mail = FakeMailProvider(codes)
        session = FakeCurlSession(
            validate_outcomes=outcomes,
            create_account_responses=[_SUCCESS_CALLBACK],
        )
        tokens = RecordingTokenGenerator()
        try:
            relay = _build_hybrid(
                session, mail_loop, mail, tokens=tokens,
                on_otp_poll_start=lambda: calls.__setitem__("n", calls["n"] + 1),
                max_resends=5,
            )
            result = relay.run()
        except Exception as exc:  # noqa: BLE001
            failures.append(
                f"case#{case_i} wrong={wrong} n_codes={n_codes}: RAISED "
                f"{type(exc).__name__}: {exc}"
            )
            continue

        checked += 1
        ca = session.create_account_post_count()
        vc = sum(1 for r in session.records
                 if r["url"].endswith("/api/accounts/email-otp/validate"))
        dd_s = next((c for c in relay._jar if c.name == "_dd_s"), None)
        oai_did = next((c for c in relay._jar if c.name == "oai-did"), None)

        problems = []
        if ca != 1:
            problems.append(f"create_account POST={ca}≠1")
        if vc != wrong + 1:
            problems.append(f"validate_count={vc}≠{wrong + 1}")
        if calls["n"] != 0:
            problems.append(f"on_otp_poll_start called {calls['n']}×")
        if not (dd_s and dd_s.domain == ".openai.com"):
            problems.append("_dd_s missing/wrong-domain")
        if not (oai_did and oai_did.value == relay.device_id):
            problems.append("oai-did != device_id")
        if not isinstance(getattr(result, "session_json", None), dict):
            problems.append("session_json not dict")
        if problems:
            failures.append(
                f"case#{case_i} wrong={wrong} n_codes={n_codes}: "
                + ", ".join(problems)
            )

    detail = f"checked {checked}/{n_cases} cases, failures={len(failures)}"
    passed = (len(failures) == 0)
    ce = None
    if not passed:
        ce = "PBT preservation counterexamples:\n    " + "\n    ".join(failures[:8])
    return passed, detail, ce


# ─────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────


def main() -> int:
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()

    results = []
    try:
        # Static / introspection
        results.append(("P2 inheritance (structural)", check_p2_inheritance_structural()))
        results.append(("P6 golden skeleton (introspection)", check_p6_golden_skeleton()))
        # Runtime single-scenario
        results.append(("P1 smart OTP loop", check_p1_smart_otp_loop(loop)))
        results.append(("P2 inheritance (runtime)", check_p2_inheritance_runtime(loop)))
        results.append(("P3 happy path steps", check_p3_happy_path_steps(loop)))
        results.append(("P4 no-pre-mint", check_p4_no_pre_mint(loop)))
        results.append(("P5 RelayResult schema", check_p5_schema(loop)))
        # Property-based
        results.append(("PBT preservation (40 cases)", check_pbt_preservation(loop)))
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5.0)

    print("=" * 70)
    print("PRESERVATION BASELINE — hybrid relay (code CHƯA fix)")
    print("=" * 70)
    n_fail = 0
    counterexamples = []
    for name, (passed, detail, ce) in results:
        tag = "PASS" if passed else "FAIL"
        print(f"[{tag}] {name}\n        {detail}")
        if not passed:
            n_fail += 1
            if ce:
                counterexamples.append((name, ce))

    if counterexamples:
        print("\n--- COUNTEREXAMPLES ---")
        for name, ce in counterexamples:
            print(f"  * {name}: {ce}")

    print("\n" + "=" * 70)
    if n_fail == 0:
        print("RESULT: tất cả PASS — baseline preservation đã chốt "
              "(fix nhánh A KHÔNG được làm hồi quy các bất biến này).")
        return 0
    print(f"RESULT: {n_fail}/{len(results)} bất biến FAIL — baseline chưa chốt được.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
