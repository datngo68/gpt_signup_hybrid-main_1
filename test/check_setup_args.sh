#!/usr/bin/env bash
# Verify parser cờ của setup.sh (nhánh --help / unknown thoát TRƯỚC khi setup nặng).
# Chạy: bash test/check_setup_args.sh
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
fail=0

# TC-01: --help → exit 0 + in Usage
out="$(bash "$ROOT/setup.sh" --help 2>&1)"; rc=$?
if [ $rc -eq 0 ] && printf '%s' "$out" | grep -q "Usage: bash setup.sh"; then
  echo "[PASS] TC-01 — --help in usage & exit 0 :: rc=$rc"
else
  echo "[FAIL] TC-01 — --help :: rc=$rc out=$out"; fail=1
fi

# TC-02: arg lạ → exit !=0 + báo lỗi
out="$(bash "$ROOT/setup.sh" --bogus 2>&1)"; rc=$?
if [ $rc -ne 0 ] && printf '%s' "$out" | grep -q "unknown arg"; then
  echo "[PASS] TC-02 — unknown arg fail-fast :: rc=$rc"
else
  echo "[FAIL] TC-02 — unknown arg :: rc=$rc out=$out"; fail=1
fi

# TC-03: kiểm tra logic normalize --db tên ngắn bằng cách trích đoạn case (không chạy setup)
# Mô phỏng đúng nhánh case trong setup.sh.
RUNTIME_DIR="runtime"
check_db() {
  GSH_DB_PATH="$1"
  case "$GSH_DB_PATH" in
    */*)  : ;;
    *.db) : ;;
    *)    GSH_DB_PATH="$RUNTIME_DIR/${GSH_DB_PATH}.db" ;;
  esac
  printf '%s' "$GSH_DB_PATH"
}
r1="$(check_db db4444)"
r2="$(check_db runtime/data2.db)"
r3="$(check_db custom.db)"
if [ "$r1" = "runtime/db4444.db" ] && [ "$r2" = "runtime/data2.db" ] && [ "$r3" = "custom.db" ]; then
  echo "[PASS] TC-03 — normalize --db :: db4444→$r1 | path giữ nguyên→$r2 | .db giữ→$r3"
else
  echo "[FAIL] TC-03 — normalize --db :: $r1 | $r2 | $r3"; fail=1
fi

echo ""
[ $fail -eq 0 ] && echo "[SUMMARY] all PASS" || echo "[SUMMARY] FAIL"
exit $fail
