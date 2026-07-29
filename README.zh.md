# 🇨🇳 gpt_signup_hybrid — 中文

> ChatGPT 自动注册流水线 + UPI 支付机器人。FastAPI + Camoufox + Rust。

[← 返回语言索引](./README.md)

---

## 💖 捐赠 / 赞助

| Method | Address |
|---|---|
| 🟡 **Binance ID** | `356552242` |
| 🟢 **USDT (BEP20)** | `0x137a3bfa30ee426127367773dfce16aefce04e02` |
| 🔴 **USDT (TRC20)** | `TFy5d1EDT4WBKgtoypx7Ua2dCZhPHMSDNs` |
| ✈️ **Telegram** | [@prr9293](https://t.me/prr9293) |
| 👥 **Telegram 群** | [t.me/+C6eafntO-Eo1Njdl](https://t.me/+C6eafntO-Eo1Njdl) |

---

## ⚠️ 必读 — 重要提示

> 🎯 **想要 ChatGPT Plus 优惠价（PPP 便宜 40-60%），登录代理必须出口在越南 (VN) 或日本 (JP)。**
>
> **如果你没有 VN/JP 登录代理**，也可以用 VPN 代替：
>
> - ✅ 在运行 tool 的设备（笔记本 / VPS / 服务器）上**安装带 VN 或 JP 服务器的 VPN**
> - ✅ VPN 启动后 → UI/CLI 里的登录代理字段**留空**，tool 会自动走 VPN 的出口 IP
> - ✅ 推荐 VPN 方案：
>   - 🛠️ 在 JP/VN VPS 上自建 **WireGuard / OpenVPN**（最便宜、最稳定）
>   - 💼 商用 VPN 含 JP/VN 节点 — Mullvad、ProtonVPN、NordVPN、Surfshark、ExpressVPN
>   - 🏠 住宅代理（Bright Data、Soax、NetNut）适合生产规模
>
> ⛔ 用美/欧 IP **没有** JP/VN VPN → Plus 价格贵 2-3 倍，UPI/GoPay 还会被地理屏蔽。
>
> 💡 **新手建议**：
> 1. 租日本 VPS（Vultr 东京约 5 美元/月）→ 直接在 VPS 上跑，不需要代理/VPN
> 2. 或在个人笔记本用 ProtonVPN 免费版（含免费日本节点）做测试

---

## 📖 项目介绍

`gpt_signup_hybrid` 是一个带本地 Web UI 的 ChatGPT 账号自动注册流水线，配套有 Rust 编写的 UPI 支付机器人。

### 核心功能

- 🎯 **混合注册** — Camoufox + curl_cffi 绕过检测
- 📧 **邮件提供商** — iCloud HME v3、Outlook 池、Gmail、自定义 Worker API
- 💳 **支付自动化** — ChatGPT Plus 结账、Stripe、GoPay/Midtrans、UPI
- 🔐 注册后**自动启用 MFA/TOTP**
- 🍎 **iCloud Hide My Email 池** — 自动生成邮箱 + profile 轮换
- 🔄 **AutoReg 循环** — HME → 账号 → MFA 全自动
- 🌐 **本地 Web UI**（FastAPI）实时 SSE 日志
- 🦀 **Rust UPI bot** — Telegram 机器人生成 UPI 二维码

### 3 种注册模式

| 模式 | 说明 |
|---|---|
| `pure_request` | 纯 HTTP，最快，仅 curl_cffi |
| `browser` | 完整 Camoufox 浏览器流程 |
| `hybrid` | **推荐** — Camoufox 做认证中继 + Python 重现字段 |

---

## 💰 UPI 功能详解

### 什么是 UPI？

**UPI (Unified Payments Interface)** 是印度的实时支付系统，由 NPCI 运营。支持通过移动 app（PhonePe、GPay、Paytm、BHIM）进行银行间即时转账。

**UPI 对 ChatGPT 的重要性？**

- 🇮🇳 ChatGPT Plus 印度区支持 **UPI 作为主要支付方式**（除 Visa/Mastercard 之外）
- 💸 由于 PPP 定价，ChatGPT Plus 印度区比美欧 **便宜 40-60%**
- 🎯 VPA 格式：`name@oksbi`、`9876543210@ybl`、`user@paytm`

### 本项目两套 UPI 系统

#### 1️⃣ Python `pay_upi_http.py` — 纯 HTTP UPI 流程

**用途**：通过纯 HTTP（不用浏览器）自动为一个 ChatGPT 账号生成 UPI 结账。

**流程**：
1. 通过 `email|pass|secret` 组合或 `session.json` 登录 ChatGPT
2. POST `/backend-api/payments/checkout` 创建结账会话
3. POST `api.stripe.com/v1/payment_pages/{id}/init` 初始化 Stripe
4. GET `api.stripe.com/v1/elements/sessions` 拿 elements 配置
5. POST `/confirm` 提交 UPI VPA
6. POST `/approve` 轮询直到用户完成支付

**特点**：
- ✅ 不用浏览器 → 轻量、快速
- ✅ curl_cffi 伪装 Chrome 145 Windows TLS
- ✅ 代理分流：登录 DIRECT（降低 captcha）、步骤 2+ 走印度代理
- ⚠️ Stripe `/confirm` 需要 3 个 JS-runtime token，脚本尽力提交
- 🎯 适合：手动测试、单/几个账号自动化

**用法**：

```bash
# 组合模式
python -m gpt_signup_hybrid.pay_upi_http \
  --combo 'email@example.com|password|totp_secret' \
  --vpa 'name@oksbi' \
  --proxy 'http://user:pass@india-proxy:port'

# Session JSON 模式
python -m gpt_signup_hybrid.pay_upi_http \
  --session ./session.json \
  --vpa '9876543210@ybl'
```

#### 2️⃣ Rust `rust_upi_bot/` — Telegram UPI QR 机器人

**用途**：Service-as-a-bot — 用户通过 Telegram 发送 `session.json`，机器人自动跑 UPI 流程并返回 QR PNG 供扫码支付。

**流程**：
1. 用户 `/start` → 机器人问候（多语言）
2. 用户上传 `session.json`（Telegram 文档）
3. 机器人解析 `access_token` + cookies
4. 任务进 FIFO 队列
5. Worker 池（默认 100 并发）有空时取任务
6. 跑 UPI 流程（与 Python 步骤 2-6 相同）
7. 渲染带 `@prr9293` 水印的 QR PNG
8. 通过 Telegram 发 QR 给用户 + 实时进度日志

**高级特性**：

| 特性 | 说明 |
|---|---|
| **FIFO 队列** | 硬上限（默认 50 pending）防 OOM |
| **每用户限制** | 默认 2 任务/用户，管理员可 `/set_user_limit @user n` 覆盖 |
| **冷却** | 同用户 2 任务间 10 秒，防垃圾 |
| **代理池** | 步骤 3 起轮换代理（登录 DIRECT） |
| **失败重启** | 连续 20 次 exception 后重启 checkout |
| **任务超时** | 1800s 硬超时 |
| **水印** | QR PNG 印 `@prr9293` |
| **管理员通知** | 其他用户成功生成 QR 时通知管理员 |
| **多语言** | i18n：EN/VI/CN/ID/HI |
| **设置持久化** | SQLite Settings Store，改 limit 不需重启 |

**用法**：

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

**Telegram 命令**：

```
/start                          — 问候
/help                           — 帮助
/status                         — 队列 + worker 状态
/lang <vi|en|zh|id|hi>          — 切换语言
/set_max_per_user <n>           — (管理员) 改每用户上限
/set_user_limit @user <n>       — (管理员) 单用户覆盖
/set_max_concurrent <n>         — (管理员) 改总并发
```

**部署目标**：

- 🐧 OpenWrt aarch64（路由器）— 二进制小，省 RAM
- 🐳 Docker（源码构建）
- ☁️ Linux x86_64 VPS — 最佳与主 app 共置

---

## 🐳 Docker 部署（主程序）

### 前置要求

- **Docker Desktop**（Windows/macOS）或 **Docker Engine + Compose**（Linux）
- 最低 **4GB 内存**（并发 ≥ 3 建议 8GB）
- 稳定网络
- **JP 或 VN 的 VPS** 获取 ChatGPT Plus 优惠

### 步骤 1 — 安装 Docker

```bash
# Linux Ubuntu/Debian
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker
docker --version
```

Windows/macOS: https://www.docker.com/products/docker-desktop/

### 步骤 2 — 克隆

```bash
git clone https://github.com/6c696e68/gpt_signup_hybrid.git
cd gpt_signup_hybrid
```

### 步骤 3 — 创建 `.env`

```bash
cp .env.docker.example .env
sed -i.bak "s/change-me-strong-random/$(openssl rand -hex 32)/" .env
```

### 步骤 4 — 构建并运行

```bash
docker compose build           # 首次 5-10 分钟
docker compose up -d
docker compose logs -f web
```

> 🟢 Apple Silicon → amd64 VPS: `docker buildx build --platform linux/amd64 -t gsh:latest --load .`

### 步骤 5 — 访问 UI

```bash
TOKEN=$(grep GPT_SIGNUP_WEB_TOKEN .env | cut -d= -f2)
echo "http://127.0.0.1:8083/?token=$TOKEN"
```

UI 标签：**Register · Session · Link · HME · AutoReg · Settings**

### 步骤 6 — 启用 iCloud HME runner（可选）

```bash
docker compose --profile hme up -d
```

### 常用命令

```bash
docker compose ps
docker compose logs -f web
docker compose restart web
docker compose down                                   # 保留 volume
docker compose down -v                                # 删数据
docker compose pull && docker compose up -d --build   # 更新
```

### 备份 / 恢复

```bash
# 备份
docker run --rm -v gpt_signup_hybrid_gsh-runtime:/data -v $(pwd):/backup \
  alpine tar czf /backup/runtime-backup-$(date +%Y%m%d).tar.gz -C /data .

# 恢复
docker run --rm -v gpt_signup_hybrid_gsh-runtime:/data -v $(pwd):/backup \
  alpine tar xzf /backup/runtime-backup-YYYYMMDD.tar.gz -C /data
```

---

## 🌏 VPN / VPS 获取 ChatGPT Plus 优惠

| 地区 | 说明 | 推荐 |
|---|---|---|
| 🇯🇵 **日本 (JP)** | 价格稳定 | **生产环境最佳** |
| 🇻🇳 **越南 (VN)** | 地区最便宜 | **测试最佳** |
| 🇮🇳 印度 | 便宜但仅 UPI | 需住宅代理 |
| 🇮🇩 印尼 | GoPay/Midtrans | 内置支持 |

### VPS 服务商

- **Vultr**: 东京/大阪
- **Linode/Akamai**: 东京
- **DigitalOcean**: 新加坡
- **越南 VPS**: Viettel IDC、FPT Cloud、BizflyCloud

最低配置: **2 vCPU / 4GB RAM / 40GB SSD / Ubuntu 22.04+**

### VPN 网关

- **WireGuard** 服务器在 JP/VN
- **OpenVPN** + JP/VN 网关
- **JP/VN 住宅代理**（Bright Data、Soax、NetNut）

UI 配置: **Settings → Proxies** 或在 `.env` 设置 `HYBRID_OUTLOOK_PROXY=http://...`。

---

## 🛠️ 手动安装（不用 Docker）

```bash
git clone https://github.com/6c696e68/gpt_signup_hybrid.git
cd gpt_signup_hybrid
bash setup.sh        # Linux/macOS
# setup.bat          # Windows
```

---

## 🔧 故障排查

| 错误 | 解决 |
|---|---|
| `GPT_SIGNUP_WEB_TOKEN required` | 重做步骤 3 |
| 容器 `unhealthy` | `docker compose logs web` 查 RAM |
| Web UI 空白 | URL 加 `?token=...` |
| 任务卡 `running` | 重启 web，查代理池 |
| Captcha/Turnstile 失败 | 换 **住宅代理** |
| UPI confirm 失败 | Stripe 需要 JS-runtime token，用 Rust bot 带浏览器阶段 |
| Apple Silicon 构建慢 | 直接在 amd64 VPS 构建 |

---

## 📁 项目结构

```
gpt_signup_hybrid/
├── cli.py, signup.py, browser_phase.py, request_phase.py    # 核心
├── session_phase.py, mfa_phase.py                           # Session + MFA
├── payment_link.py, pay_upi_http.py                         # 支付（Python UPI）
├── db/                                                      # SQLite + Settings
├── web/                                                     # FastAPI + UI
├── icloud_hme/                                              # iCloud HME 池
├── autoreg/                                                 # AutoReg 循环
├── rust_upi_bot/                                            # Rust UPI bot
└── test/, docs/                                             # 测试 + 文档
```
