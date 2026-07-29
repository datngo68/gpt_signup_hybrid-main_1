# Bugfix Requirements Document

## Introduction

Tài khoản đăng ký qua luồng `reg_mode="hybrid"` bị **deactivated ngay sau khi reg thành công** (pipeline báo `success=True`, callback + `/api/auth/session` 200 nhưng account bị server vô hiệu hóa gần như tức thì — dấu hiệu **deferred ban**).

**Tiền đề (do user chỉ định, là chuẩn của spec này):**
`chatgpt_camoufox` là **golden standard**. Cụ thể `chatgpt_camoufox/chatgpt_camoufox/client.py` → class `ChatGPTRelay` và method `ChatGPTRelay.run()` là tiêu chuẩn vàng và **KHÔNG được sửa**. `reg_hybrid/relay.py` → `HybridChatGPTRelay(ChatGPTRelay)` chỉ được phép **override `run()`** để thêm smart OTP loop; mọi thứ khác (`_dd_s`, cookie, sentinel, `device_id`, headers, TLS impersonate) phải **kế thừa nguyên** từ `ChatGPTRelay`.

**Kết quả điều tra (so khớp từng bước golden vs hybrid):**

`HybridChatGPTRelay.run()` là một bản **copy thủ công toàn bộ chuỗi bước** của golden `ChatGPTRelay.run()`. Khi đối chiếu trực tiếp:

- Hybrid **KHÔNG bỏ sót** bước nào của golden và giữ **đúng thứ tự**: `get_csrf → signin → authorize → register → otp_send → [OTP] → create_account → callback → get_session`. Các bước này đều gọi method kế thừa từ `ChatGPTRelay`, nên `_dd_s`/`oai-did`/cookie absorption/sentinel ordering/header order được bảo toàn ở các bước đó.
- Sai khác của hybrid so với golden **mang tính cộng thêm (additive), không phải thiếu bước**:
  1. **`create_account` có thể POST 2 lần.** Golden gọi `create_account()` đúng **một lần**/session. Hybrid bọc trong `_create_account_with_retry`: khi `parse_callback_from_create_account` raise `ValueError`, nó mint sentinel + SO mới và **POST `/api/accounts/create_account` lần thứ hai** trên cùng OAuth session — pattern golden không bao giờ phát ra.
  2. **Số request OTP lệch golden khi mail chậm.** Golden phát đúng **1× GET `/email-otp/send`** + **1× POST `/email-otp/validate`**. Hybrid (smart OTP loop) có thể resend `/send` nhiều lần và validate `/validate` nhiều lần. *Đây là behavior có chủ đích và phải giữ nguyên* — nêu ở Unchanged.

Vì `run()` được override bằng bản copy tay, nó **không có cơ chế ràng buộc "chỉ OTP loop khác golden"**: phần create_account đã drift (double-POST) và mọi thay đổi tương lai của golden sẽ không tự propagate. Đây là nguồn của request pattern khác-golden có thể bị server chấm là automation → deferred ban.

> Lưu ý: spec cũ đi sai hướng khi cho rằng `ChatGPTRelay` tự diverge khỏi browser thật ở cookie `_dd_s` (`rum=0/2`, scope domain) và đề xuất sửa `fields.py`/`client.py` trong `chatgpt_camoufox`. Theo tiền đề mới, golden là chuẩn — **cấm sửa `chatgpt_camoufox`**; mọi fix nằm ở `reg_hybrid/relay.py`.

## Bug Analysis

### Current Behavior (Defect)

`HybridChatGPTRelay.run()` (override) phát ra request pattern khác golden ở các bước **ngoài** OTP loop:

1.1 WHEN `HybridChatGPTRelay.run()` chạy THEN hệ thống thực thi một bản sao thủ công của toàn bộ chuỗi golden `ChatGPTRelay.run()` thay vì uỷ quyền cho golden, nên không có gì đảm bảo "chỉ OTP loop khác golden" — phần ngoài OTP loop có thể (và đã) drift khỏi chuẩn mà không bị phát hiện.

1.2 WHEN OTP validate ban đầu fail và `parse_callback_from_create_account` raise `ValueError` THEN `_create_account_with_retry` mint sentinel/SO mới và POST `/api/accounts/create_account` **lần thứ hai** trên cùng OAuth session — golden chỉ POST đúng một lần, không bao giờ tạo create_account lặp.

1.3 WHEN bất kỳ bước nào ngoài OTP loop (csrf/signin/authorize/register/otp_send/create_account/callback/get_session) chạy trong hybrid THEN hệ thống dựa vào việc bản copy tay gọi lại đúng method golden — không có ràng buộc tĩnh nào ngăn chuỗi/thứ tự/side-effect lệch golden, khiến request pattern có thể khác golden baseline (tín hiệu deferred ban).

### Expected Behavior (Correct)

`HybridChatGPTRelay.run()` phải đồng nhất với golden `ChatGPTRelay.run()` ở mức HTTP-request, **chỉ khác duy nhất ở OTP acquisition/verify loop**:

2.1 WHEN `HybridChatGPTRelay.run()` chạy THEN hệ thống SHALL tái lập **đúng chuỗi bước, đúng thứ tự và đúng side-effect** của golden `ChatGPTRelay.run()`, với **bước OTP là khác biệt có chủ đích duy nhất** (isolate phần OTP, các bước còn lại uỷ quyền/gọi nguyên method golden kế thừa).

2.2 WHEN tới bước create_account THEN hệ thống SHALL gọi `create_account` **đúng một lần**/session như golden (sentinel/SO mint tại thời điểm gọi, qua method kế thừa) — KHÔNG POST `/api/accounts/create_account` lần thứ hai.

2.3 WHEN các bước csrf/signin/authorize/register/otp_send/callback/get_session chạy THEN hệ thống SHALL để **method golden kế thừa** sinh request + side-effect (`_dd_s`, `oai-did`, cookie absorption, sentinel ordering, header order, TLS impersonate) nguyên vẹn, không can thiệp.

### Unchanged Behavior (Regression Prevention)

2.x chỉ thay đổi `reg_hybrid/relay.py`; các phần sau phải giữ nguyên:

3.1 WHEN bất kỳ luồng nào chạy THEN package `chatgpt_camoufox` (`client.py` `ChatGPTRelay`/`ChatGPTRelay.run`, `fields.py`, `headers.py`, `sentinel.py`) SHALL CONTINUE TO là golden standard và **không bị sửa**.

3.2 WHEN account mới cần OTP THEN smart OTP loop của hybrid (resend, multi-code `poll_all_codes`, `prefer_second_code`, verify retry với `_otp_validate_soft`, human-like delay 2–4s, `prefer_newest_untried_otp_sync`) SHALL CONTINUE TO hoạt động như hiện tại — đây là khác biệt có chủ đích duy nhất so với golden.

3.3 WHEN `HybridChatGPTRelay` khởi tạo THEN hệ thống SHALL CONTINUE TO kế thừa nguyên `_dd_s`, `oai-did`, sentinel, `device_id`, headers, cookie jar từ `ChatGPTRelay` qua `super().__init__()` — không tự set/đổi các giá trị này trong subclass.

3.4 WHEN OTP về nhanh (1× send + 1× validate, không cần resend/retry) THEN hệ thống SHALL CONTINUE TO phát đúng chuỗi request **y hệt golden** từ đầu tới `get_session`.

3.5 WHEN pre-mint sentinel cho `create_account` được xét THEN hệ thống SHALL CONTINUE TO **không pre-mint** (mint sentinel chỉ tại `create_account`, sau OTP validate OK) — giữ đúng fix anti-ban trước đó (`on_otp_poll_start=None`).

3.6 WHEN reg thành công THEN hệ thống SHALL CONTINUE TO trả `SignupResult` với đầy đủ `session_token`/`access_token`/`account_id`/`cookies` theo schema hiện tại.

---

## Bug Condition & Property (bug condition methodology)

**Định nghĩa:**
- **F**: `HybridChatGPTRelay.run()` hiện tại — override copy tay, có thể double-POST `create_account` và drift khỏi golden ở các bước ngoài OTP loop.
- **F'**: `HybridChatGPTRelay.run()` sau fix — chỉ OTP acquisition/verify loop khác golden; mọi bước khác = golden (uỷ quyền method kế thừa, create_account đúng 1 lần).

**Bug Condition** — điều kiện kích hoạt bug:

```pascal
FUNCTION isBugCondition(X)
  INPUT: X = phiên reg hybrid (relay run)
  OUTPUT: boolean

  // Bug kích hoạt khi run() phát request pattern khác golden ở bước NGOÀI OTP loop:
  RETURN X.mode = "hybrid"
     AND (
          createAccountPostCount(X) > 1                 // create_account lặp
       OR stepSequence(X, exclude=OTP_LOOP) != golden_stepSequence  // bước/thứ tự lệch
       OR inheritedSideEffectsDropped(X)                // mất side-effect golden do override
     )
END FUNCTION
```

**Property: Fix Checking** — hành vi đúng cho input buggy:

```pascal
FOR ALL X WHERE isBugCondition(X) DO
  result ← F'(X)
  ASSERT createAccountPostCount(result) = 1                          // đúng 1 lần như golden
     AND stepSequence(result, exclude=OTP_LOOP) = golden_stepSequence // bước/thứ tự khớp golden
     AND inheritedSideEffects(result) = golden_sideEffects            // _dd_s/oai-did/cookie/sentinel/headers nguyên
     AND otpLoop(result) = smart_otp_loop                            // OTP loop giữ nguyên
END FOR
```

**Property: Preservation Checking** — giữ nguyên với input không-buggy:

```pascal
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
  // pure_request mode, package chatgpt_camoufox, smart OTP loop, no-pre-mint,
  // schema SignupResult — không đổi.
END FOR
```
