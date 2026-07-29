# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), [SemVer](https://semver.org/).

## [3.6.0] — 2026-06-29

### Added — Help modal + Telegram contact saturation + multi-language docs

#### Help & Guide modal (UI)
- New **Help** button in topbar opens a self-contained modal (`#help-modal`) with friendly English guide.
- Sections: Welcome, Quick Start (3 steps), Tab Guide, Mail Modes, Reg Modes, Settings (Proxies/Telegram/Tunnel), UPI flow walkthrough, Troubleshooting (captcha/409/blocked/OTP/401), Contact card.
- Modal reuses `.modal`/`.modal-content` pattern, scrollable body, 820px max width, 90vh height, Escape/overlay/close-button dismiss, focus restore.
- Inline IIFE handles open/close, prevents body scroll while open.
- Responsive: full-screen on mobile, label-only icon on narrow header.

#### Telegram contact saturation (UI)
- New `.brand-telegram` pill next to "Batch signup + 2FA" in topbar — gradient + hover lift, auto-collapses on mobile.
- New `.card-head-tg` inline badge in card heads of **Reg → Input Combos**, **Get Session → Input**, **UPI QR → Input** — paper-plane icon + @prr9293, hides label <700px.
- New floating **`.telegram-fab`** (bottom-right, z=9000) — always-visible Telegram contact button across every tab/scroll position, gradient + glow, collapses to icon-only ≤600px.
- Browser tab title now reads `GSH Tool · @prr9293 (Telegram)`.
- Session and UPI textarea placeholders prepended with `💬 Need iCloud mail / support? Contact @prr9293 on Telegram`.
- iCloud v3 + iCloud Worker mail mode placeholders + help text rewritten in English: `Contact @prr9293 on Telegram (https://t.me/prr9293) to buy iCloud mail`.
- Settings → Telegram section gains a contact hint linking `@prr9293` and the community group.

#### Multi-language READMEs
- `README.md` (root) replaced with a short index page: donation block, language picker, sub-system list, TL;DR one-line Docker bootstrap.
- New per-language files: `README.vi.md` (Vietnamese), `README.en.md` (English), `README.zh.md` (Chinese), `README.id.md` (Indonesian), `README.hi.md` (Hindi).
- Each language README contains full guide: detailed UPI feature explanation (Python `pay_upi_http.py` + Rust `rust_upi_bot/`), Docker step-by-step, VPN/VPS for JP/VN ChatGPT Plus discount, manual install, troubleshooting, project structure.
- Prominent "READ FIRST" notice on top of every language README: login proxy MUST exit VN/JP, otherwise install JP/VN VPN on the host running the tool, with beginner tip (Vultr Tokyo VPS / ProtonVPN free Japan node).
- New `rust_upi_bot/README.md` — dedicated sub-system README with 5 language sections, architecture diagram, OpenWrt aarch64 cross-compile instructions, Telegram command list.

### Changed
- Donation block (Binance ID, BEP20, TRC20, Telegram DM + group) elevated to the top of the root README.

### Test
- No new tests in this release — UI-only and documentation changes.

## [3.5.0] — 2026-06-27

### Added — Hybrid registration pipeline + Camoufox relay (Phase 10-11)

#### OpenAI sentinel sidecar + K2/K2c integration (Phase 10)
- `sentinel_sidecar.py` — QuickJS K2/K2c executor chạy trong Camoufox process cô lập.
- `sentinel_browser.py` — bridge layer `page.evaluate` → sidecar RPC.
- `openai_sentinel_in_page.js` — K2/K2c wire format + proof validation logic.
- `SIDECAR_SHARED_PROXY` env — share một Camoufox instance per proxy key để giảm RAM khi chạy concurrent signup.
- `_submit_otp` refactor: human-like typing + `expect_response` wrapper, trả về `(continue_url, source)` distinguishing UI vs API submission paths.
- HAR-aligned OTP form (per-char typing + Enter key) khớp với record tay.

#### Hybrid registration mode + Camoufox relay (Phase 11)
- Mode `hybrid` (alongside `pure_request` + `browser`): Camoufox (Firefox-shaped) làm ChatGPT auth relay, reproduce field bằng pure Python.
- `CamoufoxTokenGenerator` — JS harness chạy sentinel SDK trong real browser để mint sentinel token (`t`) + session-observer token (`so`).
- Browser pool factory cho concurrent Camoufox instance với connection reuse.
- OTP acquisition loop tích hợp mail provider adapter (iCloud, Gmail, custom API).
- Sentinel TTL tracking, thread-affinity check, quickwin performance validator.
- CLI flag `--reg-mode hybrid` + cấu hình locale/platform/proxy cho Camoufox relay.
- Web UI và API routes hỗ trợ chọn `reg_mode` (`browser` | `pure_request` | `hybrid`).
- Bump default concurrency 1 → 3 (3 signup song song trên 1 Camoufox process).

#### iCloud v3 provider + Worker v2 relay
- `IcloudV3Provider` (mail_providers.py): mailbox-specific URL binding, không cần auth token.
- OTP extraction từ iCloud v3 Worker endpoint (`/readmail/<token>/data`).
- CLI flag `--icloud-v3 <email|api_url>` với auto-detect provider + auto-derive email khi `--email` không truyền.
- `SignupRequest.icloud_v3_url` (model field + regex validation).
- Provider factory `build_provider_icloud_v3()` + auto-detect order: `icloud_v3` > `outlook` > `worker`.
- Web UI: render iCloud v3 input fields + provider toggle (`web/mail_modes.py`, `web/manager.py`, `web/server.py`, `web/static/app.js`).

#### OTP resend HAR alignment + UI success fanfare
- `HybridChatGPTRelay._resend_otp()` đổi `/resend` → `/send` (golden HAR path) — bỏ fallback POST, dùng GET `/email-otp/send` consistent với `otp_send()`.
- `playSuccessAlert()` (web/static/app.js): 5-note ascending fanfare + chord finale khi job transition sang `success`. Expose qua `window.GptUi`.
- Bỏ `openQrModal()` redundant call ở job render (giữ copyQrToClipboard flow).

### Schema
- Không thay đổi schema (vẫn v12 từ 3.2.0).

### Test
- 30+ test/check_*.py mới phủ Phase 10-11:
  - `check_hybrid_*.py` (perf, sentinel TTL, so observer, thread affinity, opt quickwins).
  - `check_k2_*.py` (leak defenses, K2 results, sidecar pure-HTTP).
  - `check_sentinel_token_source.py` — trace nguồn sinh token.
  - `check_sidecar_proxy_decouple.py` — proxy sharing isolation.
  - `check_har_signup_deep.py`, `check_otp_continue_url.py`, `check_password_create_timing.py` — HAR alignment.
  - `smoke_hybrid_*.py` — asyncio safety, MFA inline, OTP loop, full reg flow.
- iCloud v3: `check_icloud_v3.py`, `check_icloud_v3_fetch.py`, `check_cli_icloud_v3.py`, `smoke_reg_icloud_v3.py`, `syntax_check_icloud_v3.py`.
- `smoke_loadable.py` — verify mọi provider import load không error.

### Operational notes
- Hybrid mode khuyến nghị cho production: tận dụng Camoufox anti-detect + tốc độ HTTP layer.
- Sentinel sidecar share Camoufox per proxy giảm RAM ~60% khi run >5 concurrent.
- CAPTCHA/Turnstile vẫn cần proxy residential India (datacenter ban dù code perfect).

## [3.2.0] — 2026-06-25

### Added — REG anti-ban master suite (Phase 1-9)

Reference: `docs/journals/260625-1224-reg-anti-ban-master-plan.md`

#### Foundation (Phase 1)
- `_geo_locale.py` — proxy IP → locale/timezone/geolocation auto-detect (top 15 country mapping).
- `random_profile_for_locale()` — name pool theo locale (en-IN → tên Ấn, en-US → tên Anglo).
- Settings Store: 6 keys mới (`reg.persona`, `reg.fresh_profile`, `reg.har_validate`, `reg.human_typing_delay_ms_min/max`, `reg.locale_auto_geo`).
- Helpers `read_oai_asli_from_ctx` + `read_oai_asli_from_session` đọc cookie cho `auth_session_logging_id`.

#### Browser anti-detection (Phase 2)
- `_human_input.py` — `human_type` (Gaussian 120-260ms + 8% pause), `human_click` (mousemove → jitter → click), `random_mouse_wander`, `dwell` jitter.
- State machine `password_create` chuyển sang form UI thật + `expect_response` capture.
- Mouse wander + dwell jitter ở 4 state transition critical.

#### Persona + cookie chain (Phase 3)
- `BrowserPersona` dataclass + 2 instance: `CHROME_145_WIN`, `FIREFOX_135_MAC`.
- Sentinel persona forwarding (`sentinel_quickjs` + `sentinel_pow` accept persona arg).
- `_datadog_session.py` — `_dd_s` Datadog RUM cookie generator + injector.
- Schema v12: `outlook_combos.persona_cookies` JSON column + `ComboRepository.{get,set}_persona_cookies`.

#### Pure_request optimize (Phase 4)
- `_navigate_headers` helper (page navigate Sec-Fetch-Mode).
- `_step_send_otp` đổi sang Sec-Fetch-Mode=navigate + follow 302.
- `_common_headers(persona=...)` persona-aware (Chrome có sec-ch-ua, Firefox không).
- `_step_auth_url` đọc cookie `oai-asli` cho query `auth_session_logging_id`.
- Visit `/email-verification` HTML thay `/create-account/password` XHR.

#### HAR alignment validation (Phase 5)
- `test/check_har_alignment.py` — 5 invariants × 19 sub-checks, jq-based pre-extract.
- CLI flag `--har-validate` + `_run_har_alignment_validate` post-reg auto-run.
- GitHub Actions workflow `.github/workflows/anti-ban-suite.yml` — trigger PR.

#### Closure + cleanup (Phase 6-7)
- `signup.py:run_signup` save persona_cookies sau signup successful (whitelist 7 cookies).
- `session_phase.py` locale auto-detect (Camoufox + Chrome runner).
- `session_phase.py` anti409 flow inject `_dd_s` Datadog cookie.
- Migration v11→v12 zero-data-loss verified.
- Removed dead code: `_step_signup`, `_step_register_password`, `passwordless/send-otp` fallback.
- CLI flag `--persona` (default `firefox_mac`) + `SignupRequest.persona` field.
- Runtime warning khi `reg_mode=pure_request` về so-token missing.

#### HAR audit gap fix (Phase 8)
- `_step_providers` — GET `/api/auth/providers` TRƯỚC csrf (browser thật làm vậy, ~337ms gap). Fix gap detect được khi audit HAR golden.

#### Camoufox anti-detect hardening (Phase 9)
- `block_webrtc=True` — chặn WebRTC mDNS IP leak khi dùng proxy.
- `humanize=True` — Camoufox native mouse jitter.
- `locale=list[str]` — pass `["en-IN", "en"]` để navigator.languages khớp record tay.

### Fixed
- 4 chỗ hardcode `Accept-Language: en-US,en;q=0.9` + `sec-ch-ua*` trong `request_phase.py` thay bằng `_navigate_headers()` persona-aware (`_prime_chatgpt_session`, `_step_oauth_init`, `_step_follow_redirects`, `_consume_callback`).
- `auth_session_logging_id` được đọc từ cookie `oai-asli` thay vì gen UUID mới (fix 3 chỗ: `browser_phase`, `session_phase` async + sync anti409).
- `profile_template` default = False (fresh profile mỗi reg để tránh CF cookie cluster ban).
- `_register_with_password` + `_PAGE_CREATE_ACCOUNT_JS` evaluate bypass form removed (so-token cần DOM events thật).
- Runtime bug: `NameError: 'settings' is not defined` trong state machine password_create (smoke test).
- Runtime bug: `NameError: 'logging_id' is not defined` outer scope `run_browser_phase` — dùng `logging_id_holder` nonlocal closure.

### Schema
- v11→v12: ALTER TABLE `outlook_combos` ADD COLUMN `persona_cookies TEXT`.

### Test
- 16 test/check_*.py mới (Phase 1-8 coverage).
- HAR alignment self-test 19/19 invariants PASS.
- Migration v11→v12 zero-loss test.
- Suite: `bash test/run_phase1_suite.sh` → PASS=16/16.

### Documentation
- `docs/journals/260625-1224-reg-anti-ban-master-plan.md` — full master plan.
- `test/golden_records/README.md` — golden HAR documentation.

### Operational notes
- Anti-detect hoàn chỉnh: Camoufox-Firefox 135 Mac persona, sentinel SDK in-page sinh so-token đầy đủ.
- Production REG cần proxy residential India (datacenter sẽ ban dù code perfect).
- CAPTCHA/Turnstile auto-solve defer (cần 3rd party API).
- Headless trên server không display khuyến nghị `xvfb-run`.

## [3.0.1] — earlier release

(see git log)

## [3.0.0] — earlier release

(see git log)
