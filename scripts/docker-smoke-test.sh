#!/usr/bin/env bash
#
# Docker smoke test cho gpt_signup_hybrid.
#
# Codify các acceptance criteria của Dockerize plan KHÔNG cần combo signup thật.
# Re-runnable, hermetic (không đụng .env thật của operator), an toàn cho CI.
#
# Usage:
#   scripts/docker-smoke-test.sh                 # image mặc định gsh:latest
#   IMAGE=gsh:dev scripts/docker-smoke-test.sh   # override image tag
#
# Cái KHÔNG cover ở đây:
#   - AC2 (signup job thật qua Camoufox headless) — cần combo thật, operator
#     tự verify sau build (xem phase-03-build-verify.md).
#
# Exit code: 0 = tất cả pass | 1 = có check fail (image lỗi) | 2 = SKIP (môi
# trường không chạy được: docker vắng / không cd được). CI nên coi 2 là neutral.

set -u

IMAGE="${IMAGE:-gsh:latest}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"

pass=0
fail=0
GREEN='\033[32m'; RED='\033[31m'; DIM='\033[2m'; RESET='\033[0m'

ok()      { printf "  ${GREEN}PASS${RESET} %s\n" "$1"; pass=$((pass + 1)); }
ko()      { printf "  ${RED}FAIL${RESET} %s\n" "$1"; fail=$((fail + 1)); }
section() { printf "\n=== %s ===\n" "$1"; }

# Chạy ở thư mục chứa compose file (repo root).
cd "$(dirname "$0")/.." || { echo "cannot cd to repo root"; exit 2; }

# ---------------------------------------------------------------------------
section "0. Tiền đề"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker không có trong PATH — bỏ qua smoke test"; exit 2
fi
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  ko "image '$IMAGE' chưa build (chạy: docker compose build)"
  echo ""; printf "  ${DIM}Build trước rồi chạy lại smoke test.${RESET}\n"
  exit 1
fi
ok "image '$IMAGE' tồn tại"

# ---------------------------------------------------------------------------
section "1. CLI khả dụng (Phase 1 — python -m gpt_signup_hybrid --help)"
# Bypass entrypoint để cô lập CLI khỏi migrate side-effect.
if docker run --rm --entrypoint python "$IMAGE" -m gpt_signup_hybrid --help >/dev/null 2>&1; then
  ok "python -m gpt_signup_hybrid --help → exit 0"
else
  ko "python -m gpt_signup_hybrid --help thất bại"
fi

# ---------------------------------------------------------------------------
section "2. Camoufox binary + GeoIP baked, KHÔNG download runtime (Phase 1)"
# --network none: pass = baked sẵn (không thể tải khi offline). KHÔNG dùng
# `camoufox version` (nó phone-home check update → fail offline + output đổi
# theo version). Check filesystem trực tiếp.
# Browser: libxul.so (core Firefox engine, ~144MB) = bằng chứng browser unpacked.
if docker run --rm --network none --entrypoint sh "$IMAGE" -c \
     'find "$HOME/.cache/camoufox" -name libxul.so 2>/dev/null | grep -q .' 2>/dev/null; then
  ok "Camoufox browser baked (libxul.so present, offline)"
else
  ko "Camoufox browser KHÔNG baked offline"
fi
# GeoIP: dùng chính MMDB_FILE của camoufox.locale (đúng path app dùng runtime).
if docker run --rm --network none --entrypoint python "$IMAGE" -c \
     "from camoufox.locale import MMDB_FILE; import sys; sys.exit(0 if MMDB_FILE.exists() else 1)" 2>/dev/null; then
  ok "GeoIP mmdb baked (MMDB_FILE exists, offline)"
else
  ko "GeoIP mmdb KHÔNG baked offline"
fi

# ---------------------------------------------------------------------------
section "3. Container chạy non-root uid 10001 (Phase 1)"
uid="$(docker run --rm --entrypoint id "$IMAGE" -u 2>/dev/null | tr -d '[:space:]')"
if [ "$uid" = "10001" ]; then
  ok "uid = 10001 (appuser non-root)"
else
  ko "uid = '$uid' (kỳ vọng 10001)"
fi

# ---------------------------------------------------------------------------
section "4. Entrypoint chạy migrate trước CMD (Phase 2)"
# Entrypoint: migrate (set -e → fail thì thoát non-zero) rồi exec CMD.
# Mount tmpfs vào runtime để migrate ghi được mà không cần named volume.
ep_out="$(docker run --rm --tmpfs /app/gpt_signup_hybrid/runtime:uid=10001 \
  "$IMAGE" python -c "print('ENTRYPOINT_OK')" 2>&1)"
if printf '%s' "$ep_out" | grep -q 'ENTRYPOINT_OK'; then
  ok "entrypoint migrate → exec CMD thành công (fresh runtime, exit 0)"
else
  ko "entrypoint/migrate thất bại trên fresh runtime"
  printf "  ${DIM}%s${RESET}\n" "$(printf '%s' "$ep_out" | tail -5 | tr '\n' '|')"
fi

# ---------------------------------------------------------------------------
section "5. docker compose config hợp lệ (Phase 2)"
# Hermetic: dùng temp project dir + temp .env, không đụng .env thật.
td="$(mktemp -d)"
cp "$COMPOSE_FILE" "$td/docker-compose.yml"
printf 'GPT_SIGNUP_WEB_TOKEN=smoke\nICLOUD_API_AUTH_TOKEN=smoke\n' > "$td/.env"
if docker compose -f "$td/docker-compose.yml" --env-file "$td/.env" config -q >/dev/null 2>&1; then
  ok "compose config (web default) hợp lệ"
else
  ko "compose config (web default) lỗi"
fi
if docker compose -f "$td/docker-compose.yml" --env-file "$td/.env" --profile hme config -q >/dev/null 2>&1; then
  ok "compose config (--profile hme) hợp lệ"
else
  ko "compose config (--profile hme) lỗi"
fi
rm -rf "$td"

# ---------------------------------------------------------------------------
section "6. Thiếu token → compose fail-fast (Phase 2)"
# Temp project + .env rỗng + unset shell var → \${VAR:?} phải fire.
td2="$(mktemp -d)"
cp "$COMPOSE_FILE" "$td2/docker-compose.yml"
: > "$td2/.env"
if env -u GPT_SIGNUP_WEB_TOKEN -u ICLOUD_API_AUTH_TOKEN \
     docker compose -f "$td2/docker-compose.yml" --env-file "$td2/.env" config -q >/dev/null 2>&1; then
  ko "thiếu GPT_SIGNUP_WEB_TOKEN nhưng config KHÔNG fail (mong đợi fail-fast)"
else
  ok "thiếu token → config fail-fast"
fi
rm -rf "$td2"

# ---------------------------------------------------------------------------
section "7. xvfb-run khởi tạo display headless (Phase 1/2)"
# CMD thật bọc 'xvfb-run -a' → cần xauth. Regression: thiếu xauth → web không
# start (xvfb-run: xauth command not found).
# Chạy qua 'sh -c timeout xvfb-run' (KHÔNG để xvfb-run làm PID 1 — khi PID 1 +
# command thoát-nhanh, xvfb-run kẹt ở cleanup wait Xvfb). timeout = safety net.
# Web service thật chạy mãi nên không dính bug PID-1-cleanup này.
if docker run --rm --entrypoint sh "$IMAGE" -c \
     'timeout 30 xvfb-run -a python -c "print(\"XVFB_OK\")"' 2>/dev/null | grep -q 'XVFB_OK'; then
  ok "xvfb-run -a chạy được (xauth present, DISPLAY ảo OK)"
else
  ko "xvfb-run -a thất bại (thiếu xauth / Xvfb không khởi tạo?)"
fi

# ---------------------------------------------------------------------------
section "Kết quả"
printf "  %d pass, %d fail\n" "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
echo "  Smoke test OK. (AC2 signup thật: operator verify thủ công với combo.)"
