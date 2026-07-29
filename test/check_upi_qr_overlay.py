"""Verify overlay email masked lên ảnh QR (PNG + SVG) trong upi_runner.

Test cases:
    TC-01: PNG QR (qrcode.make) → overlay tăng chiều cao + chứa text masked
           (verify gián tiếp qua dimensions thay đổi và bytes khác).
    TC-02: SVG QR (manually crafted) → root height/viewBox tăng, có <text>
           chứa masked email, có <rect> band trắng.
    TC-03: Magic-bytes detect — extension .svg nhưng payload PNG (case Stripe
           HTML→PNG re-render giữ extension cũ) phải route sang nhánh PNG.
    TC-04: Format không hỗ trợ (bytes ngẫu nhiên) → ValueError.
    TC-05: Mask email khớp với telegram_notifier._mask_email (single source).

Chạy: python3 test/check_upi_qr_overlay.py
"""
from __future__ import annotations

import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

# Đảm bảo workspace root nằm trong sys.path để import gpt_signup_hybrid_new.*
ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT.parent
for p in (str(PARENT), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from gpt_signup_hybrid_new.web.upi_runner import (  # noqa: E402
    _overlay_email_on_qr,
    _overlay_email_on_png,
    _overlay_email_on_svg,
)
from gpt_signup_hybrid_new.web.telegram_notifier import _mask_email  # noqa: E402

FAIL = 0
TOTAL = 0


def _check(tc: str, label: str, ok: bool, detail: str = "") -> None:
    global FAIL, TOTAL
    TOTAL += 1
    tag = "[PASS]" if ok else "[FAIL]"
    if not ok:
        FAIL += 1
    print(f"{tag} {tc} — {label}" + (f" :: {detail}" if detail else ""), flush=True)


def _make_png_qr(path: Path, payload: str = "upi://test") -> None:
    import qrcode  # type: ignore[import-untyped]

    img = qrcode.make(payload)
    img.save(path)


def _make_svg_qr(path: Path, size: int = 256) -> None:
    """Craft 1 SVG QR mẫu (hình chữ nhật đen trên nền trắng) để test overlay."""
    svg = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{size}" height="{size}" viewBox="0 0 {size} {size}">'
        f'<rect x="0" y="0" width="{size}" height="{size}" fill="white"/>'
        f'<rect x="32" y="32" width="64" height="64" fill="black"/>'
        f'</svg>'
    )
    path.write_text(svg, encoding="utf-8")


def tc01_png_overlay() -> None:
    print("[1/5] TC-01 PNG overlay tăng chiều cao + bytes thay đổi", flush=True)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "qr.png"
        _make_png_qr(p, "upi://pay?pa=test@upi&am=20.00")
        before_bytes = p.read_bytes()

        from PIL import Image

        with Image.open(p) as im:
            w0, h0 = im.size

        _overlay_email_on_qr(p, "lantrinh1xyz@hotmail.com")

        after_bytes = p.read_bytes()
        with Image.open(p) as im:
            w1, h1 = im.size

        _check("TC-01", "width giữ nguyên", w0 == w1, f"{w0}→{w1}")
        _check("TC-01", "height tăng", h1 > h0, f"{h0}→{h1}")
        _check("TC-01", "bytes thay đổi", before_bytes != after_bytes,
               f"before={len(before_bytes)} after={len(after_bytes)}")
        _check("TC-01", "PNG signature giữ nguyên",
               after_bytes.startswith(b"\x89PNG\r\n\x1a\n"), "")


def tc02_svg_overlay() -> None:
    print("[2/5] TC-02 SVG overlay viewBox + <text> masked", flush=True)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "qr.svg"
        _make_svg_qr(p, size=256)

        _overlay_email_on_qr(p, "lantrinh1xyz@hotmail.com")

        tree = ET.parse(p)
        root = tree.getroot()
        ns = "{http://www.w3.org/2000/svg}"

        height = float(root.attrib.get("height", "0").replace("px", ""))
        viewbox = root.attrib.get("viewBox", "")
        vb = [float(v) for v in viewbox.split()] if viewbox else []

        _check("TC-02", "height tăng (>256)", height > 256, f"height={height}")
        _check("TC-02", "viewBox 4 phần", len(vb) == 4, f"viewBox={viewbox}")
        if len(vb) == 4:
            _check("TC-02", "viewBox.h tăng (>256)", vb[3] > 256, f"vh={vb[3]}")

        texts = root.findall(f"{ns}text")
        rects = root.findall(f"{ns}rect")
        _check("TC-02", "có ≥1 <text> mới", len(texts) >= 1, f"texts={len(texts)}")
        _check("TC-02", "có ≥2 <rect> (rect cũ + band trắng)",
               len(rects) >= 2, f"rects={len(rects)}")

        # Text element cuối cùng phải chứa masked email.
        if texts:
            masked = _mask_email("lantrinh1xyz@hotmail.com")
            _check("TC-02", f"<text> chứa masked = {masked!r}",
                   texts[-1].text == masked, f"got={texts[-1].text!r}")


def tc03_magic_bytes_detect() -> None:
    print("[3/5] TC-03 magic-bytes detect — file .svg nhưng payload PNG", flush=True)
    with tempfile.TemporaryDirectory() as td:
        # Save 1 PNG nhưng đặt extension .svg (case Stripe trả URL .svg, server
        # sau đó re-render PNG nhưng giữ Path extension cũ).
        p = Path(td) / "qr.svg"
        _make_png_qr(p, "upi://test")
        head = p.read_bytes()[:8]
        _check("TC-03", "file thực sự là PNG (magic ok)",
               head.startswith(b"\x89PNG\r\n\x1a\n"), repr(head))

        # overlay phải route sang nhánh PNG (không raise ParseError).
        try:
            _overlay_email_on_qr(p, "abc@gmail.com")
        except Exception as exc:  # noqa: BLE001
            _check("TC-03", "overlay không raise", False,
                   f"{type(exc).__name__}: {exc}")
            return
        _check("TC-03", "overlay không raise", True, "")
        # Sau overlay vẫn là PNG.
        _check("TC-03", "vẫn là PNG sau overlay",
               p.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"), "")


def tc04_unsupported_format() -> None:
    print("[4/5] TC-04 format không hỗ trợ → ValueError", flush=True)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "qr.bin"
        p.write_bytes(b"GIF89a\x00\x00random_bytes_here_xxxxxxxxxxxxxxxx")
        try:
            _overlay_email_on_qr(p, "abc@gmail.com")
        except ValueError as exc:
            _check("TC-04", "raise ValueError", True, str(exc)[:80])
            return
        except Exception as exc:  # noqa: BLE001
            _check("TC-04", "raise ValueError", False,
                   f"raise {type(exc).__name__} thay vì ValueError")
            return
        _check("TC-04", "raise ValueError", False, "không raise")


def tc05_mask_consistency() -> None:
    print("[5/5] TC-05 _mask_email consistency (single source)", flush=True)
    cases = [
        ("lantrinh1xyz@hotmail.com", "lantrinh1***@****.com"),
        ("abcdef@gmail.com", "abc***@****.com"),
        ("a@b.c", "***@****.c"),
        ("", "***@****"),
        ("invalid", "***@****"),
    ]
    for raw, expected in cases:
        got = _mask_email(raw)
        _check("TC-05", f"_mask_email({raw!r}) → {expected!r}",
               got == expected, f"got={got!r}")


def main() -> int:
    # Sanity: 2 helper sub có thể call trực tiếp (không qua dispatcher).
    print("[0/5] sanity import _overlay_email_on_png/svg", flush=True)
    _check("TC-00", "import _overlay_email_on_png", callable(_overlay_email_on_png))
    _check("TC-00", "import _overlay_email_on_svg", callable(_overlay_email_on_svg))

    tc01_png_overlay()
    tc02_svg_overlay()
    tc03_magic_bytes_detect()
    tc04_unsupported_format()
    tc05_mask_consistency()

    print("", flush=True)
    print(f"[SUMMARY] {TOTAL - FAIL}/{TOTAL} pass", flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
