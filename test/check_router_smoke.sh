#!/bin/sh
# Smoke test trên router: chạy binary với fake token 5s, capture log,
# verify khởi tạo OK trước khi getUpdates fail (mong đợi).
set -e

ROUTER=192.168.8.1
BIN_REMOTE=/tmp/upi-qr-bot
LOG_REMOTE=/tmp/upi-qr-bot.smoke.log

ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@$ROUTER \
  "rm -f $LOG_REMOTE; \
   ($BIN_REMOTE \
        --telegram-token '111:invalid_smoke_token' \
        --max-concurrent 4 \
        --approve-retries 10 \
        --proxy-pool '' \
        --db-path /tmp/upi-smoke.db \
        --qr-out-dir /tmp/upi-smoke-qr \
        --bundles-cache-dir /tmp/upi-smoke-bundles \
        > $LOG_REMOTE 2>&1) & \
   PID=\$!; \
   sleep 5; \
   kill \$PID 2>/dev/null || true; \
   sleep 1; \
   echo '=== LOG ==='; \
   cat $LOG_REMOTE; \
   echo '=== STATE ==='; \
   ls -la /tmp/upi-smoke.db /tmp/upi-smoke-qr /tmp/upi-smoke-bundles 2>&1"
