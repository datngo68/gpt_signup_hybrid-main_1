<!-- =====================================================================
  rust_upi_bot — UPI QR Telegram Bot (Rust)
  ===================================================================== -->

<div align="center">

# 🦀 rust_upi_bot

**Telegram bot — UPI QR generator for ChatGPT Plus India payment**

[![Rust 1.78+](https://img.shields.io/badge/rust-1.78%2B-orange.svg?logo=rust)](https://www.rust-lang.org/)
[![Tokio](https://img.shields.io/badge/runtime-tokio-blue.svg)](https://tokio.rs/)
[![OpenWrt](https://img.shields.io/badge/target-OpenWrt%20aarch64-green.svg)](https://openwrt.org/)

[← Back to main project](../README.md)

</div>

---

## 💖 Donate

| Method | Address |
|---|---|
| 🟡 Binance ID | `356552242` |
| 🟢 USDT (BEP20) | `0x137a3bfa30ee426127367773dfce16aefce04e02` |
| 🔴 USDT (TRC20) | `TFy5d1EDT4WBKgtoypx7Ua2dCZhPHMSDNs` |
| ✈️ Telegram | [@prr9293](https://t.me/prr9293) |

---

## ⚠️ READ FIRST — IMPORTANT NOTICE · CHÚ Ý

> 🎯 **The Rust UPI bot runs the ChatGPT login step against India region. The ChatGPT account itself must already be created from a VN or JP IP to get the discounted Plus price — otherwise the checkout will show the expensive US/EU price even if UPI works.**
>
> **For the main signup tool (Python `gpt_signup_hybrid`)** — if you have no VN/JP login proxy, install a **VN or JP VPN** on the host running the tool (laptop / VPS / server). See the main project READMEs for details.
>
> **For this Rust UPI bot specifically**:
> - 🇮🇳 Login + UPI flow targets India endpoints → use an **India residential proxy** for step 3+ (configured via `PROXY_POOL` env)
> - 🇯🇵 / 🇻🇳 If you cannot get an India residential proxy and your account was created via JP/VN, the bot still works for many cases but Stripe/Turnstile rejection rate is higher
> - 🛠️ Best practice: pair this bot with the main project's signup pipeline running on a **JP or VN VPS** so the entire account lifecycle (signup → UPI) keeps the PPP discount
>
> ⛔ Datacenter IPs (especially in India) are aggressively banned by Cloudflare Turnstile → residential proxy is mandatory for production scale.
>
> ---
>
> 🇻🇳 **Tiếng Việt**: Account ChatGPT phải được tạo từ IP VN hoặc JP để có giá Plus rẻ. Bot này chỉ chạy bước UPI — không tạo account. Pair với main project (signup) chạy trên VPS JP/VN.
> 🇨🇳 **中文**: ChatGPT 账号必须从 VN 或 JP IP 创建才能获得 Plus 优惠价。本 bot 只跑 UPI 步骤，不创建账号。建议与主项目（signup）一起在 JP/VN VPS 上运行。
> 🇮🇩 **ID**: Akun ChatGPT harus dibuat dari IP VN atau JP untuk dapat harga Plus diskon. Bot ini hanya jalankan langkah UPI. Pasangkan dengan main project di VPS JP/VN.
> 🇮🇳 **हिन्दी**: ChatGPT account VN या JP IP से create होना चाहिए Plus discount के लिए। यह bot सिर्फ UPI step run करता है। Main project के साथ JP/VN VPS पर use करें।

---

## 🇻🇳 Tiếng Việt

### Công dụng

Telegram bot dạng **service-as-a-bot** cho phép user gửi `session.json` của ChatGPT account India → bot tự động:

1. Parse `access_token` + cookies từ file
2. Đẩy job vào FIFO queue
3. Worker pool chạy UPI payment flow (giống `pay_upi_http.py` Python nhưng bằng Rust async)
4. Render QR PNG có watermark `@prr9293`
5. Gửi QR cho user qua Telegram để quét bằng app UPI (PhonePe/GPay/Paytm)

**Tại sao Rust thay vì Python?**

- ⚡ Tốc độ: Rust async/Tokio xử lý concurrent cao hơn (default 100 worker)
- 💾 Tiết kiệm RAM: chạy được trên **router OpenWrt aarch64**
- 🔒 Memory safety: không lo crash do GC pause
- 🚀 Binary nhỏ gọn, deploy đơn giản

### Tính năng

| Feature | Mô tả |
|---|---|
| 📥 **FIFO Queue** | Hard cap pending (default 50) chống OOM |
| 👥 **Per-user limit** | Default 2 job song song/user, admin override được |
| ⏱️ **Cooldown** | 10s giữa 2 job cùng user |
| 🔄 **Proxy pool** | Rotate proxy từ step 3 (login DIRECT giảm captcha) |
| 🔁 **Auto restart** | Restart checkout sau 20 lần exception liên tiếp |
| ⏰ **Job timeout** | 1800s hard timeout per job |
| 🖼️ **Watermark** | QR PNG đóng dấu `@prr9293` (không đè QR) |
| 📢 **Admin notify** | Notify admin khi user khác tạo QR thành công |
| 🌐 **i18n** | EN/VI/CN/ID/HI |
| 💾 **Persist settings** | SQLite Settings Store, đổi limit không cần restart |

### Build & chạy

```bash
# Yêu cầu: Rust 1.78+
cd rust_upi_bot
cargo build --release

# Chạy (env)
TELEGRAM_TOKEN=<bot_token_from_@BotFather> \
ALLOWED_USERS=123456789,987654321 \
MAX_CONCURRENT=50 \
MAX_PER_USER=2 \
ADMIN_CHAT_ID=123456789 \
QR_WATERMARK='@prr9293' \
PROXY_POOL='http://u:p@h1:8080,http://u:p@h2:8080' \
PROXY_FROM_STEP=3 \
APPROVE_RETRIES=200 \
APPROVE_DELAY_SECS=3 \
JOB_TIMEOUT_SECONDS=1800 \
./target/release/upi-qr-bot
```

### Telegram commands

```
/start                          — Greeting bot (auto-detect language)
/help                           — Hướng dẫn sử dụng
/status                         — Xem queue + worker status
/lang <vi|en|zh|id|hi>          — Đổi ngôn ngữ
/cancel                         — Huỷ job đang queue của mình
/set_max_per_user <n>           — (Admin) đổi default limit per-user
/set_user_limit @user <n>       — (Admin) override limit cho 1 user
/set_max_concurrent <n>         — (Admin) đổi tổng concurrency
```

### Cross-compile cho OpenWrt aarch64

```bash
rustup target add aarch64-unknown-linux-musl
cargo build --release --target aarch64-unknown-linux-musl
# Binary: target/aarch64-unknown-linux-musl/release/upi-qr-bot
# Copy qua router → systemd/procd service
```

---

## 🇬🇧 English

### Purpose

Telegram **service-as-a-bot** that lets users submit ChatGPT India account `session.json` → bot automatically:

1. Parses `access_token` + cookies from file
2. Enqueues job in FIFO queue
3. Worker pool runs UPI payment flow (same as Python `pay_upi_http.py` but Rust async)
4. Renders QR PNG with `@prr9293` watermark
5. Sends QR to user via Telegram for scanning with UPI app (PhonePe/GPay/Paytm)

**Why Rust instead of Python?**

- ⚡ Speed: Rust async/Tokio handles higher concurrency (default 100 workers)
- 💾 RAM efficient: runs on **OpenWrt aarch64 routers**
- 🔒 Memory safety: no GC pause crashes
- 🚀 Compact binary, simple deployment

### Features

| Feature | Description |
|---|---|
| 📥 **FIFO Queue** | Hard pending cap (default 50) to prevent OOM |
| 👥 **Per-user limit** | Default 2 parallel jobs/user, admin overridable |
| ⏱️ **Cooldown** | 10s between jobs per user |
| 🔄 **Proxy pool** | Rotate proxies from step 3 (login DIRECT lowers captcha) |
| 🔁 **Auto restart** | Restart checkout after 20 consecutive exceptions |
| ⏰ **Job timeout** | 1800s hard timeout per job |
| 🖼️ **Watermark** | QR PNG stamped `@prr9293` (no overlap with QR) |
| 📢 **Admin notify** | Notifies admin when other users generate QR successfully |
| 🌐 **i18n** | EN/VI/CN/ID/HI |
| 💾 **Persist settings** | SQLite Settings Store, no restart on limit change |

### Build & run

```bash
# Requires: Rust 1.78+
cd rust_upi_bot
cargo build --release

# Run (env)
TELEGRAM_TOKEN=<bot_token_from_@BotFather> \
ALLOWED_USERS=123456789,987654321 \
MAX_CONCURRENT=50 \
MAX_PER_USER=2 \
ADMIN_CHAT_ID=123456789 \
QR_WATERMARK='@prr9293' \
PROXY_POOL='http://u:p@h1:8080,http://u:p@h2:8080' \
PROXY_FROM_STEP=3 \
APPROVE_RETRIES=200 \
APPROVE_DELAY_SECS=3 \
JOB_TIMEOUT_SECONDS=1800 \
./target/release/upi-qr-bot
```

### Telegram commands

```
/start                          — Bot greeting (auto-detect language)
/help                           — Usage instructions
/status                         — Show queue + worker status
/lang <vi|en|zh|id|hi>          — Switch language
/cancel                         — Cancel your queued job
/set_max_per_user <n>           — (Admin) change default per-user limit
/set_user_limit @user <n>       — (Admin) override limit for one user
/set_max_concurrent <n>         — (Admin) change total concurrency
```

### Cross-compile for OpenWrt aarch64

```bash
rustup target add aarch64-unknown-linux-musl
cargo build --release --target aarch64-unknown-linux-musl
# Binary: target/aarch64-unknown-linux-musl/release/upi-qr-bot
# Copy to router → systemd/procd service
```

---

## 🇨🇳 中文

### 用途

Telegram **service-as-a-bot** 让用户提交 ChatGPT 印度账号的 `session.json` → 机器人自动：

1. 从文件 parse `access_token` + cookies
2. 任务入 FIFO 队列
3. Worker 池跑 UPI 支付流程（与 Python `pay_upi_http.py` 相同，Rust async 实现）
4. 渲染带 `@prr9293` 水印的 QR PNG
5. 通过 Telegram 发 QR 给用户，用 UPI app（PhonePe/GPay/Paytm）扫码

**为何选 Rust 而非 Python？**

- ⚡ 速度：Rust async/Tokio 处理更高并发（默认 100 worker）
- 💾 省 RAM：可在 **OpenWrt aarch64 路由器**运行
- 🔒 内存安全：无 GC 暂停崩溃
- 🚀 二进制小，部署简单

### 主要特性

参考英文表格上方。

### 构建与运行

```bash
# 需要: Rust 1.78+
cd rust_upi_bot
cargo build --release

TELEGRAM_TOKEN=<bot_token> \
ALLOWED_USERS=123456789 \
MAX_CONCURRENT=50 \
PROXY_POOL='http://u:p@h1:8080' \
./target/release/upi-qr-bot
```

### Telegram 命令

```
/start, /help, /status, /lang, /cancel
/set_max_per_user <n>     (管理员)
/set_user_limit @user <n> (管理员)
/set_max_concurrent <n>   (管理员)
```

### OpenWrt aarch64 交叉编译

```bash
rustup target add aarch64-unknown-linux-musl
cargo build --release --target aarch64-unknown-linux-musl
```

---

## 🇮🇩 Bahasa Indonesia

### Tujuan

Telegram **service-as-a-bot** untuk user submit `session.json` akun ChatGPT India → bot otomatis:

1. Parse `access_token` + cookies
2. Job masuk FIFO queue
3. Worker pool jalankan flow pembayaran UPI
4. Render QR PNG dengan watermark `@prr9293`
5. Kirim QR ke user via Telegram untuk scan dengan app UPI

**Mengapa Rust?** Cepat (Tokio async), hemat RAM (jalan di OpenWrt router), memory safe.

### Build & run

```bash
cd rust_upi_bot
cargo build --release

TELEGRAM_TOKEN=<bot_token> \
ALLOWED_USERS=123456789 \
MAX_CONCURRENT=50 \
./target/release/upi-qr-bot
```

---

## 🇮🇳 हिन्दी / Hindi

### उद्देश्य

Telegram **service-as-a-bot** users को ChatGPT India account का `session.json` submit करने देता है → bot automatically:

1. File से `access_token` + cookies parse करता है
2. Job FIFO queue में जाता है
3. Worker pool UPI payment flow run करता है (Python `pay_upi_http.py` जैसा but Rust async)
4. `@prr9293` watermark के साथ QR PNG render
5. UPI app (PhonePe/GPay/Paytm) से scan करने के लिए Telegram के through QR भेजता है

**Rust क्यों Python की बजाय?**

- ⚡ Speed: Rust async/Tokio higher concurrency handle करता है (default 100 workers)
- 💾 RAM efficient: **OpenWrt aarch64 routers** पर भी चलता है
- 🔒 Memory safe: कोई GC pause crash नहीं
- 🚀 Compact binary, simple deployment

### Build & run

```bash
cd rust_upi_bot
cargo build --release

TELEGRAM_TOKEN=<bot_token> \
ALLOWED_USERS=123456789 \
MAX_CONCURRENT=50 \
MAX_PER_USER=2 \
PROXY_POOL='http://u:p@h1:8080' \
./target/release/upi-qr-bot
```

### Telegram commands

```
/start, /help, /status, /lang, /cancel
/set_max_per_user <n>     (Admin)
/set_user_limit @user <n> (Admin)
/set_max_concurrent <n>   (Admin)
```

### OpenWrt aarch64 cross-compile

```bash
rustup target add aarch64-unknown-linux-musl
cargo build --release --target aarch64-unknown-linux-musl
```

---

## 🏗️ Architecture

```
┌──────────────┐
│ Telegram     │
│ (user upload │
│  session.json)
└──────┬───────┘
       │
       ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│ Telegram     │───▶│ FIFO Queue   │───▶│ Worker Pool  │
│ Client       │    │ (max pending)│    │ (N workers)  │
└──────────────┘    └──────────────┘    └──────┬───────┘
                                               │
                                               ▼
                                        ┌──────────────┐
                                        │ UPI Runner   │
                                        │ 1.checkout   │
                                        │ 2.stripe init│
                                        │ 3.elements   │
                                        │ 4.confirm    │
                                        │ 5.approve    │
                                        └──────┬───────┘
                                               │
                                               ▼
                                        ┌──────────────┐
                                        │ QR PNG       │
                                        │ + watermark  │
                                        └──────┬───────┘
                                               │
                                               ▼
                                        ┌──────────────┐
                                        │ Send to user │
                                        │ via Telegram │
                                        └──────────────┘
```

## 📂 Module structure

```
rust_upi_bot/src/
├── main.rs                   # CLI args + bot bootstrap
├── http.rs                   # HTTP client wrapper (reqwest)
├── proxy_format.rs           # Proxy URL parsing/masking
├── auth.rs                   # session.json parsing
├── settings.rs               # SQLite Settings Store
├── stripe.rs, stripe_token.rs # Stripe API client
├── upi/                      # UPI payment flow
│   └── runner.rs             # 6-step UPI job runner
├── bot/
│   ├── telegram.rs           # Telegram Bot API client
│   ├── queue.rs              # FIFO queue + worker pool
│   ├── board.rs              # Job board (status tracking)
│   ├── dashboard.rs          # Realtime progress dashboard
│   ├── limiter.rs            # Per-user rate limit + cooldown
│   ├── registry.rs           # User registry
│   └── i18n.rs               # Multi-language strings
└── random_profile.rs         # India persona generator
```

## ⚖️ Disclaimer

This tool is for **educational and authorized testing only**. Users must comply with OpenAI Terms of Service, Telegram Bot API ToS, and local laws.
