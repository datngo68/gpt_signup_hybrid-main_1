# reg-hybrid-deactivated-after-signup Bugfix Design

## Overview

Account reg qua `reg_mode="hybrid"` báo `success=True` (callback + `/api/auth/session`
200) nhưng bị server vô hiệu hóa gần như tức thì — dấu hiệu **deferred ban**: server
chấp nhận phiên rồi chấm điểm automation sau và ban.

Tiền đề (do user chỉ định, là chuẩn của spec này): package `chatgpt_camoufox` là **golden
standard** và **CẤM sửa** (`client.py`/`ChatGPTRelay`, `fields.py`, `headers.py`,
`sentinel.py`, `camoufox_vm.py`, assets). Mọi fix **chỉ nằm trong `reg_hybrid/`**.

Điều tra cho thấy hybrid drift khỏi golden ở **hai tầng độc lập**, mỗi tầng là một root
cause khả dĩ:

- **Nhánh A — drift ở `run()` (request layer).** `HybridChatGPTRelay.run()` là bản copy
  tay toàn bộ chuỗi golden. Khác biệt rõ nhất ngoài OTP loop: `_create_account_with_retry`
  có thể **POST `/api/accounts/create_account` lần thứ hai** trên cùng OAuth session, trong
  khi golden chỉ POST đúng một lần/session. Đây là request pattern golden không bao giờ phát.

- **Nhánh B — drift ở khâu KHỞI TẠO relay (oracle/token layer).** Hybrid không dùng golden
  `CamoufoxTokenGenerator` trực tiếp mà bọc qua `browser_pool.HybridContextHandle`, trong đó
  **inject synthetic DOM events** (`_OBSERVER_FEEDER_JS`/`_OBSERVER_BURST_JS`) để ép
  `sessionObserverToken()` có data, và **share một Camoufox browser xuyên nhiều signup**.
  Golden (`__main__.run_line`) launch một `CamoufoxTokenGenerator` mới mỗi account, page
  tĩnh, **không feed event**. Nếu account bị ban kể cả khi OTP về nhanh (happy path request
  y hệt golden), thì root cause KHÔNG ở `run()` mà ở tầng này: `so` token sinh từ chuỗi
  event giả + fingerprint browser dùng lại = tín hiệu automation.

Chiến lược: fix tối thiểu cho cả hai nhánh, chỉ trong `reg_hybrid/`, không đổi kiến trúc
golden. Vì chưa có log so khớp request hybrid-vs-golden trên happy path để chốt chắc nhánh
nào là nguyên nhân chính, design nêu cả hai và kèm **bước phân loại** (xem cuối Testing
Strategy) để xác định trước khi cam kết fix nặng tay ở nhánh B.

## Glossary

- **Bug_Condition (C)**: phiên reg hybrid phát tín hiệu khác golden — hoặc request pattern
  ngoài OTP loop lệch golden (nhánh A), hoặc token/fingerprint từ khâu khởi tạo lệch golden
  (nhánh B).
- **Property (P)**: hành vi đúng — request pattern và token signature của hybrid đồng nhất
  golden ở mọi bước, **chỉ khác duy nhất ở OTP acquisition/verify loop** (delta có chủ đích).
- **Preservation**: hành vi không được đổi — package `chatgpt_camoufox`, smart OTP loop,
  kế thừa `_dd_s`/`oai-did`/sentinel/headers, no-pre-mint, schema `SignupResult`.
- **F**: hybrid hiện tại — `run()` double-POST create_account + pool feed synthetic events.
- **F'**: hybrid sau fix — create_account đúng 1 lần; oracle khởi tạo bám golden.
- **golden `ChatGPTRelay.run()`**: chuỗi `csrf → signin → authorize → register → otp_send →
  get_code → otp_validate → create_account → callback → get_session`, create_account 1 lần.
- **OTP_LOOP**: phần khác biệt có chủ đích — multi-code fetch, resend, verify-retry,
  human-like delay. Đây là delta hợp lệ duy nhất của hybrid.
- **oracle layer**: khâu mint sentinel/`so` token qua Camoufox (`camoufox_vm` golden vs
  `browser_pool` hybrid).
- **Observer feeder**: script `setInterval` fire synthetic mousemove/scroll/click/focus mà
  `browser_pool` inject vào sentinel frame — golden không có.

## Bug Details

### Bug Condition

Bug manifest khi phiên reg chạy ở `mode="hybrid"` và phát tín hiệu khác golden ở một trong
hai tầng: (A) `run()` POST create_account >1 lần / chuỗi bước ngoài OTP loop lệch golden;
hoặc (B) `so`/sentinel token sinh từ synthetic events + fingerprint browser dùng lại thay vì
từ `CamoufoxTokenGenerator` golden tươi.

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X = phiên reg hybrid (relay run + khởi tạo oracle)
  OUTPUT: boolean

  RETURN X.mode = "hybrid"
     AND (
        // Nhánh A — request layer
          createAccountPostCount(X) > 1
       OR stepSequence(X, exclude=OTP_LOOP) != golden_stepSequence

        // Nhánh B — oracle layer
       OR soTokenFromSyntheticEvents(X)            // feeder inject events
       OR browserFingerprintReusedAcrossSignups(X) // pool share browser
     )
END FUNCTION
```

### Examples

- **A1**: OTP đầu sai → `_create_account_with_retry` mint sentinel/SO mới và POST
  `create_account` lần hai trên cùng session. Golden chỉ POST một lần. (Sai — double-POST.)
- **A2**: Comment của `_create_account_with_retry` viện dẫn `_patched_mint_token` đã
  invalidate cache, nhưng runtime chính `on_otp_poll_start=None` và không gọi
  `_patch_tokens_cache` → cơ chế đó **không active**; lần POST thứ hai dựa trên giả định lỗi
  thời, vừa vô nghĩa vừa lệch golden. (Sai — dead-path drift.)
- **B1**: `browser_pool` inject `_OBSERVER_FEEDER_JS` (mousemove/scroll/click đều đặn mỗi
  200ms) để `mint_so` không empty. `so` token phản ánh chuỗi tương tác giả có pattern máy
  móc; golden page tĩnh không feed. (Sai — so signature giả.)
- **B2**: Pool share một Camoufox browser cho nhiều signup (context isolated cookies nhưng
  fingerprint browser-level — canvas/webgl/navigator — dùng lại). Golden launch browser mới
  mỗi account. (Sai — cluster fingerprint.)
- **Edge (happy path)**: OTP về nhanh (1× send + 1× validate, không retry) → nhánh A im
  lặng (create_account 1 lần) nhưng account VẪN bị ban → counterexample loại trừ A, chỉ
  nhánh B (oracle layer) còn lại.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Package `chatgpt_camoufox` (`ChatGPTRelay`/`run`, `fields`, `headers`, `sentinel`,
  `camoufox_vm`, assets) vẫn là golden, **không bị sửa** (3.1).
- Smart OTP loop của hybrid (resend, `poll_all_codes`, `prefer_second_code`, verify retry
  với `_otp_validate_soft`, human-like delay 2–4s, `prefer_newest_untried_otp_sync`) giữ
  nguyên — đây là delta có chủ đích duy nhất (3.2).
- `HybridChatGPTRelay` vẫn kế thừa nguyên `_dd_s`/`oai-did`/sentinel/`device_id`/headers/
  cookie jar từ `ChatGPTRelay` qua `super().__init__()` (3.3).
- Khi OTP về nhanh, chuỗi request từ đầu tới `get_session` y hệt golden (3.4).
- Không pre-mint sentinel cho create_account (mint chỉ tại `create_account`, sau OTP validate
  OK) — giữ fix anti-ban trước đó (`on_otp_poll_start=None`) (3.5).
- `SignupResult` vẫn trả đủ `session_token`/`access_token`/`account_id`/`cookies` theo schema
  hiện tại (3.6).

**Scope:**
Mọi input KHÔNG phải `mode="hybrid"` không bị ảnh hưởng: luồng `pure_request`, luồng
`browser`, và mọi cookie/header/token golden khác.

(Hành vi đúng kỳ vọng cho input buggy nằm ở mục Correctness Properties — Property 1.)

## Hypothesized Root Cause

### Nhánh A — drift ở `HybridChatGPTRelay.run()` (request layer)

1. **`_create_account_with_retry` double-POST.** Khi `parse_callback_from_create_account`
   raise `ValueError`, helper re-fire POST `/api/accounts/create_account` lần hai trên cùng
   OAuth session. Golden không bao giờ POST create_account hai lần/session → server thấy
   pattern lạ.

2. **Retry dựa trên cơ chế không còn tồn tại.** Logic retry giả định
   `_patched_mint_token` đã invalidate sentinel cache stale. Nhưng runtime chính
   (`run_hybrid_signup`) đặt `on_otp_poll_start=None` và **không** patch tokens → không có
   cache để invalidate. Lần POST thứ hai vì vậy là dead-path drift.

3. **Override toàn bộ `run()` = không có ràng buộc "chỉ OTP loop khác".** Vì CẤM sửa golden,
   không thể chèn template-method hook vào `ChatGPTRelay.run()`. Hybrid copy tay từng bước
   nên mọi thay đổi tương lai của golden không tự propagate, và drift (như double-POST) lọt
   qua không bị phát hiện.

### Nhánh B — drift ở khâu khởi tạo relay (oracle/token layer)

So `reg_hybrid` (runner/camoufox_factory/browser_pool) với golden (`__main__.run_line` +
`camoufox_vm.CamoufoxTokenGenerator`):

| Khía cạnh | Golden | Hybrid | Rủi ro automation |
|---|---|---|---|
| Mint sentinel/so | `CamoufoxTokenGenerator` mới mỗi account, page tĩnh | `browser_pool.HybridContextHandle` | — |
| Observer events | KHÔNG feed (page tĩnh) | **inject `_OBSERVER_FEEDER_JS` + `_OBSERVER_BURST_JS`** (synthetic mousemove/scroll/click/focus đều đặn 200ms) | **CAO** — `so` token = bằng chứng human interaction; chuỗi event giả có pattern máy móc có thể bị server phân tích |
| Browser lifecycle | launch mới mỗi account, close trong `finally` | **share 1 Camoufox xuyên N signup** (context isolated, browser fingerprint dùng lại) | TRUNG BÌNH–CAO — cluster fingerprint giữa nhiều account |
| `mint_so` khi empty | raise ngay | **retry + burst events** | TRUNG BÌNH — thêm activity giả |
| proxy | cùng proxy Camoufox + curl | cùng proxy (factory set cả hai) | THẤP — khớp |
| TLS verify | `verify=not insecure` (mặc định ON) | `verify=not request.tls_insecure` (mặc định ON) | THẤP — khớp, không insecure-by-default |
| profile/fingerprint | `profile_for_locale(135, Windows)` | `profile_for_locale(135, Windows)`, locale default en-US | THẤP — khớp (golden CLI default vi-VN chỉ là preference) |
| header order / `_dd_s` / `oai-did` | golden | kế thừa nguyên qua `ChatGPTRelay` | THẤP — khớp |
| monkey-patch `_patched_mint_token` | không | có định nghĩa nhưng **không active** runtime chính | THẤP (chỉ dead code) |
| pre-mint thread | không | đã bỏ (`on_otp_poll_start=None`) | THẤP — khớp |

**Ứng viên #1 nhánh B**: Observer feeder synthetic events. `sessionObserverToken` được thiết
kế để chứng minh tương tác người thật; feeder tạo chuỗi event đều đặn (interval cố định
200ms, jitter uniform) → `so` token mang chữ ký máy. Golden chạy không feeder, nên nếu golden
mint được `so` thì cơ chế golden tạo ra signature khác (hoặc golden thực chạy non-headless có
interaction thật). Đây là drift đáng ngờ nhất gây deferred ban khi mọi request đã giống golden.

**Ứng viên #2 nhánh B**: shared browser fingerprint giữa các signup → cluster detection.

### Đánh giá hợp nhất

- Nếu account bị ban **chỉ khi** có OTP retry / double-POST → root cause nghiêng **nhánh A**.
- Nếu account bị ban **cả trên happy path** (OTP nhanh, request y hệt golden) → nhánh A bị
  loại trừ, root cause là **nhánh B** (oracle layer, gần như chắc là Observer feeder).
- Hiện chưa có log phân loại → **chưa chốt chắc**. Xem "Phân loại root cause" cuối Testing
  Strategy. Fix nhánh A là an toàn vô điều kiện (đúng theo golden); fix nhánh B nặng tay
  (bỏ feeder/pool) chỉ commit sau khi bước phân loại xác nhận.

## Correctness Properties

Property 1: Bug Condition — Request & token signature đồng nhất golden

_For any_ phiên reg hybrid mà bug condition thành lập (`isBugCondition` trả true), relay sau
fix (F') SHALL: (a) gọi `create_account` **đúng một lần**/session như golden; (b) tái lập
đúng chuỗi bước, thứ tự và side-effect golden ở mọi bước **ngoài** OTP loop; (c) sinh
sentinel/`so` token qua cơ chế bám golden (không từ synthetic event feed), với OTP
acquisition/verify loop là khác biệt có chủ đích duy nhất.

**Validates: Requirements 2.1, 2.2, 2.3**

Property 2: Preservation — Hành vi golden + smart OTP loop không đổi

_For any_ input mà bug condition KHÔNG thành lập (`isBugCondition` trả false) — gồm luồng
`pure_request`, luồng `browser`, package `chatgpt_camoufox`, và mọi cookie/header/token golden
khác — relay sau fix (F') SHALL cho kết quả giống relay gốc (F), bảo toàn package golden,
smart OTP loop, kế thừa `_dd_s`/`oai-did`/sentinel/device_id/headers, no-pre-mint và schema
`SignupResult`.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

## Fix Implementation

### Nhánh A — `reg_hybrid/relay.py` (fix vô điều kiện)

**File**: `reg_hybrid/relay.py`

1. **Bỏ `_create_account_with_retry` double-POST.** Trong `run()`, thay
   `callback_url = self._create_account_with_retry()` bằng `callback_url = self.create_account()`
   — gọi đúng method golden kế thừa, **đúng một lần**/session.
   - Xóa hẳn helper `_create_account_with_retry` (dead-path: dựa trên cache không còn active).
   - Nếu `create_account` raise (sentinel reject / age / rate-limit) → để propagate lên
     `run_hybrid_signup` outer-loop classify như golden, KHÔNG re-POST trên cùng session.

2. **Thu hẹp delta của `run()` về đúng OTP loop.** Giữ override `run()` (bắt buộc vì CẤM
   sửa golden), nhưng cấu trúc lại để mọi bước ngoài OTP gọi nguyên method kế thừa
   (`get_csrf`/`signin`/`authorize`/`register`/`otp_send`/`create_account`/`callback`/
   `get_session`) — không thêm side-effect. Tách smart OTP thành một method riêng
   `_acquire_and_validate_otp()` để `run()` đọc ra đúng skeleton golden + 1 điểm khác.
   - Mục tiêu: diff `run()` hybrid vs golden chỉ còn ở block OTP.

3. **Test guard chống drift tương lai** (thay cho template-method không thể chèn vào golden):
   thêm test so khớp `relay.steps` của hybrid (happy path, OTP 1 lần) với golden
   `ChatGPTRelay.run()` — bất kỳ bước/thứ tự lệch nào ngoài OTP loop sẽ fail test.

### Nhánh B — khâu khởi tạo relay (commit SAU bước phân loại)

**Files**: `reg_hybrid/browser_pool.py`, `reg_hybrid/camoufox_factory.py`, `reg_hybrid/runner.py`

4. **Loại synthetic Observer feeder** (ứng viên #1): bỏ inject `_OBSERVER_FEEDER_JS` +
   `_OBSERVER_BURST_JS` khỏi `_acquire_context_in_thread`/`_mint_so_in_thread`. Để
   `sessionObserverToken` chạy đúng như golden `CamoufoxTokenGenerator`. Nếu vì page tĩnh
   headless mà `so` empty → giải bằng cơ chế golden-compatible (vd chạy non-headless cho
   oracle, hoặc dùng thẳng `CamoufoxTokenGenerator` golden), KHÔNG bù bằng event giả.

5. **Cân nhắc bỏ shared-browser pool** (ứng viên #2): cho hybrid dùng path
   `_NoPoolThreadAffinityWrapper(CamoufoxTokenGenerator(...))` (đã có khi `HYBRID_POOL_DISABLED=1`)
   làm mặc định, để mỗi signup launch browser golden riêng — khớp lifecycle golden, tránh
   cluster fingerprint. Đánh đổi tốc độ (cold launch ~5–10s/signup); chỉ bật pool lại khi
   xác nhận fingerprint per-context đủ khác.

6. **Giữ nguyên** proxy (cùng proxy cả hai), TLS verify (mặc định ON), profile Firefox 135
   Windows, header order, no-pre-mint — các khía cạnh này đã khớp golden.

> Fix nhánh B (#4, #5) chỉ commit sau khi bước phân loại (dưới) xác nhận ban xảy ra trên
> happy path. Nếu phân loại cho thấy ban chỉ gắn với double-POST → fix nhánh A là đủ, không
> đụng oracle layer (tránh hi sinh tốc độ pool vô ích).

## Testing Strategy

Theo AGENTS.md: mọi check nằm trong file `.py` thật ở `test/` (đặt tên `test/check_*.py`,
`test/smoke_*.py`, `test/test_*.py`), chạy `python3 test/<file>.py`. **Cấm** inline
`python3 -c`/`node -e`/`bash -c`. Tất cả test offline (inject session + token generator giả
lập), không chạm network/Camoufox thật.

### Validation Approach

Hai pha: trước hết surface counterexample chứng minh bug trên code CHƯA fix, sau đó verify
fix đúng và không hồi quy. Vì có hai nhánh chưa phân loại chắc, thêm một bước phân loại
(diff request log hybrid vs golden) để hướng quyết định fix nhánh B.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexample chứng minh drift TRƯỚC khi fix; xác nhận/bác bỏ root cause
từng nhánh. Nếu bác bỏ, re-hypothesize.

**Test Plan** (`test/check_hybrid_drift.py`):
- Build `HybridChatGPTRelay` với `session` giả (fake curl recorder ghi lại
  method+url+body+headers), `tokens` giả (stub `mint_token`/`mint_so` đếm số lần gọi),
  `mail_provider` giả trả OTP theo kịch bản. Build song song golden `ChatGPTRelay` với cùng
  fake session + OTP về ngay.

**Test Cases**:
1. **A — double-POST**: kịch bản OTP đầu sai → assert recorder ghi `create_account` **>1
   lần** trên code CHƯA fix (counterexample nhánh A).
2. **A — step diff happy path**: OTP về ngay → so `relay.steps` hybrid vs golden, assert
   **khác** trên code cũ ở chỗ ngoài OTP loop (nếu có drift) / xác nhận giống.
3. **B — synthetic feeder hiện diện**: assert `browser_pool` source/đường mint `so` có inject
   `_OBSERVER_FEEDER_JS` (counterexample nhánh B: token sinh từ event giả) trên code cũ.
4. **B — shared browser**: assert pool tái dùng cùng browser instance cho 2 lần `acquire`
   khác signup (counterexample cluster) trên code cũ.

**Expected Counterexamples**:
- `create_account` POST 2 lần khi OTP retry; `so` token đến từ synthetic events; browser
  fingerprint dùng lại giữa signup.

### Fix Checking

**Goal**: Với mọi input thỏa bug condition, F' cho hành vi đúng (Property 1).

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := relayFixed(input)
  ASSERT createAccountPostCount(result) = 1
     AND stepSequence(result, exclude=OTP_LOOP) = golden_stepSequence
     AND NOT soTokenFromSyntheticEvents(result)
     AND otpLoop(result) = smart_otp_loop
END FOR
```

### Preservation Checking

**Goal**: Với mọi input KHÔNG thỏa bug condition, F' cho kết quả giống F (Property 2).

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT relayOriginal(input) = relayFixed(input)
  // pure_request, browser mode, package chatgpt_camoufox, smart OTP loop,
  // kế thừa _dd_s/oai-did/sentinel/headers, no-pre-mint, schema SignupResult — không đổi.
END FOR
```

**Testing Approach**: Property-based testing phù hợp cho preservation vì sinh nhiều case tự
động trên miền input (kịch bản OTP, thứ tự bước, số lần retry), bắt edge case mà unit test bỏ
sót.

**Test Plan** (`test/test_hybrid_relay_preservation.py`): quan sát hành vi golden + smart OTP
loop trên code CHƯA fix, chốt lại sau fix.

**Test Cases**:
1. **Smart OTP loop bất biến**: resend / multi-code / verify-retry / human-like delay vẫn
   hoạt động như trước.
2. **Kế thừa bất biến**: `_dd_s`/`oai-did`/sentinel/device_id/headers vẫn từ
   `super().__init__()`, subclass không override.
3. **No-pre-mint bất biến**: sentinel `oauth_create_account` chỉ mint tại `create_account`,
   sau OTP validate OK; không spawn pre-mint thread.
4. **Schema bất biến**: `SignupResult` đủ `session_token`/`access_token`/`account_id`/`cookies`.
5. **Golden package bất biến**: không file nào trong `chatgpt_camoufox/` bị sửa.

### Unit Tests (`test/test_hybrid_create_account_once.py`)

- `run()` (fake session, OTP về ngay): `create_account` POST đúng 1 lần.
- `run()` với OTP đầu sai + verify-retry: vẫn chỉ POST `create_account` 1 lần (không re-POST).
- `_create_account_with_retry` đã bị xóa (assert attribute không tồn tại).

### Property-Based Tests (`test/test_hybrid_pbt.py`)

- Sinh ngẫu nhiên kịch bản OTP (số mã, số lần sai, delay resend) → assert
  `create_account` luôn POST đúng 1 lần và step sequence ngoài OTP loop luôn khớp golden.
- Sinh ngẫu nhiên thứ tự bước → assert delta hybrid vs golden chỉ nằm trong OTP loop.

### Integration Tests (`test/smoke_hybrid_vs_golden.py`)

- Full flow hybrid offline (session/otp/token giả): trace `relay.steps`, assert chuỗi
  `csrf/signin/authorize/register/otp_send/create_account/callback/get_session` khớp golden,
  `create_account` 1 lần.
- (Sau fix nhánh B) assert đường mint `so` không inject synthetic feeder; pool mặc định
  không share browser xuyên signup.

### Phân loại root cause (BẮT BUỘC trước khi commit fix nhánh B)

Vì chưa chốt chắc nhánh nào là nguyên nhân chính, chạy bước phân loại offline trước:

- **Diff request log hybrid vs golden trên happy path** (`test/check_happy_path_request_diff.py`):
  với cùng input (OTP về ngay, 1× validate), ghi lại đầy đủ
  `method + url + body + header order + cookie` của mọi request hybrid và golden, rồi diff.
  - Nếu **giống hệt** trên happy path nhưng account thực vẫn bị ban → loại trừ nhánh A,
    nguyên nhân là nhánh B (oracle layer / `so` signature) → commit #4, #5.
  - Nếu **khác** (vd double-POST chỉ xuất hiện khi retry, happy path sạch) → root cause là
    nhánh A → fix #1–#3 là đủ, KHÔNG đụng oracle layer.
- Bổ sung (nếu có mẫu account ban thực): đối chiếu account ban "happy path" vs "có retry" để
  khẳđịnh nhánh trước khi hi sinh tốc độ pool.
