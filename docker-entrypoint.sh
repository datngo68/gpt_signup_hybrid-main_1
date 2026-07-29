#!/bin/sh
set -e

# Migrate idempotent (version-based) — chạy trước mọi command (web/hme/cli).
# DB engine tự tạo runtime/ + data.db (db/engine.py mkdir parents).
# Migrate không cần X display nên KHÔNG bọc xvfb-run ở đây; chỉ CMD
# (web/browser job) mới cần display, đã bọc xvfb-run trong CMD/compose command.
python -m gpt_signup_hybrid migrate

exec "$@"
