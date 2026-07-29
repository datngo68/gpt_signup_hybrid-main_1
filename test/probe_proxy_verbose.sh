#!/bin/sh
# Verbose probe — capture đầy đủ CONNECT response (headers + body) để decode
# nghĩa của HTTP 612 từ bestproxy.com.
set -u

LINE="proxy.bestproxy.com:2312:bp-bhatio_area-IN_life-5_session-2ih2MC:Z6HU2kxuTZPF6Zr"
HOST=$(echo "$LINE" | cut -d: -f1)
PORT=$(echo "$LINE" | cut -d: -f2)
USER=$(echo "$LINE" | cut -d: -f3)
PASS=$(echo "$LINE" | cut -d: -f4)

echo "== verbose CONNECT (HTTPS target) =="
curl -v --max-time 8 \
    --proxy "http://${USER}:${PASS}@${HOST}:${PORT}" \
    "https://api64.ipify.org" 2>&1 | head -40

echo ""
echo "== HTTP target (không CONNECT, server có thể trả body chi tiết) =="
curl -v --max-time 8 \
    --proxy "http://${USER}:${PASS}@${HOST}:${PORT}" \
    "http://api64.ipify.org" 2>&1 | head -40

echo ""
echo "== test với password sai để so sánh response =="
curl -v --max-time 8 \
    --proxy "http://${USER}:WRONG_PASSWORD@${HOST}:${PORT}" \
    "http://api64.ipify.org" 2>&1 | head -25

echo ""
echo "== TCP reach gateway? =="
nc -zv -w 5 "$HOST" "$PORT" 2>&1
