#!/bin/sh
# Test run-once flow trên router với fake session.json — kỳ vọng:
#   - Stripe bundle fetch OK (TLS + parse)
#   - Step 2 (chatgpt checkout) FAIL với HTTP 401 (token fake) hoặc 403
#   - Output JSON kết quả với error rõ ràng
set -e

ROUTER=192.168.8.1
BIN_REMOTE=/tmp/upi-qr-bot

# Tạo fake session.json
ssh -o BatchMode=yes root@$ROUTER 'cat > /tmp/fake_session.json << EOF
{
  "user": {"email": "fake@test.com"},
  "accessToken": "fake_token_for_smoke_test_will_fail_at_checkout"
}
EOF'

# Chạy run-once với approve_retries=2, restart=0/0 để loop fail nhanh
ssh -o BatchMode=yes root@$ROUTER \
  "$BIN_REMOTE run-once \
        --session-json /tmp/fake_session.json \
        --qr-out /tmp/fake_qr.png \
   --telegram-token 'unused' \
   --approve-retries 2 \
   --restart-threshold 0 \
   --max-restarts 0 \
   --proxy-pool '' \
   --db-path /tmp/upi-runonce.db \
   --bundles-cache-dir /tmp/upi-bot-bundles \
   2>&1"
