# Bugfix Requirements Document

## Introduction

Trong nhánh `one_time_code_mode` (login bằng one-time code khi password ban đầu sai),
flow signup gọi `_REGISTER_USER_JS` để set password mới trước khi fill `/about-you`.
Hiện tại, kết quả của lời gọi này chỉ được **log** chứ không được kiểm tra: exception
bị `except Exception` nuốt, HTTP non-2xx và body báo lỗi đều bị bỏ qua. Flow vẫn tiếp
tục fill `/about-you`, return `callback_url` như success, và caller (cli/http_phase/db)
mark job là **thành công** với password user yêu cầu.

Hệ quả nghiêm trọng (P1): user nhận account "thành công" nhưng password trên backend
OpenAI chưa được set (hoặc set sai) → login bằng password đó luôn fail. Bug vi phạm
nguyên tắc **Fail-Fast** của project (xem `AGENTS.md` & `project-rules.md`): code đang
fallback che lỗi thay vì raise.

Bug này chỉ xảy ra trong nhánh `screen == "about_you"` khi `one_time_code_mode=True`
(file `browser_phase.py`, ~line 700-720). Các flow khác — signup mới qua password
thẳng (`screen == "password_create"`) và login OTP của account đã có password —
không bị ảnh hưởng và phải được preserve.

## Bug Analysis

### Current Behavior (Defect)

Khi `one_time_code_mode=True` và lời gọi set password (`_REGISTER_USER_JS`) thất bại,
flow hiện tại không phát hiện và vẫn báo success.

1.1 WHEN `one_time_code_mode=True` AND `page.evaluate(_REGISTER_USER_JS, ...)` raise exception THEN the system chỉ log message `"set password (about_you ctx) failed: ..."` rồi tiếp tục fill `/about-you` và return `callback_url` như success
1.2 WHEN `one_time_code_mode=True` AND set password trả về HTTP status không phải 2xx (vd 400/401/403/409/500) THEN the system chỉ log HTTP status rồi tiếp tục fill `/about-you` và return `callback_url` như success
1.3 WHEN `one_time_code_mode=True` AND set password trả về body chứa error indicator (vd `body.error`, `body.detail`, hoặc shape khác báo lỗi) dù HTTP 200 THEN the system chỉ log body rồi tiếp tục fill `/about-you` và return `callback_url` như success
1.4 WHEN `one_time_code_mode=True` AND set password fail theo bất kỳ điều kiện 1.1/1.2/1.3 THEN caller (cli.py / http_phase.py / db/repositories.py) nhận được `callback_url` hợp lệ và mark account/job thành công với password mà thực tế account chưa có password đó

### Expected Behavior (Correct)

Khi set password fail trong nhánh `one_time_code_mode`, flow phải dừng ngay lập tức
với lỗi rõ ràng để caller mark job là failed.

2.1 WHEN `one_time_code_mode=True` AND `page.evaluate(_REGISTER_USER_JS, ...)` raise exception THEN the system SHALL raise `BrowserPhaseError` với reason `"set_password_failed"` (hoặc tương đương) kèm chi tiết exception, KHÔNG fill `/about-you`, KHÔNG return `callback_url`
2.2 WHEN `one_time_code_mode=True` AND set password trả về HTTP status không phải 2xx THEN the system SHALL raise `BrowserPhaseError` với reason `"set_password_failed"` kèm HTTP status và body, KHÔNG fill `/about-you`, KHÔNG return `callback_url`
2.3 WHEN `one_time_code_mode=True` AND set password trả về body báo lỗi dù HTTP 200 THEN the system SHALL raise `BrowserPhaseError` với reason `"set_password_failed"` kèm nội dung lỗi từ body, KHÔNG fill `/about-you`, KHÔNG return `callback_url`
2.4 WHEN `one_time_code_mode=True` AND set password fail theo bất kỳ điều kiện 2.1/2.2/2.3 THEN caller (cli.py / http_phase.py / db/repositories.py) SHALL mark account/job là failed với reason rõ ràng (vd `"set_password_failed"`) thay vì success

### Unchanged Behavior (Regression Prevention)

Các flow khác — không thuộc nhánh `one_time_code_mode` set-password trong `about_you`
— phải được preserve nguyên vẹn.

3.1 WHEN `one_time_code_mode=True` AND set password trả về HTTP 2xx với body hợp lệ (success) THEN the system SHALL CONTINUE TO fill `/about-you` và return `callback_url` như flow hiện tại
3.2 WHEN `one_time_code_mode=False` (signup mới qua password thẳng, nhánh `screen == "password_create"`) THEN the system SHALL CONTINUE TO gọi register endpoint và xử lý success/already-exists/error theo logic hiện tại trong nhánh `password_create`, KHÔNG bị ảnh hưởng bởi check mới
3.3 WHEN login OTP của account đã có password (verify OTP xong vào thẳng `/about-you` hoặc chatgpt.com mà không cần set password) THEN the system SHALL CONTINUE TO không gọi `_REGISTER_USER_JS` (vì `one_time_code_mode=False`) và flow chạy nguyên vẹn
3.4 WHEN flow vào nhánh `about_you` với `one_time_code_mode=True` AND set password thành công THEN the system SHALL CONTINUE TO gọi `_wait_oai_sc`, `_fill_about_you`, `_wait_chatgpt_session` theo thứ tự hiện tại
3.5 WHEN caller xử lý lỗi từ `browser_phase` ở các flow khác (vd timeout, auth_error, mfa_challenge) THEN the system SHALL CONTINUE TO mark job failed theo cơ chế xử lý `BrowserPhaseError` hiện có, không bị thay đổi semantics

## Bug Condition & Property (Pseudocode)

### Bug Condition Function

```pascal
FUNCTION isBugCondition(X)
  INPUT:
    X = {
      one_time_code_mode: boolean,
      register_call_outcome: {
        kind: 'exception' | 'http_response',
        exception?: Exception,
        status?: integer,
        body?: any
      }
    }
  OUTPUT: boolean

  // Chỉ thuộc bug condition khi vào nhánh OTC set-password VÀ call fail
  IF X.one_time_code_mode = false THEN
    RETURN false
  END IF

  outcome ← X.register_call_outcome

  IF outcome.kind = 'exception' THEN
    RETURN true
  END IF

  IF outcome.kind = 'http_response' THEN
    IF outcome.status < 200 OR outcome.status >= 300 THEN
      RETURN true
    END IF
    IF body_indicates_error(outcome.body) THEN
      RETURN true
    END IF
  END IF

  RETURN false
END FUNCTION
```

### Property: Fix Checking

```pascal
// Với mọi input thuộc bug condition, F'(X) phải fail-fast với BrowserPhaseError,
// KHÔNG return callback_url, KHÔNG fill /about-you.
FOR ALL X WHERE isBugCondition(X) DO
  result ← drive_signup_flow'(X)
  ASSERT result is BrowserPhaseError
    AND error_reason_indicates_set_password_failed(result)
    AND about_you_was_NOT_filled(X)
    AND callback_url_was_NOT_returned(X)
    AND caller_marks_job_as_failed(X)
END FOR
```

### Property: Preservation Checking

```pascal
// Với mọi input KHÔNG thuộc bug condition, F'(X) phải hành xử y hệt F(X).
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT drive_signup_flow(X) = drive_signup_flow'(X)
END FOR
```

### Counterexamples (Concrete)

- `one_time_code_mode=True`, register call raise `playwright.TimeoutError` → hiện tại return success, đúng phải raise `BrowserPhaseError("set_password_failed: ...")`.
- `one_time_code_mode=True`, register call return `{"status": 400, "body": {"error": "weak_password"}}` → hiện tại return success, đúng phải raise.
- `one_time_code_mode=True`, register call return `{"status": 200, "body": {"error": "session_invalid"}}` → hiện tại return success, đúng phải raise.
- `one_time_code_mode=True`, register call return `{"status": 200, "body": {"continue_url": "..."}}` → preserve: tiếp tục fill `/about-you`, return success.
- `one_time_code_mode=False`, signup password thẳng → preserve: nhánh `password_create` xử lý nguyên vẹn.
