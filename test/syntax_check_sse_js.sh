#!/usr/bin/env bash
# Syntax-check app.js sau khi đổi SseBus sang fetch(POST)+ReadableStream.
# Chạy: bash test/syntax_check_sse_js.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
node --check "$ROOT/web/static/app.js"
echo "[PASS] app.js syntax OK"
