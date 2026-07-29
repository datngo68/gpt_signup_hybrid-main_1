# 🇬🇧 gpt_signup_hybrid — English

> Automated ChatGPT signup pipeline + UPI payment bot. FastAPI + Camoufox + Rust.

[← Back to language index](./README.md)

---

## 💖 Donate / Support

| Method | Address |
|---|---|
| 🟡 **Binance ID** | `356552242` |
| 🟢 **USDT (BEP20)** | `0x137a3bfa30ee426127367773dfce16aefce04e02` |
| 🔴 **USDT (TRC20)** | `TFy5d1EDT4WBKgtoypx7Ua2dCZhPHMSDNs` |
| ✈️ **Telegram** | [@prr9293](https://t.me/prr9293) |
| 👥 **Telegram Group** | [t.me/+C6eafntO-Eo1Njdl](https://t.me/+C6eafntO-Eo1Njdl) |

---

## ⚠️ READ FIRST — IMPORTANT NOTICE

> 🎯 **To get ChatGPT Plus discounted pricing (PPP 40-60% off), your login proxy MUST exit from Vietnam (VN) or Japan (JP).**
>
> **If you DO NOT have a VN/JP proxy for login**, you can still use the tool — just use a VPN instead:
>
> - ✅ **Install a VPN with a VN or JP server** on the device running this tool (laptop / VPS / server)
> - ✅ Once the VPN is active → leave the login proxy field **empty** in UI/CLI; the tool will route via your VPN's exit IP
> - ✅ Recommended VPN options:
>   - 🛠️ Self-hosted **WireGuard / OpenVPN** on a JP/VN VPS (cheapest and most reliable)
>   - 💼 Commercial VPN with JP/VN nodes — Mullvad, ProtonVPN, NordVPN, Surfshark, ExpressVPN
>   - 🏠 Residential proxies (Bright Data, Soax, NetNut) for production scale
>
> ⛔ Running from a US/EU IP **without** a JP/VN VPN → Plus price is 2-3× higher, or UPI/GoPay geo-blocked.
>
> 💡 **Beginner recommendation**:
> 1. Rent a Japan VPS (Vultr Tokyo ~$5/month) → run the tool directly there, NO proxy/VPN needed
> 2. Or use ProtonVPN free tier (includes a free Japan node) on your personal laptop for testing

---

## 📖 Introduction

`gpt_signup_hybrid` is an automated ChatGPT signup pipeline with a local web UI, paired with a Rust-based UPI payment bot.

### Core features

- 🎯 **Hybrid registration** — Camoufox + curl_cffi to bypass detection
- 📧 **Mail providers** — iCloud HME v3, Outlook pool, Gmail, custom Worker API
- 💳 **Payment automation** — ChatGPT Plus checkout, Stripe, GoPay/Midtrans, UPI
- 🔐 **MFA/TOTP** auto-enable after signup
- 🍎 **iCloud Hide My Email pool** — auto-generate + rotate profiles
- 🔄 **AutoReg loop** — HME → account → MFA pipeline
- 🌐 **Local Web UI** (FastAPI) with realtime SSE
- 🦀 **Rust UPI bot** — Telegram bot for UPI QR generation

### 3 registration modes

| Mode | Description |
|---|---|
| `pure_request` | HTTP-only, fastest, curl_cffi only |
| `browser` | Full Camoufox browser flow |
| `hybrid` | **Recommended** — Camoufox auth relay + Python field reproduction |

---

## 💰 UPI Feature in detail

### What is UPI?

**UPI (Unified Payments Interface)** is India's real-time payment system, operated by NPCI. It enables instant bank-to-bank transfers via mobile apps (PhonePe, GPay, Paytm, BHIM).

**Why UPI matters for ChatGPT?**

- 🇮🇳 ChatGPT Plus in India accepts **UPI as primary payment method** (alongside Visa/Mastercard)
- 💸 ChatGPT Plus is **40-60% cheaper in India** vs US/EU due to PPP pricing
- 🎯 VPA format: `name@oksbi`, `9876543210@ybl`, `user@paytm`

### Two UPI systems in this project

#### 1️⃣ Python `pay_upi_http.py` — pure-HTTP UPI flow

**Purpose**: Automatically generate UPI checkout for one ChatGPT account via pure HTTP (no browser).

**Pipeline**:
1. Login ChatGPT via combo `email|pass|secret` or `session.json`
2. POST `/backend-api/payments/checkout` to create checkout session
3. POST `api.stripe.com/v1/payment_pages/{id}/init` to initialize Stripe
4. GET `api.stripe.com/v1/elements/sessions` for elements config
5. POST `/confirm` to submit UPI VPA
6. POST `/approve` polling until user completes payment

**Characteristics**:
- ✅ No browser → lightweight, fast
- ✅ curl_cffi impersonates Chrome 145 Windows TLS
- ✅ Proxy split: login DIRECT (lower captcha), step 2+ via India proxy
- ⚠️ Stripe `/confirm` requires 3 JS-runtime tokens (`js_checksum`, `rv_timestamp`, `passive_captcha_token`) — best-effort submit
- 🎯 Best for: manual testing, single/few account automation

**Usage**:

```bash
# Combo mode
python -m gpt_signup_hybrid.pay_upi_http \
  --combo 'email@example.com|password|totp_secret' \
  --vpa 'name@oksbi' \
  --proxy 'http://user:pass@india-proxy:port'

# Session JSON mode
python -m gpt_signup_hybrid.pay_upi_http \
  --session ./session.json \
  --vpa '9876543210@ybl'
```

#### 2️⃣ Rust `rust_upi_bot/` — Telegram UPI QR bot

**Purpose**: Service-as-a-bot — user sends `session.json` via Telegram, bot runs UPI flow and returns QR PNG for payment.

**Pipeline**:
1. User `/start` → bot greeting (multi-language)
2. User uploads `session.json` (Telegram document)
3. Bot parses `access_token` + cookies
4. Job enters FIFO queue
5. Worker pool (default 100 concurrent) picks up job
6. Runs UPI flow (steps 2-6 like Python)
7. Renders QR PNG with `@prr9293` watermark
8. Sends QR to user via Telegram + realtime progress log

**Advanced features**:

| Feature | Description |
|---|---|
| **FIFO Queue** | Hard cap (default 50 pending) to prevent OOM |
| **Per-user limit** | Default 2 jobs/user, admin override via `/set_user_limit @user n` |
| **Cooldown** | 10s between jobs per user, anti-spam |
| **Proxy pool** | Rotate proxies from step 3 (login DIRECT) |
| **Restart on failures** | Restart checkout after 20 consecutive exceptions |
| **Job timeout** | 1800s hard timeout |
| **Watermark** | QR PNG stamped with `@prr9293` |
| **Admin notification** | Notify admin when other users generate QR successfully |
| **Multi-language** | i18n: EN/VI/CN/ID/HI |
| **Settings persistence** | SQLite Settings Store, no restart needed for limit changes |

**Usage**:

```bash
cd rust_upi_bot
cargo build --release

TELEGRAM_TOKEN=xxxxxxxx \
ALLOWED_USERS=123456789,987654321 \
MAX_CONCURRENT=50 \
MAX_PER_USER=2 \
PROXY_POOL='http://u:p@h1:8080,http://u:p@h2:8080' \
./target/release/upi-qr-bot
```

**Telegram commands**:

```
/start                          — Bot greeting
/help                           — Help
/status                         — Queue + worker status
/lang <vi|en|zh|id|hi>          — Switch language
/set_max_per_user <n>           — (Admin) change per-user limit
/set_user_limit @user <n>       — (Admin) override limit for one user
/set_max_concurrent <n>         — (Admin) change total concurrency
```

**Deployment targets**:

- 🐧 OpenWrt aarch64 (router) — small binary, low RAM
- 🐳 Docker (build from source)
- ☁️ Linux x86_64 VPS — best to colocate with main app

---

## 🐳 Docker Setup (Main app)

### Requirements

- **Docker Desktop** (Windows/macOS) or **Docker Engine + Compose** (Linux)
- Minimum **4GB RAM** (8GB if concurrency ≥ 3)
- Stable internet
- **VPS in JP or VN** for ChatGPT Plus discount (see VPN section)

### Step 1 — Install Docker

```bash
# Linux Ubuntu/Debian
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker
docker --version
```

Windows/macOS: download Docker Desktop at https://www.docker.com/products/docker-desktop/

### Step 2 — Clone

```bash
git clone https://github.com/6c696e68/gpt_signup_hybrid.git
cd gpt_signup_hybrid
```

### Step 3 — Create `.env`

```bash
cp .env.docker.example .env
sed -i.bak "s/change-me-strong-random/$(openssl rand -hex 32)/" .env
```

### Step 4 — Build & run

```bash
docker compose build           # 5-10 min first time
docker compose up -d
docker compose logs -f web
```

> 🟢 Apple Silicon → amd64 VPS: `docker buildx build --platform linux/amd64 -t gsh:latest --load .`

### Step 5 — Access UI

```bash
TOKEN=$(grep GPT_SIGNUP_WEB_TOKEN .env | cut -d= -f2)
echo "http://127.0.0.1:8083/?token=$TOKEN"
```

UI tabs: **Register · Session · Link · HME · AutoReg · Settings**

### Step 6 — Enable iCloud HME runner (optional)

```bash
docker compose --profile hme up -d
```

### Useful commands

```bash
docker compose ps                                     # status
docker compose logs -f web                            # tail logs
docker compose restart web                            # restart
docker compose down                                   # stop (keep volume)
docker compose down -v                                # stop + delete data
docker compose pull && docker compose up -d --build   # update
```

### Backup / Restore

```bash
# Backup
docker run --rm -v gpt_signup_hybrid_gsh-runtime:/data -v $(pwd):/backup \
  alpine tar czf /backup/runtime-backup-$(date +%Y%m%d).tar.gz -C /data .

# Restore
docker run --rm -v gpt_signup_hybrid_gsh-runtime:/data -v $(pwd):/backup \
  alpine tar xzf /backup/runtime-backup-YYYYMMDD.tar.gz -C /data
```

---

## 🌏 VPN / VPS for ChatGPT Plus discount

ChatGPT Plus uses **PPP pricing** — prices vary by country.

| Region | Notes | Recommendation |
|---|---|---|
| 🇯🇵 **Japan (JP)** | Stable, less cross-check | **Best for production** |
| 🇻🇳 **Vietnam (VN)** | Cheapest in region | **Best for testing** |
| 🇮🇳 India | Cheap but UPI-only + datacenter ban | Needs residential proxy |
| 🇮🇩 Indonesia | GoPay/Midtrans built-in | Built-in support |

### VPS providers

- **Vultr**: Tokyo/Osaka
- **Linode/Akamai**: Tokyo
- **DigitalOcean**: Singapore (close to JP)
- **Vietnam VPS**: Viettel IDC, FPT Cloud, BizflyCloud

Minimum spec: **2 vCPU / 4GB RAM / 40GB SSD / Ubuntu 22.04+**

### VPN gateway

- **WireGuard** server in JP/VN
- **OpenVPN** with JP/VN gateway
- **Residential proxy JP/VN** (Bright Data, Soax, NetNut)

Configure proxy: UI **Settings → Proxies** or set `HYBRID_OUTLOOK_PROXY=http://...` in `.env`.

---

## 🛠️ Manual install (no Docker)

```bash
git clone https://github.com/6c696e68/gpt_signup_hybrid.git
cd gpt_signup_hybrid
bash setup.sh        # Linux/macOS
# setup.bat          # Windows
```

---

## 🔧 Troubleshooting

| Error | Fix |
|---|---|
| `GPT_SIGNUP_WEB_TOKEN required` | Re-do Step 3 |
| Container `unhealthy` | `docker compose logs web`, check RAM |
| Blank Web UI | Add `?token=...` to URL |
| Job stuck | `docker compose restart web`, check proxy pool |
| Captcha/Turnstile fail | Switch to **residential proxy** |
| UPI confirm fail | Stripe requires JS-runtime tokens, use Rust bot with browser stage |
| Apple Silicon slow build | Build directly on amd64 VPS |

---

## 📁 Project structure

```
gpt_signup_hybrid/
├── cli.py, signup.py, browser_phase.py, request_phase.py    # Core
├── session_phase.py, mfa_phase.py                           # Session + MFA
├── payment_link.py, pay_upi_http.py                         # Payment (Python UPI)
├── db/                                                      # SQLite + Settings Store
├── web/                                                     # FastAPI + UI
├── icloud_hme/                                              # iCloud HME pool
├── autoreg/                                                 # AutoReg loop
├── rust_upi_bot/                                            # Rust UPI Telegram bot
└── test/, docs/                                             # Tests + docs
```

See also: [`AGENTS.md`](./AGENTS.md), [`.planning/codebase/ARCHITECTURE.md`](./.planning/codebase/ARCHITECTURE.md)
