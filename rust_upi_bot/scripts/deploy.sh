#!/bin/sh
# Cross-build + deploy upi-qr-bot lên router OpenWrt.
# Chạy từ workspace root: bash rust_upi_bot/scripts/deploy.sh [router_ip]
set -e

ROUTER=${1:-192.168.8.1}
ROOT_DIR="$(dirname "$0")/.."

cd "$ROOT_DIR"

# BoringSSL (wreq, login JA3) build qua cmake → cần C/C++ compiler có sysroot
# musl đầy đủ. zig ship sẵn sysroot musl; wrapper dịch clang-triple
# `aarch64-unknown-linux-musl` → zig `aarch64-linux-musl`.
#   - CC_<target>/CXX_<target>: cho cc-rs (zstd-sys...).
#   - CC/CXX plain: cmake của BoringSSL đọc $CC lúc project().
export CC_aarch64_unknown_linux_musl="$PWD/scripts/zig-cc.sh"
export CXX_aarch64_unknown_linux_musl="$PWD/scripts/zig-cxx.sh"
export CC="$PWD/scripts/zig-cc.sh"
export CXX="$PWD/scripts/zig-cxx.sh"
chmod +x scripts/zig-cc.sh scripts/zig-cxx.sh

echo "→ cargo zigbuild release aarch64-musl..."
cargo zigbuild --release --target aarch64-unknown-linux-musl

BIN=target/aarch64-unknown-linux-musl/release/upi-qr-bot
SIZE=$(stat -f%z "$BIN" 2>/dev/null || stat -c%s "$BIN")
echo "→ binary $SIZE bytes"

echo "→ scp binary → $ROUTER:/usr/bin/upi-qr-bot"
scp -o BatchMode=yes "$BIN" "root@$ROUTER:/usr/bin/upi-qr-bot"

echo "→ scp init script → $ROUTER:/etc/init.d/upi-qr-bot"
scp -o BatchMode=yes scripts/upi-qr-bot.init "root@$ROUTER:/etc/init.d/upi-qr-bot"

echo "→ scp env example → $ROUTER:/etc/upi-qr-bot.env.example (giữ existing /etc/upi-qr-bot.env)"
scp -o BatchMode=yes scripts/upi-qr-bot.env.example "root@$ROUTER:/etc/upi-qr-bot.env.example"

ssh -o BatchMode=yes "root@$ROUTER" "
  chmod +x /usr/bin/upi-qr-bot /etc/init.d/upi-qr-bot
  [ ! -f /etc/upi-qr-bot.env ] && cp /etc/upi-qr-bot.env.example /etc/upi-qr-bot.env && chmod 600 /etc/upi-qr-bot.env
  /etc/init.d/upi-qr-bot enable 2>&1 || true
  echo ok
"
echo "✓ deployed. Sửa /etc/upi-qr-bot.env (TELEGRAM_TOKEN), rồi chạy:"
echo "    ssh root@$ROUTER /etc/init.d/upi-qr-bot start"
