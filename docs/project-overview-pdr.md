# Project Overview & Product Development Requirements

## Executive Summary

**gpt_signup_hybrid** — Tool tự động hoá vòng đời tài khoản ChatGPT: đăng ký hàng loạt → bật 2FA → lấy session token → lấy QR/link thanh toán (UPI India). Điều khiển qua Web UI cục bộ (FastAPI, port 8083) hoặc CLI. Hỗ trợ hybrid signup flow (browser + pure HTTP) với persistence layer (SQLite) + recovery system.

**Version:** 2.0.0 · **Runtime:** Python 3.11+ · **DB:** SQLite WAL · **Stack:** Camoufox + curl_cffi + FastAPI

---

## Core Features

| Feature | Status | Notes |
|---------|--------|-------|
| **Multi-account signup (batch)** | ✅ Complete | Camoufox anti-detect + OTP polling (5 providers) |
| **2FA enrollment (TOTP)** | ✅ Complete | Auto-enroll + extract secret → `accounts.txt` |
| **Session extraction** | ✅ Complete | Login → accessToken + planType (free/plus/team) |
| **UPI QR payment** | ✅ Complete | ChatGPT checkout → Stripe confirm → QR render |
| **iCloud HME pool** | ✅ Complete | Hide-My-Email generation + bulk management (infinite loop runner) |
| **Auto-registration** | ✅ Complete | Poll iCloud HME emails → run signup → save to DB |
| **Web UI (dynamic)** | ✅ Complete | 6 tabs (Reg/Session/UPI QR/Settings/iCloud HME/Get Link) + SSE live updates |
| **Proxy pool** | ✅ Complete | Round-robin/random rotation + auto-mark dead |
| **Persistence recovery** | ✅ Complete | Jobs/combos/sessions survive restart from SQLite |
| **Payment link extraction** | ✅ Complete | ChatGPT checkout → GoPay/Stripe hosted URL |

---

## Product Requirements

### Functional Requirements

1. **Signup Pipeline (Hybrid)**
   - Phase 1: Camoufox + Playwright drives `auth.openai.com/signup` (page-state machine)
   - OTP polling: 5 backends (Worker/Outlook/DongVanFB/GmailAdvanced)
   - Phase 2: curl_cffi TLS-fingerprint impersonate → session_token + access_token
   - Phase 3: Optional TOTP 2FA enroll + save credentials to `accounts.txt`
   - Pure-HTTP variant available (no browser required)
   - **Acceptance:** Account created, credentials in SQLite + file export

2. **Web UI (Multi-job)**
   - 6 independent tabs (Registration/Session/UPI QR/Settings/iCloud HME/Get Link)
   - Real-time job progress via SSE (`/api/sse` multi-channel)
   - 1-10 concurrent worker threads (configurable)
   - Stagger delay 5–10s between job starts
   - **Acceptance:** Jobs display live progress, restart shows recovery state

3. **Data Persistence**
   - Single SQLite file `runtime/data.db` (WAL mode)
   - Tables: jobs, job_logs, outlook_combos, session_results, settings, icloud_emails, chatgpt_accounts
   - Version-based migrations (idempotent, auto-apply on startup)
   - **Acceptance:** Restart → jobs resume, no data loss

4. **iCloud HME Management**
   - HmeRunner: infinite loop controller (generate/check/deactivate/bulk delete/update meta)
   - 7 actions per cycle, 900s retry interval (configurable, min 10s)
   - Graceful stop via SIGINT (cancel_event + pause/resume events)
   - Log buffer capped at 10K entries + SSE stream
   - **Acceptance:** Runner stays responsive, logs persist, stop() returns summary

5. **Payment Integration**
   - UPI QR probe: login → ChatGPT checkout → Stripe UPI confirm → QR PNG
   - Stripe token extraction (js_checksum + rv_timestamp reverse-engineer)
   - Proxy rotation during confirm loop (up to 10 retries)
   - Payment link extraction (ChatGPT → GoPay/Stripe hosted URL)
   - **Acceptance:** QR generated + saved as PNG, link extracted

6. **CLI Interface**
   - Commands: web, signup, enable-2fa, totp, record, pool-status, import-pool, migrate
   - Entry points for pay_upi_http, icloud_hme, codex_auth
   - Support for environment variable config + .env file
   - **Acceptance:** All commands work via `python -m gpt_signup_hybrid <cmd>`

7. **Authentication**
   - Token auth on all `/api/*` endpoints (required)
   - Auto-inject token via meta tag for loopback (127.0.0.1)
   - Manual token for non-loopback (header/query/cookie)
   - Redact secrets in logs (proxy creds, API keys, tokens)
   - **Acceptance:** Unauthorized request → 401, authorized → 200/response

### Non-Functional Requirements

| Requirement | Target | Notes |
|-------------|--------|-------|
| **Concurrency** | 1–10 workers | Configurable per job type; default 2 |
| **Job timeout** | 240s (signup), 180s (OTP) | Prevent hanging browser/HTTP calls |
| **Proxy failover** | Auto-mark dead, round-robin next | No manual intervention |
| **Memory footprint** | <500MB typical | In-memory job queue bounded |
| **Startup time** | <5s (empty DB), <10s (with recovery) | FastAPI + migration overhead |
| **SSE latency** | <500ms event propagation | Push to all connected clients |
| **Browser anti-detect** | Headed mode (not headless) | Sentinel/Cloudflare detection lower |
| **Sentinel PoW** | 0.2–0.5s per signature | QuickJS VM (primary) or Python solver (fallback) |
| **Python version** | 3.11+ (modern typing) | `from __future__ import annotations` used |

---

## Architecture (High Level)

```
┌─────────────────────────────────────────────────────┐
│ Frontend (static JS, no bundler)                     │
│ index.html + app.js + session.js + upi.js + ...      │
│    ↓ REST (X-API-Token) ↓ SSE (Bearer)               │
└─────────────────────────────────────────────────────┘
              ↓                ↓
         FastAPI server    SSE mux
              ↓                ↓
     ┌───────────────┬──────────────┐
     ↓               ↓              ↓
JobManager    SessionJobManager  UpiJobManager  (+ LinkJobManager, HmeRunner)
     ↓               ↓              ↓
 Domain layer (signup.py, session_phase.py, upi_runner.py, payment_link.py)
     ↓               ↓              ↓
 Signup            Session      Payment
 (browser/HTTP)    (HTTP)       (HTTP)
     ↓               ↓              ↓
 SQLite (jobs/combos/session_results/settings)
```

**Key subsystems:**
- **signup.py + phases:** Orchestrator (browser/request → http → mfa)
- **mail_providers.py:** 5 OTP backends (Worker/Outlook/DongVanFB/GmailAdvanced)
- **web/:** FastAPI server + 4 job managers + static UI + SSE mux
- **db/:** SQLite pool + schema + migrations + repositories
- **icloud_hme/:** HmeRunner + 4 services (generator/checker/manager/pool)
- **pay_upi_http.py + upi_runner.py:** UPI flow + QR probe
- **stripe_token.py:** Reverse-engineer Stripe js_checksum

---

## Configuration

### Environment Variables (.env)

| Variable | Default | Range | Purpose |
|----------|---------|-------|---------|
| `BROWSER_ENGINE` | `camoufox` | camoufox, playwright | Browser type for signup |
| `BROWSER_RANDOM_SCREEN` | `true` | bool | Random viewport (stealth) |
| `RUNTIME_DIR` | `runtime` | path | Profiles, sessions, DB |
| `HYBRID_MAX_CONCURRENT` | `2` | 1–10 | Signup job workers |
| `HYBRID_JOB_TIMEOUT` | `240` | 30–600 | Sec, must > 180 (OTP) |
| `HYBRID_OUTLOOK_PROXY` | `` | CSV URL list | Seed proxy pool (optional) |
| `ICLOUD_RETRY_INTERVAL` | `900` | 10–86400 | Sec between HmeRunner cycles |
| `ICLOUD_MAX_ERRORS_PER_CYCLE` | `0` | 0+ | Cap errors/cycle (0=unlimited) |
| `ICLOUD_API_AUTH_TOKEN` | `` | string | Bearer token for /api/icloud/* |
| `GPT_SIGNUP_WEB_TOKEN` | `` | string | Override web auth token |

Priority: env var > .env > defaults in `config.py`.

### Runtime Settings (SQLite)

Single source of truth: `settings` table. Namespace: `namespace.key` (lowercase, dot-separated).

Examples: `reg.headless=false`, `proxy.pool=...`, `upi.approve_retries=10`.

**Access via:**
- Backend: `SettingsRepository.get(key)` / `.save(key, val)`
- Frontend: `Settings.get(key)` / `Settings.save(key, val, token)` (JS)

All new keys must be added to `_EXACT_KEYS` + `_validate_type_constraint()` before use.

---

## Data Model

### Core Tables

| Table | Purpose | Key Fields |
|-------|---------|-----------|
| `jobs` | Job lifecycle (reg/session/link) | id, job_type, email, status, created_at, updated_at |
| `job_logs` | Line-by-line logs | job_id, seq, level, message, metadata |
| `outlook_combos` | Outlook pool | email, password, refresh_token, client_id, status, used_count |
| `session_results` | Session JSON + MFA pending | id, email, session_json, mfa_pending, session_data |
| `settings` | KV store (single source of truth) | key, value (JSON) |
| `chatgpt_accounts` | Accounts created by AutoReg | email, password, user_id, created_at |
| `icloud_emails` | HME emails + status | email, status, created_at, used_count, label, note |
| `icloud_accounts` | iCloud pool management | apple_id, email, password, created_at |

### Output Files

| File | Format | When Written |
|------|--------|-----|
| `runtime/sessions/signup-<ts>-<email>.json` | Full SignupResult (browser + http + mfa) | After successful signup |
| `runtime/sessions/accounts.txt` | email\|password\|2fa_secret (per line) | After 2FA enroll (both branches) |
| `runtime/sessions/links.txt` | Payment URL (1 per line) | After get-link success |
| `runtime/upi_qr/<id>.png` | QR image | After UPI confirm |

---

## Security Model

**Authentication:** Bearer token on all `/api/*` routes.
- **Loopback (127.0.0.1):** Token auto-inject via meta tag.
- **Non-loopback:** Token required in header `Authorization: Bearer`, query `?token=`, or cookie `gsh_token`.
- **Scope:** All endpoints protected; no public routes.

**Secrets Handling:**
- Proxy credentials masked in logs
- API keys redacted (Outlook refresh_token, Worker auth, Gmail API key)
- Auth token never logged (except test/ probes)

**Browser Automation:**
- Headed mode only (not headless) — Sentinel/Cloudflare detection lower
- Anti-detect via Camoufox (Firefox stealth + random profile)
- User-agent + curl_cffi impersonate must match (firefox135 default)

**Constraints:**
- Refresh token rotates on MS Graph call — persist-before-mutate
- Hard cap 15s on token refresh (prevent hanging)
- Headless detection by Sentinel/CloudFlare higher (verify empirically)

---

## Testing Strategy

### Current State

**Verification scripts** (in `test/`), NOT formal test suite:
- `check_*.py` — module import + sanity checks (12 per script)
- `smoke_*.py` — FastAPI integration (TestClient)
- `test_*.py` — pytest-compatible unit tests
- `probe_*.py` — research/exploratory

**Examples:**
- `check_upi_module_imports.py` — UPI module chain verifies
- `smoke_upi_server_boot.py` — FastAPI startup + /api/* endpoints respond

### Recommended Additions

1. **End-to-end signup** — local test account (prevent rate-limit)
2. **Mail provider mock** — test OTP polling logic without real API
3. **Database migration** — verify schema upgrade path
4. **Payment link** — mock Stripe checkout response
5. **iCloud HME** — mock Apple API (rate-limit sensitive)

---

## Deployment

### Local Setup

```bash
bash setup.sh                # Create .venv + install deps + fetch Camoufox
.venv/bin/python -m gpt_signup_hybrid web --host 127.0.0.1 --port 8083
```

### Production Considerations

**NOT hardened for public deployment:**
- No HTTPS (run behind reverse proxy)
- No rate limiting (throttle at firewall)
- No request logging (sensitive ops)
- Bearer token is single shared secret (rotate occasionally)

**Safe to expose internally (LAN):**
- Use `--unsafe-expose-network` if binding non-loopback
- Firewall to trusted IPs only
- Rotate auth token periodically

---

## Known Constraints

1. **Headless mode** — Detection by Sentinel/CloudFlare higher; headed default
2. **Outlook refresh token** — Rotates per Graph API call; must persist before mutation
3. **2FA enroll** — 2 code paths (signup main + retry-only); both write `accounts.txt`
4. **Payment link** — Requires active ChatGPT Plus account (India region for UPI)
5. **iCloud HME** — Rate-limited by Apple; 900s retry interval (configurable, min 10s)
6. **Pure HTTP signup** — Sentinel PoW solver required; CSRF/OAuth/device_id tracking complex
7. **Test suite** — Limited automated coverage; manual CLI/UI verification recommended

---

## Success Metrics

| Metric | Target | How Measured |
|--------|--------|-----|
| **Signup success rate** | >90% | Job success count / total jobs submitted |
| **2FA enrollment rate** | >95% | 2FA-enabled accounts / signup-success |
| **Job recovery rate** | 100% | Restarted jobs complete same as fresh |
| **OTP latency** | <120s (outlier 180s) | Time from request to OTP received |
| **UPI QR latency** | <60s | Time from login to QR PNG saved |
| **Uptime (web UI)** | >99.5% | Minutes without /api/* 5xx errors |
| **Proxy failover** | Auto + transparent | Dead proxy removed next request |
| **Auth token entropy** | High | Token >32 chars, random base64 |

---

## Rollout Plan

### Phase 1: Stabilize Core Signup (v2.0.0)
- ✅ Hybrid flow (browser + HTTP)
- ✅ OTP polling (5 providers)
- ✅ 2FA enrollment
- ✅ SQLite persistence

### Phase 2: Web UI & Multi-job (v2.1.0)
- ✅ FastAPI server
- ✅ 4 job managers (Reg/Session/Link/UPI)
- ✅ Static JS frontend
- ✅ SSE real-time updates

### Phase 3: Payment Integration (v2.2.0)
- ✅ UPI QR probe
- ✅ Stripe token extraction
- ✅ Payment link extraction

### Phase 4: iCloud HME Runner (v2.3.0)
- ✅ HmeRunner infinite loop
- ✅ AutoReg (poll emails → signup)
- ✅ Bulk management actions

### Phase 5: Polish & Hardening (v2.4.0)
- Codex OAuth (separate tool)
- Extended test coverage
- Performance optimization (parallel jobs)

---

## Unresolved Questions

1. **Rate limiting:** Should we throttle ChatGPT API calls to avoid bot detection? Current approach relies on proxy rotation.
2. **Account recovery:** If signup partially succeeds (email created but no password set), can we resume from checkpoint?
3. **iCloud HME sync:** How to detect expired profiles without polling?
4. **Stripe token rotation:** js_checksum lifetime; do we cache or re-extract per request?
5. **Test coverage:** Can we safely mock mail providers without hitting real APIs?
