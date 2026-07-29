"""Phase 1 verify — AST parse tất cả file đã modify + import smoke.

File modified (Phase 1):
    _nextauth_bootstrap.py    (+ helpers oai-asli)
    browser_phase.py          (logging_id + locale auto)
    session_phase.py          (logging_id 2 chỗ)
    db/repositories.py        (+ 6 settings keys)
    config.py                 (browser_use_profile_template default=False)
    cli.py                    (profile_template CLI default=False)
    models.py                 (locale + timezone fields, profile_template default)
    random_profile.py         (+ random_profile_for_locale)
    signup.py                 (wire random_profile_for_locale)

File new:
    _geo_locale.py

Chạy: .venv/bin/python3 test/syntax_check_phase1.py
"""
from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


PHASE1_FILES = [
    "_nextauth_bootstrap.py",
    "_geo_locale.py",
    "_human_input.py",
    "_datadog_session.py",
    "browser_phase.py",
    "session_phase.py",
    "db/repositories.py",
    "db/schema.py",
    "config.py",
    "cli.py",
    "models.py",
    "random_profile.py",
    "signup.py",
    "user_agent_profile.py",
    "sentinel_quickjs.py",
    "sentinel_pow.py",
    "request_phase.py",
]


def main() -> int:
    failures: list[str] = []

    # ── 1. AST parse từng file ──────────────────────────
    print("── AST parse Phase 1 files ──")
    for rel in PHASE1_FILES:
        p = ROOT / rel
        if not p.exists():
            failures.append(f"{rel}: file missing")
            print(f"  [FAIL] {rel}: file missing")
            continue
        try:
            ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            print(f"  [PASS] {rel}")
        except SyntaxError as e:
            failures.append(f"{rel}: SyntaxError: {e}")
            print(f"  [FAIL] {rel}: {e}")

    # ── 2. Import smoke test (modules pure-Python, không cần Camoufox) ──
    print()
    print("── Import smoke test ──")
    pure_modules = [
        "_nextauth_bootstrap",
        "_geo_locale",
        "_human_input",
        "_datadog_session",
        "random_profile",
        "models",
        "user_agent_profile",
    ]
    for m in pure_modules:
        try:
            importlib.import_module(m)
            print(f"  [PASS] import {m}")
        except Exception as e:  # noqa: BLE001
            failures.append(f"import {m}: {e}")
            print(f"  [FAIL] import {m}: {e}")

    # ── 3. Cross-check: helper public APIs available ──
    print()
    print("── Public API smoke ──")
    try:
        from _nextauth_bootstrap import (
            read_oai_asli_from_ctx,
            read_oai_asli_from_session,
            bootstrap_authorize_url,
            BOOTSTRAP_JS,
        )
        print("  [PASS] _nextauth_bootstrap exports 4 public symbols")
    except ImportError as e:
        failures.append(f"_nextauth_bootstrap exports: {e}")

    try:
        from _geo_locale import (
            lookup_proxy_country,
            locale_for_country,
            resolve_proxy_locale,
            clear_cache,
        )
        print("  [PASS] _geo_locale exports 4 public symbols")
    except ImportError as e:
        failures.append(f"_geo_locale exports: {e}")

    try:
        from _human_input import (
            human_type,
            human_click,
            random_mouse_wander,
            dwell,
        )
        print("  [PASS] _human_input exports 4 public symbols")
    except ImportError as e:
        failures.append(f"_human_input exports: {e}")

    try:
        from _datadog_session import gen_dd_s_cookie, inject_dd_s
        print("  [PASS] _datadog_session exports 2 public symbols")
    except ImportError as e:
        failures.append(f"_datadog_session exports: {e}")

    try:
        from user_agent_profile import (
            BrowserPersona,
            CHROME_145_WIN,
            FIREFOX_135_MAC,
            get_persona,
        )
        print("  [PASS] user_agent_profile exports BrowserPersona + 2 personas")
    except ImportError as e:
        failures.append(f"user_agent_profile exports: {e}")

    try:
        from random_profile import (
            random_profile,
            random_profile_for_locale,
            random_india_profile,
            random_password,
        )
        print("  [PASS] random_profile exports 4 public symbols")
    except ImportError as e:
        failures.append(f"random_profile exports: {e}")

    print()
    if failures:
        print(f"[FAIL] {len(failures)} failures:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print(f"[OK] Phase 1 syntax check pass ({len(PHASE1_FILES)} files).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
