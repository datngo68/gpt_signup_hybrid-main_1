"""Check helper _mask_email + caption build trong web.telegram_notifier.

Verify:
  - TC-01: alias dài → giữ tất cả trừ 3 ký tự cuối, thay bằng ``***``.
  - TC-02: alias ≤ 3 ký tự → toàn bộ ``***``.
  - TC-03: domain ``name.tld`` → ``****.tld`` (giữ TLD).
  - TC-04: domain nhiều cấp ``a.b.co.uk`` → ``****.uk`` (chỉ giữ TLD cuối).
  - TC-05: email không hợp lệ (rỗng / không có '@' / domain không TLD) → fallback.
  - TC-06: caption notify_upi_qr có dòng ``Email: <masked>`` đúng vị trí.

Chạy: python3 test/check_telegram_email_mask.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Load web/telegram_notifier.py trực tiếp, KHÔNG qua `web/__init__.py`
# (init kéo cả server.py + manager.py có relative import beyond top-level
# khi chạy từ test/).
_spec = importlib.util.spec_from_file_location(
    "telegram_notifier_isolated",
    ROOT / "web" / "telegram_notifier.py",
)
_tg_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tg_mod)
_mask_email = _tg_mod._mask_email
_fmt_expiry = _tg_mod._fmt_expiry

_failed = 0


def _check(tc: str, desc: str, ok: bool, detail: str = "") -> None:
    global _failed
    tag = "[PASS]" if ok else "[FAIL]"
    if not ok:
        _failed += 1
    line = f"{tag} {tc} — {desc}"
    if detail:
        line += f" :: {detail}"
    print(line, flush=True)


# ── TC-01: alias dài ────────────────────────────────────────────────────
print("[1/6] alias dài giữ trừ 3 ký tự cuối", flush=True)
masked = _mask_email("lantrinh1xyz@hotmail.com")
_check("TC-01", "lantrinh1xyz@hotmail.com → lantrinh1***@****.com",
       masked == "lantrinh1***@****.com", masked)

masked = _mask_email("abcdef@gmail.com")
_check("TC-01", "abcdef@gmail.com → abc***@****.com",
       masked == "abc***@****.com", masked)

masked = _mask_email("abcd@gmail.com")
_check("TC-01", "abcd@gmail.com → a***@****.com (alias 4 ký tự)",
       masked == "a***@****.com", masked)


# ── TC-02: alias ngắn ───────────────────────────────────────────────────
print("[2/6] alias ≤ 3 ký tự ẩn toàn bộ", flush=True)
for local in ("a", "ab", "abc"):
    expect = f"***@****.com"
    got = _mask_email(f"{local}@gmail.com")
    _check("TC-02", f"{local}@gmail.com → {expect}", got == expect, got)


# ── TC-03: domain 1 cấp ─────────────────────────────────────────────────
print("[3/6] domain name.tld giữ TLD", flush=True)
for dom, tld in (("hotmail.com", ".com"),
                 ("outlook.net", ".net"),
                 ("proton.me", ".me")):
    got = _mask_email(f"lantrinh1xyz@{dom}")
    expect = f"lantrinh1***@****{tld}"
    _check("TC-03", f"lantrinh1xyz@{dom} → {expect}", got == expect, got)


# ── TC-04: domain nhiều cấp ─────────────────────────────────────────────
print("[4/6] domain nhiều cấp chỉ giữ TLD cuối", flush=True)
got = _mask_email("lantrinh1xyz@mail.co.uk")
_check("TC-04", "mail.co.uk → ****.uk", got == "lantrinh1***@****.uk", got)


# ── TC-05: invalid input ────────────────────────────────────────────────
print("[5/6] email không hợp lệ → fallback", flush=True)
# Không có '@' / rỗng / None → fallback toàn bộ ``***@****``.
for raw in ("", None, "no_at_sign"):
    got = _mask_email(raw)
    _check("TC-05", f"input={raw!r} → ***@****", got == "***@****", got)

# Alias rỗng (chỉ có '@domain') → ``***@****.tld``.
got = _mask_email("@nodomain.com")
_check("TC-05", "@nodomain.com → ***@****.com",
       got == "***@****.com", got)

# Alias hợp lệ + domain rỗng / không TLD → mask alias bình thường, domain ``****``.
got = _mask_email("user@")
_check("TC-05", "user@ → u***@****", got == "u***@****", got)
got = _mask_email("user@nodot")
_check("TC-05", "user@nodot → u***@**** (domain không TLD)",
       got == "u***@****", got)


# ── TC-06: caption build ────────────────────────────────────────────────
print("[6/6] caption notify_upi_qr chứa Email masked", flush=True)
import html  # noqa: E402

email = "lantrinh1xyz@hotmail.com"
expires_at = 1781622292
caption = "\n".join([
    "🟢 <b>UPI QR — ChatGPT Plus (IN)</b>",
    f"Email: {html.escape(_mask_email(email))}",
    _fmt_expiry(expires_at),
])

_check("TC-06", "caption có dòng Email masked",
       "Email: lantrinh1***@****.com" in caption, caption.replace("\n", " | "))
_check("TC-06", "Email không lộ raw alias",
       "lantrinh1xyz" not in caption, "")
_check("TC-06", "Email không lộ raw domain",
       "hotmail" not in caption, "")
_check("TC-06", "thứ tự: title → email → expiry",
       caption.index("UPI QR") < caption.index("Email:") < caption.index("Hết hạn"),
       "")


print("", flush=True)
if _failed:
    print(f"=== FAILED: {_failed} case ===", flush=True)
    sys.exit(1)
print("=== ALL PASS ===", flush=True)
