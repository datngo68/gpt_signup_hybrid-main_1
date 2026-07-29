"""Bước phân loại root cause (gate cho nhánh B).

Spec: .kiro/specs/reg-hybrid-deactivated-after-signup (bugfix), Task 2.

Mục đích (design.md → "Phân loại root cause"):
    Với CÙNG input happy path (OTP về ngay, 1× send + 1× validate, KHÔNG
    retry), ghi đầy đủ ``method + url + body + header order + cookie`` của MỌI
    request của ``HybridChatGPTRelay.run()`` và golden ``ChatGPTRelay.run()``,
    rồi DIFF từng bước.

Gate quyết định:
    - happy path KHÁC golden (step thừa / body / header order / cookie lệch)
      → drift nhánh A là THỰC → fix nhánh A (task 4) là cần & đủ; KHÔNG mở
      nhánh B chỉ vì kết quả này.
    - happy path GIỐNG HỆT golden → nhánh A im lặng trên happy path → nếu
      account thực vẫn bị ban thì nghi vấn dồn về nhánh B (oracle/token layer)
      → mở gate task 5.

DRY: tái dùng fakes (FakeCurlSession recorder, FakeTokenGenerator,
    FakeMailProvider, FakeOTPReader) từ ``test/check_hybrid_drift.py`` — KHÔNG
    định nghĩa lại. Chỉ thêm:
      * RecordingSession: subclass ghi thêm full_url (giữ query) để diff.
      * fixture device_id/logging_id/name/birthdate/_dd_s CỐ ĐỊNH để loại
        nhiễu ngẫu nhiên, diff chỉ còn drift cấu trúc thực.

Đây là test PHÂN LOẠI — KHÔNG sửa code production.

Chạy:  python3 test/check_happy_path_request_diff.py
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
    FakeOTPReader,
    FakeTokenGenerator,
    _SUCCESS_CALLBACK,
    _patch_sleep_fast,
)

# Giá trị CỐ ĐỊNH để loại nhiễu ngẫu nhiên (device_id / logging_id / _dd_s /
# name / birthdate) — diff còn lại = drift cấu trúc thực, byte-comparable.
FIXED_DEVICE_ID = "00000000-0000-4000-8000-000000000abc"
FIXED_LOGGING_ID = "00000000-0000-4000-8000-000000000def"
FIXED_DD_S = "logs=1&id=fixed-dd-session&created=0&expire=0"
FIXED_NAME = "Test User"
FIXED_BIRTHDATE = "1990-01-01"


# ─────────────────────────────────────────────────────────────────────
# Recording session — ghi thêm full_url (FakeCurlSession chỉ giữ path)
# ─────────────────────────────────────────────────────────────────────


class RecordingSession(FakeCurlSession):
    """Như FakeCurlSession nhưng giữ luôn URL đầy đủ (kèm query) để diff."""

    def _handle(self, method, url, headers, json_body, data):
        resp = super()._handle(method, url, headers, json_body, data)
        # record vừa được append trong super()._handle.
        self.records[-1]["full_url"] = url
        return resp


# ─────────────────────────────────────────────────────────────────────
# Builders — fixed identity để diff sạch
# ─────────────────────────────────────────────────────────────────────


def _build_account():
    from chatgpt_camoufox.chatgpt_camoufox.client import Account
    return Account(
        email="probe@example.com",
        password="Passw0rd!xyz",
        api="manual",
        name=FIXED_NAME,
        birthdate=FIXED_BIRTHDATE,
    )


def _normalize_random_cookies(relay) -> None:
    """Ép _dd_s (random per-relay) về giá trị cố định để cookie diff sạch.

    oai-did/logging_id đã cố định qua constructor; _dd_s do
    ``fields.new_datadog_session()`` random nên phải override sau __init__.
    """
    relay._set_cookie("_dd_s", FIXED_DD_S, domain=".openai.com")


def build_hybrid(session, mail_loop, mail_provider):
    from reg_hybrid.relay import HybridChatGPTRelay
    relay = HybridChatGPTRelay(
        _build_account(),
        session=session,
        tokens=FakeTokenGenerator(),
        device_id=FIXED_DEVICE_ID,
        logging_id=FIXED_LOGGING_ID,
        mail_provider=mail_provider,
        mail_loop=mail_loop,
        recipient="probe@example.com",
        otp_timeout_seconds=30.0,
        otp_poll_interval_seconds=5.0,
        otp_resend_after_seconds=20.0,
        max_resends=2,
        log=lambda _m: None,
    )
    _normalize_random_cookies(relay)
    return relay


def build_golden(session, code="123456"):
    from chatgpt_camoufox.chatgpt_camoufox.client import ChatGPTRelay
    relay = ChatGPTRelay(
        _build_account(),
        otp_reader=FakeOTPReader(code),
        session=session,
        tokens=FakeTokenGenerator(),
        device_id=FIXED_DEVICE_ID,
        logging_id=FIXED_LOGGING_ID,
    )
    _normalize_random_cookies(relay)
    return relay


# ─────────────────────────────────────────────────────────────────────
# Summarize + diff
# ─────────────────────────────────────────────────────────────────────


def _summarize(records: list[dict]) -> list[dict]:
    """records (FakeCurlSession) → list bản ghi chuẩn hóa cho diff."""
    out = []
    for r in records:
        headers = r["headers"]
        header_order = list(headers.keys())
        cookie = None
        for k, v in headers.items():
            if k.lower() == "cookie":
                cookie = v
                break
        body = r["json"] if r["json"] is not None else r["data"]
        out.append({
            "method": r["method"],
            "path": r["url"],            # path (đã strip query)
            "full_url": r.get("full_url"),
            "body": body,
            "header_order": header_order,
            "cookie": cookie,
        })
    return out


def _print_request_log(title: str, summary: list[dict]) -> None:
    print(f"\n{'─' * 70}\n{title} — {len(summary)} request(s)\n{'─' * 70}")
    for i, s in enumerate(summary, 1):
        print(f"[{i:>2}] {s['method']:<4} {s['path']}")
        print(f"      body          : {s['body']}")
        print(f"      header order  : {s['header_order']}")
        print(f"      cookie        : {s['cookie']}")


def _diff_common_step(g: dict, h: dict) -> list[str]:
    """So 1 bước chung (cùng path) golden vs hybrid → list điểm khác."""
    diffs = []
    if g["method"] != h["method"]:
        diffs.append(f"method: golden={g['method']} hybrid={h['method']}")
    if g["body"] != h["body"]:
        diffs.append(f"body: golden={g['body']} hybrid={h['body']}")
    if g["header_order"] != h["header_order"]:
        diffs.append(
            f"header order:\n        golden={g['header_order']}"
            f"\n        hybrid={h['header_order']}"
        )
    if g["cookie"] != h["cookie"]:
        diffs.append(f"cookie: golden={g['cookie']} hybrid={h['cookie']}")
    return diffs


def run_diff() -> int:
    _patch_sleep_fast()

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        # ── Golden happy path: OTP về ngay, 1× send + 1× validate ──
        gsession = RecordingSession(
            validate_outcomes=[200],
            create_account_responses=[_SUCCESS_CALLBACK],
        )
        build_golden(gsession).run()

        # ── Hybrid happy path: cùng input (mail trả 1 code ngay) ──
        hsession = RecordingSession(
            validate_outcomes=[200],
            create_account_responses=[_SUCCESS_CALLBACK],
        )
        build_hybrid(hsession, loop, FakeMailProvider(["123456"])).run()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5.0)

    golden = _summarize(gsession.records)
    hybrid = _summarize(hsession.records)

    print("=" * 70)
    print("HAPPY PATH REQUEST DIFF — hybrid vs golden (cùng input, OTP về ngay)")
    print("=" * 70)
    _print_request_log("GOLDEN ChatGPTRelay.run()", golden)
    _print_request_log("HYBRID HybridChatGPTRelay.run()", hybrid)

    g_paths = [s["path"] for s in golden]
    h_paths = [s["path"] for s in hybrid]

    # Bước thừa/thiếu (so theo path, mỗi path xuất hiện 1 lần ở happy path).
    extra = [p for p in h_paths if p not in g_paths]
    missing = [p for p in g_paths if p not in h_paths]

    g_by_path = {s["path"]: s for s in golden}
    h_by_path = {s["path"]: s for s in hybrid}

    print(f"\n{'═' * 70}\nDIFF\n{'═' * 70}")
    print(f"golden step paths ({len(g_paths)}): {g_paths}")
    print(f"hybrid step paths ({len(h_paths)}): {h_paths}")

    n_diff = 0

    if extra:
        n_diff += 1
        print(f"\n[STEP THỪA ở hybrid] (golden KHÔNG có): {extra}")
    if missing:
        n_diff += 1
        print(f"\n[STEP THIẾU ở hybrid] (golden CÓ): {missing}")

    print("\n[FIELD DIFF các bước chung] (method/body/header order/cookie):")
    field_drift = False
    for path in g_paths:
        if path not in h_by_path:
            continue
        diffs = _diff_common_step(g_by_path[path], h_by_path[path])
        if diffs:
            field_drift = True
            n_diff += 1
            print(f"  ✗ {path}")
            for d in diffs:
                print(f"      - {d}")
        else:
            print(f"  ✓ {path} — khớp golden (method/body/header order/cookie)")
    if not field_drift:
        print("  (mọi bước chung khớp golden byte-for-byte)")

    # create_account POST count (happy path golden = 1).
    g_ca = gsession.create_account_post_count()
    h_ca = hsession.create_account_post_count()
    print(f"\n[create_account POST count] golden={g_ca}, hybrid={h_ca}")
    if h_ca != g_ca:
        n_diff += 1
        print(f"  ✗ hybrid POST create_account {h_ca} lần ≠ golden {g_ca}")

    # Skeleton golden check (sanity).
    print(f"\n[Golden skeleton sanity] golden_paths == GOLDEN_STEP_PATHS: "
          f"{g_paths == GOLDEN_STEP_PATHS}")

    # ── Kết luận gate ──
    print(f"\n{'═' * 70}\nKẾT LUẬN GATE\n{'═' * 70}")
    if n_diff == 0:
        print(
            "HAPPY PATH GIỐNG HỆT GOLDEN.\n"
            "→ Nhánh A im lặng trên happy path (create_account 1 lần, không\n"
            "  step thừa, header/body/cookie khớp). Nếu account THỰC vẫn bị ban\n"
            "  trên happy path → loại trừ nhánh A, nghi vấn dồn về NHÁNH B\n"
            "  (oracle/token layer — synthetic Observer feeder / shared browser).\n"
            "→ GATE: MỞ task 5 (chỉ khi có bằng chứng ban happy path thực tế)."
        )
        gate = "B"
    else:
        print(
            f"HAPPY PATH KHÁC GOLDEN ({n_diff} điểm drift).\n"
            "→ Drift nhánh A là THỰC ngay trên happy path (xem các điểm khác ở\n"
            "  trên: step thừa / body / header order / cookie / create_account).\n"
            "→ GATE: fix NHÁNH A (task 4) là cần & đủ để khử các drift này.\n"
            "  KHÔNG mở nhánh B chỉ dựa trên kết quả này (tránh hi sinh tốc độ\n"
            "  pool vô ích) — chỉ mở task 5 nếu sau khi fix nhánh A account thực\n"
            "  vẫn bị ban trên happy path."
        )
        gate = "A"

    print(f"\nGATE_DECISION={gate}; DRIFT_POINTS={n_diff}; "
          f"EXTRA_STEPS={extra}; MISSING_STEPS={missing}")
    return 0 if n_diff >= 0 else 1  # luôn 0: đây là test phân loại, không assert pass/fail


if __name__ == "__main__":
    sys.exit(run_diff())
