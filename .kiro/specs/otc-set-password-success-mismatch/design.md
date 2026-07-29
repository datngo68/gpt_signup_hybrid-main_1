# Bugfix Design Document

## Overview

Bug nằm trong `browser_phase.py`, nhánh `screen == "about_you"` khi `one_time_code_mode=True` (line ~700-720). Lời gọi `_REGISTER_USER_JS` để set password mới chỉ được log, không validate kết quả: exception bị `except Exception` nuốt, HTTP non-2xx và body báo lỗi đều bị bỏ qua. Flow vẫn fill `/about-you` và return `callback_url` như success → caller (`signup.py` / `cli.py` / `web/manager.py`) mark job thành công với password thực tế chưa được set trên backend OpenAI.

Fix: validate response của `_REGISTER_USER_JS` theo strict mode (không accept 409/already-exists, không nuốt exception), raise `BrowserPhaseError` với context rõ ràng để caller chain xử lý theo cơ chế hiện có. Đồng thời refactor: tách logic validation ra helper riêng `_assert_register_success` để DRY giữa hai chỗ gọi (`password_create` và `about_you` OTC), với tham số `accept_already_exists` để phân biệt semantics 2 nhánh.

## Glossary

- **OTC (one-time code) mode**: trạng thái flow sau khi user click "Log in with a one-time code" do password sai. Được track bởi cờ `one_time_code_mode` trong `_drive_signup_screens`.
- **`/about-you` screen**: form name + birthdate cuối cùng của onboard ChatGPT (auth.openai.com).
- **`_REGISTER_USER_JS`**: JS snippet gọi `POST /api/accounts/user/register` trên page context, dùng cho cả tạo account mới (password_create) và set password sau khi login OTC.
- **`BrowserPhaseError`**: exception dùng cho mọi fail của browser phase. Caller (`signup.py`, `web/manager.py`) catch và mark job `error`.
- **`accept_already_exists`**: tham số helper validation. `True` = nhận HTTP 409 / body "already exists" như outcome non-fatal (`password_create` cần). `False` = strict, mọi non-success → raise (OTC cần).

## Bug Details

### Buggy code (verbatim, `browser_phase.py:700-715`)

```python
if one_time_code_mode:
    try:
        reg = await page.evaluate(
            _REGISTER_USER_JS,
            {"username": request.email, "password": request.password},
        )
        st = reg.get("status") if isinstance(reg, dict) else None
        bd = reg.get("body") if isinstance(reg, dict) else reg
        log(f"[flow] set password (about_you ctx): HTTP {st}: {str(bd)[:100]}")
    except Exception as exc:
        log(f"[flow] set password (about_you ctx) failed: {exc}")
```

3 đường vào bug:
1. `page.evaluate` raise → bị `except Exception` nuốt, chỉ log.
2. HTTP non-2xx → log nhưng không check.
3. Body có error indicator dù 200 → không parse, không check.

Ở cả 3 trường hợp, code rớt xuống dưới và tiếp tục:

```python
try:
    await _wait_oai_sc(ctx, timeout_seconds=15, log=log)
except BrowserPhaseError:
    pass
callback_url = await _fill_about_you(...)   # Vẫn chạy
await _wait_chatgpt_session(...)            # Vẫn chờ session
return callback_url, otp_seconds_total      # Vẫn return success
```

### Caller chain hiện có (verified, không cần sửa)

- `signup.py:154`: catch `BrowserPhaseError` chung với phase errors khác → set `status='error'`, persist failure.
- `cli.py:693`: `combo_repo.mark_success(email)` chỉ chạy sau khi `run_browser_phase` return thành công. Khi exception → đi qua except, `combo_repo.mark_failure` được gọi.
- `web/manager.py`: worker loop bắt exception → `_persist_status(job, "error", ...)`.

→ Fix chỉ cần đảm bảo nhánh OTC raise `BrowserPhaseError` khi set password fail. Caller chain đã đúng.

## Expected Behavior

Tham chiếu `bugfix.md` section 2:

- **2.1** Exception từ `page.evaluate` → raise `BrowserPhaseError` với context "set_password OTC".
- **2.2** HTTP non-2xx → raise.
- **2.3** HTTP 200 nhưng body báo lỗi (`error` / `detail` field) → raise.
- **2.4** Caller mark job failed thay vì success.

Preservation (bugfix.md section 3):

- **3.1** OTC + register HTTP 2xx + body OK → tiếp tục fill /about-you, return callback_url.
- **3.2** Nhánh `password_create` (non-OTC) → giữ nguyên semantics (HTTP 200 → success, 409/already → continue, else → raise).
- **3.3** Login OTP của account đã có password → `one_time_code_mode=False`, không gọi `_REGISTER_USER_JS`, flow nguyên vẹn.
- **3.4** OTC success → vẫn gọi `_wait_oai_sc`, `_fill_about_you`, `_wait_chatgpt_session` đúng thứ tự.
- **3.5** Caller xử lý `BrowserPhaseError` cho các flow khác → không thay đổi semantics.

## Hypothesized Root Cause

Tác giả ban đầu thiết kế nhánh OTC theo giả định "set password chỉ là best-effort" — vì account đã verify OTP, user đã login được. Nếu set password fail thì user vẫn dùng one-time code lần sau được. Nên dùng `try/except Exception: log only`.

Tuy nhiên, thực tế bug thể hiện ở 2 cấp:

1. **Hợp đồng caller không khớp giả định**: caller (`cli.py`, `web/manager.py`) coi return value của `run_browser_phase` là "signup thành công với password đã yêu cầu", không phải "session đã verify OTP". Combo / job được mark success → user dùng password để login → fail.
2. **Vi phạm fail-fast rule** (đã ghi rõ trong `AGENTS.md` & `project-rules.md`): không nuốt exception, không fallback che lỗi.

Cách "log only" không bao giờ đúng với hợp đồng `run_browser_phase` này. Phải fail-fast.

Logic validation đã có sẵn ở `password_create` branch (line 463-481) nhưng không được tái sử dụng cho nhánh OTC — thiếu DRY là root cause kỹ thuật.

## Correctness Properties

### Property 1: Fix Checking — bug condition phải fail-fast

**Validates: Requirements 2.1, 2.2, 2.3, 2.4**

Với mọi input thuộc bug condition (`one_time_code_mode=True` AND register call fail theo bugfix.md), `drive_signup_flow'(X)` phải raise `BrowserPhaseError`, không fill `/about-you`, không return `callback_url`.

```pascal
FOR ALL X WHERE isBugCondition(X) DO
  result ← drive_signup_flow'(X)
  ASSERT result is BrowserPhaseError
    AND error_reason_indicates_set_password_failed(result)
    AND about_you_was_NOT_filled(X)
    AND callback_url_was_NOT_returned(X)
    AND caller_marks_job_as_failed(X)
END FOR
```

### Property 2: Preservation Checking — non-buggy input không thay đổi

**Validates: Requirements 3.1, 3.3, 3.4, 3.5**

Với mọi input KHÔNG thuộc bug condition, `drive_signup_flow'(X) = drive_signup_flow(X)` về behavior quan sát được (callback_url, exception, log sequence semantically equivalent).

```pascal
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT drive_signup_flow(X) ≡ drive_signup_flow'(X)
END FOR
```

### Property 3: Refactor Regression — password_create branch giữ semantics

**Validates: Requirements 3.2**

Refactor `password_create` dùng helper `_assert_register_success(accept_already_exists=True)` không được thay đổi outcome cho mọi input đã quan sát được trên flow hiện tại (success, already_exists, raise).

```pascal
FOR ALL Y in password_create_branch_inputs DO
  ASSERT outcome_before_refactor(Y) = outcome_after_refactor(Y)
END FOR
```

⚠ Exception đã đánh dấu trong section Risk: trường hợp HTTP 200 + body có `error`/`detail` → behavior MỚI (raise) thay vì pass-through. Đây là sự thay đổi có chủ đích, được chấp nhận với risk thấp; nếu false positive xảy ra trên prod, mở flag mở rộng helper.

## Fix Implementation

### 1. New helper `_assert_register_success` (browser_phase.py, đặt sau `_REGISTER_USER_JS` ~line 82)

```python
def _assert_register_success(
    result: object,
    *,
    context: str,
    accept_already_exists: bool = False,
) -> tuple[str, dict]:
    """Validate response của _REGISTER_USER_JS, fail-fast nếu fail.

    Args:
        result: giá trị từ `await page.evaluate(_REGISTER_USER_JS, ...)`.
            Expected shape: {"status": int, "body": dict | str}.
        context: prefix cho error message để distinguish callsite trong logs
            (vd "register password_create", "set_password OTC (about_you)").
        accept_already_exists:
            True  → HTTP 409 hoặc body chứa "already" / "exists" → return
                    ("already_exists", body) thay vì raise. password_create cần.
            False → strict, mọi non-success → raise. OTC cần.

    Returns:
        ("success", body_dict) — HTTP 2xx và body không báo lỗi.
        ("already_exists", body_dict) — chỉ khi accept_already_exists=True và
        match điều kiện 409/"already"/"exists".

    Raises:
        BrowserPhaseError: shape sai, status non-2xx, body có error/detail key,
        hoặc 409/already khi accept_already_exists=False.
    """
    if not isinstance(result, dict):
        raise BrowserPhaseError(f"{context}: unexpected result shape: {result!r}")

    status = result.get("status")
    body = result.get("body")
    if body is None:
        body = {}
    body_str = json.dumps(body) if isinstance(body, dict) else str(body)

    is_2xx = isinstance(status, int) and 200 <= status < 300
    looks_already_exists = (
        status == 409
        or "already" in body_str.lower()
        or "exists" in body_str.lower()
    )

    if not is_2xx:
        if accept_already_exists and looks_already_exists:
            return ("already_exists", body if isinstance(body, dict) else {})
        raise BrowserPhaseError(
            f"{context}: HTTP {status}: {body_str[:200]}"
        )

    # 2xx — kiểm tra body báo lỗi dù status OK
    if isinstance(body, dict):
        for err_key in ("error", "detail"):
            if body.get(err_key):
                raise BrowserPhaseError(
                    f"{context}: HTTP {status} but body.{err_key}: {body_str[:200]}"
                )

    return ("success", body if isinstance(body, dict) else {})
```

### 2. Refactor `password_create` branch (browser_phase.py:453-481)

Thay block hiện tại bằng:

```python
if screen == "password_create":
    if register_attempted:
        await asyncio.sleep(1.0)
        continue
    log(f"[flow] POST /api/accounts/user/register (email={request.email})")
    result = await page.evaluate(
        _REGISTER_USER_JS, {"username": request.email, "password": request.password},
    )
    register_attempted = True
    outcome, body = _assert_register_success(
        result,
        context="register password_create",
        accept_already_exists=True,
    )
    if outcome == "already_exists":
        log("[flow] account already exists — page sẽ chuyển login")
        await asyncio.sleep(1.5)
        continue
    # outcome == "success"
    continue_url = body.get("continue_url") if isinstance(body, dict) else None
    log(f"[flow] register OK → continue_url={continue_url}")
    if continue_url:
        if continue_url.startswith("/"):
            continue_url = f"https://auth.openai.com{continue_url}"
        await page.goto(continue_url, wait_until="domcontentloaded")
    await asyncio.sleep(1.0)
    continue
```

### 3. Sửa OTC branch (browser_phase.py:701-715)

Thay block hiện tại bằng:

```python
if one_time_code_mode:
    log(f"[flow] POST /api/accounts/user/register (set password OTC, email={request.email})")
    reg = await page.evaluate(
        _REGISTER_USER_JS,
        {"username": request.email, "password": request.password},
    )
    _outcome, _body = _assert_register_success(
        reg,
        context="set_password OTC (about_you)",
        accept_already_exists=False,
    )
    log("[flow] set password OTC OK")
```

Chú ý:
- Bỏ `try/except Exception` — exception từ `page.evaluate` (network, page closed, JS exception) propagate ra `run_browser_phase` → `BrowserPhaseError` qua except handler ở caller.
- `accept_already_exists=False` — strict, vì account đã tồn tại (vừa OTC login), 409/already không có ý nghĩa fallback.
- Helper raise `BrowserPhaseError` với context "set_password OTC (about_you)" → log dễ truy.

## Testing Strategy

Tuân thủ project rule: file thật trong `test/`, không inline `python3 -c`.

### `test/test_otc_register_assert.py` — unit test helper (không cần Playwright)

Mỗi case = một test function, đối chiếu với counterexamples từ `bugfix.md`.

| # | Input | accept_already_exists | Expected |
|---|---|---|---|
| 1 | `{"status": 200, "body": {"continue_url": "/x"}}` | both | `("success", {"continue_url": "/x"})` |
| 2 | `{"status": 201, "body": {}}` | both | `("success", {})` |
| 3 | `{"status": 204, "body": None}` | both | `("success", {})` |
| 4 | `"oops"` | both | raise (`unexpected result shape`) |
| 5 | `None` | both | raise |
| 6 | `{"body": {}}` (status missing) | both | raise (`HTTP None`) |
| 7 | `{"status": 400, "body": {"error": "weak_password"}}` | both | raise (`HTTP 400`) |
| 8 | `{"status": 500, "body": {}}` | both | raise (`HTTP 500`) |
| 9 | `{"status": 200, "body": {"error": "session_invalid"}}` | both | raise (`body.error`) |
| 10 | `{"status": 200, "body": {"detail": "..."}}` | both | raise (`body.detail`) |
| 11 | `{"status": 409, "body": {}}` | False | raise |
| 12 | `{"status": 409, "body": {}}` | True | `("already_exists", {})` |
| 13 | `{"status": 422, "body": {"message": "already exists"}}` | False | raise |
| 14 | `{"status": 422, "body": {"message": "already exists"}}` | True | `("already_exists", {"message": ...})` |
| 15 | `{"status": 200, "body": "ok"}` | both | `("success", {})` (body string không match error key) |

Run: `python3 test/test_otc_register_assert.py`.

### `test/check_otc_branch_raise.py` — smoke nhánh OTC

Mock fake `page` async object để chạy đoạn OTC isolated. Verify:

1. Khi `page.evaluate` raise → block raise lại `BrowserPhaseError` (vì bỏ `except Exception`).
2. Khi `page.evaluate` return `{"status": 400, ...}` → block raise `BrowserPhaseError` chứa "set_password OTC".
3. Khi `page.evaluate` return `{"status": 200, "body": {"error": "..."}}` → block raise.
4. Khi `page.evaluate` return `{"status": 200, "body": {}}` → block không raise, log "set password OTC OK".

Strategy: extract đoạn OTC thành function nhỏ có thể test riêng, hoặc dùng AST/source-level isolation. Đơn giản nhất: gọi trực tiếp `_assert_register_success` với fake result + fake exception path đã cover trong test #1; smoke chỉ verify integration "exception không bị nuốt" qua wrapper async test gọi trực tiếp coroutine `await page.evaluate` mock raise rồi assert exception leak.

Run: `python3 test/check_otc_branch_raise.py`.

### Out of scope test

- Playwright integration end-to-end (cần OpenAI sandbox, không khả thi).
- Test caller chain (`signup.py`, `cli.py`, `web/manager.py`) — không thay đổi.

## Risk & Regression Analysis

### Code paths đi qua nhánh OTC about_you

Path duy nhất: signup mới → password sai 3 lần ở `password_login` → click "Log in with a one-time code" → `one_time_code_mode=True` → poll OTP → submit → `screen == "about_you"` → set password OTC.

Sau fix: nếu set password fail → raise sớm, không fill `/about-you`, caller mark failed. **Đây chính là behavior mong muốn** (bugfix.md 2.x).

### Regression risk: `password_create` branch

Behavior trước fix:
- Result không phải dict → raise.
- HTTP 200 → success (extract `continue_url`).
- HTTP 409 OR body chứa "already" / "exists" → continue (fallback OTP login).
- Else → raise.

Behavior sau refactor (`_assert_register_success(accept_already_exists=True)`):
- Result không phải dict → raise. ✓
- HTTP 200 + body không có `error`/`detail` → ("success", body). ✓
- HTTP 200 + body có `error`/`detail` → raise. ⚠ **Behavior MỚI** — trước đây không check.
- HTTP 409 OR body chứa "already" → ("already_exists", body). ✓
- Else (HTTP non-2xx, không match already) → raise. ✓

**Đánh giá điểm mới**: nếu OpenAI register endpoint có case trả 200 với body báo error hợp lệ (soft-fail không phải lỗi thật) → fix gây false positive. Nhưng trong logs hiện tại, 200 + `continue_url` là expected success path; 200 + body error chưa từng thấy. **Nguy cơ thấp**, prefix log "register password_create" giúp truy ngay nếu xảy ra.

Mở rộng nếu cần: thêm flag `lenient_body_error_for_200` cho password_create. **Không thêm ngay** — fail-fast trước, nới lỏng dựa trên evidence.

### Regression risk: nhánh `about_you` khi OTC success

Path success: HTTP 200 + body OK → helper return ("success", body) → log "set password OTC OK" → tiếp tục `_wait_oai_sc`, `_fill_about_you`, `_wait_chatgpt_session`. ✓

### Regression risk: nhánh `about_you` khi `one_time_code_mode=False`

Path: signup bình thường → `one_time_code_mode=False` → bỏ qua block set password → tiếp tục flow. ✓ Không thay đổi.

## Out of Scope

- Caller chain (`signup.py`, `cli.py`, `web/manager.py`) — đã đúng, không sửa.
- Retry logic OTP / login.
- DB schema field cho error code (giữ message string như pattern hiện tại của `BrowserPhaseError`).
- `_register_with_password` function (line 121-217) — không gọi từ flow chính `_drive_signup_screens`. Nếu sau fix có audit thấy dead code thì xử lý spec khác.

## Implementation Order

1. Thêm `_assert_register_success` helper.
2. Refactor `password_create` branch dùng helper.
3. Refactor `about_you` OTC branch dùng helper.
4. Viết `test/test_otc_register_assert.py` + `test/check_otc_branch_raise.py`.
5. Run test, verify pass.
