#!/bin/sh
# Probe trực tiếp bằng curl 10 dòng proxy bestproxy.com user gửi —
# cùng endpoint mà bot dùng (api64.ipify.org), cùng timeout 6s.
# Mục đích: cô lập lỗi do proxy chết/auth/IP-allowlist hay do code bot.
set -u

ENDPOINT="https://api64.ipify.org"
TIMEOUT_SECS=6
COUNT_OK=0
COUNT_AUTH=0
COUNT_IP=0
COUNT_OTHER=0

PROXIES="
proxy.bestproxy.com:2312:bp-bhatio_area-IN_life-5_session-2ih2MC:Z6HU2kxuTZPF6Zr
proxy.bestproxy.com:2312:bp-bhatio_area-IN_life-5_session-X3oZBf:Z6HU2kxuTZPF6Zr
proxy.bestproxy.com:2312:bp-bhatio_area-IN_life-5_session-xyf1Pr16q:Z6HU2kxuTZPF6Zr
proxy.bestproxy.com:2312:bp-bhatio_area-IN_life-5_session-SB7iAdKf3ZD1:Z6HU2kxuTZPF6Zr
proxy.bestproxy.com:2312:bp-bhatio_area-IN_life-5_session-iovQBxBc:Z6HU2kxuTZPF6Zr
proxy.bestproxy.com:2312:bp-bhatio_area-IN_life-5_session-0SNQQd:Z6HU2kxuTZPF6Zr
proxy.bestproxy.com:2312:bp-bhatio_area-IN_life-5_session-m7IyLI9Q1:Z6HU2kxuTZPF6Zr
proxy.bestproxy.com:2312:bp-bhatio_area-IN_life-5_session-9DjDmGKy6ku5:Z6HU2kxuTZPF6Zr
proxy.bestproxy.com:2312:bp-bhatio_area-IN_life-5_session-F2MlhYe4YH:Z6HU2kxuTZPF6Zr
proxy.bestproxy.com:2312:bp-bhatio_area-IN_life-5_session-oD4Z6m:Z6HU2kxuTZPF6Zr
"

i=0
echo "$PROXIES" | while IFS= read -r line; do
    [ -z "$line" ] && continue
    i=$((i+1))
    HOST=$(echo "$line" | cut -d: -f1)
    PORT=$(echo "$line" | cut -d: -f2)
    USER=$(echo "$line" | cut -d: -f3)
    PASS=$(echo "$line" | cut -d: -f4)
    PROXY_URL="http://${USER}:${PASS}@${HOST}:${PORT}"
    SHORT_USER=$(echo "$USER" | sed 's/.*session-//')
    printf "%2d. session=%-15s " "$i" "$SHORT_USER"

    OUT=$(curl -sS \
        --max-time "$TIMEOUT_SECS" \
        --proxy "$PROXY_URL" \
        -o /tmp/probe_body.$$ \
        -w "HTTP=%{http_code} TOTAL=%{time_total}s SIZE=%{size_download}" \
        "$ENDPOINT" 2>&1)
    RC=$?
    BODY=$(cat /tmp/probe_body.$$ 2>/dev/null | head -c 60)
    rm -f /tmp/probe_body.$$

    if [ "$RC" -eq 0 ]; then
        echo "✓ OK $OUT body=$BODY"
    else
        # Phân loại sơ bộ qua exit code curl + message
        case "$OUT" in
            *407*|*"Proxy Authentication"*) echo "✗ AUTH (407) $OUT" ;;
            *"Could not resolve"*|*"Name or service"*) echo "✗ DNS $OUT" ;;
            *"timed out"*|*"Operation timeout"*|*"Connection timed out"*) echo "✗ TIMEOUT $OUT" ;;
            *"Connection refused"*) echo "✗ REFUSED $OUT" ;;
            *) echo "✗ rc=$RC $OUT" ;;
        esac
    fi
done

echo ""
echo "=== DIAG: nếu TẤT CẢ FAIL với cùng error trên máy local nhưng bot router cũng fail →"
echo "    nhiều khả năng: (a) proxy thực sự chết / (b) IP-allowlist của bestproxy chưa whitelist IP router."
echo "    nếu local OK nhưng router FAIL → router bị firewall chặn outbound port 2312."
