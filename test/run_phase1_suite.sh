#!/bin/bash
# Phase 1 + 2 + 3 full test suite — chạy mọi test/check_*.py liên quan.
#
# Chạy: bash test/run_phase1_suite.sh
set -u
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python3"
FAIL=0
PASS=0

run() {
  local name="$1"
  local file="$2"
  echo
  echo "═══════════════════════════════════════════════════════════"
  echo "  ▶ $name"
  echo "═══════════════════════════════════════════════════════════"
  if "$PYTHON" "$file"; then
    PASS=$((PASS + 1))
    echo "  ✓ $name PASS"
  else
    FAIL=$((FAIL + 1))
    echo "  ✗ $name FAIL"
  fi
}

# ── Phase 1 tests ──
run "P1.1 logging_id" test/check_logging_id_consistency.py
run "P1.3 settings keys" test/check_settings_keys_anti_ban.py
run "P1.5 fresh profile" test/check_fresh_profile_default.py
run "P1.6 random profile locale" test/check_random_profile_locale.py
run "P1.4 locale geo mapping" test/check_locale_geo_mapping.py

# ── Phase 2 tests ──
run "P2 human input + form refactor" test/check_human_input.py

# ── Phase 3 tests ──
run "P3.1 BrowserPersona dataclass" test/check_persona_dataclass.py
run "P3.2+3.5 sentinel persona + _dd_s" test/check_sentinel_persona_dd_s.py
run "P3.3 persona_cookies persistence" test/check_persona_cookies_persistence.py

# ── Phase 4 tests ──
run "P4 pure_request optimize" test/check_request_phase_p4.py

# ── Phase 5 tests ──
if [ "${SKIP_HAR_ALIGNMENT:-0}" = "1" ]; then
  echo "  ⓘ Skip P5 HAR alignment (SKIP_HAR_ALIGNMENT=1 — golden HAR missing in CI)"
else
  run "P5 HAR alignment self-test" test/check_har_alignment.py
fi

# ── Phase 6 closure tests ──
run "P6 closure (signup wire + session locale + _dd_s)" test/check_phase6_closure.py
run "P6.4 migration v11→v12" test/check_migration_v12.py

# ── Phase 7 final cleanup ──
run "P7 cleanup (dead code + persona wire + audit)" test/check_phase7_cleanup.py

# ── Phase 3.4 closure (oai-sc cookie scope offline verify) ──
if [ "${SKIP_HAR_ALIGNMENT:-0}" = "1" ]; then
  echo "  ⓘ Skip P3.4 oai-sc scope (SKIP_HAR_ALIGNMENT=1 — needs golden HAR)"
else
  run "P3.4 closure oai-sc cookie scope" test/check_oai_sc_scope.py
fi

# ── Syntax check (Phase 1 + 2 + 3 + 4 + 5 + 6 + 7 + 8) ──
run "Syntax check Phase 1-8" test/syntax_check_phase1.py

echo
echo "═══════════════════════════════════════════════════════════"
echo "  Summary: PASS=$PASS  FAIL=$FAIL"
echo "═══════════════════════════════════════════════════════════"
[ "$FAIL" -eq 0 ]
