#!/bin/bash
set +e
cd /Users/vippro/Developments/gpt_signup_hybrid
export PYTHONUNBUFFERED=1

OUT=test/_all.out
rm -f $OUT

run() {
  local name="$1"; local file="$2"
  echo "=== $name ===" >> $OUT
  .venv/bin/python3 -u "$file" >> $OUT 2>&1
  local ex=$?
  echo "[EXIT=$ex]" >> $OUT
  echo "" >> $OUT
}

run P0_P1_otp                test/check_otp_continue_url.py
run P2_password_create       test/check_password_create_timing.py
run P3_sentinel_token        test/check_sentinel_token_source.py
run P9_headless_fingerprint  test/check_headless_fingerprint.py
run P10_P11_sidecar_pool     test/check_sidecar_pure_http.py
