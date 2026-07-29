# Codebase Summary

Generated: 2026-06-17 | Version: 2.0.0

---

## Module Overview

### Root-Level Modules (600+ LOC)

| Module | LOC | Responsibility |
|--------|-----|-----------------|
| **signup.py** | 198 | Orchestrator: routes browser/request phase → http phase → mfa phase |
| **browser_phase.py** | 2001 | Camoufox Playwright state machine, fills signup form, OTP polling integration |
| **request_phase.py** | 1012 | Pure HTTP signup state machine (curl_cffi), alternative to browser_phase |
| **http_phase.py** | 179 | Extract session_token + access_token via curl_cffi after browser phase |
| **session_phase.py** | 1045 | Login account (browser or HTTP) → extract accessToken + planType + profile |
| **mfa_phase.py** | 427 | Enroll TOTP 2FA, extract secret, save to DB/file |
| **mail_providers.py** | 1228 | 5 mail backends (Worker/Outlook/DongVanFB/GmailAdvanced/OutlookCascade) |
| **sentinel_quickjs.py** | 462 | OpenAI Sentinel PoW solver via QuickJS subprocess (primary) |
| **sentinel_pow.py** | 185 | Fallback Python FNV-1a PoW solver |
| **outlook_pool.py** | 257 | Outlook combo pool management (load/status) |
| **payment_link.py** | 882 | ChatGPT checkout → Stripe init → hosted payment URL extraction |
| **pay_upi_http.py** | 1175 | Pure HTTP UPI payment flow (Stripe confirm variants) + constants |
| **stripe_token.py** | 597 | Reverse-engineer js_checksum + rv_timestamp from Stripe bundle |
| **record_pay_upi.py** | 825 | Hybrid recorder (browser + HTTP) for payment flow debugging |
| **config.py** | 477 | Pydantic config dataclass + .env parsing + runtime paths |
| **models.py** | 147 | Pydantic (SignupRequest, SignupResult, BrowserHandoff) |
| **cli.py** | 885 | Typer CLI interface (web, signup, enable-2fa, totp, migrate, record) |
| **web_recorder.py** | 776 | Browser recorder + HAR capture + trace export |
| **random_profile.py** | 197 | Camoufox profile randomization (user-agent, locale, timezone) |
| **totp_helper.py** | 90 | TOTP secret generation/validation helper |

### Web Layer (web/, 9352 LOC total)

| Module | LOC | Responsibility |
|--------|-----|-----------------|
| **server.py** | 1784 | FastAPI app, 30+ endpoints, auth middleware, startup/shutdown |
| **manager.py** | 4226 | 4 job managers (Reg/Session/UPI/Link), worker pool, SSE broadcast, recovery |
| **upi_runner.py** | 1124 | Async UPI QR probe (login → checkout → confirm → QR render) |
| **icloud_routes.py** | 1257 | /api/icloud/* routes, AutoReg runner, HmeRunner delegation |
| **mail_modes.py** | 319 | Mail provider registry (map mode name → constructor) |
| **proxy_pool.py** | 170 | Proxy round-robin/random, mark-dead tracking, atomic pick/mark-dead |
| **proxy_format.py** | 105 | {SID} placeholder parsing, materialize_proxy(), mask_proxy() for logging |
| **proxy_health.py** | 265 | probe_proxy() L4 test, acquire_live_proxy() rotate loop, asyncio.Semaphore bound |
| **runner_config_store.py** | 254 | Runtime settings store + validation |
| **auth.py** | 115 | Bearer token verification (header/query/cookie/meta-tag) |
| **sse_mux.py** | 105 | SSE multi-channel fan-out + snapshot |
| **static/** | — | Frontend (index.html, app.js, session.js, upi.js, hme.js, autoreg.js, settings*.js) |

### Database Layer (db/, 4204 LOC total)

| Module | LOC | Responsibility |
|--------|-----|-----------------|
| **engine.py** | 488 | SQLite WAL pool, transaction, connection mgmt |
| **schema.py** | 578 | DDL for jobs, job_logs, combos, session_results, settings, icloud_*, chatgpt_accounts |
| **repositories.py** | 2617 | Data access: JobRepository, ComboRepository, SessionResultRepository, SettingsRepository, iCloud*, ChatGptAccountRepository |
| **migrate.py** | 427 | Version-based idempotent migrations (auto-apply on startup) |
| **__init__.py** | 94 | Exports get_engine(), get_repos(), get_settings_repo() |

### iCloud HME Subsystem (icloud_hme/, 10295 LOC total)

| Module | LOC | Responsibility |
|--------|-----|-----------------|
| **runner.py** | 250 | HmeRunner: infinite loop, 7 actions, pause/resume/cancel, log buffer |
| **generator.py** | 2100+ | HmeGenerator: create HME via Apple API (batch + infinite) |
| **checker.py** | 1500+ | ProfileChecker: check iCloud profile status |
| **manager.py** | 2000+ | HmeManager: deactivate/reactivate/delete/update bulk, list_sync |
| **pool.py** | 1200+ | IcloudPoolManager: profile pool lifecycle |
| **client.py** | 1200+ | Apple API client (session, OTP, HME CRUD) |
| **session.py** | 500+ | iCloud session + 2FA handling |
| **exceptions.py** | 100+ | iCloud-specific exceptions |
| **models.py** | 200+ | iCloud Pydantic models |
| **web/** | 500+ | iCloud-specific routes + frontend (hme.js) |

### Autoreg Subsystem (autoreg/, 496 LOC)

| Module | LOC | Responsibility |
|--------|-----|-----------------|
| **runner.py** | ~400 | AutoRegRunner: poll HME emails → run signup + 2FA → save chatgpt_accounts |
| **models.py** | ~100 | AutoReg Pydantic models |

### Codex OAuth (codex_auth/, 812 LOC)

| Module | LOC | Responsibility |
|--------|-----|-----------------|
| **runner.py** | ~200 | Entry: get_codex_auth() orchestrator |
| **browser.py** | ~150 | Camoufox drives OAuth flow (PKCE) |
| **oauth.py** | ~200 | Token exchange, redirect handling |
| **pkce.py** | ~100 | PKCE code challenge/verifier generation |
| **errors.py** | ~50 | Custom exceptions |
| **__main__.py** | ~50 | CLI entry |

### Test & Verification (test/, minimal)

| Script | Purpose |
|--------|---------|
| **check_upi_module_imports.py** | 12 sanity checks: UPI module chain imports |
| **smoke_upi_server_boot.py** | 12 integration checks: FastAPI TestClient /api/* |
| **test_*.py, probe_*.py** | Research/unit tests (as needed) |

---

## Dependency Graph (High Level)

```
┌─ CLI (cli.py)
│   └─ Commands: web, signup, enable-2fa, totp, migrate, record, pool-status, import-pool
│
├─ Web Entry (web/server.py)
│   ├─ FastAPI app
│   ├─ Auth middleware (auth.py)
│   ├─ 30+ endpoints (reg/session/upi/link/icloud/settings)
│   ├─ 4 job managers (manager.py)
│   ├─ SSE mux (sse_mux.py)
│   └─ Static UI (static/*.js, index.html)
│
├─ Proxy Layer (all login flows)
│   ├─ proxy_format.py (materialize_proxy: {SID} → concrete URL)
│   ├─ proxy_health.py (acquire_live_proxy: probe-rotate loop)
│   └─ [Used by: Get Session, Get Link, Reg flows; mode=probe in pool config]
│
├─ Signup Entry (signup.py → run_signup)
│   ├─ Phase 1: browser_phase.py or request_phase.py
│   │   ├─ mail_providers.py (OTP polling)
│   │   ├─ sentinel_quickjs.py / sentinel_pow.py (PoW)
│   │   └─ [Camoufox + Playwright] / [curl_cffi]
│   ├─ Phase 2: http_phase.py (extract tokens)
│   └─ Phase 3: mfa_phase.py (TOTP enroll)
│
├─ Session Entry (session_phase.py → get_session)
│   ├─ [Routed via proxy_health.acquire_live_proxy if pool active]
│   ├─ browser_phase.login_browser() or request_phase.get_session_pure_request()
│   └─ [Camoufox + Playwright] / [curl_cffi]
│
├─ Payment Flow
│   ├─ payment_link.py → get_checkout_url()
│   ├─ stripe_token.py → extract_config_live()
│   ├─ pay_upi_http.py → _stripe_init / _stripe_confirm_upi
│   └─ upi_runner.py → run_upi_qr_probe() (manager.py integration)
│      └─ [Routed via proxy_health.acquire_live_proxy if pool active]
│
├─ iCloud HME (icloud_hme/runner.py → HmeRunner)
│   ├─ generator.py (HmeGenerator.generate)
│   ├─ checker.py (ProfileChecker.check_all)
│   ├─ manager.py (bulk deactivate/reactivate/delete/update)
│   ├─ pool.py (IcloudPoolManager)
│   ├─ client.py (Apple API)
│   └─ [asyncio infinite loop]
│
├─ AutoReg (autoreg/runner.py → AutoRegRunner)
│   ├─ Poll icloud_emails (status='created') from DB
│   ├─ run_signup per email
│   ├─ Save chatgpt_accounts on success
│   └─ [asyncio queue pipeline]
│
└─ Database (db/engine.py + repositories.py)
    ├─ SQLite `runtime/data.db` (WAL mode)
    ├─ Auto-migration on startup (db/migrate.py)
    ├─ JobRepository, ComboRepository, SessionResultRepository, SettingsRepository
    ├─ iCloud repositories (IcloudAccountRepository, IcloudEmailRepository)
    └─ ChatGptAccountRepository

```

---

## Data Flow Diagrams

### Signup Flow (Web UI Reg Tab)

```
User: POST /api/jobs/register
  └─ Payload: {email, password, mail_provider, account_type, proxy, ...}
       │
       └─ JobManager.enqueue()
            └─ _persistent_register() worker
                 ├─ run_signup(request) [signup.py]
                 │   ├─ Phase 1: browser_phase or request_phase
                 │   │   ├─ Poll OTP via mail_provider (mail_providers.py)
                 │   │   │   └─ Worker / Outlook / DongVanFB / Gmail
                 │   │   ├─ PoW solver (Sentinel challenge)
                 │   │   └─ Fill signup form
                 │   ├─ Phase 2: http_phase extract tokens
                 │   └─ Phase 3: optional mfa_phase (TOTP enroll)
                 │
                 └─ SessionResultRepository.insert()
                      └─ SQLite session_results table
                           └─ SSE broadcast /api/sse (job_updated, signup_complete)
                                └─ Frontend: app.js updates UI
```

### Session Extraction (Web UI Get Session Tab)

```
User: POST /api/jobs/session
  └─ Payload: {email, password, ...}
       │
       └─ SessionJobManager.enqueue()
            └─ _persistent_session() worker
                 ├─ session_phase.get_session() [browser or HTTP]
                 │   ├─ Login → accessToken
                 │   └─ Fetch profile → planType
                 │
                 └─ SessionResultRepository.insert() + SSE broadcast
```

### UPI QR Probe (Web UI UPI QR Tab)

```
User: POST /api/upi/jobs
  └─ Payload: {email, password, secret, ...}
       │
       └─ UpiJobManager.enqueue()
            └─ _persistent_upi() worker
                 ├─ web/upi_runner.run_upi_qr_probe()
                 │   ├─ session_phase.get_session_pure_request() [HTTP direct]
                 │   ├─ payment_link.get_checkout_url()
                 │   ├─ stripe_token.extract_config_live() [parse js_checksum]
                 │   ├─ pay_upi_http._stripe_init() [create payment method]
                 │   ├─ pay_upi_http._stripe_confirm_upi() [confirm UPI, retry loop]
                 │   └─ qrcode render → PNG save to runtime/upi_qr/
                 │
                 └─ Return {qr_path, upi_uri} → SSE broadcast
                      └─ Frontend: upi.js renders PNG in modal
```

### iCloud HME Runner (Infinite Loop)

```
User: POST /api/icloud/run
  └─ Payload: {action, params, retry_interval}
       │
       └─ HmeRunner.start()
            ├─ spawn cancel_event / pause_event / resume_event
            └─ [WHILE NOT CANCEL]
                 │
                 ├─ Cycle N: dispatch action
                 │   ├─ generate → HmeGenerator.generate()
                 │   ├─ check_all → ProfileChecker.check_all()
                 │   ├─ deactivate_bulk → HmeManager.deactivate_bulk()
                 │   └─ ... (5 other actions)
                 │
                 ├─ Log fan-out (LogCallback)
                 │   └─ Web: LogBuffer → SSE /api/icloud/run/log/stream
                 │   └─ CLI: stderr
                 │
                 └─ Sleep retry_interval (interruptible 1s chunks)
                      └─ react ≤ 1.5s to pause/resume/cancel
                           │
                           └─ On stop(): return {total_cycles, created, errors, skipped}
```

### AutoReg Pipeline (Autoreg Runner)

```
[Infinite asyncio loop]
  └─ Poll icloud_emails (status='created') from DB
       │
       ├─ Dequeue email batch (workers: 1-5 concurrent)
       │   │
       │   ├─ run_signup(email, password)
       │   │   └─ [Same as Signup Flow above]
       │   │
       │   └─ On success: ChatGptAccountRepository.insert()
       │       └─ SQLite chatgpt_accounts table
       │
       └─ Retry queue (transient DB errors, bounded 3 retries)
```

---

## Code Patterns & Conventions

### Async Architecture

- **FastAPI:** async routes, async managers spawning asyncio.create_task()
- **Managers (manager.py):** WorkerPool with asyncio.Queue, bounded concurrency
- **iCloud HmeRunner:** asyncio.Event (cancel/pause/resume), sleep interruptible
- **AutoReg:** asyncio.Queue pipeline (poll → sign up → save)

### Error Handling

- **Custom exceptions:** e.g., `SentinelFailedError`, `OtpTimeoutError`, `PaymentLinkError`
- **Retry logic:** bounded retries (3x default for transient SQLite), exponential backoff for mail APIs
- **Sentinel fallback:** QuickJS → Python PoW solver on timeout
- **Mail provider fallback:** DongVanFB → Outlook (OutlookCascadeProvider)

### Persistence

- **SQLite WAL mode:** single `runtime/data.db`, auto-migration on startup
- **Persist-before-mutate:** Outlook refresh_token saved before Graph API call
- **Job recovery:** on web startup, fetch `status IN (pending, running)` from DB, re-enqueue
- **Settings:** single source of truth in `settings` table, not .env or JSON files

### Authentication

- **Token auth:** all `/api/*` endpoints require Bearer token (header/query/cookie/meta-tag)
- **Auto-inject loopback:** meta tag for 127.0.0.1 access (no token pass needed in URL)
- **Non-loopback:** explicit token in header `Authorization: Bearer` or query `?token=`

### Logging

- **Web:** job logs persist to `job_logs` table + SSE broadcast
- **CLI:** rich.print() to stderr
- **iCloud HmeRunner:** LogBuffer (capped FIFO 10K) + SSE stream
- **Redaction:** proxy creds, API keys, auth token masked in logs

### Configuration

- **Pydantic dataclass:** `config.py` with typed fields, env var defaults
- **Runtime settings:** SQLite `settings` table (single source of truth)
- **Priority:** os.environ > .env > defaults

---

## File Ownership & Responsibilities

### Signup & Authentication
- `signup.py` — Orchestrator
- `browser_phase.py` — Camoufox automation
- `request_phase.py` — Pure HTTP alternative
- `http_phase.py` — Token extraction post-browser
- `session_phase.py` — Login + profile fetch
- `mfa_phase.py` — TOTP enrollment
- `mail_providers.py` — OTP backends (5 types)
- `sentinel_*.py` — Bot detection evasion

### Web & API
- `web/server.py` — FastAPI routing + auth
- `web/manager.py` — Job orchestration (4 managers)
- `web/upi_runner.py` — UPI QR async probe
- `web/auth.py` — Token verification
- `web/sse_mux.py` — Real-time event streaming

### Database
- `db/engine.py` — SQLite pool
- `db/schema.py` — DDL
- `db/migrate.py` — Migrations
- `db/repositories.py` — Data access layer

### Subsystems
- `icloud_hme/` — Hide-My-Email management
- `autoreg/` — Auto-registration pipeline
- `codex_auth/` — Codex OAuth tool
- `pay_upi_http.py` — UPI payment flow
- `payment_link.py` — Checkout URL extraction
- `stripe_token.py` — Stripe reverse-engineering

### Configuration & Utilities
- `config.py` — App config + paths
- `models.py` — Pydantic data models
- `cli.py` — CLI interface
- `web_recorder.py` — HAR/trace recording

---

## External Dependencies (Top 20)

| Package | Version | Use |
|---------|---------|-----|
| camoufox | 0.4.11 | Firefox anti-detect browser |
| curl_cffi | 0.15 | TLS-impersonate HTTP client |
| playwright | 1.49 | Browser automation (Camoufox driver) |
| fastapi | 0.136 | Web framework |
| uvicorn | 0.30+ | ASGI server |
| pydantic | 2.13+ | Data validation |
| httpx | 0.26+ | Async HTTP client (fallback) |
| typer | 0.9+ | CLI framework |
| rich | 13.0+ | Terminal UI |
| pyotp | 2.9+ | TOTP generation |
| qrcode[pil] | 7.4+ | QR code rendering |
| SQLAlchemy | 2.0+ | ORM (optional, raw sql3 used) |
| requests | 2.31+ | HTTP client (legacy) |
| beautifulsoup4 | 4.12+ | HTML parsing |
| lxml | 4.9+ | XML parsing |
| cryptography | 41+ | Crypto utilities |
| websocket-client | 1.6+ | WebSocket (iCloud HME) |
| aiofiles | 23.2+ | Async file I/O |
| loguru | 0.7+ | Enhanced logging (optional) |
| python-dotenv | 1.0+ | .env loading |

---

## Module Metrics

### Largest Modules (by LOC)

1. browser_phase.py — 2001 LOC (signup page state machine)
2. icloud_hme/generator.py — 2100+ LOC (HME generation)
3. icloud_hme/manager.py — 2000+ LOC (bulk HME operations)
4. web/manager.py — 4226 LOC (job managers + worker pool)
5. db/repositories.py — 2617 LOC (data access layer)

### Critical Path (Most Connected)

1. **web/server.py** → all routes depend on auth + manager dispatch
2. **web/manager.py** → all job execution flows pass through
3. **mail_providers.py** → OTP polling bottleneck (browser_phase + request_phase)
4. **db/repositories.py** → all persistence flows
5. **config.py** → app-wide settings

### Cyclomatic Complexity (High Risk)

- **browser_phase._drive_signup_flow()** — ~30 branches (page-state machine)
- **request_phase.run_signup_request()** — ~25 branches (state transitions)
- **mail_providers.py** — multiple providers with conditional logic (~15–20 per provider)
- **stripe_token.extract_config_live()** — regex/parsing complexity (~12)

---

## Technology Stack Summary

| Layer | Technology | Version |
|-------|-----------|---------|
| **Language** | Python | 3.11+ |
| **Web Framework** | FastAPI | 0.136 |
| **Browser Automation** | Camoufox + Playwright | 0.4.11 + 1.49 |
| **HTTP Client** | curl_cffi | 0.15 |
| **Database** | SQLite | WAL mode (stdlib) |
| **Frontend** | Vanilla JS (no framework) | ES6+ |
| **CLI** | Typer + Rich | 0.9+ + 13.0+ |
| **Validation** | Pydantic | 2.13+ |
| **2FA** | pyotp | 2.9+ |
| **QR Encoding** | qrcode[pil] | 7.4+ |
| **Async** | asyncio | stdlib |

---

## Notable Features & Edge Cases

1. **Hybrid Signup:** Browser (anti-detect) + pure HTTP (TLS-spoof) both supported
2. **OTP Polling:** 5 backends (Worker/Outlook/DongVanFB/Gmail/Cascade) with timeout + retry
3. **Sentinel PoW:** QuickJS VM (primary) + Python fallback, configurable timeout
4. **2FA Enroll:** 2 code paths (signup main vs retry-only), both append to `accounts.txt`
5. **Payment:** Stripe reverse-engineer (js_checksum + rv_timestamp), UPI QR + link extraction
6. **iCloud HME:** Infinite loop with pause/resume/cancel, graceful SIGINT handling
7. **AutoReg:** Queue pipeline (poll HME → sign up → save), bounded concurrency
8. **Proxy Failover:** Round-robin + auto-mark dead, transparent retry
9. **Job Recovery:** SQLite persistence, auto-resume on web restart
10. **Real-time UI:** SSE multi-channel (reg/session/upi/icloud logs) + event fan-out

---

## Known Technical Debt

1. **browser_phase.py line complexity** — state machine could benefit from refactoring to state pattern
2. **Test coverage** — limited automated tests; reliant on manual CLI/UI verification
3. **Payment link** — regex-based parsing of Stripe bundle (fragile to JS changes)
4. **iCloud session** — Apple API undocumented; reverse-engineered (may break)
5. **Pure HTTP signup** — complex state tracking; prefer browser phase when possible
6. **Sentinel solver** — QuickJS subprocess overhead; consider pre-warming worker
7. **SQLite scaling** — WAL mode fine for <100K jobs; consider PostgreSQL for production scale
8. **Logging redaction** — currently manual; could use decorator-based filtering

---

## Quick Navigation

**Want to...**
- **Add new mail provider?** → Edit `mail_providers.py` + register in `web/mail_modes.py`
- **Add new CLI command?** → Edit `cli.py` (Typer), dispatch in `__main__.py`
- **Add new web endpoint?** → Edit `web/server.py`, add manager method in `web/manager.py`
- **Add new DB table?** → Edit `db/schema.py` + add migration in `db/migrate.py` + add repository in `db/repositories.py`
- **Debug signup failure?** → Check `browser_phase._drive_signup_flow()` state transitions + logs in `job_logs` table
- **Optimize job throughput?** → Increase `HYBRID_MAX_CONCURRENT` in `.env` (test proxy availability)
- **Change UPI confirm retry logic?** → Edit `pay_upi_http._stripe_confirm_upi()` retry loop
