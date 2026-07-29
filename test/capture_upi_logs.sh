#!/bin/sh
# Chạy run-once cho vài account để THU log chuẩn (mọi dòng runner phát ra),
# phục vụ thiết kế parser cho thẻ tiến trình thân thiện.
# Mỗi account dùng approve-retries nhỏ để thấy đủ chuỗi bước nhanh.
set -e

BIN=./target/release/upi-qr-bot
OUT=/tmp/upi_logs
mkdir -p "$OUT"

COMMON="--db-path /tmp/upi-cap.db --qr-out-dir /tmp/upicap-qr --bundles-cache-dir /tmp/upicap-bundles --approve-retries 4"

# (label, combo)
run_one() {
  label="$1"; combo="$2"
  echo "==================== [$label] START ===================="
  # timeout mềm 120s/account để không kẹt
  "$BIN" $COMMON run-once --combo "$combo" --qr-out "$OUT/$label.png" > "$OUT/$label.log" 2>&1 || true
  echo "[$label] DONE → $OUT/$label.log ($(wc -l < "$OUT/$label.log") dòng)"
  # in các dòng bước (không in spam approve)
  grep -E "Account:|\[1/6\]|\[2/6|\[3/6|\[4/6|\[5a\]|\[5b|\[5c|\[6/6\]|login|RESULT|error|FAIL" "$OUT/$label.log" | grep -vE "try [0-9]" | head -40
  echo "---- mẫu approve ----"
  grep -E "try [0-9]" "$OUT/$label.log" | head -3
  echo "==================== [$label] END ======================"
  echo
}

run_one igler "iglercontos2302@hotmail.com|Prx12x123999@|NIUCHTC5NGHM62PZUMQK4262AMISLHMG"
run_one oritz "oritzcarlyn9686@hotmail.com|Prx12x123999@|3VAY5ZK55MJSSFMPHXNBWZTPJDNRAZBP"
run_one parah "parahmalloy56@hotmail.com|Prx12x123999@|T5N5C5CB4VEFAT2NFJRUJFBVKCMHNFBM"
run_one sack  "usackblasius81@hotmail.com|Prx12x123999@|W2X4CYEGJXS2MM726DD3VK34IP6N4UD7"

echo "######## TỔNG HỢP CÁC DÒNG LOG DUY NHẤT (đã chuẩn hoá) ########"
cat "$OUT"/*.log | sed -E 's/[0-9]+/N/g; s/cs_live_[A-Za-z0-9]+/cs_live_X/g' | sort -u | grep -vE "^\{|^\}|^ +\"" | head -80
