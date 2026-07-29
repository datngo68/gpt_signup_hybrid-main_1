# CLAUDE.md

Guide cho Claude Code khi làm việc trong repo này.

## Project Overview

Tool tự động đăng ký ChatGPT account theo hướng **hybrid**: Phase 1 dùng Camoufox (Firefox anti-detect via Playwright) để đi qua signup flow, Phase 2 dùng `curl_cffi` (TLS fingerprint spoof) để extract `session_token` + `access_token` nhanh (~0.2s). Phase 3 optional enroll TOTP 2FA.

Khác với upstream `6c696e68/gpt_signup_hybrid`, bản này có thêm:
- **SQLite persistence layer** (`db/`) — jobs/combos/session_results survive process restart
- **Recovery + retry queue** — interrupted jobs được resume khi web server start lại
- **Auth token cho web UI** — meta tag + URL query + localStorage (chống lộ ra ngoài loopback)
- **DongVanFB là provider riêng** (không replace Outlook) — chọn qua `mail_provider="dongvanfb"`. UI mới đã gộp vào mode `outlook` (cascade).
- **Aggregated logs**: `runtime/sessions/accounts.txt` (`email|password|2fa_secret`) + `links.txt`

## Setup

```bash
bash setup.sh              # macOS/Linux — venv + deps + camoufox + .env
setup.bat                  # Windows tương đương
```

Setup tạo `.venv/`, install deps, fetch Camoufox Firefox binary, tạo `.env` từ `.env.example`.

## Run

```bash
# Web UI (port mặc định 8083, không phải 8089)
.venv/bin/python -m gpt_signup_hybrid web --host 127.0.0.1 --port 8083

# CLI signup — Outlook combo (cascade: DongVanFB primary → Microsoft fallback)
.venv/bin/python -m gpt_signup_hybrid signup \
  --outlook-combo "user@hotmail.com|pass|refresh_token|client_id"

# CLI signup — DongVanFB direct (legacy, bỏ qua cascade)
.venv/bin/python -m gpt_signup_hybrid signup \
  --outlook-combo "user@hotmail.com|pass|refresh_token|client_id" \
  --mail-provider dongvanfb

# CLI signup — Worker (iCloud relay)
.venv/bin/python -m gpt_signup_hybrid signup \
  --email user@icloud.com --mail-provider worker \
  --logs-url https://your-worker.workers.dev/logs --api-key TOKEN

# Khác
.venv/bin/python -m gpt_signup_hybrid totp <SECRET>
.venv/bin/python -m gpt_signup_hybrid enable-2fa --session-file runtime/sessions/<file>.json
.venv/bin/python -m gpt_signup_hybrid pool-status pool.txt
.venv/bin/python -m gpt_signup_hybrid migrate           # apply DB migrations
.venv/bin/python -m gpt_signup_hybrid import-pool ...   # import combo pool vào SQLite

# Web recorder — record manual web flow (DOM actions + full HAR + Playwright trace)
.venv/bin/python -m gpt_signup_hybrid record \
  --url https://chatgpt.com/ --browser camoufox \
  --email user@icloud.com --secret <OTP_SECRET>   # --email/--secret cho lệnh 'otp' trong recorder
.venv/bin/python -m gpt_signup_hybrid record --dry-run   # validate wiring, không mở browser
```

## Architecture

```
run_signup (signup.py)
  ├─ Phase 1: browser_phase.py   — Camoufox điều khiển auth.openai.com signup
  ├─ OTP polling: mail_providers.py — Worker / Outlook Graph / DongVanFB / GmailAdvanced
  ├─ Phase 2: http_phase.py      — curl_cffi replay → session_token + access_token
  └─ Phase 3: mfa_phase.py       — optional TOTP enroll
```

Phase 1 là **page-state driven**: `_drive_signup_flow` đọc URL/DOM, dispatch handler tương ứng. Hỗ trợ "Log in with a one-time code" fallback khi password sai (set flag `one_time_code_mode`, set password mới ở `about_you` screen).

### Mail providers (`mail_providers.py`)

4 backends qua `MailProvider` Protocol:
| Provider | Use case | Combo format |
|---|---|---|
| `WorkerMailProvider` | iCloud relay via Cloudflare Worker | `--logs-url` + `--api-key` |
| `OutlookMailProvider` | Microsoft Graph API trực tiếp | `email\|pass\|refresh_token\|client_id` |
| `DongVanFBOutlookProvider` | API `tools.dongvanfb.net` | Same combo format, `mail_provider=dongvanfb` |
| `OutlookCascadeProvider` | DongVanFB primary → Microsoft fallback | Default cho `mail_provider=outlook` |
| `GmailAdvancedProvider` | API `checkotpgmail.live` cho Gmail mua | `--gmail-api-url` |

`OutlookMailProvider` persist refresh_token qua **SQLite `ComboRepository`** (single source of truth) + JSON state file (backward compat fallback). Persist-before-mutate để crash-safe.

### Web layer (`web/`)

- `server.py` — FastAPI app, 30+ endpoints (Reg / Session / Link / Pool / Auth)
- `manager.py` — `JobManager` + `SessionJobManager` + `LinkJobManager`. Worker pool concurrency, stagger delay 5–10s, SSE broadcast `/api/events`. Recovery từ SQLite, retry queue, bounded persist-retry (3 lần) cho transient SQLite fail.
- `mail_modes.py` — registry map mail mode name → provider constructor
- `static/` — 3 tabs (Reg = `app.js`, Get Session = `session.js`, Get Link = `link.js`). Auth token attached qua header `X-API-Token` + EventSource query param.

### Persistence (`db/`)

- `engine.py` — SQLite connection pool + WAL mode (`runtime/data.db`)
- `schema.py` — DDL cho jobs/combos/session_results
- `migrate.py` — version-based migrations
- `repositories.py` — `JobRepository` / `ComboRepository` / `SessionResultRepository`

Trên fresh checkout chưa có DB: `migrate_cmd` chạy auto khi web start; hoặc gọi `python -m gpt_signup_hybrid migrate`.

### iCloud HME Runner (`icloud_hme/`)

Subsystem riêng quản lý pool iCloud profile + Hide-My-Email (HME) generation. Layer Job cũ (~1.500 LOC) đã được thay bằng `HmeRunner` chạy **infinite loop** (`cycle → wait retry_interval → cycle`); service core (`generator.py`, `checker.py`, `manager.py`, `pool.py`) giữ nguyên.

Kiến trúc 3 layer:

```
Presentation (CLI / Web)
  └─> HmeRunner (icloud_hme/runner.py)        ← loop controller, ~250 LOC
        └─> Services (UNCHANGED):
              ├─ HmeGenerator   (generator.py)
              ├─ ProfileChecker (checker.py)
              ├─ HmeManager     (manager.py)
              └─ IcloudPoolManager (pool.py)
```

`HmeRunner` dispatch 1 trong **7 actions** mỗi cycle, không chứa business logic:

| Action | Service method | Mô tả |
|---|---|---|
| `generate` | `HmeGenerator.generate(infinite=False, count=...)` | Tạo HME bounded mỗi cycle |
| `check_all` | `ProfileChecker.check_all(...)` | Kiểm tra trạng thái toàn bộ profile |
| `deactivate_bulk` | `HmeManager.deactivate_bulk(emails, dry_run)` | Bulk deactivate HME |
| `reactivate_bulk` | `HmeManager.reactivate_bulk(...)` | Bulk reactivate |
| `delete_bulk` | `HmeManager.delete_bulk(...)` | Bulk delete |
| `update_meta_bulk` | `HmeManager.update_meta_bulk(items, dry_run)` | Bulk update label/note |
| `list_sync` | `HmeManager.list_sync(apple_id)` | Sync HME list từ Apple về DB |

Lifecycle: `start(action, params)` → spawn `cancel_event` / `pause_event` / `resume_event`, vòng lặp `while not cancel`, sleep `retry_interval` interruptible (chunk 1s, react ≤ 1.5s khi `stop()`). Single-instance guard: gọi `start()` lần 2 raise `RuntimeError("Runner đang chạy action khác")`. Khi cancel trả summary `{total_cycles, created, errors, skipped, stopped_by}`.

Log fan-out qua `LogCallback = Callable[[str, str, dict], Awaitable[None]]` — transport-agnostic (CLI in stderr; Web push vào `LogBuffer` capped FIFO 10.000 entry rồi broadcast SSE).

#### CLI commands (đi qua Runner)

```bash
# Infinite loop generate, drain pool mỗi cycle
.venv/bin/python -m gpt_signup_hybrid.icloud_hme generate

# Bounded mỗi cycle, retry interval custom
.venv/bin/python -m gpt_signup_hybrid.icloud_hme generate \
    --count-per-cycle 50 --retry-interval 600 \
    --label "batch-Q1" --note "auto" --proxy http://...

# Infinite loop check toàn bộ profile
.venv/bin/python -m gpt_signup_hybrid.icloud_hme check --all --retry-interval 1800
```

Cờ chung Runner: `--count-per-cycle <N>` (chỉ `generate`), `--retry-interval <S>` (validate `>= 10`). SIGINT → `runner.stop()` graceful, không raise `KeyboardInterrupt`. KHÔNG còn cờ `--infinite`.

Các command còn lại (`bootstrap`, `profile open/delete`, `status`, `reconcile`, `email *`, `audit *`) giữ hành vi 1-shot blocking, **không** đi qua Runner.

#### Web endpoints (Bearer auth bắt buộc)

| Method | Path | Mô tả |
|---|---|---|
| `POST` | `/api/icloud/run` | Body `{action, params, retry_interval?}` → spawn `asyncio.create_task(runner.start(...))`. 409 nếu đang chạy |
| `POST` | `/api/icloud/run/stop` | Set `cancel_event` |
| `POST` | `/api/icloud/run/pause` | Set `pause_event` |
| `POST` | `/api/icloud/run/resume` | Set `resume_event` |
| `GET` | `/api/icloud/run/status` | `{running, action, cycle, stats, retry_interval, next_cycle_at}` |
| `GET` | `/api/icloud/run/log?offset=N&limit=M` | Pagination log buffer theo `seq` |
| `GET` | `/api/icloud/run/log/stream` | SSE stream `LogEvent` real-time |

Mọi path `/api/icloud/run/*` thiếu/sai header `Authorization: Bearer <token>` trả 401, không gọi method nào trên Runner.

### Aggregated output files (`runtime/sessions/`)

| File | Format | Khi nào ghi |
|---|---|---|
| `signup-<ts>-<email>.json` | Full SignupResult + `two_factor` merged | Mỗi job thành công (bao gồm cả retry-2fa path) |
| `<file>.2fa.json` | Email + user_id + two_factor | Khi enroll 2FA (backward compat) |
| `accounts.txt` | `email\|password\|2fa_secret` per line | Sau enroll 2FA thành công (cả 2 branch: signup + retry-2fa) |
| `links.txt` | 1 URL per line | Sau get-link thành công (post-reg + standalone) |

## Key Configuration (`.env`)

| Variable | Default | Notes |
|---|---|---|
| `BROWSER_ENGINE` | `camoufox` | `camoufox` (khuyến nghị) hoặc `playwright` |
| `BROWSER_RANDOM_SCREEN` | `true` | true = stealth tốt hơn; false = viewport cố định |
| `RUNTIME_DIR` | `runtime` | Profiles + sessions + DB |
| `HYBRID_MAX_CONCURRENT` | `2` | Job concurrency web UI [1–10] |
| `HYBRID_JOB_TIMEOUT` | `240` | Phải > OTP timeout (180s) |
| `HYBRID_OUTLOOK_PROXY` | `` | Seed proxy pool lúc startup (ưu tiên cấu hình ở UI Settings > Proxies) |
| `ICLOUD_RETRY_INTERVAL` | `900` | Giây giữa 2 cycle của `HmeRunner`, min `10` (fail-fast) |
| `ICLOUD_MAX_ERRORS_PER_CYCLE` | `0` | Giới hạn lỗi mỗi cycle Runner; `0` = không cap |

Priority: `os.environ` > `.env` > defaults trong `config.py`.

## Important Constraints

- **Headless không khuyến nghị** — Sentinel/Cloudflare detection cao hơn nhiều ở headless. Default headed.
- **`curl_cffi` impersonate phải khớp browser** — `user_agent` + `impersonate` trong `SignupRequest` phải match Camoufox FF version (default `firefox135`).
- **Outlook refresh token rotates** — mỗi Graph API call thành công đổi token. Persist trước, mutate sau (đã handle ở `OutlookMailProvider._refresh_access`).
- **Refresh token có hard cap 15s** (`_OUTLOOK_REFRESH_TOTAL_TIMEOUT`) — chống treo job khi `login.microsoftonline.com` không phản hồi.
- **2FA enroll có 2 code path** — signup chính (Branch A trong `manager.py:1120+`) và retry-2fa-only (Branch B trong `manager.py:863+`). Cả 2 đều ghi `.2fa.json` + merge `two_factor` vào session JSON + append `accounts.txt`.
- **Auth token bind**: khi web bind 0.0.0.0 (non-loopback), token bắt buộc; khi bind 127.0.0.1, token được inject sẵn qua meta tag.
- **Python 3.11+** — dùng modern typing + `from __future__ import annotations`.

## Khi thêm features

1. **Plan trước** vào `tasks/todo.md` (xem `/Volumes/SSD/Developments/CLAUDE.md` root).
2. **Verify** bằng cách chạy CLI hoặc web UI thực tế — chưa có test suite tự động đủ tốt.
3. **SQLite migration** mới: thêm vào `db/migrate.py`, version tăng dần, idempotent.
4. **Nhánh enroll-2FA**: nếu sửa Branch A nhớ check Branch B (manager.py line 863+) — dễ quên.
5. **Provider mới**: register ở `web/mail_modes.py` + factory ở `mail_providers.py` + nhánh ở `signup.py:_build_mail_provider`.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **gpt_signup_hybrid_release** (26928 symbols, 57842 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/gpt_signup_hybrid_release/context` | Codebase overview, check index freshness |
| `gitnexus://repo/gpt_signup_hybrid_release/clusters` | All functional areas |
| `gitnexus://repo/gpt_signup_hybrid_release/processes` | All execution flows |
| `gitnexus://repo/gpt_signup_hybrid_release/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
