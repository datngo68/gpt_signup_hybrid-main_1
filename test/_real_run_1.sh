#!/bin/bash
# Real-world signup test 1: pure_request mode (Phase 10/11 sidecar)
set +e
cd /Users/vippro/Developments/gpt_signup_hybrid
export PYTHONUNBUFFERED=1
mkdir -p runtime/sessions

EMAIL="$1"
[ -z "$EMAIL" ] && EMAIL="temper-17-harvest+jnpeb0x@icloud.com"
MODE="$2"
[ -z "$MODE" ] && MODE="pure_request"
OUTFILE="$3"
[ -z "$OUTFILE" ] && OUTFILE="test/_real_${MODE}_$(date +%H%M%S).log"
RESULT="test/_real_${MODE}_$(date +%H%M%S).json"

echo "EMAIL=$EMAIL MODE=$MODE OUT=$OUTFILE RESULT=$RESULT" > "$OUTFILE"
echo "STARTED_AT=$(date -Iseconds)" >> "$OUTFILE"
echo "===" >> "$OUTFILE"

.venv/bin/python -m gpt_signup_hybrid signup \
  --email "$EMAIL" \
  --logs-url https://icloud-cf-mail-v2.n5pskgzs9g.workers.dev/logs \
  --api-key '12345678@' \
  --reg-mode "$MODE" \
  --headless \
  --name "Real Test" \
  --birthdate 2000-06-26 \
  --otp-timeout 200 \
  --output "$RESULT" \
  2>&1 | tee -a "$OUTFILE"

EX=$?
echo "===" >> "$OUTFILE"
echo "ENDED_AT=$(date -Iseconds) EXIT=$EX" >> "$OUTFILE"
