# 🇮🇳 gpt_signup_hybrid — हिन्दी / Hindi

> Automated ChatGPT signup pipeline + UPI payment bot. FastAPI + Camoufox + Rust.

[← भाषा index पर वापस](./README.md)

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

## ⚠️ पहले पढ़ें — महत्वपूर्ण सूचना

> 🎯 **ChatGPT Plus की discounted pricing (PPP 40-60% off) पाने के लिए, आपका login proxy Vietnam (VN) या Japan (JP) से exit करना ज़रूरी है।**
>
> **अगर आपके पास login के लिए VN/JP proxy नहीं है**, तो भी आप tool use कर सकते हैं — बस VPN use करें:
>
> - ✅ Tool चलाने वाले device (laptop / VPS / server) पर **VN या JP server वाला VPN install करें**
> - ✅ VPN active होने पर → UI/CLI में login proxy field **खाली** छोड़ दें; tool VPN के exit IP से route करेगा
> - ✅ Recommended VPN options:
>   - 🛠️ JP/VN VPS पर self-host **WireGuard / OpenVPN** (सबसे सस्ता और reliable)
>   - 💼 JP/VN nodes वाले commercial VPN — Mullvad, ProtonVPN, NordVPN, Surfshark, ExpressVPN
>   - 🏠 Production scale के लिए residential proxies (Bright Data, Soax, NetNut)
>
> ⛔ US/EU IP से **बिना** JP/VN VPN के run करना → Plus price 2-3× ज़्यादा, या UPI/GoPay geo-blocked।
>
> 💡 **शुरुआती लोगों के लिए recommendation**:
> 1. Japan VPS rent करें (Vultr Tokyo ~$5/month) → directly वहीं tool चलाएँ, कोई proxy/VPN नहीं चाहिए
> 2. या testing के लिए personal laptop पर ProtonVPN free tier (free Japan node है) use करें

---

## 📖 परिचय

`gpt_signup_hybrid` एक automated ChatGPT account signup pipeline है जिसमें local web UI है, और साथ में Rust-based UPI payment bot है।

### मुख्य Features

- 🎯 **Hybrid registration** — Camoufox + curl_cffi detection bypass के लिए
- 📧 **Mail providers** — iCloud HME v3, Outlook pool, Gmail, custom Worker API
- 💳 **Payment automation** — ChatGPT Plus checkout, Stripe, GoPay/Midtrans, **UPI (India)**
- 🔐 **MFA/TOTP** signup के बाद auto-enable
- 🍎 **iCloud Hide My Email pool** — auto-generate + profile rotation
- 🔄 **AutoReg loop** — HME → account → MFA pipeline
- 🌐 **Local Web UI** (FastAPI) realtime SSE के साथ
- 🦀 **Rust UPI bot** — UPI QR generation के लिए Telegram bot

### 3 registration modes

| Mode | विवरण |
|---|---|
| `pure_request` | HTTP-only, सबसे fast, सिर्फ curl_cffi |
| `browser` | Full Camoufox browser flow |
| `hybrid` | **Recommended** — Camoufox auth relay + Python field reproduction |

---

## 💰 UPI Feature detail में

### UPI क्या है?

**UPI (Unified Payments Interface)** India का realtime payment system है, जो NPCI चलाता है। Mobile apps (PhonePe, GPay, Paytm, BHIM) के through bank-to-bank instant transfer enable करता है।

**ChatGPT के लिए UPI क्यों important?**

- 🇮🇳 ChatGPT Plus India में **UPI को primary payment method** accept करता है (Visa/Mastercard के अलावा)
- 💸 PPP pricing की वजह से ChatGPT Plus India में **40-60% सस्ता** है US/EU से
- 🎯 VPA format: `name@oksbi`, `9876543210@ybl`, `user@paytm`

### Project में दो UPI systems

#### 1️⃣ Python `pay_upi_http.py` — pure-HTTP UPI flow

**उद्देश्य**: Pure HTTP के through (browser के बिना) एक ChatGPT account के लिए automatically UPI checkout generate करना।

**Pipeline**:
1. ChatGPT में combo `email|pass|secret` या `session.json` से login
2. POST `/backend-api/payments/checkout` checkout session बनाने के लिए
3. POST `api.stripe.com/v1/payment_pages/{id}/init` Stripe initialize करने के लिए
4. GET `api.stripe.com/v1/elements/sessions` elements config लेने के लिए
5. POST `/confirm` UPI VPA submit करने के लिए
6. POST `/approve` polling जब तक user payment complete न करे

**विशेषताएँ**:
- ✅ Browser नहीं → lightweight, fast
- ✅ curl_cffi Chrome 145 Windows TLS impersonate करता है
- ✅ Proxy split: login DIRECT (कम captcha), step 2+ via India proxy
- ⚠️ Stripe `/confirm` को 3 JS-runtime tokens चाहिए — best-effort submit
- 🎯 इसके लिए best: manual testing, single/few account automation

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

**उद्देश्य**: Service-as-a-bot — user Telegram के through `session.json` भेजता है, bot UPI flow run करता है और payment के लिए QR PNG return करता है।

**Pipeline**:
1. User `/start` → bot greeting (multi-language)
2. User `session.json` upload करता है (Telegram document)
3. Bot `access_token` + cookies parse करता है
4. Job FIFO queue में जाता है
5. Worker pool (default 100 concurrent) job pick करता है
6. UPI flow run करता है (Python के steps 2-6)
7. `@prr9293` watermark के साथ QR PNG render
8. Telegram के through QR user को भेजता है + realtime progress log

**Advanced features**:

| Feature | विवरण |
|---|---|
| **FIFO Queue** | Hard cap (default 50 pending) OOM रोकने के लिए |
| **Per-user limit** | Default 2 jobs/user, admin `/set_user_limit @user n` से override कर सकता है |
| **Cooldown** | 10s per-user jobs के बीच, anti-spam |
| **Proxy pool** | Step 3 से proxy rotate (login DIRECT) |
| **Restart on failures** | 20 consecutive exceptions के बाद checkout restart |
| **Job timeout** | 1800s hard timeout |
| **Watermark** | QR PNG पर `@prr9293` stamp |
| **Admin notification** | जब other users QR generate करें तो admin को notify |
| **Multi-language** | i18n: EN/VI/CN/ID/HI |
| **Settings persistence** | SQLite Settings Store, limit change पर restart नहीं चाहिए |

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
/lang <vi|en|zh|id|hi>          — भाषा switch करें
/set_max_per_user <n>           — (Admin) per-user limit बदलें
/set_user_limit @user <n>       — (Admin) एक user के लिए override
/set_max_concurrent <n>         — (Admin) total concurrency बदलें
```

**Deployment targets**:

- 🐧 OpenWrt aarch64 (router) — small binary, कम RAM
- 🐳 Docker (build from source)
- ☁️ Linux x86_64 VPS — main app के साथ colocate करना best

### India users के लिए विशेष notes

- ChatGPT Plus India में **UPI payment** से purchase होता है — code में built-in support
- **Residential proxy ज़रूरी** है (datacenter IPs अक्सर ban हो जाते हैं Turnstile पर)
- Rust UPI bot router-grade hardware पर भी चल सकता है (OpenWrt aarch64)
- Test scripts: `test/check_har_signup_deep.py`, `pay_upi_http.py`

---

## 🐳 Docker Setup (Main app)

### Requirements

- **Docker Desktop** (Windows/macOS) या **Docker Engine + Compose** (Linux)
- Minimum **4GB RAM** (concurrency ≥ 3 के लिए 8GB)
- Stable internet
- **JP या VN में VPS** ChatGPT Plus discount के लिए

### Step 1 — Docker install करें

```bash
# Linux Ubuntu/Debian
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker
docker --version
```

Windows/macOS: https://www.docker.com/products/docker-desktop/

### Step 2 — Clone

```bash
git clone https://github.com/6c696e68/gpt_signup_hybrid.git
cd gpt_signup_hybrid
```

### Step 3 — `.env` create करें

```bash
cp .env.docker.example .env
sed -i.bak "s/change-me-strong-random/$(openssl rand -hex 32)/" .env
```

### Step 4 — Build & run

```bash
docker compose build           # पहली बार 5-10 minutes
docker compose up -d
docker compose logs -f web
```

> 🟢 Apple Silicon → amd64 VPS: `docker buildx build --platform linux/amd64 -t gsh:latest --load .`

### Step 5 — UI access करें

```bash
TOKEN=$(grep GPT_SIGNUP_WEB_TOKEN .env | cut -d= -f2)
echo "http://127.0.0.1:8083/?token=$TOKEN"
```

UI tabs: **Register · Session · Link · HME · AutoReg · Settings**

### Step 6 — iCloud HME runner enable (optional)

```bash
docker compose --profile hme up -d
```

### Useful commands

```bash
docker compose ps
docker compose logs -f web
docker compose restart web
docker compose down                                   # volume safe
docker compose down -v                                # data delete
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

## 🌏 ChatGPT Plus discount के लिए VPN / VPS

| Region | Notes | Recommendation |
|---|---|---|
| 🇯🇵 **Japan (JP)** | Stable pricing | **Production के लिए best** |
| 🇻🇳 **Vietnam (VN)** | Region में सबसे सस्ता | **Testing के लिए best** |
| 🇮🇳 **India** | सस्ता but UPI-only | **Residential proxy ज़रूरी** |
| 🇮🇩 Indonesia | GoPay/Midtrans built-in | Built-in support |

### VPS providers

- **Vultr**: Tokyo/Osaka
- **Linode/Akamai**: Tokyo
- **DigitalOcean**: Singapore
- **Vietnam VPS**: Viettel IDC, FPT Cloud, BizflyCloud

Minimum spec: **2 vCPU / 4GB RAM / 40GB SSD / Ubuntu 22.04+**

### VPN gateway

- **WireGuard** server JP/VN में
- **OpenVPN** + JP/VN gateway
- **Residential proxy JP/VN** (Bright Data, Soax, NetNut)

Proxy configure: UI **Settings → Proxies** या `.env` में `HYBRID_OUTLOOK_PROXY=http://...` set करें।

---

## 🛠️ Manual install (Docker के बिना)

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
| `GPT_SIGNUP_WEB_TOKEN required` | Step 3 दोबारा करें |
| Container `unhealthy` | `docker compose logs web`, RAM check करें |
| Blank Web UI | URL में `?token=...` add करें |
| Job stuck | Web restart, proxy pool check करें |
| Captcha/Turnstile fail | **Residential proxy** use करें |
| UPI confirm fail | Stripe को JS-runtime tokens चाहिए, Rust bot use करें browser stage के साथ |
| Apple Silicon slow build | Directly amd64 VPS पर build करें |

---

## 📁 Project structure

```
gpt_signup_hybrid/
├── cli.py, signup.py, browser_phase.py, request_phase.py    # Core
├── session_phase.py, mfa_phase.py                           # Session + MFA
├── payment_link.py, pay_upi_http.py                         # Payment (Python UPI)
├── db/                                                      # SQLite + Settings
├── web/                                                     # FastAPI + UI
├── icloud_hme/                                              # iCloud HME pool
├── autoreg/                                                 # AutoReg loop
├── rust_upi_bot/                                            # Rust UPI bot
└── test/, docs/                                             # Tests + docs
```
