"""Guard chống drift tương lai — chuỗi bước run() hybrid vs golden (Task 4.3).

Spec: .kiro/specs/reg-hybrid-deactivated-after-signup (bugfix), Task 4.3.

Bối cảnh:
    Vì CẤM sửa ``chatgpt_camoufox`` (golden), không thể chèn template-method hook
    vào ``ChatGPTRelay.run()`` để ràng buộc "chỉ OTP loop khác golden".
    ``HybridChatGPTRelay.run()`` là override copy tay nên drift (double-POST,
    GET lạ, reorder) có thể lọt qua mà không bị phát hiện. Test này là GUARD
    TĨNH thay cho template-method: nó so khớp chuỗi bước (recorded request paths)
    của ``HybridChatGPTRelay.run()`` happy path (OTP 1 lần) với golden
    ``ChatGPTRelay.run()`` và CHỈ chấp nhận đúng các delta hợp lệ đã biết.

Delta hợp lệ đã biết (design.md + relay.py docstring run()):
    Δ#1  Promo landing: 1× GET ``https://chatgpt.com/`` (``_visit_promo_landing``)
         CHÈN ở ĐẦU flow, TRƯỚC ``get_csrf`` — intentional (gắn campaign).
    Δ#2  OTP loop: trên HAPPY PATH (OTP về ngay, 1× send + 1× validate) OTP loop
         phát đúng chuỗi golden (1× /email-otp/send + 1× /email-otp/validate) →
         KHÔNG tạo delta path nào trên happy path. (Resend/retry chỉ xảy ra khi
         mail chậm/sai — ngoài phạm vi guard happy path này.)

Guard assertion:
    hybrid_step_paths == [PROMO_LANDING] + golden_step_paths   (đúng thứ tự)
    ⇔ bỏ đúng delta hợp lệ (promo landing ở đầu) khỏi hybrid → phần còn lại
      KHỚP golden byte-path-for-byte-path.
    BẤT KỲ lệch nào KHÁC (double-POST create_account quay lại, GET lạ thêm,
    thiếu bước, đảo thứ tự) → FAIL.

DRY: tái dùng fakes (FakeCurlSession recorder, FakeTokenGenerator,
    FakeMailProvider, builders, GOLDEN_STEP_PATHS) từ ``test/check_hybrid_drift.py``
    — KHÔNG định nghĩa lại.

Chạy:  .venv/bin/python test/check_run_step_guard.py
EXPECTED OUTCOME (code SAU fix 4.1/4.2): PASS — hybrid = golden + đúng promo landing.
"""
from __future__ import annotations

import asyncio
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
    _SUCCESS_CALLBACK,
    _build_golden,
    _build_hybrid,
    _patch_sleep_fast,
)

# Delta hợp lệ đã biết — promo landing GET (query/fragment bị FakeCurlSession
# strip ở _handle qua url.split("?")[0] → còn lại path gốc).
PROMO_LANDING_PATH = "https://chatgpt.com/"


def _run_happy_path_paths(loop) -> tuple[list[str], list[str], int, int]:
    """Chạy golden + hybrid trên CÙNG happy path (OTP về ngay) → step paths."""
    # Golden: OTP về ngay, 1× validate ok, create_account success.
    gsession = FakeCurlSession(
        validate_outcomes=[200],
        create_account_responses=[_SUCCESS_CALLBACK],
    )
    _build_golden(gsession).run()

    # Hybrid: cùng input (mail trả 1 code ngay → 1× send + 1× validate).
    hsession = FakeCurlSession(
        validate_outcomes=[200],
        create_account_responses=[_SUCCESS_CALLBACK],
    )
    _build_hybrid(hsession, loop, FakeMailProvider(["123456"])).run()

    return (
        gsession.step_paths(),
        hsession.step_paths(),
        gsession.create_account_post_count(),
        hsession.create_account_post_count(),
    )


def _strip_known_valid_delta(hybrid_paths: list[str]) -> tuple[list[str], bool]:
    """Bỏ ĐÚNG delta hợp lệ (promo landing ở đầu) khỏi hybrid.

    Trả ``(remaining, promo_at_head)``. Chỉ strip 1 promo landing DUY NHẤT ở
    INDEX 0 — nếu promo landing xuất hiện ở vị trí khác / nhiều lần thì đó là
    drift, không strip (remaining sẽ lệch golden → fail).
    """
    promo_at_head = bool(hybrid_paths) and hybrid_paths[0] == PROMO_LANDING_PATH
    if promo_at_head:
        return hybrid_paths[1:], True
    return list(hybrid_paths), False


def _diff_sequences(expected: list[str], actual: list[str]) -> list[str]:
    """Liệt kê điểm lệch giữa 2 chuỗi bước (theo index)."""
    diffs: list[str] = []
    n = max(len(expected), len(actual))
    for i in range(n):
        e = expected[i] if i < len(expected) else "∅ (thiếu)"
        a = actual[i] if i < len(actual) else "∅ (thiếu)"
        if e != a:
            diffs.append(f"  [{i}] golden_skeleton={e}  |  hybrid={a}")
    return diffs


def main() -> int:
    _patch_sleep_fast()

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        golden_paths, hybrid_paths, g_ca, h_ca = _run_happy_path_paths(loop)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5.0)

    print("=" * 70)
    print("RUN STEP GUARD — hybrid.run() vs golden.run() (happy path, OTP 1 lần)")
    print("=" * 70)
    print(f"golden step paths ({len(golden_paths)}):")
    for i, p in enumerate(golden_paths):
        print(f"  [{i}] {p}")
    print(f"hybrid step paths ({len(hybrid_paths)}):")
    for i, p in enumerate(hybrid_paths):
        print(f"  [{i}] {p}")

    failures: list[str] = []

    # ── Sanity: golden skeleton đúng như đặc tả (bảo vệ chính baseline) ──
    if golden_paths != GOLDEN_STEP_PATHS:
        failures.append(
            "golden ChatGPTRelay.run() KHÔNG khớp GOLDEN_STEP_PATHS — "
            "golden skeleton đã đổi (kiểm tra package golden):\n"
            + "\n".join(_diff_sequences(GOLDEN_STEP_PATHS, golden_paths))
        )

    # ── Guard chính: hybrid == golden (không còn promo landing) ──
    # Trước đây hybrid chèn 1× promo landing TRƯỚC csrf. Đã BỎ (camoufox launch
    # qua proxy timeout 90s vì GET / extra) → hybrid = golden skeleton thuần.
    # Bất kỳ promo landing nào xuất hiện trở lại = regression.
    expected_hybrid = list(golden_paths)
    remaining, promo_at_head = _strip_known_valid_delta(hybrid_paths)

    if promo_at_head:
        failures.append(
            f"REGRESSION: promo landing ({PROMO_LANDING_PATH}) xuất hiện trở lại "
            "ở đầu hybrid.run() — đã yêu cầu bỏ (launch qua proxy timeout). "
            "Xoá `_visit_promo_landing()` khỏi reg_hybrid/relay.py."
        )

    if hybrid_paths != expected_hybrid:
        seq_diff = _diff_sequences(golden_paths, hybrid_paths)
        extra = [p for p in hybrid_paths if p not in golden_paths]
        missing = [p for p in golden_paths if p not in hybrid_paths]
        failures.append(
            "DRIFT chuỗi bước: hybrid KHÔNG khớp golden skeleton (sau khi bỏ promo).\n"
            f"  step THỪA (golden không có): {extra or '∅'}\n"
            f"  step THIẾU (golden có):      {missing or '∅'}\n"
            "  diff theo index (golden_skeleton vs hybrid):\n"
            + ("\n".join(seq_diff) if seq_diff else "    (không có lệch theo index)")
        )

    # ── Guard phụ: create_account POST đúng 1 lần như golden (chống double-POST) ──
    if h_ca != 1 or h_ca != g_ca:
        failures.append(
            f"create_account POST count drift: hybrid={h_ca}, golden={g_ca} "
            "(phải == 1 — chống double-POST quay lại)"
        )

    print("\n" + "=" * 70)
    if failures:
        print(f"RESULT: FAIL — {len(failures)} vi phạm guard:")
        for i, f in enumerate(failures, 1):
            print(f"\n[{i}] {f}")
        print(
            "\n→ Chuỗi bước hybrid.run() đã DRIFT khỏi golden. "
            "Bất kỳ bước/thứ tự lệch ngoài OTP loop là regression."
        )
        return 1

    print(
        "RESULT: PASS — hybrid.run() = golden skeleton thuần (đã bỏ promo).\n"
        f"  Δ OTP loop happy path = golden (1× send + 1× validate).\n"
        f"  create_account POST = {h_ca} (== golden {g_ca}, không double-POST).\n"
        "  Mọi bước khớp golden đúng thứ tự — không drift."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
