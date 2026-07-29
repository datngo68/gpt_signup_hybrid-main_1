#!/usr/bin/env python3
"""Verify diagnostic + fail-fast cho UPI proxy resolution.

Bug fix mục tiêu: trước fix, nếu line đầu pool format lỗi → `_safe_materialize`
nuốt ValueError → `first_proxy=None` → flow chạy DIRECT toàn bộ mà user không
biết (cảm giác "chập chờn ăn proxy"). Sau fix:
    - Log warning chi tiết format error.
    - Raise UpiQrError nếu pool có entries nhưng resolve fail.
    - Log "proxy policy" diagnostic show first_proxy + via_proxy steps + direct steps.

Test cases:
    [TC-01] `_materialize_or_log_warning` log warning khi format lỗi, return None.
    [TC-02] `_materialize_or_log_warning` return URL khi line OK (incl. {SID}).
    [TC-03] `_materialize_or_log_warning` không log khi line None/empty (no spam).
    [TC-04] `run_upi_qr_probe` raise UpiQrError khi pool có entries nhưng line[0] format lỗi.
    [TC-05] `run_upi_qr_probe` log "proxy policy" diagnostic format đúng.

Chạy: python3 test/check_upi_proxy_diagnostic.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _ok(tc, label, detail=""):
    msg = f"[PASS] {tc} — {label}"
    if detail:
        msg += f" :: {detail}"
    print(msg, flush=True)


def _fail(tc, label, detail=""):
    msg = f"[FAIL] {tc} — {label}"
    if detail:
        msg += f" :: {detail}"
    print(msg, flush=True)


def t01_materialize_warning_on_bad_format():
    print("[1/5] TC-01 — _materialize_or_log_warning log warning + return None", flush=True)
    from web.upi_runner import _materialize_or_log_warning

    captured: list[str] = []
    log = lambda s: captured.append(s)

    # `single-token` không đủ host:port → ValueError trong materialize_proxy.
    result = _materialize_or_log_warning("garbage_no_colon", log)
    if result is not None:
        _fail("TC-01", f"expect None, got {result!r}")
        return False
    matched = [m for m in captured if "format-error" in m]
    if not matched:
        _fail("TC-01", "không có log [proxy] format-error", repr(captured))
        return False
    _ok("TC-01", "log warning có 'format-error' tag", matched[0][:120])

    # Verify masked credential trong log (không leak raw line).
    if "garbage_no_colon" not in matched[0]:
        # Mask helper có thể che hết → chỉ cần log có chứa SOMETHING liên quan
        _ok("TC-01", "raw line đã mask trong log (good)")
    return True


def t02_materialize_ok_for_valid_line():
    print("[2/5] TC-02 — _materialize_or_log_warning OK với line hợp lệ", flush=True)
    from web.upi_runner import _materialize_or_log_warning

    log = lambda s: None
    cases = [
        ("host.example.com:8080", "http://host.example.com:8080"),
        ("host:8080:user:pass", "http://user:pass@host:8080"),
        ("http://user:pass@host:8080", "http://user:pass@host:8080"),  # passthrough
    ]
    for line, expect_prefix in cases:
        result = _materialize_or_log_warning(line, log)
        if not result or not result.startswith("http://"):
            _fail("TC-02", f"line {line!r} resolve fail: got {result!r}")
            return False
        _ok("TC-02", f"{line!r} → {result!r}")
    return True


def t03_materialize_silent_on_none():
    print("[3/5] TC-03 — empty/None không spam log", flush=True)
    from web.upi_runner import _materialize_or_log_warning

    captured: list[str] = []
    log = lambda s: captured.append(s)

    for v in (None, "", "   "):
        result = _materialize_or_log_warning(v, log)
        if result is not None:
            _fail("TC-03", f"expect None for {v!r}, got {result!r}")
            return False
    if captured:
        _fail("TC-03", "log spam khi line rỗng (expect silent)", repr(captured)[:200])
        return False
    _ok("TC-03", "rỗng/None → silent return None")
    return True


def t04_runner_failfast_on_bad_pool():
    print("[4/5] TC-04 — run_upi_qr_probe raise UpiQrError khi pool[0] format lỗi", flush=True)
    from web.upi_runner import run_upi_qr_probe, UpiQrError

    captured: list[str] = []
    log = lambda s: captured.append(s)

    async def _attempt():
        try:
            await run_upi_qr_probe(
                email="x@example.com",
                password="dummy",
                secret=None,
                proxy_pool=["this_is_not_a_valid_proxy_line"],  # <-- format lỗi
                approve_retries=10,
                qr_out_path=Path("/tmp/_unused.png"),
                log=log,
                proxy_from_step=1,
            )
        except UpiQrError as exc:
            return exc
        except Exception as exc:  # noqa: BLE001
            return exc
        return None

    exc = asyncio.run(_attempt())
    if exc is None:
        _fail("TC-04", "run_upi_qr_probe không raise dù pool[0] format lỗi")
        return False
    if not isinstance(exc, UpiQrError):
        _fail("TC-04", f"raised {type(exc).__name__} thay vì UpiQrError",
              str(exc)[:200])
        return False
    msg = str(exc)
    if "first_proxy" not in msg or "format" not in msg.lower():
        _fail("TC-04", "error message không đề cập first_proxy/format", msg[:200])
        return False
    _ok("TC-04", f"raised UpiQrError: {msg[:120]}")

    # Verify đã log warning trước khi raise.
    fmt_err_logs = [m for m in captured if "format-error" in m]
    if not fmt_err_logs:
        _fail("TC-04", "thiếu log [proxy] format-error trước khi raise")
        return False
    _ok("TC-04", "log warning xuất hiện trước raise", fmt_err_logs[0][:120])
    return True


def t05_runner_diagnostic_log_format():
    """Verify log `[upi] proxy policy info` xuất hiện với format đúng."""
    print("[5/5] TC-05 — log diagnostic 'proxy policy' show first_proxy + via_proxy + direct", flush=True)
    src = (ROOT / "web" / "upi_runner.py").read_text(encoding="utf-8")
    expectations = [
        ('"upi", "proxy policy"', "log key proxy policy"),
        ('"first_proxy"', "kv first_proxy"),
        ('"via_proxy"', "kv via_proxy"),
        ('"direct"', "kv direct"),
        ('"login"', "step 1 name"),
        ('"checkout"', "step 2 name"),
        ('"stripe_init"', "step 3 name"),
        ('"elements"', "step 4 name"),
        ('"confirm"', "step 5 name"),
        ('"approve"', "step 6 name"),
    ]
    failed = False
    for s, lbl in expectations:
        if s not in src:
            _fail("TC-05", f"missing: {lbl}", repr(s)[:60])
            failed = True
        else:
            _ok("TC-05", lbl)
    return not failed


def main():
    cases = [
        ("TC-01 log warning on bad format", t01_materialize_warning_on_bad_format),
        ("TC-02 OK on valid line", t02_materialize_ok_for_valid_line),
        ("TC-03 silent on empty/None", t03_materialize_silent_on_none),
        ("TC-04 runner fail-fast", t04_runner_failfast_on_bad_pool),
        ("TC-05 diagnostic log format", t05_runner_diagnostic_log_format),
    ]
    failures = 0
    for name, fn in cases:
        try:
            ok = fn()
        except Exception as exc:  # noqa: BLE001
            _fail(name, "raised", repr(exc))
            ok = False
        if not ok:
            failures += 1
        print("", flush=True)
    total = len(cases)
    print(f"[summary] passed={total - failures}/{total}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
