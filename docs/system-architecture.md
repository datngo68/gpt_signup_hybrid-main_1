# System Architecture

---

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     FRONTEND LAYER                               │
│  Vanilla JS (no framework/bundler) + Vanilla CSS + HTML         │
│  ├─ index.html (static server by FastAPI)                       │
│  ├─ app.js (Registration tab)                                   │
│  ├─ session.js (Get Session tab)                                │
│  ├─ upi.js (UPI QR tab)                                         │
│  ├─ hme.js (iCloud HME management)                              │
│  ├─ autoreg.js (AutoReg status)                                 │
│  ├─ settings.js (Settings panel)                                │
│  └─ settings-*.js (proxy, mail modes, etc.)                     │
│                                                                  │
│  Communication:                                                  │
│  • REST API: /api/* (POST/GET/DELETE)                           │
│    Header: X-API-Token (required)                               │
│  • Server-Sent Events: /api/sse (Bearer token)                  │
│    Channels: job_updated, signup_complete, upi_*, etc.         │
└────────┬───────────────────────────────────────────┬────────────┘
         │ HTTP                                       │ WebSocket-like
         │ (Bearer auth)                             │ (SSE EventSource)
         ▼                                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                    WEB API LAYER (FastAPI)                      │
│  web/server.py (1784 LOC) — 30+ endpoints                      │
│  ├─ Auth Middleware: verify_token() on all /api/*              │
│  ├─ Startup: apply_settings() from SQLite                      │
│  ├─ Registration routes: /api/jobs/register, GET, DELETE       │
│  ├─ Session routes: /api/jobs/session, GET                     │
│  ├─ UPI routes: /api/upi/jobs, GET, DELETE, /qr (PNG)        │
│  ├─ Link routes: /api/jobs/link (payment URL extraction)       │
│  ├─ iCloud routes: /api/icloud/* (HmeRunner delegation)        │
│  ├─ Settings routes: /api/settings/* (KV store)                │
│  ├─ Proxy routes: /api/proxies/* (pool management)             │
│  ├─ Admin routes: /api/admin/* (job recovery, cleanup)         │
│  └─ SSE route: /api/sse (event streaming via sse_mux.py)       │
│                                                                  │
│  Error Handling:                                                │
│  • 401 Unauthorized (missing/invalid token)                     │
│  • 400 Bad Request (Pydantic validation)                        │
│  • 404 Not Found (job/resource doesn't exist)                   │
│  • 409 Conflict (HmeRunner already running, job duplicate)      │
│  • 5xx Internal (unhandled exception)                           │
└────────┬──────────────┬──────────────┬──────────────┬───────────┘
         │              │              │              │
         ▼              ▼              ▼              ▼
    Job Manager    Session Mgr   UPI Job Mgr   Link/HME Mgr
    (Reg jobs)     (Get Session) (UPI QR)      (AutoReg)
         │              │              │              │
         └──────────────┴──────────────┴──────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────────┐
│              DOMAIN/BUSINESS LOGIC LAYER                         │
│  Orchestrators + Phase Controllers                              │
│  ├─ signup.py (run_signup)                                      │
│  │   ├─ Phase 1: browser_phase.py or request_phase.py           │
│  │   ├─ OTP polling: mail_providers.py (5 backends)             │
│  │   ├─ Phase 2: http_phase.py (extract tokens)                 │
│  │   └─ Phase 3: mfa_phase.py (TOTP enrollment)                 │
│  │                                                               │
│  ├─ session_phase.py (get_session)                              │
│  │   ├─ Browser-based: Camoufox + Playwright                    │
│  │   └─ HTTP-based: curl_cffi TLS-spoof                         │
│  │                                                               │
│  ├─ payment_link.py (get_checkout_url)                          │
│  │   └─ ChatGPT checkout → Stripe init → hosted URL            │
│  │                                                               │
│  ├─ pay_upi_http.py (pure HTTP UPI payment flow)                │
│  │   ├─ _stripe_init (create payment method)                    │
│  │   ├─ _stripe_confirm_upi (confirm UPI, retry variants)       │
│  │   └─ constants (Stripe endpoints, headers)                   │
│  │                                                               │
│  ├─ proxy_format.py (proxy line parsing + SID materialization)  │
│  │   ├─ materialize_proxy (replace {SID}/{sid} → concrete URL)  │
│  │   ├─ gen_sid (random session ID generator)                   │
│  │   └─ mask_proxy (redact credentials for logging)             │
│  │                                                               │
│  ├─ proxy_health.py (proxy health-check loop)                   │
│  │   ├─ probe_proxy (L4 connectivity test)                      │
│  │   ├─ acquire_live_proxy (SID-rotate until probe OK)          │
│  │   └─ [asyncio.Semaphore bounded concurrency]                 │
│  │                                                               │
│  ├─ upi_runner.py (async UPI QR probe)                          │
│  │   ├─ Login → get accessToken                                 │
│  │   ├─ Fetch checkout → Stripe init                            │
│  │   ├─ Extract js_checksum (stripe_token.py)                   │
│  │   ├─ Confirm UPI (retry loop, proxy rotation)                │
│  │   └─ Render QR → save PNG                                    │
│  │                                                               │
│  ├─ icloud_hme/runner.py (HmeRunner infinite loop)              │
│  │   ├─ 7 actions: generate, check, deactivate, etc.            │
│  │   ├─ Cycle-based execution with pause/resume/cancel          │
│  │   └─ Log buffer + SSE stream                                 │
│  │                                                               │
│  └─ autoreg/runner.py (AutoReg queue pipeline)                  │
│      ├─ Poll icloud_emails from DB                              │
│      ├─ run_signup per email                                    │
│      └─ Save chatgpt_accounts on success                        │
│                                                                  │
│  Sentinel Anti-Bot:                                             │
│  ├─ sentinel_quickjs.py (primary: QuickJS VM, ~0.2s)            │
│  └─ sentinel_pow.py (fallback: Python FNV-1a solver)            │
└────────┬──────────────────────────────────────────┬─────────────┘
         │                                          │
         ▼                                          ▼
┌─────────────────────────────────────────────────────────────────┐
│              PERSISTENCE LAYER (SQLite WAL)                     │
│  db/engine.py — connection pool, transaction                    │
│  db/schema.py — DDL (tables, indexes)                           │
│  db/migrate.py — version-based migrations                       │
│  db/repositories.py — data access                               │
│                                                                  │
│  Tables:                                                         │
│  ├─ jobs, job_logs — job lifecycle + logs                       │
│  ├─ outlook_combos — combo pool                                 │
│  ├─ session_results — session JSON + MFA pending                │
│  ├─ settings — single source of truth (KV)                      │
│  ├─ icloud_accounts, icloud_emails, icloud_audit_log — HME      │
│  ├─ chatgpt_accounts — accounts created by AutoReg              │
│  └─ pool_state — proxy pool metadata                            │
│                                                                  │
│  File: runtime/data.db (auto-create on startup)                 │
└────────┬────────────────────────────────────────┬────────────────┘
         │                                        │
         └────────────────┬───────────────────────┘
                          │
         ┌────────────────┴────────────────┐
         │                                 │
         ▼                                 ▼
    EXTERNAL APIs                    OUTPUT FILES
    ├─ auth.openai.com              ├─ runtime/sessions/
    │  (signup, login, logout)       │  ├─ signup-<ts>-<email>.json
    ├─ ChatGPT API                   │  ├─ accounts.txt (email|pass|2fa)
    │  (plan type, profile)          │  └─ links.txt (payment URLs)
    ├─ Stripe Payment                │
    │  (checkout init, confirm)      └─ runtime/upi_qr/
    ├─ Microsoft Graph               │  └─ <job_id>.png (QR images)
    │  (Outlook OTP polling)         │
    ├─ Apple iCloud                  └─ runtime/
    │  (HME generation/mgmt)         ├─ camoufox/ (Firefox profiles)
    ├─ Cloudflare Worker             └─ playwright/ (browser cache)
    │  (iCloud relay)                │
    ├─ DongVanFB API                 
    │  (Outlook alternate)           
    └─ GmailAdvanced API             
       (Gmail OTP polling)           
```

---

## Component Interaction Diagram

### Registration Job Flow

```
┌──────────────┐
│  User (web)  │
└──────┬───────┘
       │ POST /api/jobs/register
       │ {email, password, mail_provider, ...}
       │
       ▼
┌────────────────────────┐
│  FastAPI endpoint      │
│  @app.post("/...")     │
└──────┬─────────────────┘
       │
       ▼
┌────────────────────────┐        ┌─────────────────┐
│  JobManager            │ ◄───── │  SettingsRepo   │
│  .enqueue(request)     │        │  .get("...")    │
└──────┬─────────────────┘        └─────────────────┘
       │
       ▼
┌────────────────────────┐
│  In-memory queue       │
│  (asyncio.Queue)       │
└──────┬─────────────────┘
       │
       │ [Broadcast via SSE]
       │ {job_id, email, status: "pending"}
       │
       ▼
┌────────────────────────────────────┐
│  Worker task (Semaphore bounded)   │
│  _persistent_register()            │
└──────┬─────────────────────────────┘
       │
       ▼
┌────────────────────────────────────┐
│  signup.run_signup(request)        │
│  (signup.py)                       │
└──────┬─────────────────────────────┘
       │
       ├─────────────────────────────────────┐
       │                                     │
       ▼                                     ▼
   ┌───────────────┐            ┌──────────────────┐
   │  Phase 1      │            │  Alternative     │
   │  Browser      │            │  Pure HTTP       │
   │  (Camoufox)   │            │  (curl_cffi)     │
   └───┬───────────┘            └────┬─────────────┘
       │                             │
       │  ┌─────────────────────────┘
       │  │
       ▼  ▼
   ┌────────────────┐
   │  Mail Provider │
   │  (5 backends)  │
   └───┬────────────┘
       │
       ▼
   ┌────────────────┐         ┌──────────────┐
   │  Poll OTP      │────────►│  Worker / MS │
   │  (180s loop)   │         │  Graph / etc │
   └────────────────┘         └──────────────┘
       │
       ▼
   ┌────────────────┐
   │  Sentinel PoW  │
   │  Solver        │
   │  (QuickJS/Py)  │
   └────────────────┘
       │
       └─ re-enqueue on form fill failure
       │
       ▼
   ┌────────────────┐
   │  Phase 2       │
   │  HTTP extract  │
   │  tokens        │
   └───┬────────────┘
       │
       ▼
   ┌────────────────┐
   │  Phase 3       │
   │  TOTP enroll   │
   │  (optional)    │
   └───┬────────────┘
       │
       ▼
   ┌────────────────┐
   │  SessionResult │
   │  .insert()     │
   │  [SQLite]      │
   └────────────────┘
       │
       │ [SSE broadcast]
       │ {job_id, email, status: "success",
       │  user_id, session_token, ...}
       │
       ▼
   ┌────────────────┐
   │  Output files  │
   │  • signup-<ts>-<email>.json
   │  • accounts.txt (if 2FA)
   └────────────────┘
       │
       ▼
   ┌────────────────┐
   │  Frontend      │
   │  (app.js)      │
   │  updates UI    │
   └────────────────┘
```

---

## Data Flow Diagrams

### UPI QR Probe

```
┌──────────────┐
│  User (web)  │
└──────┬───────┘
       │ POST /api/upi/jobs
       │ {email, password, secret, ...}
       │
       ▼
┌──────────────────────┐
│  UpiJobManager       │
│  .enqueue(request)   │
└──────┬───────────────┘
       │
       ▼
┌──────────────────────┐
│  _persistent_upi()   │
│  worker              │
└──────┬───────────────┘
       │
       ▼
┌────────────────────────────────┐
│  upi_runner.run_upi_qr_probe() │
└──────┬────────────────────────┘
       │
       ├──────────────────────────────┐
       │                              │
       ▼                              ▼
   ┌─────────────────┐        ┌────────────────┐
   │  Login (HTTP)   │        │  Get Checkout  │
   │  pure_request() │        │  URL           │
   └──────┬──────────┘        └────────┬───────┘
          │                           │
          ▼                           ▼
      ┌─────────────┐           ┌─────────────┐
      │ accessToken │           │ checkoutUrl │
      └─────────────┘           └─────────────┘
              │                       │
              └───────────┬───────────┘
                          │
                          ▼
                  ┌──────────────────┐
                  │  extract_config  │
                  │  _live()         │
                  │  [stripe_token]  │
                  └────┬─────────────┘
                       │
                       ▼
                  ┌──────────────────┐
                  │  js_checksum +   │
                  │  rv_timestamp    │
                  └──────────────────┘
                       │
       ┌───────────────┴───────────────┐
       │                               │
       ▼                               ▼
   ┌─────────────┐           ┌──────────────────┐
   │  _stripe    │           │  _stripe_confirm │
   │  _init()    │           │  _upi()          │
   │  [init      │           │  [UPI variants]  │
   │   payment   │           │  [retry loop]    │
   │   method]   │           │  [proxy rotate]  │
   └──────┬──────┘           └────┬─────────────┘
          │                      │
          └──────────┬───────────┘
                     │
                     ▼
              ┌────────────────┐
              │  QR URI / URL  │
              │  (upi://...)   │
              └────────┬───────┘
                       │
                       ▼
              ┌────────────────┐
              │  qrcode render │
              │  → PNG file    │
              └────────┬───────┘
                       │
                       ▼
              ┌────────────────┐
              │  Save to       │
              │  runtime/      │
              │  upi_qr/       │
              │  <id>.png      │
              └────────┬───────┘
                       │
                       │ [SSE broadcast]
                       │ {qr_path, upi_uri}
                       │
                       ▼
              ┌────────────────┐
              │  Frontend      │
              │  (upi.js)      │
              │  shows modal   │
              └────────────────┘
```

### Proxy Health-Check Loop (All Login Flows)

```
┌──────────────────────┐
│  Proxy pool          │  raw line/template
│  (host:port:user:pass│  may contain {SID}/{sid}
│   or {SID} template) │
└──────┬───────────────┘
       │
       ▼
┌──────────────────────────────────┐
│  pick() → materialize_proxy()    │
│  ├─ Replace {SID} → random      │
│   └─ Return concrete URL        │
└──────┬───────────────────────────┘
       │
       ▼
┌──────────────────────────────────┐
│  probe_proxy (async)             │
│  ├─ HEAD https://api64.ipify.org │
│  └─ Classify: 407 (auth) vs      │
│     timeout (IP-level)           │
└──────┬───────────────────────────┘
       │
       ├─ [OK] ────────────────────────────┐
       │                                   │
       ├─ [Auth fail] (407)                │ Use for login
       │  └─ mark_dead(raw_line)           │ (all 4 flows:
       │     loop → next SID               │  UPI, Session,
       │                                   │  Link, Reg)
       └─ [IP-level fail] (timeout)        │
          └─ rotate SID, retry same line   │
             (up to sid_retry_per_line)    ▼
                                    return (url, line)
             [if exhausted] → fallback (None, None) → DIRECT
```

**Concurrency guard:** `asyncio.Semaphore(N)` (N = `proxy.probe_concurrency`, default 4).
Prevents thundering-herd when `HYBRID_MAX_CONCURRENT` = 10 jobs probe simultaneously.

---

### iCloud HME Runner (Infinite Loop)

```
┌──────────────────────┐
│  User / CLI / Web    │
└──────┬───────────────┘
       │ POST /api/icloud/run
       │ {action: "generate", params: {...}}
       │
       ▼
┌──────────────────────┐
│  HmeRunner.start()   │
│  (icloud_hme/runner) │
└──────┬───────────────┘
       │
       ├─ spawn asyncio.Event
       │  (cancel_event, pause_event, resume_event)
       │
       └─ [WHILE NOT CANCEL]
            │
            ▼
       ┌───────────────────────┐
       │  Cycle N              │
       │  Dispatch action      │
       │  ├─ generate          │
       │  │  └─ HmeGenerator   │
       │  │     .generate()    │
       │  ├─ check_all         │
       │  │  └─ ProfileChecker │
       │  ├─ deactivate_bulk   │
       │  │  └─ HmeManager     │
       │  └─ ... (4 more)      │
       └───────┬───────────────┘
               │
               ├─ [Log callback] ◄──┐
               │                    │
               ▼                    │
           ┌──────────────┐         │
           │  Log buffer  │ ────────┘
           │  (FIFO capped│
           │   10K)       │
           └──────┬───────┘
                  │
                  │ [SSE stream]
                  │ /api/icloud/run/log/stream
                  │
                  ▼
             ┌─────────────┐
             │  Frontend   │
             │  (hme.js)   │
             │  displays   │
             │  live log   │
             └─────────────┘
               │
               ├─ [User: pause/resume/cancel]
               │  └─ POST /api/icloud/run/{action}
               │     └─ set Event
               │
               ▼
           ┌──────────────────┐
           │  Sleep           │
           │  retry_interval  │
           │  (interruptible  │
           │   1s chunks)     │
           │                  │
           │  ├─ pause_event? │
           │  │  └─ wait      │
           │  ├─ cancel_event?│
           │  │  └─ break loop│
           │  ├─ resume_event?│
           │  │  └─ continue  │
           └──────┬───────────┘
                  │
                  └──► [next cycle or exit]
                       │
                       ▼
                  ┌────────────────┐
                  │  Return summary│
                  │  {total_cycles,│
                  │   created,     │
                  │   errors,      │
                  │   stopped_by}  │
                  └────────────────┘
```

---

## Database Schema (Normalized)

```sql
-- Core job management
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL,  -- 'signup', 'session', 'upi', 'link'
    email TEXT NOT NULL,
    status TEXT NOT NULL,    -- 'pending', 'running', 'success', 'failed'
    progress INTEGER DEFAULT 0,  -- percentage
    error_message TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE job_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    level TEXT,  -- 'info', 'warning', 'error'
    message TEXT NOT NULL,
    seq INTEGER,  -- for ordering
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Combo pool (Outlook)
CREATE TABLE outlook_combos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    client_id TEXT NOT NULL,
    status TEXT DEFAULT 'available',  -- 'available', 'running', 'failed'
    used_count INTEGER DEFAULT 0,
    last_used_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Session storage
CREATE TABLE session_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    session_json TEXT,  -- full SignupResult JSON
    mfa_pending BOOLEAN DEFAULT FALSE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Runtime settings (single source of truth)
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT,  -- JSON-encoded
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- iCloud HME management
CREATE TABLE icloud_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    apple_id TEXT UNIQUE NOT NULL,
    email TEXT,
    password TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE icloud_emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    apple_id TEXT REFERENCES icloud_accounts(apple_id),
    status TEXT DEFAULT 'created',  -- 'created', 'active', 'inactive', 'used'
    used_for_email TEXT,  -- ChatGPT email
    used_at DATETIME,
    label TEXT,
    note TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- AutoReg accounts
CREATE TABLE chatgpt_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    user_id TEXT UNIQUE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Proxy pool metadata
CREATE TABLE pool_state (
    proxy_url TEXT PRIMARY KEY,
    status TEXT DEFAULT 'active',  -- 'active', 'dead'
    failure_count INTEGER DEFAULT 0,
    last_used_at DATETIME
);
```

**Indexes:**
```sql
CREATE INDEX IF NOT EXISTS jobs_email ON jobs(email);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS job_logs_job_id ON job_logs(job_id);
CREATE INDEX IF NOT EXISTS icloud_emails_status ON icloud_emails(status);
CREATE INDEX IF NOT EXISTS session_results_email ON session_results(email);
```

---

## Authentication & Authorization

### Token Flow

```
┌──────────────────┐
│  User requests   │
│  http://127.0.0.1:8083/
└──────┬───────────┘
       │ (loopback)
       ▼
┌────────────────────────────────┐
│  FastAPI serve static index.html
│  ├─ Inject meta tag
│  │  <meta name="api-token" 
│  │    content="{token}">
│  └─ Frontend reads meta tag
│      → localStorage
│      → X-API-Token header
└────────────────────────────────┘

Alternate:
┌──────────────────────────────┐
│  User: http://192.168.1.100:8083/?token=...
│  (non-loopback, manual token)
└──────┬───────────────────────┘
       │
       ▼
┌──────────────────────────────┐
│  Frontend extracts from URL
│  → header, query, localStorage
└──────────────────────────────┘
```

### Verification (Middleware)

```python
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        token = verify_token(request)
        if not token:
            return JSONResponse({"error": "Unauthorized"}, 401)
    return await call_next(request)

def verify_token(request: Request) -> Optional[str]:
    # 1. Check header X-API-Token
    # 2. Check query ?token=...
    # 3. Check cookie gsh_token
    # 4. If loopback, auto-OK
    # 5. Return token or None
```

---

## Error Handling Strategy

### HTTP Error Codes

| Code | Meaning | Example |
|------|---------|---------|
| 200 | Success | POST /api/jobs/register → {id, status} |
| 400 | Bad Request | Invalid email, missing password |
| 401 | Unauthorized | Missing/invalid token |
| 404 | Not Found | GET /api/jobs/999 (doesn't exist) |
| 409 | Conflict | HmeRunner already running, job duplicate |
| 5xx | Internal Error | Unhandled exception in handler |

### Custom Exceptions

```python
class SentinelFailedError(Exception):
    """PoW solver timeout or max retries exceeded."""
    
class OtpTimeoutError(Exception):
    """OTP not received after polling window."""
    
class PaymentLinkError(Exception):
    """Payment URL extraction failed."""
    
class BrowserPhaseError(Exception):
    """Camoufox automation failed."""
    
class HTTPPhaseError(Exception):
    """Token extraction via curl_cffi failed."""
```

### Retry Strategy

| Scenario | Retries | Backoff | Notes |
|----------|---------|---------|-------|
| Transient DB lock | 3 | exponential | WAL handles most cases |
| Mail API timeout | 2 | linear 30s | Quick fail over (OTP polling loop) |
| Sentinel PoW | 2 | none | Fallback to Python solver |
| HTTP 5xx (external) | 2 | exponential | Proxy rotation in UPI confirm |
| Browser element not found | 3 | linear 2s | Re-fetch page state |

---

## Concurrency & Resource Management

### Bounded Worker Pool

```
┌─────────────────────────────────────┐
│  JobManager with Semaphore(3)       │
├─────────────────────────────────────┤
│ [Worker 1]  [Worker 2]  [Worker 3]  │
│  Running     Running     Running     │
│                                     │
│ [Queue]                             │
│  • Job 4 (pending)                  │
│  • Job 5 (pending)                  │
│  • Job 6 (pending)                  │
│  • ... (bounded by max_concurrent)  │
└─────────────────────────────────────┘

When worker finishes → dequeue next
Semaphore ensures max 3 run simultaneously
```

### Memory Limits

- **In-memory job queue:** bounded by `HYBRID_MAX_CONCURRENT` (1–10)
- **SQLite connection pool:** max 5 connections (configurable)
- **SSE broadcast:** O(n) fan-out (n = clients), capped by server resources
- **Log buffer:** FIFO capped at 10K entries (oldest dropped)

### Cleanup

- **Completed jobs:** auto-delete after 24h (configurable)
- **Failed jobs:** kept for 7d (retry/audit)
- **Old log entries:** pruned on job cleanup
- **Temporary files:** QR PNGs cleaned up after 48h

---

## Failure Recovery

### Job Recovery (on web restart)

```
1. Web startup
2. DB migration (if needed)
3. Query: SELECT * FROM jobs WHERE status IN ('pending', 'running')
4. For each: re-enqueue(job_data)
5. Manager resumes processing
```

### SQLite Recovery

```
WAL mode ensures:
• Writes don't block reads
• Crash recovery via write-ahead log
• Automatic recovery on next open
```

### Outlook Token Rotation

```
OLD TOKEN → CALL GRAPH API
            ↓ (response contains new token)
         PERSIST NEW TOKEN
            ↓ (before returning)
         USE NEW TOKEN FOR NEXT CALL
```

**If crash between fetch and persist:** token lost, requires re-auth.

---

## Performance Characteristics

### Throughput

- **Signup:** 2–10 accounts/min (limited by proxy availability, OTP latency)
- **Session:** 5–20 accounts/min (HTTP only, faster)
- **UPI QR:** 1–3 QRs/min (Stripe confirm loop, retry overhead)

### Latency

| Operation | Typical | P95 | P99 |
|-----------|---------|-----|-----|
| Signup | 120–240s | 300s | 360s |
| Session | 10–30s | 45s | 60s |
| UPI QR | 30–60s | 90s | 120s |
| OTP polling | 30–120s | 180s | 180s |

### Resource Usage

| Metric | Typical | Peak |
|--------|---------|------|
| Memory | 100–200MB | 500MB (10 jobs × browser profiles) |
| CPU | 10–20% | 60% (browser automation) |
| Disk (DB) | <100MB | <500MB (500K+ jobs) |
| Network | 1–5 Mbps | 20Mbps (proxy failures, retries) |

---

## Deployment Model

### Local (Recommended)

- Single machine, loopback binding
- SQLite on local disk
- Camoufox Firefox download (auto)
- No external infrastructure

### LAN Exposing

```bash
python -m gpt_signup_hybrid web --host 0.0.0.0 --unsafe-expose-network
# Token printed to stdout
# Share via: http://192.168.1.100:8083/?token=<generated>
```

### NOT Suitable For

- Public internet (no HTTPS, no rate limiting)
- Multi-tenant (shared token, no user isolation)
- Cloud (large file downloads, ephemeral storage)

---

## Key Invariants

1. **Single source of truth:** Settings in SQLite only, never env var at runtime
2. **Persist before mutate:** Token/combo state saved before API call
3. **Bounded resources:** Semaphore on workers, max queue size, log buffer capped
4. **Idempotent migrations:** Can re-run without side effects
5. **Token auth required:** All `/api/*` endpoints protected
6. **Async/await:** No blocking I/O in async functions
7. **Graceful shutdown:** SIGINT → stop jobs, flush logs, close DB
8. **Deterministic recovery:** Restart always produces same job states
