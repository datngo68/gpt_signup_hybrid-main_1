#!/usr/bin/env python3
"""Test expire enforcement logic.

Cover:
    TC-01: Dev mode (no _expire_const) → bypass, không exit.
    TC-02: now < BUILD_TIME (tua quá khứ) → block.
    TC-03: now > EXPIRES_AT (hết hạn) → block.
    TC-04: now < last_seen - GRACE (tua lùi sau khi đã chạy) → block.
    TC-05: Bình thường (BUILD_TIME ≤ now ≤ EXPIRES_AT, no last_seen) → pass.
    TC-06: Online time mismatch lớn + online > expires → block (chống tua giờ).
    TC-07: BUILD_TIME malformed (=0) → fail-open (return).

Chạy:
    .venv/bin/python3 test/check_expire_logic.py
"""
from __future__ import annotations

import importlib
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


def _fresh_expire_module():
    """Reload _expire_check để pick up thay đổi của _expire_const + reset
    state path."""
    mod_name = "gpt_signup_hybrid_new._expire_check"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    return importlib.import_module(mod_name)


def _set_expire_const(*, build_time: int, expires_at: int) -> None:
    """Inject fake _expire_const vào package."""
    fake = types.ModuleType("gpt_signup_hybrid_new._expire_const")
    fake.BUILD_TIME = build_time
    fake.EXPIRES_AT = expires_at
    sys.modules["gpt_signup_hybrid_new._expire_const"] = fake


def _clear_expire_const() -> None:
    sys.modules.pop("gpt_signup_hybrid_new._expire_const", None)


class _ExitGuard:
    """Context: capture sys.exit instead of letting it kill test runner."""
    def __init__(self):
        self.code = None

    def __enter__(self):
        self._orig = sys.exit
        def fake_exit(code=0):
            self.code = code
            raise _ExitGuardException()
        sys.exit = fake_exit
        return self

    def __exit__(self, *exc):
        sys.exit = self._orig
        return False


class _ExitGuardException(Exception):
    pass


def _patch_state_file(mod, tmpdir: Path) -> None:
    """Override _state_file_path để mỗi test case dùng tmpdir riêng."""
    state_file = tmpdir / ".gsh_state_test"
    mod._state_file_path = lambda: state_file  # type: ignore[attr-defined]


def _patch_online_offline(mod) -> None:
    """Force fail offline path."""
    mod._fetch_online_time = lambda: None  # type: ignore[attr-defined]


def _patch_online_time(mod, ts: int) -> None:
    mod._fetch_online_time = lambda: ts  # type: ignore[attr-defined]


def tc01_dev_mode_no_const_bypasses():
    _clear_expire_const()
    mod = _fresh_expire_module()
    # Không cần patch — _expire_const không tồn tại → return ngay.
    mod.enforce_expiry()
    print("[PASS] TC-01 dev mode (no _expire_const) → bypass, không exit", flush=True)


def tc02_now_before_build_time_blocks():
    import time
    now = int(time.time())
    _set_expire_const(build_time=now + 86400, expires_at=now + 86400 * 30)  # build "tương lai"
    mod = _fresh_expire_module()
    with tempfile.TemporaryDirectory() as td:
        _patch_state_file(mod, Path(td))
        _patch_online_offline(mod)
        with _ExitGuard() as guard:
            try:
                mod.enforce_expiry()
                raise AssertionError("expected sys.exit")
            except _ExitGuardException:
                pass
        assert guard.code == 2, f"expected exit code 2, got {guard.code}"
    print("[PASS] TC-02 now < BUILD_TIME → block exit code 2", flush=True)


def tc03_now_after_expires_blocks():
    import time
    now = int(time.time())
    _set_expire_const(build_time=now - 86400 * 30, expires_at=now - 60)  # expired 1 phút trước
    mod = _fresh_expire_module()
    with tempfile.TemporaryDirectory() as td:
        _patch_state_file(mod, Path(td))
        _patch_online_offline(mod)
        with _ExitGuard() as guard:
            try:
                mod.enforce_expiry()
                raise AssertionError("expected sys.exit")
            except _ExitGuardException:
                pass
        assert guard.code == 2
    print("[PASS] TC-03 now > EXPIRES_AT → block", flush=True)


def tc04_clock_rewind_blocks():
    import time
    now = int(time.time())
    _set_expire_const(build_time=now - 86400, expires_at=now + 86400)
    mod = _fresh_expire_module()
    with tempfile.TemporaryDirectory() as td:
        _patch_state_file(mod, Path(td))
        # Seed last_seen ở tương lai xa (giả lập user đã chạy app sau đó tua lùi)
        state_file = mod._state_file_path()
        state_file.parent.mkdir(parents=True, exist_ok=True)
        future = now + 86400 * 7  # 7 ngày sau (vượt 1h grace)
        state_file.write_text(str(future), encoding="ascii")

        _patch_online_offline(mod)
        with _ExitGuard() as guard:
            try:
                mod.enforce_expiry()
                raise AssertionError("expected sys.exit")
            except _ExitGuardException:
                pass
        assert guard.code == 2
    print("[PASS] TC-04 last_seen ở tương lai xa → block (clock rewind)", flush=True)


def tc05_normal_passes():
    import time
    now = int(time.time())
    _set_expire_const(build_time=now - 3600, expires_at=now + 86400)
    mod = _fresh_expire_module()
    with tempfile.TemporaryDirectory() as td:
        _patch_state_file(mod, Path(td))
        _patch_online_offline(mod)
        # Không có last_seen → first run, ratchet không enforce
        mod.enforce_expiry()  # phải không exit
        # Sau enforce, last_seen đã được ghi ≈ now
        last_seen_str = mod._state_file_path().read_text(encoding="ascii").strip()
        assert int(last_seen_str) >= now, \
            f"last_seen should be >= now, got {last_seen_str}"
    print("[PASS] TC-05 BUILD_TIME ≤ now ≤ EXPIRES_AT → pass + ratchet ghi", flush=True)


def tc06_online_time_overrides_expired():
    import time
    now_local = int(time.time())
    # Local clock đứng yên trong hạn, nhưng online time đã quá hạn 2 ngày
    # (case: user tua local về quá khứ để "ở trong hạn"; online time thật
    # đã expired).
    _set_expire_const(
        build_time=now_local - 86400 * 30,
        expires_at=now_local + 3600,  # local: còn 1 giờ
    )
    mod = _fresh_expire_module()
    with tempfile.TemporaryDirectory() as td:
        _patch_state_file(mod, Path(td))
        # Online time = local + 2 ngày → vượt EXPIRES_AT
        _patch_online_time(mod, now_local + 86400 * 2)
        with _ExitGuard() as guard:
            try:
                mod.enforce_expiry()
                raise AssertionError("expected sys.exit (online expired)")
            except _ExitGuardException:
                pass
        assert guard.code == 2
    print("[PASS] TC-06 online time > EXPIRES_AT (local còn hạn) → block", flush=True)


def tc07_malformed_const_fail_open():
    _set_expire_const(build_time=0, expires_at=0)
    mod = _fresh_expire_module()
    # Không patch state file vì path_file không được dùng (return early).
    mod.enforce_expiry()  # không exit
    print("[PASS] TC-07 BUILD_TIME=0 hoặc EXPIRES_AT=0 → fail-open (dev safety)", flush=True)


def main():
    tests = [
        tc01_dev_mode_no_const_bypasses,
        tc02_now_before_build_time_blocks,
        tc03_now_after_expires_blocks,
        tc04_clock_rewind_blocks,
        tc05_normal_passes,
        tc06_online_time_overrides_expired,
        tc07_malformed_const_fail_open,
    ]
    failures = 0
    for tc in tests:
        try:
            tc()
        except AssertionError as exc:
            print(f"[FAIL] {tc.__name__} :: {exc}", flush=True)
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {tc.__name__} :: {type(exc).__name__}: {exc}", flush=True)
            failures += 1
        finally:
            _clear_expire_const()
    print(
        f"\n{len(tests) - failures}/{len(tests)} passed"
        + (f" — {failures} failures" if failures else ""),
        flush=True,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
