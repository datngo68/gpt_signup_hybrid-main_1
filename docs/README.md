# gpt_signup_hybrid Documentation

Comprehensive documentation suite for ChatGPT account automation tool (v2.0.0).

---

## Quick Navigation

### 👤 For Product Managers & Stakeholders
Start here: **[Project Overview & PDR](./project-overview-pdr.md)**
- Product features & requirements
- Success metrics & rollout plan
- Known constraints
- Open questions

### 👨‍💻 For Developers (Getting Started)
1. **[Codebase Summary](./codebase-summary.md)** — Module map, quick reference, navigation guide
2. **[Code Standards](./code-standards.md)** — Python/JS conventions, patterns, anti-patterns
3. **[System Architecture](./system-architecture.md)** — Technical design, data flows, diagrams

### 🚀 For Operations & Deployment
**[Deployment Guide](./deployment-guide.md)**
- Local setup (4 steps)
- Configuration & runtime settings
- CLI commands
- Troubleshooting
- Security checklist
- Performance tuning

### 📋 For Planning & Roadmap
**[Project Roadmap](./project-roadmap.md)**
- Phase overview (6 phases, v1.0.0 → v3.0.0)
- Feature status table (24 features)
- Current limitations & known issues
- Success metrics
- Risk register & decision log

### 💳 For Payment Integration
**[UPI Payment Runbook](./pay_upi_runbook.md)** (specialized)
**[UPI QR API Reference](./upi_qr_api.md)** (API endpoints)

---

## Documentation Index

| Document | Purpose | Audience | LOC |
|----------|---------|----------|-----|
| **[project-overview-pdr.md](./project-overview-pdr.md)** | Product requirements + PDR | Product, stakeholders | 325 |
| **[codebase-summary.md](./codebase-summary.md)** | Module map + metrics | Developers | 458 |
| **[code-standards.md](./code-standards.md)** | Development guidelines | Developers | 714 |
| **[system-architecture.md](./system-architecture.md)** | Technical design | Architects, senior devs | 791 |
| **[project-roadmap.md](./project-roadmap.md)** | Timeline + decisions | Team, planning | 387 |
| **[deployment-guide.md](./deployment-guide.md)** | Setup + operations | DevOps, operators | 688 |
| **[pay_upi_runbook.md](./pay_upi_runbook.md)** | UPI payment ops | Payment team | 385 |
| **[upi_qr_api.md](./upi_qr_api.md)** | UPI QR API reference | Integration engineers | 190 |

**Total:** 3,938 LOC of documentation

---

## Common Tasks

### I want to...

**Understand what this project does**
→ Read [Project Overview & PDR](./project-overview-pdr.md) (5 min)

**Start developing a feature**
→ [Codebase Summary](./codebase-summary.md) → [Code Standards](./code-standards.md) (15 min)

**Debug a failing job**
→ [System Architecture](./system-architecture.md) (data flows) → [Deployment Guide](./deployment-guide.md) (troubleshooting) (20 min)

**Deploy locally**
→ [Deployment Guide](./deployment-guide.md) section "Local Setup" (10 min)

**Expose to LAN**
→ [Deployment Guide](./deployment-guide.md) section "LAN Exposure (Non-Loopback)" (5 min)

**Add a new mail provider**
→ [Code Standards](./code-standards.md) (patterns) → grep `MailProvider` in code (30 min)

**Optimize performance**
→ [Deployment Guide](./deployment-guide.md) section "Performance Tuning" (15 min)

**Plan next sprint**
→ [Project Roadmap](./project-roadmap.md) section "Proposed Milestones" (20 min)

**Report a bug**
→ [Deployment Guide](./deployment-guide.md) section "Troubleshooting" (10 min)

---

## Architecture Overview

```
Frontend (Vanilla JS)
    ↓ REST API (Bearer token)
    ↓ SSE (real-time events)
    
FastAPI Web Server
    ├─ 30+ endpoints
    ├─ Job managers (Reg/Session/UPI/Link)
    └─ Worker pools (bounded concurrency)
    
Domain Layer
    ├─ signup.py (orchestrator)
    ├─ browser_phase.py (Camoufox)
    ├─ mail_providers.py (5 OTP backends)
    ├─ payment_link.py (payment URLs)
    ├─ upi_runner.py (QR probes)
    └─ icloud_hme/runner.py (HME management)
    
Persistence Layer
    └─ SQLite WAL (runtime/data.db)
        ├─ jobs + job_logs
        ├─ combos + session_results
        ├─ settings (single source of truth)
        └─ icloud_emails + chatgpt_accounts
```

See [System Architecture](./system-architecture.md) for detailed diagrams.

---

## Key Features

| Feature | Status | Docs |
|---------|--------|------|
| Multi-account signup (hybrid: browser + HTTP) | ✅ | PDR, codebase |
| 2FA enrollment (TOTP) | ✅ | codebase, deployment |
| Session extraction | ✅ | PDR, deployment |
| UPI QR payment | ✅ | UPI runbook, UPI API |
| iCloud HME pool | ✅ | architecture, deployment |
| Auto-registration | ✅ | architecture, codebase |
| Web UI (6 tabs) | ✅ | deployment, architecture |
| Proxy pool | ✅ | deployment, code standards |
| Job recovery | ✅ | architecture, deployment |
| Real-time updates (SSE) | ✅ | architecture, code standards |

---

## Technology Stack

| Layer | Technology | Details |
|-------|-----------|---------|
| **Language** | Python 3.11+ | Modern typing, async/await |
| **Web Framework** | FastAPI 0.136 | REST API, SSE, OpenAPI |
| **Browser** | Camoufox 0.4.11 | Firefox anti-detect |
| **HTTP Client** | curl_cffi 0.15 | TLS fingerprint spoof |
| **Database** | SQLite WAL | Single-file, auto-recovery |
| **Frontend** | Vanilla JS (ES6) | No framework, no bundler |
| **Validation** | Pydantic 2.13+ | Type-safe request/response |
| **CLI** | Typer + Rich | Command-line interface |

See [Code Standards](./code-standards.md) for detailed dependency list.

---

## Security Model

- **Authentication:** Bearer token on all `/api/*` endpoints
- **Loopback (127.0.0.1):** Token auto-injected via meta tag
- **LAN (non-loopback):** Token required in header/query/cookie
- **Credentials:** Persist-before-mutate, redacted in logs
- **Browser:** Headed mode only (higher anti-detect than headless)

See [System Architecture](./system-architecture.md) section "Authentication & Authorization" for details.

---

## Deployment Options

| Option | Scope | Suitable For | Documentation |
|--------|-------|-------------|---------|
| **Local (127.0.0.1)** | Single machine | Development, testing | [Deployment Guide](./deployment-guide.md) |
| **LAN (192.168.x.x)** | Trusted network | Team testing | [Deployment Guide](./deployment-guide.md) → LAN Exposure |
| **Public (not recommended)** | Internet | ❌ Not supported (no HTTPS, no rate limiting) | — |
| **Horizontal scaling** | Multiple instances | v3.0.0+ (PostgreSQL required) | [Roadmap](./project-roadmap.md) → Scaling |

---

## Development Workflow

### Before You Start
1. Read [Codebase Summary](./codebase-summary.md) → quick navigation
2. Read [Code Standards](./code-standards.md) → conventions + patterns
3. Read relevant section of [System Architecture](./system-architecture.md)

### When Implementing
- Follow [Code Standards](./code-standards.md) checklist
- Check [Codebase Summary](./codebase-summary.md) for file ownership
- Use [System Architecture](./system-architecture.md) data flows to trace impact

### Before Committing
- Code review checklist: [Code Standards](./code-standards.md) → "Code Review Checklist"
- Conventional commit format: [Code Standards](./code-standards.md) → "Git Commit Messages"
- Update relevant docs if user-facing changes

### For Help
- Debug troubleshooting: [Deployment Guide](./deployment-guide.md) → "Troubleshooting"
- Understanding a module: [Codebase Summary](./codebase-summary.md) → "Quick Navigation"
- Architecture questions: [System Architecture](./system-architecture.md)

---

## Roadmap

**Current Version:** v2.0.0 (June 2026)

| Phase | Version | Status | Docs |
|-------|---------|--------|------|
| **Foundation** | v1.0.0 | ✅ Complete | PDR (legacy) |
| **Persistence & Web UI** | v2.0.0 | ✅ Live | All docs |
| **Payment Integration** | v2.2.0 | ✅ Live | UPI runbook |
| **iCloud HME & AutoReg** | v2.3.0 | ✅ Live | Architecture, codebase |
| **Polish & Hardening** | v2.4.0 | 🔄 In Progress | Roadmap |
| **Advanced Features** | v2.5.0 | 📋 Planned | Roadmap |
| **Scaling (PostgreSQL)** | v3.0.0 | 📋 Planned | Roadmap |

See [Project Roadmap](./project-roadmap.md) for detailed timeline + milestones.

---

## Questions?

### Not Found in Documentation?

1. **Check the [Codebase Summary](./codebase-summary.md)** — "Quick Navigation" section lists common tasks
2. **Search this docs folder:** `grep -r "your_topic" docs/`
3. **Check code docstrings:** `grep -r "def your_function" gpt_signup_hybrid/`
4. **Ask on team Slack** (reference the relevant doc in your question)

### Found an Error?

Please report with:
- Which doc + section
- What's inaccurate
- Correct information or source file

---

## Contribution Guidelines

When updating documentation:

1. **Keep files ≤800 LOC** — split if growing larger
2. **Use self-documenting file names** — kebab-case, descriptive
3. **Cross-reference, don't duplicate** — link to existing docs
4. **Verify against source code** — grep/read actual files
5. **Update this README.md** if adding new docs
6. **Test links** — all `[text](./file.md)` must exist
7. **Vietnamese + English** — prose in Vietnamese, technical terms in English

---

## Quick Links

- **Repository:** `/Users/huunguyen/Documents/Code-Projects/gpt_signup_hybrid`
- **Main README:** `README.md` (project overview in Vietnamese)
- **Architecture Guide:** `CLAUDE.md` (internal AI guide)
- **Requirements:** `requirements.txt` (Python dependencies)
- **Configuration:** `.env.example` (runtime settings)

---

**Last Updated:** 2026-06-17 | **Version:** 2.0.0 | **Total Docs:** 3,938 LOC
