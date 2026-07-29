# 🇮🇩 gpt_signup_hybrid — Bahasa Indonesia

> Pipeline pendaftaran ChatGPT otomatis + UPI payment bot. FastAPI + Camoufox + Rust.

[← Kembali ke index bahasa](./README.md)

---

## 💖 Donasi / Dukungan

| Method | Address |
|---|---|
| 🟡 **Binance ID** | `356552242` |
| 🟢 **USDT (BEP20)** | `0x137a3bfa30ee426127367773dfce16aefce04e02` |
| 🔴 **USDT (TRC20)** | `TFy5d1EDT4WBKgtoypx7Ua2dCZhPHMSDNs` |
| ✈️ **Telegram** | [@prr9293](https://t.me/prr9293) |
| 👥 **Grup Telegram** | [t.me/+C6eafntO-Eo1Njdl](https://t.me/+C6eafntO-Eo1Njdl) |

---

## ⚠️ BACA DULU — PEMBERITAHUAN PENTING

> 🎯 **Untuk dapat diskon harga ChatGPT Plus (PPP 40-60% lebih murah), proxy login HARUS keluar dari Vietnam (VN) atau Jepang (JP).**
>
> **Jika kamu TIDAK punya proxy VN/JP untuk login**, kamu masih bisa pakai tool — cukup gunakan VPN sebagai gantinya:
>
> - ✅ **Pasang VPN dengan server VN atau Jepang** di perangkat yang menjalankan tool (laptop / VPS / server)
> - ✅ Setelah VPN aktif → biarkan field proxy login **kosong** di UI/CLI; tool akan otomatis pakai IP VPN
> - ✅ Pilihan VPN rekomendasi:
>   - 🛠️ Self-host **WireGuard / OpenVPN** di VPS JP/VN (paling murah dan stabil)
>   - 💼 VPN komersial dengan node JP/VN — Mullvad, ProtonVPN, NordVPN, Surfshark, ExpressVPN
>   - 🏠 Residential proxy (Bright Data, Soax, NetNut) untuk skala produksi
>
> ⛔ Jalan dari IP US/EU **tanpa** VPN JP/VN → harga Plus 2-3× lebih mahal, atau UPI/GoPay di-geo-block.
>
> 💡 **Rekomendasi untuk pemula**:
> 1. Sewa VPS Jepang (Vultr Tokyo ~$5/bulan) → jalankan tool langsung di sana, TANPA perlu proxy/VPN
> 2. Atau pakai ProtonVPN free tier (ada node Jepang gratis) di laptop pribadi untuk testing

---

## 📖 Pengenalan

`gpt_signup_hybrid` adalah pipeline pendaftaran akun ChatGPT otomatis dengan local web UI, dipasangkan dengan UPI payment bot berbasis Rust.

### Fitur utama

- 🎯 **Hybrid registration** — Camoufox + curl_cffi untuk bypass deteksi
- 📧 **Mail providers** — iCloud HME v3, Outlook pool, Gmail, custom Worker API
- 💳 **Otomasi pembayaran** — ChatGPT Plus checkout, Stripe, **GoPay/Midtrans (Indonesia)**, UPI
- 🔐 **MFA/TOTP** auto-enable setelah signup
- 🍎 **iCloud Hide My Email pool** — auto-generate + rotasi profile
- 🔄 **AutoReg loop** — HME → akun → MFA pipeline
- 🌐 **Local Web UI** (FastAPI) dengan log SSE realtime
- 🦀 **Rust UPI bot** — Telegram bot untuk UPI QR generation

### 3 mode pendaftaran

| Mode | Deskripsi |
|---|---|
| `pure_request` | HTTP-only, tercepat, hanya curl_cffi |
| `browser` | Full Camoufox browser flow |
| `hybrid` | **Direkomendasikan** — Camoufox auth relay + Python field reproduction |

---

## 💰 Detail fitur UPI

### Apa itu UPI?

**UPI (Unified Payments Interface)** adalah sistem pembayaran realtime India, dioperasikan NPCI. Memungkinkan transfer instan antar bank via app mobile (PhonePe, GPay, Paytm, BHIM).

**Mengapa UPI penting untuk ChatGPT?**

- 🇮🇳 ChatGPT Plus di India menerima **UPI sebagai metode pembayaran utama** (selain Visa/Mastercard)
- 💸 ChatGPT Plus **40-60% lebih murah di India** vs US/EU karena PPP pricing
- 🎯 Format VPA: `name@oksbi`, `9876543210@ybl`, `user@paytm`

### Dua sistem UPI di project ini

#### 1️⃣ Python `pay_upi_http.py` — flow UPI pure-HTTP

**Tujuan**: Generate UPI checkout otomatis untuk satu akun ChatGPT via pure HTTP (tanpa browser).

**Pipeline**:
1. Login ChatGPT via combo `email|pass|secret` atau `session.json`
2. POST `/backend-api/payments/checkout` buat checkout session
3. POST `api.stripe.com/v1/payment_pages/{id}/init` inisialisasi Stripe
4. GET `api.stripe.com/v1/elements/sessions` ambil elements config
5. POST `/confirm` submit UPI VPA
6. POST `/approve` polling sampai user selesai bayar

**Karakteristik**:
- ✅ Tanpa browser → ringan, cepat
- ✅ curl_cffi impersonate Chrome 145 Windows TLS
- ✅ Proxy split: login DIRECT (kurangi captcha), step 2+ via India proxy
- ⚠️ Stripe `/confirm` butuh 3 JS-runtime token — best-effort submit
- 🎯 Cocok untuk: testing manual, otomasi 1-beberapa akun

**Cara pakai**:

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

**Tujuan**: Service-as-a-bot — user kirim `session.json` via Telegram, bot jalankan flow UPI dan kirim balik QR PNG untuk pembayaran.

**Pipeline**:
1. User `/start` → greeting bot (multi-bahasa)
2. User upload `session.json` (Telegram document)
3. Bot parse `access_token` + cookies
4. Job masuk FIFO queue
5. Worker pool (default 100 concurrent) ambil job
6. Jalankan flow UPI (step 2-6 seperti Python)
7. Render QR PNG dengan watermark `@prr9293`
8. Kirim QR ke user via Telegram + log progress realtime

**Fitur lanjutan**:

| Fitur | Deskripsi |
|---|---|
| **FIFO Queue** | Hard cap (default 50 pending) cegah OOM |
| **Per-user limit** | Default 2 job/user, admin override via `/set_user_limit @user n` |
| **Cooldown** | 10s antar job per user, anti-spam |
| **Proxy pool** | Rotasi proxy mulai step 3 (login DIRECT) |
| **Restart on failures** | Restart checkout setelah 20 exception berturut |
| **Job timeout** | 1800s hard timeout |
| **Watermark** | QR PNG cap `@prr9293` |
| **Notifikasi admin** | Notify admin saat user lain sukses generate QR |
| **Multi-bahasa** | i18n: EN/VI/CN/ID/HI |
| **Settings persistence** | SQLite Settings Store, tanpa restart saat ubah limit |

**Cara pakai**:

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

**Perintah Telegram**:

```
/start                          — Greeting
/help                           — Bantuan
/status                         — Status queue + worker
/lang <vi|en|zh|id|hi>          — Ganti bahasa
/set_max_per_user <n>           — (Admin) ubah limit per-user
/set_user_limit @user <n>       — (Admin) override limit satu user
/set_max_concurrent <n>         — (Admin) ubah total concurrency
```

**Target deployment**:

- 🐧 OpenWrt aarch64 (router) — binary kecil, hemat RAM
- 🐳 Docker (build from source)
- ☁️ Linux x86_64 VPS — terbaik colocate dengan main app

---

## 🐳 Docker Setup (Main app)

### Kebutuhan

- **Docker Desktop** (Windows/macOS) atau **Docker Engine + Compose** (Linux)
- RAM minimum **4GB** (8GB jika concurrency ≥ 3)
- Internet stabil
- **VPS di JP atau VN** untuk diskon ChatGPT Plus

### Langkah 1 — Install Docker

```bash
# Linux Ubuntu/Debian
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker
docker --version
```

Windows/macOS: https://www.docker.com/products/docker-desktop/

### Langkah 2 — Clone

```bash
git clone https://github.com/6c696e68/gpt_signup_hybrid.git
cd gpt_signup_hybrid
```

### Langkah 3 — Buat `.env`

```bash
cp .env.docker.example .env
sed -i.bak "s/change-me-strong-random/$(openssl rand -hex 32)/" .env
```

### Langkah 4 — Build & run

```bash
docker compose build           # 5-10 menit pertama kali
docker compose up -d
docker compose logs -f web
```

> 🟢 Apple Silicon → amd64 VPS: `docker buildx build --platform linux/amd64 -t gsh:latest --load .`

### Langkah 5 — Akses UI

```bash
TOKEN=$(grep GPT_SIGNUP_WEB_TOKEN .env | cut -d= -f2)
echo "http://127.0.0.1:8083/?token=$TOKEN"
```

Tab UI: **Register · Session · Link · HME · AutoReg · Settings**

### Langkah 6 — Aktifkan iCloud HME runner (opsional)

```bash
docker compose --profile hme up -d
```

### Perintah berguna

```bash
docker compose ps
docker compose logs -f web
docker compose restart web
docker compose down                                   # volume tetap
docker compose down -v                                # hapus data
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

## 🌏 VPN / VPS untuk diskon ChatGPT Plus

| Region | Catatan | Rekomendasi |
|---|---|---|
| 🇯🇵 **Jepang (JP)** | Harga stabil | **Terbaik production** |
| 🇻🇳 **Vietnam (VN)** | Termurah di region | **Terbaik testing** |
| 🇮🇳 India | Murah tapi UPI-only | Butuh residential proxy |
| 🇮🇩 **Indonesia** | GoPay/Midtrans | **Support built-in** |

### Provider VPS

- **Vultr**: Tokyo/Osaka
- **Linode/Akamai**: Tokyo
- **DigitalOcean**: Singapore
- **Vietnam VPS**: Viettel IDC, FPT Cloud, BizflyCloud

Spec minimum: **2 vCPU / 4GB RAM / 40GB SSD / Ubuntu 22.04+**

### VPN gateway

- **WireGuard** server di JP/VN
- **OpenVPN** dengan gateway JP/VN
- **Residential proxy JP/VN** (Bright Data, Soax, NetNut)

Konfigurasi proxy: UI **Settings → Proxies** atau set `HYBRID_OUTLOOK_PROXY=http://...` di `.env`.

---

## 🛠️ Install manual (tanpa Docker)

```bash
git clone https://github.com/6c696e68/gpt_signup_hybrid.git
cd gpt_signup_hybrid
bash setup.sh        # Linux/macOS
# setup.bat          # Windows
```

---

## 🔧 Troubleshooting

| Error | Solusi |
|---|---|
| `GPT_SIGNUP_WEB_TOKEN required` | Ulangi Langkah 3 |
| Container `unhealthy` | `docker compose logs web`, cek RAM |
| Web UI kosong | Tambah `?token=...` ke URL |
| Job stuck | Restart web, cek proxy pool |
| Captcha/Turnstile gagal | Ganti **residential proxy** |
| UPI confirm gagal | Stripe butuh JS-runtime token, pakai Rust bot dengan stage browser |
| Build lambat Apple Silicon | Build langsung di VPS amd64 |

---

## 📁 Struktur project

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
