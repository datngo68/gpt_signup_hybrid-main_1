# 🇻🇳 gpt_signup_hybrid — Tiếng Việt

> Pipeline tự động đăng ký ChatGPT + UPI payment bot. FastAPI + Camoufox + Rust.

[← Quay về index đa ngôn ngữ](./README.md)

---

## 💖 Donate / Ủng hộ

| Method | Address |
|---|---|
| 🟡 **Binance ID** | `356552242` |
| 🟢 **USDT (BEP20)** | `0x137a3bfa30ee426127367773dfce16aefce04e02` |
| 🔴 **USDT (TRC20)** | `TFy5d1EDT4WBKgtoypx7Ua2dCZhPHMSDNs` |
| ✈️ **Telegram** | [@prr9293](https://t.me/prr9293) |
| 👥 **Telegram Group** | [t.me/+C6eafntO-Eo1Njdl](https://t.me/+C6eafntO-Eo1Njdl) |

---

## ⚠️ ĐỌC TRƯỚC — CHÚ Ý QUAN TRỌNG

> 🎯 **Để nhận ưu đãi giá ChatGPT Plus (PPP rẻ hơn 40-60%), proxy login BẮT BUỘC phải ra IP Việt Nam (VN) hoặc Nhật Bản (JP).**
>
> **Nếu bạn KHÔNG có proxy VN/JP cho proxy login**, bạn vẫn có thể dùng tool — chỉ cần dùng VPN thay thế:
>
> - ✅ **Cài VPN có server VN hoặc Japan** ngay trên thiết bị chạy tool (laptop / VPS / server)
> - ✅ Khi VPN đã active → để trống ô proxy login trong UI/CLI, tool sẽ tự dùng IP từ VPN
> - ✅ Các loại VPN gợi ý:
>   - 🛠️ Tự dựng **WireGuard / OpenVPN** trên VPS Nhật/Việt Nam (rẻ và an toàn nhất)
>   - 💼 VPN thương mại có node JP/VN — Mullvad, ProtonVPN, NordVPN, Surfshark, ExpressVPN
>   - 🏠 Residential proxy (Bright Data, Soax, NetNut) cho production scale
>
> ⛔ Chạy ở IP US/EU **mà không có** VPN JP/VN → giá Plus đắt gấp 2-3 lần, hoặc bị geo-block UPI/GoPay.
>
> 💡 **Khuyên dùng cho người mới**:
> 1. Thuê VPS Nhật Bản (Vultr Tokyo ~5$/tháng) → chạy thẳng trên VPS, KHÔNG cần proxy/VPN
> 2. Hoặc dùng ProtonVPN free tier (có node Japan miễn phí) trên laptop cá nhân để test

---

## 📖 Giới thiệu

`gpt_signup_hybrid` là pipeline tự động đăng ký tài khoản ChatGPT có giao diện web local, đi kèm UPI payment bot viết bằng Rust.

### Tính năng chính

- 🎯 **Hybrid registration** — Camoufox + curl_cffi né detection
- 📧 **Mail providers** — iCloud HME v3, Outlook pool, Gmail, custom Worker API
- 💳 **Payment automation** — ChatGPT Plus checkout, Stripe, GoPay/Midtrans, UPI
- 🔐 **MFA/TOTP** auto-enable sau signup
- 🍎 **iCloud Hide My Email pool** — tự sinh email + rotate profile
- 🔄 **AutoReg loop** — sinh HME → tạo account → enable MFA tự động
- 🌐 **Local Web UI** (FastAPI) realtime SSE
- 🦀 **Rust UPI bot** — Telegram bot tạo QR thanh toán UPI

### 3 chế độ đăng ký

| Mode | Mô tả |
|---|---|
| `pure_request` | HTTP-only nhanh nhất, chỉ curl_cffi |
| `browser` | Camoufox full browser flow |
| `hybrid` | **Khuyên dùng** — Camoufox làm auth relay + Python tái tạo field |

---

## 💰 Chi tiết tính năng UPI

### UPI là gì?

**UPI (Unified Payments Interface)** là hệ thống thanh toán thời gian thực của Ấn Độ, do NPCI vận hành. Cho phép chuyển tiền giữa các ngân hàng qua mobile app (PhonePe, GPay, Paytm, BHIM).

**Tại sao UPI quan trọng cho ChatGPT?**

- 🇮🇳 ChatGPT Plus tại India dùng **UPI làm phương thức thanh toán chính** (ngoài thẻ Visa/Master)
- 💸 Giá ChatGPT Plus tại India **rẻ hơn 40-60%** so với US/EU nhờ PPP pricing
- 🎯 VPA format: `name@oksbi`, `9876543210@ybl`, `user@paytm`

### Dự án có 2 hệ thống UPI

#### 1️⃣ Python `pay_upi_http.py` — pure-HTTP UPI flow

**Công dụng**: Tự động tạo checkout UPI cho 1 account ChatGPT bằng pure HTTP (không browser).

**Pipeline**:
1. Login ChatGPT bằng combo `email|pass|secret` hoặc `session.json`
2. POST `/backend-api/payments/checkout` tạo checkout session
3. POST `api.stripe.com/v1/payment_pages/{id}/init` khởi tạo Stripe
4. GET `api.stripe.com/v1/elements/sessions` lấy elements config
5. POST `/confirm` submit UPI VPA
6. POST `/approve` polling cho đến khi user thanh toán xong

**Đặc điểm**:
- ✅ Không dùng browser → nhẹ, nhanh
- ✅ curl_cffi impersonate Chrome 145 Windows TLS
- ✅ Proxy split: login DIRECT (giảm captcha), step 2+ qua proxy India
- ⚠️ Stripe `/confirm` yêu cầu 3 JS-runtime token (`js_checksum`, `rv_timestamp`, `passive_captcha_token`) — script submit best-effort
- 🎯 Phù hợp: test thủ công, automation 1-vài account

**Cách chạy**:

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

#### 2️⃣ Rust `rust_upi_bot/` — Telegram UPI QR generator bot

**Công dụng**: Service-as-a-bot — user gửi `session.json` qua Telegram, bot tự động chạy UPI flow và trả về QR PNG để quét thanh toán.

**Pipeline**:
1. User `/start` → bot greeting (đa ngôn ngữ)
2. User upload `session.json` (Telegram document)
3. Bot parse `access_token` + cookies
4. Job vào FIFO queue
5. Worker pool (max 100 concurrent default) pick job khi có slot
6. Chạy UPI flow (steps 2-6 như Python)
7. Render QR PNG có watermark `@prr9293`
8. Gửi QR cho user qua Telegram + realtime progress log

**Tính năng nâng cao**:

| Feature | Mô tả |
|---|---|
| **FIFO Queue** | Hard cap (default 50 pending) chống OOM |
| **Per-user limit** | Default 2 job/user, admin override qua `/set_user_limit @user n` |
| **Cooldown** | 10s giữa 2 job cùng user, chống spam |
| **Proxy pool** | Rotate proxy từ step 3 (login DIRECT) |
| **Restart on failures** | Restart checkout sau 20 lần `exception` liên tiếp |
| **Job timeout** | 1800s hard timeout |
| **Watermark** | QR PNG đóng dấu `@prr9293` |
| **Admin notification** | Notify admin khi user khác tạo QR thành công |
| **Multi-language** | i18n: EN/VI/CN/ID/HI |
| **Settings persistence** | SQLite Settings Store, không restart khi đổi limit |

**Cách chạy**:

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
/help                           — Hướng dẫn
/status                         — Trạng thái queue + worker
/lang <vi|en|zh|id|hi>          — Đổi ngôn ngữ
/set_max_per_user <n>           — (Admin) đổi limit per-user
/set_user_limit @user <n>       — (Admin) override limit cho 1 user
/set_max_concurrent <n>         — (Admin) đổi tổng concurrent
```

**Triển khai**:

- 🐧 OpenWrt aarch64 (router) — binary nhỏ, ít RAM
- 🐳 Docker (build từ source)
- ☁️ VPS Linux x86_64 — tốt nhất chạy chung với main app

---

## 🐳 Cài đặt với Docker (Main app)

### Yêu cầu

- **Docker Desktop** (Windows/macOS) hoặc **Docker Engine + Compose** (Linux)
- RAM tối thiểu **4GB** (8GB nếu concurrency ≥ 3)
- Internet ổn định
- **VPS tại JP hoặc VN** để nhận ưu đãi (xem mục VPN)

### Bước 1 — Cài Docker

```bash
# Linux Ubuntu/Debian
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker
docker --version
```

Windows/macOS: tải Docker Desktop tại https://www.docker.com/products/docker-desktop/

### Bước 2 — Clone

```bash
git clone https://github.com/6c696e68/gpt_signup_hybrid.git
cd gpt_signup_hybrid
```

### Bước 3 — Tạo `.env`

```bash
cp .env.docker.example .env
sed -i.bak "s/change-me-strong-random/$(openssl rand -hex 32)/" .env
```

### Bước 4 — Build & run

```bash
docker compose build           # 5-10 phút lần đầu
docker compose up -d
docker compose logs -f web     # tail log
```

> 🟢 Apple Silicon → amd64 VPS: `docker buildx build --platform linux/amd64 -t gsh:latest --load .`

### Bước 5 — Truy cập UI

```bash
TOKEN=$(grep GPT_SIGNUP_WEB_TOKEN .env | cut -d= -f2)
echo "http://127.0.0.1:8083/?token=$TOKEN"
```

UI tabs: **Register · Session · Link · HME · AutoReg · Settings**

### Bước 6 — Bật iCloud HME runner (tuỳ chọn)

```bash
docker compose --profile hme up -d
```

### Lệnh quản lý

```bash
docker compose ps                                     # status
docker compose logs -f web                            # tail logs
docker compose restart web                            # restart
docker compose down                                   # stop (giữ data)
docker compose down -v                                # stop + xóa data
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

## 🌏 VPN / VPS để nhận ưu đãi ChatGPT Plus

ChatGPT Plus áp dụng **PPP pricing** — giá thay đổi theo quốc gia.

| Vùng | Đặc điểm | Khuyến nghị |
|---|---|---|
| 🇯🇵 **Japan (JP)** | Ổn định, ít cross-check | **Tốt nhất production** |
| 🇻🇳 **Vietnam (VN)** | Rẻ nhất khu vực | **Tốt nhất testing** |
| 🇮🇳 India | Rẻ nhưng UPI-only + dễ ban datacenter | Cần residential proxy |
| 🇮🇩 Indonesia | GoPay/Midtrans built-in | Hỗ trợ sẵn |

### VPS providers

- **Vultr**: Tokyo/Osaka
- **Linode/Akamai**: Tokyo
- **DigitalOcean**: Singapore (gần JP)
- **VPS Việt Nam**: Viettel IDC, FPT Cloud, BizflyCloud

Cấu hình tối thiểu: **2 vCPU / 4GB RAM / 40GB SSD / Ubuntu 22.04+**

### VPN gateway

- **WireGuard** server tại JP/VN
- **OpenVPN** với gateway JP/VN
- **Residential proxy JP/VN** (Bright Data, Soax, NetNut)

Config proxy: UI **Settings → Proxies** hoặc set `HYBRID_OUTLOOK_PROXY=http://...` trong `.env`.

---

## 🛠️ Cài thủ công (không Docker)

```bash
git clone https://github.com/6c696e68/gpt_signup_hybrid.git
cd gpt_signup_hybrid
bash setup.sh        # Linux/macOS
# setup.bat          # Windows
```

---

## 🔧 Troubleshooting

| Lỗi | Cách xử lý |
|---|---|
| `GPT_SIGNUP_WEB_TOKEN bắt buộc` | Sửa `.env` Bước 3 |
| Container `unhealthy` | `docker compose logs web`, kiểm tra RAM |
| Web UI trắng | Thêm `?token=...` vào URL |
| Job stuck | `docker compose restart web`, check proxy pool |
| Captcha/Turnstile fail | Đổi sang **residential proxy** |
| UPI confirm fail | Stripe yêu cầu JS-runtime token, dùng Rust bot có browser stage |
| Build chậm trên Apple Silicon | Build trực tiếp trên VPS amd64 |

---

## 📁 Cấu trúc dự án

```
gpt_signup_hybrid/
├── cli.py, signup.py, browser_phase.py, request_phase.py    # Core
├── session_phase.py, mfa_phase.py                           # Session + MFA
├── payment_link.py, pay_upi_http.py                         # Payment (UPI Python)
├── db/                                                      # SQLite + Settings Store
├── web/                                                     # FastAPI + UI
├── icloud_hme/                                              # iCloud HME pool
├── autoreg/                                                 # AutoReg loop
├── rust_upi_bot/                                            # Rust UPI Telegram bot
└── test/, docs/                                             # Tests + docs
```

Xem thêm: [`AGENTS.md`](./AGENTS.md), [`.planning/codebase/ARCHITECTURE.md`](./.planning/codebase/ARCHITECTURE.md)
