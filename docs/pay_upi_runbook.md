# Runbook — `record_pay_upi` & `pay_upi_http`

Hướng dẫn chạy 2 entrypoint thanh toán UPI (India) trong repo. Hai script khác mục đích nhưng chia sẻ cùng cơ chế `_ProxyPolicy` để chuyển đổi điểm áp proxy bằng CLI flag — không sửa code.

---

## 1. Mục đích từng script

| Script | Mục đích | Browser? | Spam? |
|---|---|---|---|
| `record_pay_upi.py` | Hybrid: login HTTP + browser checkout. Dùng để **research / debug / record HAR** một thanh toán đơn lẻ với DOM thật + Stripe Payment Element. | ✓ Camoufox | ✗ |
| `pay_upi_http.py` | Pure-HTTP: login + Stripe confirm + ChatGPT approve, KHÔNG browser. Dùng để **spam N attempt** với `--sub`, đo lường rate/risk. | ✗ | ✓ (`--sub N`) |

`record_pay_upi` ghi đầy đủ: HAR full, trace.zip, actions.jsonl, requests.jsonl, console.jsonl, screenshots → phù hợp khi cần *bằng chứng* hoặc *reproduce step-by-step*.

`pay_upi_http` chạy nhanh, tiết kiệm tài nguyên, nhưng phụ thuộc Stripe token được extract live từ stripe.js (`js_checksum`, `rv_timestamp`) — không có browser nên `passive_captcha_token` không sinh được, Stripe có thể reject với risk score cao hơn.

---

## 2. Yêu cầu môi trường

```bash
# venv đã có sẵn
.venv/bin/python --version   # >= 3.10
```

Combo định dạng `email|password|totp_secret`:
- `email`: Outlook/Hotmail account đã bật 2FA
- `password`: mật khẩu account
- `totp_secret`: secret base32 OTP của 2FA (32 ký tự)

VPA (UPI Virtual Payment Address) định dạng `name@bank_handle`:
- `test@okhdfcbank` (HDFC)
- `test@okicici` (ICICI)
- `name@okaxis`, `name@oksbi`, `name@okpaytm`, …

---

## 3. Proxy India xoay

Hai script bắt buộc dùng proxy IP India khi áp ở các step liên quan checkout — Stripe + ChatGPT đối chiếu IP với billing country (`country=IN`). IP non-IN → block hoặc captcha hard.

Mẫu proxy xoay (rotating residential India) đã được kiểm thử với cả 2 script:

```
http://zp76579_2nhyyb:iMLh6AsVLahI3cwW_country-India@209.38.173.242:31112
```

Format chung:
```
http://<user>:<pass>_country-<COUNTRY>@<host>:<port>
```

- `country-India` là tham số rotating cho IP IN ngẫu nhiên trong pool.
- Mỗi connection sẽ rotate sang exit node IN khác → tránh IP bị reuse, giảm risk Stripe đánh dấu trùng.
- Nếu test rejection rate cao, đổi sang proxy provider khác (vd Bright Data IN, Soax IN residential).

> Cảnh báo: proxy datacenter (DC) thường bị Stripe block ngay. Phải dùng **residential** hoặc **mobile** IN.

---

## 4. Lệnh 1 — `record_pay_upi.py`

### Lệnh đầy đủ

```bash
.venv/bin/python -u -m gpt_signup_hybrid.record_pay_upi \
  --combo 'BovenSegers215@hotmail.com|Acbz1226666899@|W5JSNPYO7OK4LENQU44HO2BHPNSB5TIT' \
  --vpa 'test@okhdfcbank' \
  --proxy 'http://zp76579_2nhyyb:iMLh6AsVLahI3cwW_country-India@209.38.173.242:31112' \
  --no-auto-submit
```

### Flow 4 step

| Step | Hành động | Thực hiện ở | Default proxy |
|---|---|---|---|
| 1 | Login pure-HTTP qua `auth.openai.com` → `accessToken` + cookies | curl_cffi | DIRECT |
| 2 | `POST chatgpt.com/backend-api/payments/checkout` → `cs_live_xxx` | curl_cffi | DIRECT |
| 3 | Phase A — launch Camoufox DIRECT, inject cookies, verify `chatgpt.com/` | browser | DIRECT |
| 4 | Phase B — relaunch Camoufox VIA PROXY, navigate `https://chatgpt.com/checkout/openai_llc/cs_live_xxx`, fill billing IN + UPI VPA, click Subscribe | browser | **VIA PROXY** |

`--no-auto-submit` = fill xong thì giữ browser mở để bạn click Subscribe thủ công và quan sát.

### Vì sao split phase A/B

Browser (Camoufox / Playwright) **không hỗ trợ đổi proxy giữa session**. Để áp proxy chỉ từ điểm goto checkout URL trở đi:

1. Phase A: launch context A no-proxy → inject cookies HTTP login → goto `chatgpt.com/` (verify session OK + để server set/refresh cookies) → close.
2. Cookies + storage state persist trong `user_data_dir` (Firefox profile folder).
3. Phase B: relaunch context B với proxy → cookies tự load lại từ `user_data_dir` → goto thẳng checkout URL với proxy IN.

Khi `--proxy-from-step` ≤ 3 (proxy áp luôn từ verify hoặc trước đó) → KHÔNG cần split, dùng 1 context single với proxy ngay từ đầu. Logic auto-decide ở `run_hybrid()`:

```python
phaseA_proxy = policy.url_for(3)
phaseB_proxy = policy.url_for(4)
need_split_browser = phaseA_proxy != phaseB_proxy
```

### Output (artifact dir)

```
runtime/research_logs/pay_upi_<timestamp>_<email>/
├── trace.har               # Full HAR (request+response body embed)
├── trace.zip               # Playwright tracing (timeline + DOM snapshot)
├── actions.jsonl           # DOM events (click/input/change/submit)
├── requests.jsonl          # Mọi request browser gửi (kể cả qua iframe)
├── console.jsonl           # Console + page errors
├── checkout_url.txt        # URL checkout đã sinh
├── profile_billing.json    # Billing IN auto-gen
├── profile/                # Firefox user_data_dir (cookies, indexedDB)
└── screenshots/
    ├── 01_verified_phaseA.png       # khi split
    ├── 01_verified_single.png       # khi không split
    ├── 02_checkout_loaded.png
    ├── 03_upi_filled.png
    ├── 04_after_first_subscribe.png
    ├── 05_billing_filled.png
    └── 06_result_<approved|blocked|unknown>.png
```

Khi script dừng (success / Ctrl+C / exception) → tự in path artifact dir để bạn không phải tìm.

### Đổi điểm áp proxy

```bash
# Mặc định (default 4) — chỉ phase B navigate checkout URL via proxy
--proxy-from-step 4

# Login HTTP DIRECT, từ POST checkout HTTP trở đi via proxy
--proxy-from-step 2

# Toàn bộ via proxy (login + browser)
--proxy-from-step 1
```

---

## 5. Lệnh 2 — `pay_upi_http.py`

### Lệnh đầy đủ

```bash
.venv/bin/python -u -m gpt_signup_hybrid.pay_upi_http \
  --combo 'lerichearcano766@hotmail.com|Acbz1226666899@|HMKPXSHCHJDI4ZYGXS5WCF6635ORUOR3' \
  --vpa 'dpmgpt123' \
  --proxy 'http://zp76579_2nhyyb:iMLh6AsVLahI3cwW_country-India@209.38.173.242:31112' \
  --sub 100
```

### Flow 6 step

| Step | Hành động | URL | Default proxy |
|---|---|---|---|
| 1 | Login pure-HTTP | `auth.openai.com` | DIRECT |
| 2 | Create checkout | `POST chatgpt.com/backend-api/payments/checkout` | DIRECT |
| 3 | Stripe init (lần đầu thấy `cs_live_xxx`) | `POST api.stripe.com/v1/payment_pages/{cs}/init` | **VIA PROXY** |
| 4 | Stripe elements session | `GET api.stripe.com/v1/elements/sessions?…` | **VIA PROXY** |
| 5a | Token extract live (fetch `js.stripe.com/v3/` + `custom-checkout-<hash>.js`, parse `js_checksum`/`rv_timestamp` từ webpack chunks) | `js.stripe.com` | **VIA PROXY** |
| 5b | Stripe confirm UPI | `POST api.stripe.com/v1/payment_pages/{cs}/confirm` | **VIA PROXY** |
| 6 | ChatGPT approve | `POST chatgpt.com/backend-api/payments/checkout/approve` | **VIA PROXY** |

Default `--proxy-from-step 3` — step 1-2 DIRECT (login + tạo checkout chưa có `cs_live_xxx`), step 3-6 VIA PROXY (mọi request bind vào `cs_live_xxx` đều cần IP IN).

### Spam loop (`--sub N`)

Mỗi sub chạy `_attempt_subscribe`:
- `confirm` (step 5b) → `approve` (step 6).
- Login + checkout + Stripe init/elements + token extract chỉ chạy **1 lần đầu** (pre-loop).
- Token config được cache theo `entry_hash` của stripe.js → tái sử dụng giữa các sub (idempotent).

```bash
--sub 100                       # spam 100 sub
--sub-delay-min 1.0             # delay tối thiểu giữa các sub (rate-limit safety)
--sub-delay-max 3.0             # delay tối đa
--sub-rotate-billing            # mỗi sub random billing IN khác (giảm dup signal)
--sub-rotate-stripe 5           # mỗi 5 sub re-init Stripe (làm mới init_checksum)
--sub-no-stop-on-approve        # KHÔNG dừng khi gặp approve, chạy đủ N lần
```

### Retry network/timeout (default 3 attempts)

Hai loại lỗi được phân biệt:

1. **PayUpiError (server reject)** — HTTP 4xx/5xx có body JSON với `error.code`. Server đã có ý kiến, retry vô nghĩa. Script trả về `stage="stripe_confirm"`, đếm vào `error`.
2. **Network/timeout** — `curl_cffi.CurlError`, `Timeout`, `OSError`, `ConnectionError`, … Server chưa thấy yêu cầu. Retry với linear backoff (`backoff * i`).

```python
# pay_upi_http._retry_call
for i in range(1, max_attempts + 1):
    try:
        return await coro_factory()
    except PayUpiError:
        raise                 # server reject → propagate
    except Exception as exc:
        # network → retry with backoff
        await asyncio.sleep(backoff * i)
```

Hết retry → trả `stage="stripe_confirm_network"` hoặc `stage="approve_network"` → spam loop **tiếp tục**, KHÔNG crash.

```bash
--retry-attempts 5              # retry 5 lần thay vì 3
--retry-backoff 1.5             # backoff 1.5s, 3s, 4.5s, 6s, 7.5s
```

Output sample khi network fail:
```
⚠ approve attempt 1/3 failed: Timeout: curl: (28) ...
└─ retry sau 2.0s…
⚠ approve attempt 2/3 failed: Timeout: ...
└─ retry sau 4.0s…
⚠ approve attempt 3/3 failed: Timeout: ...
└─ NETWORK ERROR (approve)  Timeout: ...  29.5s — tiếp tục loop
```

### Đổi điểm áp proxy

```bash
# Mặc định (default 3) — step 3-6 via proxy
--proxy-from-step 3

# Chỉ confirm + approve via proxy (token extract từ js.stripe.com đi DIRECT)
--proxy-from-step 5

# Chỉ approve via proxy
--proxy-from-step 6

# Toàn bộ via proxy
--proxy-from-step 1
```

### Output

`pay_upi_http` không ghi artifact. Dùng `--output result.json` để dump kết quả:

```bash
--output runtime/spam_results/run_$(date +%s).json
```

Cấu trúc result.json:
```json
{
  "ok": true,
  "stage": "spam_summary",
  "result": "approved",
  "checkout_session_id": "cs_live_xxx",
  "sub_count": 100,
  "counts": {"approved": 7, "blocked": 89, "error": 4, "other": 0},
  "approved_at": 23,
  "history": [...],
  "token_config": {...}
}
```

---

## 6. `_ProxyPolicy` — single point of control

Class chung ở `pay_upi_http.py`, `record_pay_upi` import lại. Quyết định mỗi step có dùng proxy hay không.

```python
class _ProxyPolicy:
    def __init__(self, proxy: str | None, from_step: int = 1):
        self.proxy_url = proxy
        self.from_step = from_step
        self._proxy_dict = {"http": proxy, "https": proxy} if proxy else None

    def url_for(self, step: int) -> str | None:
        return self.proxy_url if (self.proxy_url and step >= self.from_step) else None

    def dict_for(self, step: int) -> dict | None:
        return self._proxy_dict if (self._proxy_dict and step >= self.from_step) else None
```

Mỗi helper HTTP nhận `proxies=` kwarg per-request (curl_cffi hỗ trợ). Caller compute:

```python
policy = _ProxyPolicy(proxy, from_step=args.proxy_from_step)
chatgpt = await _create_chatgpt_checkout(sess, ..., proxies=policy.dict_for(2))
init = await _stripe_init(sess, ..., proxies=policy.dict_for(3))
elements = await _stripe_elements_session(sess, ..., proxies=policy.dict_for(4))
token_cfg = await extract_config_live(sess, ..., proxies=policy.dict_for(5))
confirm = await _stripe_confirm_upi(sess, ..., proxies=policy.dict_for(5))
approve = await _chatgpt_approve(sess, ..., proxies=policy.dict_for(6))
```

`record_pay_upi` thêm 1 abstraction: browser launch chỉ check `policy.url_for(3)` và `policy.url_for(4)`. Nếu khác nhau → split phase A/B; nếu giống → 1 context.

Default mapping bảo đảm "áp proxy bắt đầu từ điểm thấy `cs_live_xxx`" theo URL:

| File | Default `--proxy-from-step` | Step DIRECT | Step VIA PROXY |
|---|---|---|---|
| `pay_upi_http` | 3 | 1, 2 | 3, 4, 5, 6 |
| `record_pay_upi` | 4 | 1, 2, 3 (phase A) | 4 (phase B) |

---

## 7. Stripe token extract (step 5a của `pay_upi_http`)

Stripe checkout `confirm` cần 3 token:
- `js_checksum` — sinh từ `caesar_shift + xor5 + base64` của `ppage_id`. **Có thể tái tạo** bằng cách extract `shift` constant từ `custom-checkout-<hash>.js`.
- `rv_timestamp` — `xor5 + base64` của `Date.now()` với key `rv` constant. **Có thể tái tạo**.
- `passive_captcha_token` — sinh runtime từ obfuscated stripe.js builder. **Không thể tái tạo ngoài browser** → script gửi `null`, Stripe đôi khi accept với risk score cao (rate ~5-10% pass).

Code: `gpt_signup_hybrid.stripe_token`:
1. `fetch_bundles_live(sess)` GET `https://js.stripe.com/v3/`, parse webpack chunk map → resolve `custom-checkout-<hash>.js` URL → fetch chunk JS.
2. `extract_config(cc_src)` parse 3 constant: `shift`, `rv`, `sv`.
3. Cache theo SHA256 của entry stripe.js → 1 lần extract dùng cho mọi attempt cùng version Stripe.
4. `compute_js_checksum(ppage_id, shift)` + `compute_rv_timestamp()` ở `_stripe_confirm_upi`.

Khi Stripe deploy bundle mới → SHA256 entry đổi → cache miss → re-extract tự động. Nếu webpack format đổi → parse fail → fallback đọc bundle từ `runtime/cache/stripe_bundles_default/` (HAR dump cũ).

---

## 8. So sánh 2 script

| | `record_pay_upi` | `pay_upi_http` |
|---|---|---|
| Browser | Camoufox (Firefox-based, anti-detect) | ❌ |
| Tốc độ 1 attempt | ~30-60s | ~3-8s |
| Stripe `passive_captcha_token` | ✓ (browser sinh) | ✗ (null) |
| Risk score Stripe | Thấp | Cao hơn |
| Approval rate (estimate) | ~30-50% | ~5-15% |
| Spam | ✗ | ✓ `--sub N` |
| HAR full | ✓ | ✗ |
| Trace.zip + screenshots | ✓ | ✗ |
| Phù hợp khi | Cần debug, record HAR thật, test 1 lần | Spam số lượng lớn, đo rate |

Workflow đề nghị:
1. Bắt đầu với `record_pay_upi --no-auto-submit` để lấy HAR thật + verify proxy + billing OK.
2. Nếu OK → chuyển sang `pay_upi_http --sub 100 --sub-rotate-billing` để spam tốc độ cao.

---

## 9. Troubleshooting

### `ConfirmExecutor [bad_request]: Your card was declined`
Stripe reject vì VPA không hợp lệ hoặc bank không support. Đổi VPA bank handle khác (`@okicici`, `@okhdfcbank`).

### `Sentinel block` (response từ ChatGPT trả `result: blocked`)
- IP proxy đã bị flag → đổi proxy mới.
- Account login từ IP non-IN, thanh toán từ IP IN → mismatch. Dùng `--proxy-from-step 1` để toàn bộ flow đi cùng IP IN.

### `Timeout: Failed to perform, curl: (28)`
Proxy upstream chậm hoặc rớt. `pay_upi_http` đã có retry (`--retry-attempts 3`) — sẽ tự retry. Nếu vẫn fail, thử proxy khác.

### Stripe webpack parse fail (`không parse được webpack chunk map`)
Stripe đã đổi format webpack. Chạy script extract bundle thủ công vào `runtime/cache/stripe_bundles_default/` rồi rerun (script tự fallback).

### Camoufox treo (record_pay_upi không thấy DOM)
- `runtime/research_logs/pay_upi_*/screenshots/` xem state lúc treo.
- Kiểm tra `console.jsonl` có error JS không.
- Restart kill process, xóa `profile/` (mất cookies → sẽ login HTTP lại).

### Default `--proxy-from-step` không phù hợp
Override bằng flag — không sửa code:
- Toàn bộ DIRECT (debug local): `--proxy-from-step 99` (vì >6 nên không có step nào ≥) — tuy nhiên CLI giới hạn 1-6/1-4. Cách thật: bỏ flag `--proxy`.
- Toàn bộ via proxy: `--proxy-from-step 1`.

---

## 10. Quick reference

```bash
# Hybrid browser, default split-after-verify
.venv/bin/python -u -m gpt_signup_hybrid.record_pay_upi \
  --combo 'EMAIL|PASS|TOTP_SECRET' \
  --vpa 'name@okbank' \
  --proxy 'http://USER:PASS_country-India@HOST:PORT' \
  --no-auto-submit

# Pure HTTP spam, default proxy-from-step=3
.venv/bin/python -u -m gpt_signup_hybrid.pay_upi_http \
  --combo 'EMAIL|PASS|TOTP_SECRET' \
  --vpa 'name@okbank' \
  --proxy 'http://USER:PASS_country-India@HOST:PORT' \
  --sub 100 \
  --sub-rotate-billing \
  --sub-rotate-stripe 5 \
  --output runtime/spam_results/run.json
```

Mọi sửa đổi proxy → chỉ thay `--proxy-from-step N`, không sửa code.
