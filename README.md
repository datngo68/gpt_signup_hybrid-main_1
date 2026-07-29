<!-- =====================================================================
  gpt_signup_hybrid — Multi-language README index
  ===================================================================== -->

<div align="center">

# 🤖 gpt_signup_hybrid

**Automated ChatGPT signup pipeline · FastAPI + Camoufox + SQLite · UPI bot**

[![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/)
[![Rust 1.78+](https://img.shields.io/badge/rust-1.78%2B-orange.svg?logo=rust)](https://www.rust-lang.org/)
[![Docker Ready](https://img.shields.io/badge/docker-ready-2496ED.svg?logo=docker)](https://www.docker.com/)
[![Version](https://img.shields.io/badge/version-3.6.0-green.svg)](./CHANGELOG.md)

</div>

---

## 💖 Donate / Support · 捐赠 · Dukungan · दान

| Method | Address |
|---|---|
| 🟡 **Binance ID** | `356552242` |
| 🟢 **USDT (BEP20)** | `0x137a3bfa30ee426127367773dfce16aefce04e02` |
| 🔴 **USDT (TRC20)** | `TFy5d1EDT4WBKgtoypx7Ua2dCZhPHMSDNs` |
| ✈️ **Telegram** | [@prr9293](https://t.me/prr9293) |
| 👥 **Telegram Group** | [t.me/+C6eafntO-Eo1Njdl](https://t.me/+C6eafntO-Eo1Njdl) |

---

## ⚠️ READ FIRST — IMPORTANT NOTICE · ĐỌC TRƯỚC · 必读 · BACA DULU · पहले पढ़ें

> 🎯 **To get ChatGPT Plus discounted pricing (PPP 40-60% off), your login proxy MUST exit from Vietnam (VN) or Japan (JP).**
>
> **If you DO NOT have a VN/JP proxy for login**, you can use a **VPN on the device running the tool** instead:
>
> - ✅ **Install a VPN with VN or JP server** on your laptop / VPS / server running this tool
> - ✅ Keep the login proxy field **empty** in UI/CLI — the tool will route via your VPN's exit IP
> - ✅ Recommended VPN options:
>   - 🛠️ Self-hosted **WireGuard / OpenVPN** at a JP/VN VPS
>   - 💼 Commercial VPN with JP/VN nodes — Mullvad, ProtonVPN, NordVPN, Surfshark, ExpressVPN
>   - 🏠 Residential proxies (Bright Data, Soax, NetNut) for production scale
>
> ⛔ Running from a US/EU IP **without** VN/JP VPN → Plus price is 2-3× higher, or UPI/GoPay geo-blocked.
>
> 🇻🇳 **Tiếng Việt**: Không có proxy VN cho login? → Cài VPN VN hoặc Japan trên thiết bị chạy tool (laptop/VPS) để nhận ưu đãi.
> 🇨🇳 **中文**：没有 VN 代理用于登录？→ 在运行 tool 的设备（笔记本/VPS）上装 VN 或 Japan VPN 即可获得优惠。
> 🇮🇩 **ID**: Tidak punya proxy VN untuk login? → Pasang VPN VN atau Jepang di perangkat yang menjalankan tool untuk dapat diskon.
> 🇮🇳 **हिन्दी**: Login के लिए VN proxy नहीं है? → Tool चलाने वाली device पर VN या Japan VPN install करें discount पाने के लिए।

---

## 🌐 Choose Your Language · 选择语言 · Pilih Bahasa · भाषा चुनें

| Language | File | Description |
|---|---|---|
| 🇻🇳 Tiếng Việt | [`README.vi.md`](./README.vi.md) | Hướng dẫn đầy đủ tiếng Việt |
| 🇬🇧 English | [`README.en.md`](./README.en.md) | Full English documentation |
| 🇨🇳 中文 | [`README.zh.md`](./README.zh.md) | 完整中文文档 |
| 🇮🇩 Bahasa Indonesia | [`README.id.md`](./README.id.md) | Dokumentasi lengkap |
| 🇮🇳 हिन्दी / Hindi | [`README.hi.md`](./README.hi.md) | पूरी हिंदी documentation |

## 📦 Sub-systems

| System | README | Purpose |
|---|---|---|
| **Main Python app** | This file + language READMEs | ChatGPT signup pipeline + Web UI |
| **Rust UPI bot** | [`rust_upi_bot/README.md`](./rust_upi_bot/README.md) | Telegram bot — UPI QR generator |

---

## ⚡ TL;DR — One-line Docker Start

```bash
git clone https://github.com/6c696e68/gpt_signup_hybrid.git && cd gpt_signup_hybrid \
  && cp .env.docker.example .env \
  && sed -i.bak "s/change-me-strong-random/$(openssl rand -hex 32)/" .env \
  && docker compose up -d \
  && echo "Open: http://127.0.0.1:8083/?token=$(grep GPT_SIGNUP_WEB_TOKEN .env | cut -d= -f2)"
```

> ⚠️ **Important**: Run on **VPS in Japan (JP) or Vietnam (VN)** to get **ChatGPT Plus discounted pricing**. Other regions are more expensive or geo-blocked.

## �️ What's inside?

- 🎯 **Automated ChatGPT signup** — `pure_request` / `browser` / `hybrid` mode (Camoufox + curl_cffi)
- 📧 **Mail providers** — iCloud HME v3, Outlook pool, Gmail, custom Worker
- 💳 **Payment automation** — ChatGPT Plus checkout, Stripe, GoPay/Midtrans, **UPI (India)**
- 🔐 **Auto MFA/TOTP** after signup
- 🍎 **iCloud HME pool** — auto-generate + rotate profiles
- 🔄 **AutoReg loop** — HME → account → MFA pipeline
- 🌐 **Local Web UI** — FastAPI + realtime SSE
- 🦀 **Rust UPI bot** — Telegram service-as-a-bot for UPI QR generation

## 📚 Resources

- 📖 [`docs/`](./docs/) — Detailed documentation
- 🏗️ [`.planning/codebase/ARCHITECTURE.md`](./.planning/codebase/ARCHITECTURE.md) — Architecture deep-dive
- 📋 [`CHANGELOG.md`](./CHANGELOG.md) — Version history
- 🤖 [`AGENTS.md`](./AGENTS.md) — AI agent (Claude/Codex/Kiro) guide

## ⚖️ Disclaimer

This tool is for **educational and authorized testing purposes only**. Users are responsible for complying with OpenAI Terms of Service and local laws. The author is not responsible for misuse.

---

<div align="center">

**Made with ❤️ · Star ⭐ if useful · [Telegram Group](https://t.me/+C6eafntO-Eo1Njdl)**

</div>
