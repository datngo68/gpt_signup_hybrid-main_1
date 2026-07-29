#!/bin/sh
# Cross-build upi-qr-bot (aarch64-musl) + deploy lên router OpenWrt qua sshpass.
# Variant của rust_upi_bot/scripts/deploy.sh: dùng password (sshpass) thay vì
# BatchMode key auth, để chạy 1 lần khi key chưa setup.
#
# Usage: SSHPASS='...' sh test/build_deploy_router.sh <router_ip>
#   - $SSHPASS phải set ở env (KHÔNG truyền qua arg, lộ qua `ps`).
#   - Bot service tự enable + start lại nếu /etc/init.d/upi-qr-bot tồn tại.
set -e

ROUTER="${1:-}"
if [ -z "$ROUTER" ]; then
  echo "Usage: SSHPASS=... sh test/build_deploy_router.sh <router_ip>" >&2
  exit 2
fi
if [ -z "${SSHPASS:-}" ]; then
  echo "SSHPASS env var bắt buộc (không truyền password qua arg)." >&2
  exit 2
fi
if ! command -v sshpass >/dev/null 2>&1; then
  echo "sshpass không có trên PATH. Cài: brew install sshpass" >&2
  exit 2
fi
if ! command -v zig >/dev/null 2>&1 || ! command -v cargo-zigbuild >/dev/null 2>&1; then
  echo "Thiếu zig hoặc cargo-zigbuild. Cài: brew install zig && cargo install cargo-zigbuild" >&2
  exit 2
fi

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUST_DIR="$ROOT_DIR/rust_upi_bot"
cd "$RUST_DIR"

# zig wrapper cho BoringSSL/wreq build (xem rust_upi_bot/scripts/zig-cc.sh).
export CC_aarch64_unknown_linux_musl="$RUST_DIR/scripts/zig-cc.sh"
export CXX_aarch64_unknown_linux_musl="$RUST_DIR/scripts/zig-cxx.sh"
export CC="$RUST_DIR/scripts/zig-cc.sh"
export CXX="$RUST_DIR/scripts/zig-cxx.sh"
chmod +x scripts/zig-cc.sh scripts/zig-cxx.sh

echo "→ cargo zigbuild --release --target aarch64-unknown-linux-musl ..."
cargo zigbuild --release --target aarch64-unknown-linux-musl

BIN="target/aarch64-unknown-linux-musl/release/upi-qr-bot"
if [ ! -f "$BIN" ]; then
  echo "Build xong nhưng không tìm thấy $BIN" >&2
  exit 1
fi
SIZE=$(stat -f%z "$BIN" 2>/dev/null || stat -c%s "$BIN")
echo "→ binary $SIZE bytes"

SSHOPTS="-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$HOME/.ssh/known_hosts"

echo "→ scp binary → root@$ROUTER:/usr/bin/upi-qr-bot"
sshpass -e scp $SSHOPTS "$BIN" "root@$ROUTER:/usr/bin/upi-qr-bot.new"

echo "→ scp init script → root@$ROUTER:/etc/init.d/upi-qr-bot"
sshpass -e scp $SSHOPTS scripts/upi-qr-bot.init "root@$ROUTER:/etc/init.d/upi-qr-bot"

echo "→ scp env example → root@$ROUTER:/etc/upi-qr-bot.env.example"
sshpass -e scp $SSHOPTS scripts/upi-qr-bot.env.example "root@$ROUTER:/etc/upi-qr-bot.env.example"

echo "→ atomic swap binary + restart service ..."
sshpass -e ssh $SSHOPTS "root@$ROUTER" '
  set -e
  chmod +x /usr/bin/upi-qr-bot.new /etc/init.d/upi-qr-bot
  if [ ! -f /etc/upi-qr-bot.env ]; then
    cp /etc/upi-qr-bot.env.example /etc/upi-qr-bot.env
    chmod 600 /etc/upi-qr-bot.env
    echo "  • created /etc/upi-qr-bot.env (sửa TELEGRAM_TOKEN trước khi start)"
  fi
  /etc/init.d/upi-qr-bot enable 2>/dev/null || true
  /etc/init.d/upi-qr-bot stop 2>/dev/null || true
  mv /usr/bin/upi-qr-bot.new /usr/bin/upi-qr-bot
  if grep -q "^TELEGRAM_TOKEN=." /etc/upi-qr-bot.env 2>/dev/null; then
    /etc/init.d/upi-qr-bot start
    echo "  • service restarted"
  else
    echo "  • TELEGRAM_TOKEN chưa set — bỏ qua start. Sửa /etc/upi-qr-bot.env rồi /etc/init.d/upi-qr-bot start"
  fi
  echo "ok"
'
echo "✓ deploy xong tới $ROUTER"
