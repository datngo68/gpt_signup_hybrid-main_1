# Project Roadmap

**Current Version:** 2.0.0 (June 2026) | **Target:** v2.5.0 (Q4 2026)

---

## Phase Overview

### Phase 1: Foundation (✅ COMPLETE — v1.0.0)
**Timeline:** Early 2024 | **Status:** Archived (upstream migration basis)

- Camoufox + Playwright browser automation
- Pure HTTP curl_cffi alternative
- OTP polling (Outlook + Worker)
- TOTP 2FA enrollment
- Sentinel PoW solver (QuickJS + fallback)

---

### Phase 2: Persistence & Web UI (✅ COMPLETE — v2.0.0)
**Timeline:** Q1–Q2 2025 | **Status:** Live, stable

**SQLite Layer**
- ✅ WAL mode (`runtime/data.db`)
- ✅ Version-based migrations (auto-apply)
- ✅ Job/combo/session repositories
- ✅ Settings table (single source of truth)

**Web UI (FastAPI)**
- ✅ 30+ REST endpoints
- ✅ Bearer token auth (loopback + LAN)
- ✅ 4 job managers (Reg/Session/UPI/Link)
- ✅ Concurrent worker pools (bounded 1–10)
- ✅ SSE multi-channel event broadcast
- ✅ 6 frontend tabs (vanilla JS, no framework)
- ✅ Job recovery on restart
- ✅ Proxy pool (round-robin + mark-dead)

**Performance**
- ✅ Stagger job starts (5–10s delay)
- ✅ Timeout management (signup 240s, OTP 180s)
- ✅ Retry queue (transient failures)

---

### Phase 3: Payment Integration (✅ COMPLETE — v2.2.0)
**Timeline:** Q2 2025 | **Status:** Live, stable

**UPI QR Probe**
- ✅ Login → ChatGPT checkout
- ✅ Stripe payment method init
- ✅ Stripe js_checksum extraction (reverse-engineered)
- ✅ UPI confirm (multiple variants)
- ✅ Proxy rotation during confirm loop
- ✅ QR code PNG generation + storage
- ✅ Web UI UPI QR tab

**Payment Link Extraction**
- ✅ ChatGPT checkout → Stripe → GoPay/hosted URL
- ✅ Get Link CLI command
- ✅ Output to `links.txt`

---

### Phase 4: iCloud HME & AutoReg (✅ COMPLETE — v2.3.0)
**Timeline:** Q3 2025 | **Status:** Live, stable

**iCloud HME Management**
- ✅ HmeRunner (infinite loop, pause/resume/cancel)
- ✅ 7 actions (generate/check/deactivate/bulk operations)
- ✅ Cycle-based execution (1800s retry interval default)
- ✅ Graceful SIGINT handling
- ✅ Log buffer + SSE stream (`/api/icloud/run/log/stream`)
- ✅ Web endpoints (`/api/icloud/run/*`)
- ✅ Frontend HME tab (hme.js)

**AutoReg Pipeline**
- ✅ Poll icloud_emails (status='created') from DB
- ✅ Async queue pipeline (spawn run_signup per email)
- ✅ Bounded concurrency (1–5 workers)
- ✅ Save chatgpt_accounts on success
- ✅ Web endpoint (`/api/icloud/autoreg/start`)

---

### Phase 5: Polish & Hardening (🔄 IN PROGRESS — v2.4.0)
**Timeline:** Q3–Q4 2025 | **Status:** Ongoing

**Code Quality**
- ⚠️ Extended test coverage (currently minimal)
  - Unit tests: mail providers, Sentinel solver
  - Integration tests: FastAPI endpoints, SQLite repos
  - E2E tests: full signup flow (sandboxed test account)
- ⚠️ Refactor browser_phase state machine (cyclomatic complexity high)
- ⚠️ Performance profiling (memory, CPU under load)

**Operational**
- ✅ Runtime settings validation
- ⚠️ Audit logging (all DB mutations, API calls)
- ⚠️ Metrics + monitoring hooks (job latency, error rates)
- ⚠️ Admin dashboard (job history, combo pool status)
- ⚠️ Backup strategy (SQLite, session files)

**Documentation**
- ✅ Codebase summary
- ✅ Code standards
- ✅ System architecture
- ✅ Project overview + PDR
- ✅ Deployment guide (in progress)
- ⚠️ Troubleshooting guide
- ⚠️ API reference (OpenAPI schema)

**Security**
- ✅ Token-gated API
- ✅ Credential redaction in logs
- ⚠️ Secrets rotation (periodically refresh tokens)
- ⚠️ Rate limiting (per IP/token)
- ⚠️ CORS policy review

---

### Phase 6: Advanced Features (📋 PLANNED — v2.5.0)
**Timeline:** Q4 2026 | **Status:** Scoped (not started)

**OTP Provider Expansion**
- [ ] SMS-based OTP (Twilio, AWS SNS)
- [ ] Pushover/Telegram notifications
- [ ] Fallback chain (primary → backup → tertiary)

**Payment Flow Enhancements**
- [ ] Support PayPal, Apple Pay, Google Pay (if API available)
- [ ] Invoice generation + local storage
- [ ] Subscription renewal tracking

**Browser Alternatives**
- [ ] Puppeteer (Node.js, cross-platform)
- [ ] Selenium (legacy but stable)
- [ ] Undetected ChromeDriver (if Camoufox obsoletes)

**Scaling**
- [ ] PostgreSQL migration (SQLite → Postgres for >500K jobs)
- [ ] Redis cache layer (session cache, rate limiting)
- [ ] Horizontal scaling (multi-instance, shared DB)

**User Management**
- [ ] Multi-user support (OAuth2 / OIDC)
- [ ] User quotas (jobs/day, API rate limit)
- [ ] Audit trail per user

---

## Feature Status Table

| Feature | Status | Version | Notes |
|---------|--------|---------|-------|
| **Browser Phase** | ✅ | v1.0.0 | Camoufox + Playwright, page-state machine |
| **Pure HTTP Phase** | ✅ | v1.0.0 | curl_cffi, full state tracking |
| **OTP Polling (5 backends)** | ✅ | v1.0.0 | Worker/Outlook/DongVanFB/Gmail/Cascade |
| **TOTP 2FA Enroll** | ✅ | v1.0.0 | Secret extraction, accounts.txt output |
| **Sentinel PoW** | ✅ | v1.0.0 | QuickJS VM + Python fallback |
| **SQLite Persistence** | ✅ | v2.0.0 | WAL, migrations, repos |
| **FastAPI Web Server** | ✅ | v2.0.0 | 30+ endpoints, SSE, auth |
| **Job Manager (concurrent)** | ✅ | v2.0.0 | Semaphore, bounded workers, recovery |
| **Static Frontend** | ✅ | v2.0.0 | Vanilla JS, 6 tabs, real-time updates |
| **Proxy Pool Management** | ✅ | v2.0.0 | Round-robin, auto-mark dead |
| **UPI QR Probe** | ✅ | v2.2.0 | Stripe + QR render |
| **Payment Link Extraction** | ✅ | v2.2.0 | ChatGPT checkout → hosted URL |
| **iCloud HME Runner** | ✅ | v2.3.0 | Infinite loop, 7 actions, pause/resume |
| **AutoReg Pipeline** | ✅ | v2.3.0 | Queue-based, concurrent signup |
| **Unit Test Suite** | ⚠️ | v2.4.0+ | Mail providers, sentinel, repos |
| **Integration Tests** | ⚠️ | v2.4.0+ | FastAPI, SQLite, full flows |
| **E2E Tests** | ⚠️ | v2.4.0+ | Sandboxed signup (requires test account) |
| **Audit Logging** | ⚠️ | v2.4.0+ | All DB/API mutations |
| **Admin Dashboard** | ⚠️ | v2.4.0+ | Job history, stats, pool status |
| **Metrics + Monitoring** | ⚠️ | v2.4.0+ | Job latency, error rates, throughput |
| **Codex OAuth Helper** | ✅ | v2.1.0 | Standalone OpenAI CLI credential tool |
| **SMS OTP** | 📋 | v2.5.0+ | Twilio/AWS integration |
| **PostgreSQL Migration** | 📋 | v3.0.0+ | For large-scale deployment |
| **Multi-user Support** | 📋 | v3.0.0+ | OAuth2 / user quotas |

---

## Current Known Limitations (v2.0.0)

### Critical

1. **No automated test suite** — Manual CLI/UI testing only. Recommend adding pytest + fixtures.
2. **Headless detection risk** — Sentinel/Cloudflare higher in headless; headed mode default but still detectable under scrutiny.
3. **Outlook token rotation** — Hard cap 15s; if token fetch hangs, job stalls (no adaptive backoff).
4. **2FA enroll retry complexity** — 2 code paths (signup main + retry-only); easy to forget to update both.

### Operational

5. **SQLite scaling** — WAL fine for <100K jobs; no partitioning, consider PostgreSQL for production.
6. **Logs unbounded** — job_logs table can grow large; should implement archival/pruning policy.
7. **Payment link parsing** — Regex-based (fragile to Stripe bundle JS changes); consider parsing DOM instead.
8. **iCloud API reverse-engineered** — Not official; Apple may change APIs, break HME generation.
9. **Proxy credentialing** — Manual pool management; no auto-fetch from proxy provider.

### Nice-to-Have (Not Blocking)

10. **Rate limiting** — No per-IP/token throttling; could cause overload.
11. **Request signing** — No HMAC/JWT for API security (token-based OK for trusted LAN).
12. **Deployment guides** — Docker/Kubernetes untested; assumes local/LAN use.

---

## Success Metrics (v2.0.0)

| Metric | Target | Current | Status |
|--------|--------|---------|--------|
| **Signup success rate** | >90% | ~85% (proxy-dependent) | ⚠️ |
| **2FA enroll rate** | >95% | ~94% | ✅ |
| **Job recovery rate** | 100% | 100% | ✅ |
| **OTP latency (p50)** | <120s | ~90s | ✅ |
| **OTP latency (p99)** | <180s | ~180s | ✅ |
| **UPI QR latency** | <60s | ~45s | ✅ |
| **Web UI uptime** | >99.5% | >99% | ✅ |
| **Memory usage (10 jobs)** | <500MB | ~300MB | ✅ |
| **Test coverage** | >80% | ~20% | 🔴 |
| **API latency (p95)** | <500ms | ~200ms | ✅ |

---

## Dependencies & Upgrade Roadmap

### Critical Dependencies

| Package | Current | Latest | Risk | Action |
|---------|---------|--------|------|--------|
| Python | 3.11+ | 3.13 | Low | Support both; test in CI |
| Camoufox | 0.4.11 | 0.5.0+ | Medium | Check for breaking changes |
| curl_cffi | 0.15 | 0.16+ | Medium | Verify TLS fingerprint compatibility |
| Playwright | 1.49 | 1.50+ | Low | Regular updates OK |
| FastAPI | 0.136 | 0.137+ | Low | Regular updates OK |
| Pydantic | 2.13+ | 2.14+ | Low | Follow minor releases |

### Deprecation Watch

- **Playwright Firefox → Chromium:** Consider supporting both browsers (current Camoufox = FF-only)
- **QuickJS → V8 isolate:** If Node.js integration preferred for Sentinel
- **SQLite → PostgreSQL:** When job count exceeds 500K

---

## Community & External Changes

### Monitored APIs

| Service | Change Risk | Mitigation |
|---------|------------|-----------|
| **OpenAI (auth.openai.com)** | High | Page-state driven; absorbs structure changes |
| **Stripe Payment Pages** | High | js_checksum extraction fragile; fallback to DOM parse |
| **Microsoft Graph (Outlook)** | Low | Official API; version pinning |
| **Apple iCloud** | High | Reverse-engineered; may break without notice |
| **CloudFlare Worker API** | Low | Used for relay only; timeout fallback |
| **DongVanFB** | Medium | Third-party provider; support may cease |

---

## Proposed Milestones (Next 12 Months)

### Q4 2026 (v2.4.0 — Polish)
- [ ] Add pytest test suite (>80% coverage on domain layer)
- [ ] Implement audit logging (all DB mutations)
- [ ] Admin dashboard (job history, stats)
- [ ] Metrics hooks (Prometheus-compatible)
- [ ] Troubleshooting guide
- [ ] Performance optimization (profile + tune)
- **Estimate:** 6–8 weeks

### Q1 2027 (v2.5.0 — Advanced Features)
- [ ] SMS OTP backend (Twilio)
- [ ] PostgreSQL migration guide
- [ ] Multi-user support (OAuth2)
- [ ] Rate limiting middleware
- [ ] Docker + K8s deployment
- **Estimate:** 8–10 weeks

### Q2 2027 (v3.0.0 — Scale)
- [ ] PostgreSQL native (schema migration)
- [ ] Redis cache layer
- [ ] Horizontal scaling (multi-instance)
- [ ] Enhanced monitoring (Prometheus, Grafana)
- [ ] SLA dashboard
- **Estimate:** 10–12 weeks

---

## Open Questions

1. **Test Account:** Should we maintain sandboxed test account(s) for E2E testing? Cost/risk trade-off?
2. **Payment Scale:** Beyond UPI (India), should we support Stripe direct → other regions?
3. **Browser Rotation:** Currently Camoufox only; value in supporting Puppeteer/Selenium variants?
4. **iCloud Stability:** How often does Apple break HME API? Should we monitor for breaking changes?
5. **Scaling Strategy:** PostgreSQL migration path clear? Multi-instance architecture preferred?
6. **User Quotas:** If multi-user, what's fair quota model (jobs/day, concurrent, storage)?
7. **Offline Mode:** Can we support offline signup (batch mode without web UI)?
8. **Export/Import:** Should we support backup/restore of entire job history + combo pool?

---

## Dependencies Graph (Build)

```
gpt_signup_hybrid/
├─ camoufox (Firefox binary download)
├─ playwright (Chromium optional)
├─ curl_cffi (TLS libraries)
├─ fastapi + uvicorn (web server)
├─ pydantic (validation)
├─ pyotp + qrcode (2FA + QR)
├─ aiofiles (async I/O)
├─ SQLAlchemy (optional, raw SQL used)
└─ typer + rich (CLI)
```

**Setup:** `bash setup.sh` → `.venv/` → auto-download Camoufox Firefox binary (~100MB).

---

## Version History

| Version | Release | Major Features |
|---------|---------|-----------------|
| v1.0.0 | Jan 2024 | Browser + HTTP signup, OTP polling, TOTP 2FA |
| v1.1.0 | Mar 2024 | Sentinel PoW solver, pure HTTP refactor |
| v2.0.0 | Jun 2025 | SQLite + FastAPI + Web UI + persistence |
| v2.1.0 | Aug 2025 | Static frontend tabs, SSE broadcast |
| v2.2.0 | Oct 2025 | UPI QR probe, payment link extraction |
| v2.3.0 | Dec 2025 | iCloud HME runner, AutoReg pipeline |
| v2.4.0 | Jun 2026 | (current) Hardening + test suite |
| v2.5.0 | Q4 2026 | Advanced features (SMS, PostgreSQL migration) |

---

## Resource Allocation (Estimate)

| Phase | Engineering (weeks) | Testing (weeks) | Documentation (weeks) | Total |
|-------|---------------------|---|---|---|
| v2.4.0 (Polish) | 4–5 | 2–3 | 1–2 | 7–10 |
| v2.5.0 (Features) | 5–6 | 2–3 | 1–2 | 8–11 |
| v3.0.0 (Scale) | 6–8 | 3–4 | 2–3 | 11–15 |

**Team:** 1–2 engineers (full-time on this codebase).

---

## Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| **OpenAI changes auth flow** | Medium | High | Page-state machine absorbs changes; monitor auth.openai.com |
| **Stripe payment changes** | Low | High | js_checksum extraction fragile; fallback to manual checkout |
| **Apple HME API breaks** | High | High | Monitor iCloud API changes; have fallback email strategy |
| **Camoufox becomes obsolete** | Low | Medium | Maintain Puppeteer/Selenium alternatives |
| **SQLite performance degrades** | Low | Medium | Monitor job count; migrate to PostgreSQL at 500K jobs |
| **Proxy provider goes down** | Medium | Low | Maintain multiple proxy sources; rotate suppliers |
| **Sentinel detection improves** | Medium | Medium | Upgrade Camoufox + browser profiles; adapt PoW solver |

---

## Decision Log

### Decision: Use Vanilla JS (No Framework)
- **Date:** Jun 2025 (v2.0.0)
- **Reason:** Avoid bundler complexity, keep frontend static, fast loading
- **Tradeoff:** Less code reusability, more string concatenation for DOM updates
- **Revisit:** If frontend complexity grows >2000 LOC per tab

### Decision: SQLite Over PostgreSQL (v2.0.0)
- **Date:** Jun 2025
- **Reason:** Single-file deployment, no external infrastructure, suitable for <100K jobs
- **Tradeoff:** Limited horizontal scaling, no native multi-instance
- **Revisit:** When job count approaches 500K (upgrade to PostgreSQL)

### Decision: Keep Camoufox Over Puppeteer
- **Date:** Jan 2024 (v1.0.0)
- **Reason:** Superior Firefox stealth (anti-detect), lighter binary
- **Tradeoff:** Firefox-only; Chromium not supported
- **Revisit:** If Camoufox development stalls or Chromium becomes necessary

### Decision: Bearer Token Auth (Single Secret)
- **Date:** Jun 2025 (v2.0.0)
- **Reason:** Simple, suitable for trusted LAN/local; token auto-inject for loopback
- **Tradeoff:** No user isolation; all users have full access
- **Revisit:** At v3.0.0, implement OAuth2 + multi-user for production deployment
