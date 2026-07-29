"""Check feature Telegram notify + UPI QR expires_at countdown.

Verify (không gọi network):
  - SettingsRepository chấp nhận key mới + reject sai type.
  - upi_runner._find_qr_expires_at trích đúng expires_at từ log probe thật.
  - telegram_notifier format caption/expiry + spoiler combo đúng.

Chạy: python3 test/check_telegram_upi_feature.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_passed = 0
_failed = 0


def _check(tc: str, desc: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    tag = "[PASS]" if cond else "[FAIL]"
    if cond:
        _passed += 1
    else:
        _failed += 1
    print(f"{tag} {tc} — {desc} :: {detail}", flush=True)


def _load_module(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = mod  # dataclass cần module có trong sys.modules
    spec.loader.exec_module(mod)
    return mod


# ── TC-01: Settings whitelist + type constraint ──────────────────────────
print("[1/4] Settings whitelist + type constraint", flush=True)
from db.repositories import _EXACT_KEYS, _SENSITIVE_KEYS, _validate_type_constraint, RepositoryError

for key in ("upi.notify_enabled", "telegram.bot_token", "telegram.chat_id"):
    _check("TC-01", f"{key} trong whitelist", key in _EXACT_KEYS, key)
_check("TC-01", "telegram.bot_token là sensitive", "telegram.bot_token" in _SENSITIVE_KEYS, "")

# Accept hợp lệ
try:
    _validate_type_constraint("upi.notify_enabled", True)
    _validate_type_constraint("telegram.bot_token", "123:ABC")
    _validate_type_constraint("telegram.bot_token", None)
    _validate_type_constraint("telegram.chat_id", "123456")
    _validate_type_constraint("telegram.chat_id", None)
    _check("TC-01", "accept giá trị hợp lệ", True, "no raise")
except RepositoryError as exc:
    _check("TC-01", "accept giá trị hợp lệ", False, str(exc))

# Reject sai type
def _expect_raise(key, val):
    try:
        _validate_type_constraint(key, val)
        return False
    except RepositoryError:
        return True

_check("TC-01", "reject notify_enabled='yes'", _expect_raise("upi.notify_enabled", "yes"), "")
_check("TC-01", "reject bot_token=123 (int)", _expect_raise("telegram.bot_token", 123), "")
_check("TC-01", "reject chat_id quá dài", _expect_raise("telegram.chat_id", "x" * 65), "")


# ── TC-02: _find_qr_expires_at trên log probe thật ───────────────────────
print("[2/4] upi_runner._find_qr_expires_at", flush=True)
upi_runner = _load_module("upi_runner_standalone", "web/upi_runner.py")

# Synthetic match giống output _find_matches cho qr_code
synthetic = [
    {"source": "confirm:qr_code", "path": "$.next_action.upi_handle_redirect_or_display_qr_code.qr_code",
     "kind": "key", "value": {"expires_at": 1781622292, "image_url_png": "https://qr.stripe.com/x.png"}},
]
exp = upi_runner._find_qr_expires_at(synthetic)
_check("TC-02", "trích expires_at từ synthetic", exp == 1781622292, f"got={exp}")

# Ignore dict không có image_url
_check("TC-02", "bỏ qua dict không có image_url",
       upi_runner._find_qr_expires_at([{"value": {"expires_at": 123}}]) is None, "")

# Trên log thật nếu có
logs = sorted((ROOT / "runtime" / "research_logs").glob("upi_qr_probe_*.json"))
real_log = next((p for p in logs if "instructions" in p.read_text(errors="ignore")
                 or "expires_at" in p.read_text(errors="ignore")), None)
if real_log:
    data = json.loads(real_log.read_text())
    matches = data.get("matches", [])
    exp_real = upi_runner._find_qr_expires_at(matches)
    _check("TC-02", f"trích expires_at từ {real_log.name}",
           isinstance(exp_real, int) and exp_real > 0, f"got={exp_real}")
else:
    print("[skip] TC-02 — không có log probe chứa expires_at", flush=True)


# ── TC-03: telegram_notifier formatting ──────────────────────────────────
print("[3/4] telegram_notifier formatting", flush=True)
tg = _load_module("telegram_notifier_standalone", "web/telegram_notifier.py")

_check("TC-03", "_fmt_amount(2000)", tg._fmt_amount(2000) == "₹20.00", tg._fmt_amount(2000))
_check("TC-03", "_fmt_amount(0)", tg._fmt_amount(0) == "-", "")
exp_text = tg._fmt_expiry(1781622292)
_check("TC-03", "_fmt_expiry có VN + IN", "Hết hạn:" in exp_text and "Expired:" in exp_text and " VN" in exp_text and " IN" in exp_text, exp_text.replace("\n", " | "))
_check("TC-03", "_fmt_expiry None", tg._fmt_expiry(None) == "Hết hạn: không xác định", "")

# Notifier state machine
n = tg.TelegramNotifier()
_check("TC-03", "default disabled", n.enabled is False, "")
_check("TC-03", "default not configured", n.configured is False, "")
n.apply_settings({"upi.notify_enabled": True, "telegram.bot_token": "123:ABC", "telegram.chat_id": "999"})
_check("TC-03", "apply_settings enabled+configured", n.enabled and n.configured, f"token={n.bot_token} chat={n.chat_id}")
n.set_credentials("  ", "")
_check("TC-03", "set_credentials blank → not configured", n.configured is False, "")


# ── TC-04: spoiler reply skip logic ──────────────────────────────────────
print("[4/4] notify_upi_qr skip logic (no network)", flush=True)
import asyncio

async def _run_skip():
    n2 = tg.TelegramNotifier()
    # disabled → False
    r1 = await n2.notify_upi_qr(email="a@b.c", password="p", secret=None, amount=0,
                                qr_path=None, qr_expires_at=None)
    # enabled nhưng chưa configured → False
    n2.set_enabled(True)
    r2 = await n2.notify_upi_qr(email="a@b.c", password="p", secret=None, amount=0,
                                qr_path=None, qr_expires_at=None)
    # configured nhưng qr_path None → False (không gọi network)
    n2.set_credentials("123:ABC", "999")
    r3 = await n2.notify_upi_qr(email="a@b.c", password="p", secret=None, amount=0,
                                qr_path=None, qr_expires_at=None)
    return r1, r2, r3

r1, r2, r3 = asyncio.run(_run_skip())
_check("TC-04", "disabled → skip", r1 is False, str(r1))
_check("TC-04", "not configured → skip", r2 is False, str(r2))
_check("TC-04", "no qr_path → skip", r3 is False, str(r3))


print(f"\n=== RESULT: {_passed} passed, {_failed} failed ===", flush=True)
sys.exit(1 if _failed else 0)
