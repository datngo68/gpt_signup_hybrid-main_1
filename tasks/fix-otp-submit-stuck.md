# Fix OTP Submit Stuck — Task Report

**Date**: 2026-05-25
**File**: `browser_phase.py` (+109 / -26 lines)
**Status**: Done

---

## Bug Report

Auto reg chạy bị stuck sau khi submit OTP: page không navigate, tool cứ retry click submit button vô hạn cho tới khi job timeout (CancelledError).

```
[21:07:15] [browser] typing OTP 043756
[21:07:15] [browser] clicked button[type="submit"]
[21:07:41] [flow] OTP screen vẫn ở đây sau submit — thử click submit lại
[21:08:10] [flow] OTP screen vẫn ở đây sau submit — thử click submit lại
[21:08:28] [browser] camoufox runner exception: CancelledError
```

## Root Cause Analysis

### Bug 1: Counter reset tạo dead code (critical)

```python
# TRƯỚC — BUG
if same_screen_count > 30:      # trigger ở ~15s
    ... re-click submit ...
    same_screen_count = 0       # RESET VỀ 0!

if same_screen_count > 60:      # DEAD CODE — không bao giờ chạy
    ... re-poll OTP code mới ...  # vì counter vừa reset về 0 ở trên
```

Flow thực tế: submit → đợi 15s → re-click → reset → đợi 15s → re-click → reset → ... → job timeout. Branch re-poll code mới (`> 60`) không bao giờ execute.

### Bug 2: Counter-based timing không chính xác

Mỗi iteration không phải 0.5s mà là ~1-2s vì `_detect_screen()` query DOM nhiều lần với timeout (200ms-800ms mỗi selector). `same_screen_count == 30` thực tế là ~30-60s chứ không phải ~15s.

### Bug 3: Chỉ có 1 strategy retry

Khi UI click submit không work (ví dụ OpenAI đổi JS handler, thêm Turnstile challenge, form submission bị block), code chỉ biết re-click cùng button đó — không có fallback nào khác.

### Bug 4: `_wait_after_otp` (login code path) thiếu API fallback

Function `_wait_after_otp` (dùng cho login-after-OTP) chỉ retry click submit 1 lần rồi chờ timeout. Không có JS submit hay API fallback.

## Changes

### 1. Wall-clock time thay counter (`_drive_signup_flow`)

```python
# SAU — dùng time.monotonic() cho timing chính xác
_otp_submit_ts = time.monotonic()  # set khi submit OTP

_otp_wait_elapsed = time.monotonic() - _otp_submit_ts
if _otp_wait_elapsed > 10.0 and not _otp_reclick_done:
    ...
elif _otp_wait_elapsed > 18.0 and not _otp_js_submit_done:
    ...
elif _otp_wait_elapsed > 25.0 and not _otp_api_done:
    ...
elif _otp_wait_elapsed > 35.0:
    ... re-poll code mới, reset all flags ...
```

### 2. Escalation 4 bước (cả 2 code paths)

| Elapsed | Strategy | Mô tả |
|---------|----------|-------|
| 10s | UI re-click | Click `button[type="submit"]` / `Continue` / `Verify` |
| 18s | JS `form.submit()` | Bypass JS event handler bị block, gọi native form submit |
| 25s | API POST `/email-otp/validate` | Bypass browser hoàn toàn, dùng context cookies |
| 35s | Re-poll OTP | Clear input, reset flags, poll mail provider cho code mới |

### 3. Turnstile/Cloudflare challenge detection

Thêm screen type `turnstile_challenge` trong `_detect_screen()`:
- Detect: `iframe[src*="challenges.cloudflare.com"]`, `#cf-turnstile`, `.cf-turnstile`, `[data-turnstile-callback]`
- Handler: log + wait (Camoufox tự solve), fail-fast nếu stuck >60 iterations

### 4. `_wait_after_otp` upgrade

- Thêm parameter `ctx` để có thể gọi `_submit_otp_via_api()`
- Cùng escalation pattern 10s → 18s → 25s
- URL logging khi retry submit

### 5. State reset consistency

Khi submit code mới (từ error detection branch hoặc fresh poll), reset tất cả flags:
```python
_otp_submit_ts = time.monotonic()
_otp_reclick_done = False
_otp_js_submit_done = False
_otp_api_done = False
```

## Files Changed

| File | Lines | Description |
|------|-------|-------------|
| `browser_phase.py` | +109 / -26 | OTP submit escalation, Turnstile detection, wall-clock timing |

## Risk Assessment

- **Low risk**: Các strategy mới (JS submit, API fallback) được wrap trong try/except, fail không ảnh hưởng flow chính
- **Backward compatible**: Timing mới (10s/18s/25s/35s) nằm trong budget cũ, không thay đổi overall timeout
- **Testable**: Log messages rõ ràng cho từng escalation step, dễ trace trong production
