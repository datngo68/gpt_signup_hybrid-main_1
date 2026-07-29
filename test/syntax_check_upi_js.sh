#!/usr/bin/env bash
# Syntax check JS files đã chỉnh trong patch UPI output.
# Mỗi file 1 dòng [PASS]/[FAIL] để dễ định vị nếu fail.
#
# Chạy:
#     bash test/syntax_check_upi_js.sh

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGETS=(
  "web/static/upi.js"
)

failures=0
total=${#TARGETS[@]}

for i in "${!TARGETS[@]}"; do
  idx=$((i + 1))
  target="${TARGETS[$i]}"
  path="${ROOT}/${target}"
  if [ ! -f "$path" ]; then
    printf '[FAIL] [%d/%d] %s :: file not found\n' "$idx" "$total" "$target"
    failures=$((failures + 1))
    continue
  fi
  if node --check "$path" 2>/dev/null; then
    printf '[PASS] [%d/%d] %s\n' "$idx" "$total" "$target"
  else
    err=$(node --check "$path" 2>&1)
    printf '[FAIL] [%d/%d] %s :: %s\n' "$idx" "$total" "$target" "$err"
    failures=$((failures + 1))
  fi
done

echo
if [ "$failures" -eq 0 ]; then
  printf 'All %d files OK\n' "$total"
  exit 0
else
  printf '%d/%d failed\n' "$failures" "$total"
  exit 1
fi
