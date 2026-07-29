# Thay đổi 2026-06-20 — CF warm-up + chrome142 ưu tiên (UPI approve)

## Bối cảnh

Approve loop của UPI QR runner liên tục dính pattern:

```
attempt N    ✗ http=403   —          proxy=<new_proxy>     ← hit 1 của proxy mới
attempt N+1  ⊘ http=200   blocked    proxy=<new_proxy>
attempt N+2  ⊘ http=200   blocked    proxy=<new_proxy>
```

Hai nguyên nhân khác nhau:

1. **HTTP 403 hit đầu mỗi proxy mới** = Cloudflare Bot Management ném challenge
   vì IP mới + chưa có cookie `__cf_bm`/`cf_clearance`.
2. **HTTP 200 `result=blocked`** = backend ChatGPT từ chối approve do risk score
   của IP datacenter VN thấp. Cookie CF không cứu được, phải đổi tier proxy.

Hai thay đổi dưới đây nhắm **vào (1)**: giảm noise 403 + giảm xác suất CF challenge
trên TLS hello mới. Không xử lý (2) — vẫn cần residential/IN proxy hoặc mark-dead.

## Phạm vi thay đổi

Chỉ trong `web/upi_runner.py`. KHÔNG động `user_agent_profile.py` global vì
sẽ ảnh hưởng signup/MFA/check_session (đã verify chrome145 OK ở các flow đó).

### 1. CF cookie warm-up — `[FIX-CF-WARMUP-2026-06-20]`

**Thêm:** hàm `_warm_cf_cookie(sess, *, proxies, log)` — best-effort GET
`https://chatgpt.com/` để CF set `__cf_bm` vào cookie jar của AsyncSession.

**Call site:** trong approve while-loop, ngay sau khi materialize proxy của
batch mới (`if batch_idx not in _approve_mat_cache:`). Cookie warm chạy đúng
1 lần/batch, request approve kế tiếp đã có cookie → không bị 403 hit đầu.

**Best-effort:** mọi exception/non-200 đều nuốt + log warn, KHÔNG fail approve
loop. Ngay cả khi warm-up fail, approve vẫn thử bình thường.

**Revert:**

```diff
-                if batch_idx not in _approve_mat_cache:
-                    _approve_mat_cache[batch_idx] = _safe_materialize(raw_approve_proxy)
-                    # [FIX-CF-WARMUP-2026-06-20] proxy mới → warm CF cookie...
-                    await _warm_cf_cookie(
-                        sess,
-                        proxies=_proxy_dict(_approve_mat_cache[batch_idx]),
-                        log=_safe_log,
-                    )
+                if batch_idx not in _approve_mat_cache:
+                    _approve_mat_cache[batch_idx] = _safe_materialize(raw_approve_proxy)
```

Và xoá toàn bộ hàm `_warm_cf_cookie` (block bao quanh comment marker
`[FIX-CF-WARMUP-2026-06-20]`).

### 2. Chrome142 ưu tiên — `[FIX-CHROME142-FIRST-2026-06-20]`

**Đổi:** `_IMPERSONATE_CHAIN` trong `web/upi_runner.py` từ giá trị global
`("chrome145", "chrome142", "chrome136")` (import từ `user_agent_profile.
CURL_IMPERSONATE_CANDIDATES`) → local override `("chrome142", "chrome145",
"chrome136")`.

**Tác dụng:** `_RotatingSession.__aenter__` khởi tạo BoringSSL context với
`chrome142` thay vì `chrome145`. CF rule với chrome145 (TLS hello mới hơn,
Y2025 deployments) đôi khi soi chặt hơn.

**Fallback giữ nguyên:** TLS error → rotate sang `chrome145` → `chrome136`.
Không mất khả năng phục hồi.

**Revert:** restore import + xoá block local:

```python
from user_agent_profile import (
    CURL_IMPERSONATE_PRIMARY as _IMPERSONATE,
    CURL_IMPERSONATE_CANDIDATES as _IMPERSONATE_CHAIN,
)
```

## Lý do KHÔNG đổi `OAI-Language: en-IN` → `en-US`

User đề xuất đổi `OAI-Language` match locale proxy. Đã quyết KHÔNG làm vì:

- Account UPI luôn dùng India profile (`random_india_profile`, billing
  `country: IN, currency: INR, timezone: Asia/Kolkata`).
- Toàn bộ checkout payload (Stripe + ChatGPT) đã đồng bộ `en-IN`/`Asia/Kolkata`
  (`pay_upi_http.py` step 2-6).
- Đổi `OAI-Language` rời rạc sang `en-US` sẽ tạo mismatch mới giữa header và
  payload → risk engine ChatGPT/Stripe nghi ngờ thêm, không phải bớt.
- Cách đúng: dùng **proxy India** (residential/datacenter IN reputation tốt)
  thay vì VN, không động header.

## Cách verify hiệu quả

Chạy lại UPI approve với pool proxy hiện tại, quan sát log:

- Trước fix: pattern `403 → blocked → blocked` mỗi 3 attempt.
- Sau fix: kỳ vọng `cf-warmup ok → blocked → blocked` (3 dòng) hoặc
  `cf-warmup ok → approved` nếu proxy đủ "sạch".

Nếu vẫn dính `blocked` đều 100% sau 20 batch → confirm vấn đề (2): pool proxy
quá bẩn cho ChatGPT, phải đổi sang residential/IN.

## Cách grep tìm các thay đổi trong repo

```sh
rg "FIX-CF-WARMUP-2026-06-20|FIX-CHROME142-FIRST-2026-06-20" -n
```

Mọi marker đều có timestamp `2026-06-20` để dễ rollback theo ngày.
