"""Verify db/repositories allowlist + validators cho 6 proxy.* knob (round-trip).

Run: .venv/bin/python test/check_proxy_settings_allowlist.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from gpt_signup_hybrid_new.db.engine import DatabaseEngine  # noqa: E402
from gpt_signup_hybrid_new.db.repositories import (  # noqa: E402
    RepositoryError,
    SettingsRepository,
    _EXACT_KEYS,
)

_KEYS_VALID = {
    "proxy.probe_endpoint": "https://api64.ipify.org",
    "proxy.probe_timeout": 6,
    "proxy.max_tries": 5,
    "proxy.sid_len": 8,
    "proxy.sid_retry_per_line": 2,
    "proxy.probe_concurrency": 4,
}


def t01_round_trip() -> int:
    # allowlist membership
    for key in _KEYS_VALID:
        if key not in _EXACT_KEYS:
            print(f"[FAIL] t01 {key} not in _EXACT_KEYS", flush=True)
            return 1
    # real set/get round-trip qua temp DB
    with tempfile.TemporaryDirectory() as d:
        eng = DatabaseEngine(Path(d) / "t.db")
        repo = SettingsRepository(eng)
        for key, val in _KEYS_VALID.items():
            repo.set(key, val)
            got = repo.get(key)
            if got != val:
                print(f"[FAIL] t01 round-trip {key} :: set {val!r} got {got!r}", flush=True)
                return 1
        eng.close()
    print("[PASS] t01 6 proxy.* key allowlist + round-trip", flush=True)
    return 0


def t02_reject_invalid() -> int:
    bad = [
        ("proxy.probe_concurrency", 99),     # > range 10
        ("proxy.probe_concurrency", 0),      # < range 1
        ("proxy.max_tries", 0),              # < 1
        ("proxy.max_tries", 21),             # > 20
        ("proxy.probe_timeout", 2),          # < 3
        ("proxy.sid_len", 33),               # > 32
        ("proxy.probe_endpoint", "ftp://x"), # not http
        ("proxy.probe_endpoint", ""),        # empty
        ("proxy.probe_concurrency", True),   # bool not int
    ]
    with tempfile.TemporaryDirectory() as d:
        eng = DatabaseEngine(Path(d) / "t.db")
        repo = SettingsRepository(eng)
        for key, val in bad:
            try:
                repo.set(key, val)
            except RepositoryError:
                continue
            print(f"[FAIL] t02 accepted invalid {key}={val!r}", flush=True)
            return 1
        eng.close()
    print("[PASS] t02 reject invalid type/range (10 cases)", flush=True)
    return 0


def main() -> int:
    print("=== check_proxy_settings_allowlist ===", flush=True)
    tests = [t01_round_trip, t02_reject_invalid]
    failures = 0
    for fn in tests:
        try:
            rc = fn()
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {fn.__name__} :: raised {type(exc).__name__}: {exc}", flush=True)
            rc = 1
        if rc != 0:
            failures += 1
    print(f"=== done :: {len(tests) - failures}/{len(tests)} pass ===", flush=True)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
