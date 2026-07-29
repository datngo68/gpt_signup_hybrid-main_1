# Bugfix Requirements Document

## Introduction

Luồng **Get Session** (`session_phase.py` → `_get_session_browser` → `_drive_session_flow`)
login ChatGPT bằng browser thật: bootstrap NextAuth → navigate authorize →
fill password → submit → (nếu MFA) fill TOTP → đợi session cookies → fetch
`/api/auth/session`. Luồng **Get Link** (`payment_link.py`) phụ thuộc
`accessToken` lấy được từ Get Session.

Sau khi submit password, code chờ một khoảng **cố định 3 giây** rồi kiểm tra MFA
**đúng một lần** (Step 4): `if "mfa" in page.url or "mfa" in content[:5000]`. Khi
account có 2FA và redirect tới `/mfa-challenge/...` xảy ra **muộn hơn mốc 3s**
(điển hình khi đi qua proxy chậm), điều kiện check trả về `False` → flow **bỏ qua
hoàn toàn bước nhập TOTP**, rơi thẳng xuống Step 5 (đợi session cookies 30s). Vì
trang đang ở `/mfa-challenge` và không có ai nhập code, cookies không bao giờ xuất
hiện → **timeout** với URL `auth.openai.com/mfa-challenge/...`.

Log thực tế minh chứng (proxy `116.104.92.161`):

```
[06:21:31] clicked button[type="submit"]          ← submit password
[06:21:34] after password: .../log-in/password    ← +3s VẪN ở trang password (chưa redirect)
[06:22:05] timeout waiting session cookies.
           URL: .../mfa-challenge/6a1b2ad1...      ← +30s: đã ở MFA nhưng không nhập code
```

Khi chạy **direct (không proxy)**, redirect thường nhanh hơn 3s nên MFA check kịp
bắt → vì vậy bug chỉ biểu hiện rõ khi dùng proxy. Đây là **race condition**: thời
điểm kiểm tra trạng thái cố định, không chờ trạng thái page ổn định.

Bug vi phạm nguyên tắc thiết kế của dự án: detection phải dựa trên **trạng thái
quan sát được** (polling tới khi đạt terminal state) thay vì **mốc thời gian
phỏng đoán**. Hệ quả: Get Session fail hàng loạt khi dùng proxy với account 2FA,
kéo theo Get Link fail vì không có `accessToken`.

## Bug Analysis

### Current Behavior (Defect)

Bối cảnh chung cho mọi điều kiện dưới đây: account đăng nhập **có 2FA enabled**,
caller truyền `secret` hợp lệ, và sau khi submit password trang redirect tới
`/mfa-challenge/...` (đây là hành vi bình thường của account 2FA).

1.1 WHEN sau submit password, trang redirect tới `/mfa-challenge/...` **muộn hơn mốc kiểm tra cố định 3s** (vd proxy chậm) THEN tại thời điểm Step 4 chạy, `page.url` vẫn là `/log-in/password` và `page.content()` chưa chứa marker `"mfa"` → điều kiện MFA = `False` → the system bỏ qua nhánh nhập TOTP

1.2 WHEN nhánh nhập TOTP bị bỏ qua (theo 1.1) THEN the system chuyển thẳng sang Step 5 (poll session cookies 30s) trong khi trang đang dừng tại `/mfa-challenge/...` chờ nhập code

1.3 WHEN Step 5 poll cookies trong lúc trang kẹt ở `/mfa-challenge/...` THEN không có cookie `__Secure-next-auth.session-token` nào xuất hiện → the system raise `SessionError("timeout waiting session cookies. URL: .../mfa-challenge/...")` sau 30s

1.4 WHEN Get Session fail theo 1.3 THEN luồng Get Link (`payment_link.get_checkout_url`) không nhận được `accessToken` hợp lệ → cũng fail (hệ quả gián tiếp)

1.5 WHEN auto-retry chạy lại Get Session với cùng proxy chậm THEN mỗi lần lặp lại đều dính cùng race condition tại Step 4 → fail lặp đi lặp lại (log: retry 1/5, 2/5, 3/5... đều timeout tại cùng `mfa-challenge` URL)

### Expected Behavior (Correct)

2.1 WHEN sau submit password, account có 2FA và trang sẽ redirect tới `/mfa-challenge/...` THEN the system SHALL **poll trạng thái page tới khi đạt một terminal state** (MFA challenge / đã đăng nhập chatgpt.com / login error) trong một deadline đủ dài, KHÔNG dựa vào mốc cố định 3s

2.2 WHEN polling phát hiện trang đã ở `/mfa-challenge/...` (qua URL hoặc content marker) tại bất kỳ thời điểm nào trong deadline THEN the system SHALL nhập TOTP code (sinh từ `secret`) và submit, bất kể redirect xảy ra ở giây thứ 1 hay giây thứ 10

2.3 WHEN account có 2FA nhưng caller KHÔNG truyền `secret` THEN the system SHALL raise `SessionError` báo thiếu secret (giữ nguyên hành vi fail-fast hiện có), KHÔNG đợi vô ích tới timeout cookies

2.4 WHEN TOTP đã được nhập và submit thành công THEN the system SHALL tiếp tục Step 5 (đợi session cookies) và Step 6 (fetch `/api/auth/session`) như hiện tại, trả về session JSON hợp lệ

2.5 WHEN Get Session trả session JSON có `accessToken` hợp lệ THEN luồng Get Link SHALL nhận được token và tạo checkout URL bình thường

### Unchanged Behavior (Regression Prevention)

3.1 WHEN account KHÔNG có 2FA (sau submit password redirect thẳng về chatgpt.com hoặc about-you) THEN the system SHALL CONTINUE TO không nhập TOTP, poll session cookies và fetch session JSON như hiện tại

3.2 WHEN chạy Get Session **không qua proxy** (direct) với account 2FA, redirect tới `/mfa-challenge` nhanh THEN the system SHALL CONTINUE TO detect MFA, nhập TOTP và lấy session thành công (fix không được làm hỏng luồng direct đang chạy được)

3.3 WHEN password sai (trang báo lỗi đăng nhập, không redirect MFA cũng không vào chatgpt) THEN the system SHALL fail-fast với lỗi rõ ràng thay vì đợi tới hết deadline rồi mới timeout chung chung

3.4 WHEN đã nhập TOTP và Step 5 vẫn không thấy session cookies trong deadline (vd code sai / mạng đứt) THEN the system SHALL CONTINUE TO raise `SessionError` timeout như cơ chế hiện có

3.5 WHEN flow ở các bước trước Step 4 (bootstrap, navigate authorize, fill email/password) THEN the system SHALL CONTINUE TO giữ nguyên logic hiện tại, fix chỉ chạm vào đoạn detection sau submit password

3.6 WHEN lỗi xảy ra TRƯỚC submit password (driver pipe đóng, network/proxy lỗi) THEN the system SHALL CONTINUE TO retry launch theo cơ chế `_LAUNCH_RETRY_MAX` hiện có; còn sau submit password vẫn fail-fast không retry (tránh login spam) — semantics retry không đổi

## Bug Condition & Property (Pseudocode)

### Bug Condition Function

```pascal
FUNCTION isBugCondition(X)
  INPUT:
    X = {
      has_mfa: boolean,             // account có 2FA enabled
      has_secret: boolean,          // caller truyền secret TOTP
      mfa_redirect_delay: float     // giây từ lúc submit password đến khi /mfa-challenge xuất hiện
    }
  OUTPUT: boolean

  // Chỉ thuộc bug khi account có 2FA và có secret để vượt qua (đáng lẽ phải thành công).
  IF has_mfa = false THEN
    RETURN false
  END IF
  IF has_secret = false THEN
    RETURN false      // case này đã fail-fast đúng, không phải bug
  END IF

  // Code hiện tại check MFA đúng một lần tại mốc cố định FIXED_MFA_CHECK_DELAY (= 3.0s).
  // Nếu redirect MFA xảy ra SAU mốc đó → detection miss → timeout cookies.
  IF mfa_redirect_delay > FIXED_MFA_CHECK_DELAY THEN
    RETURN true
  END IF

  RETURN false
END FUNCTION
```

### Property: Fix Checking

```pascal
// Với mọi account 2FA + có secret, bất kể redirect MFA nhanh hay chậm (trong deadline),
// F'(X) phải detect MFA, nhập TOTP và lấy được session — KHÔNG timeout cookies do miss MFA.
FOR ALL X WHERE isBugCondition(X) DO
  result ← get_session'(X)
  ASSERT mfa_was_detected(X)
    AND totp_was_submitted(X)
    AND result_is_valid_session_json(result)
    AND NOT timed_out_at_mfa_challenge(result)
END FOR
```

### Property: Preservation Checking

```pascal
// Với mọi input KHÔNG thuộc bug condition, F'(X) phải hành xử tương đương F(X).
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT get_session(X) ≡ get_session'(X)
END FOR

// Cụ thể:
//  - has_mfa=false              → không nhập TOTP, lấy session bình thường (3.1)
//  - has_mfa=true, fast redirect → detect MFA, nhập TOTP, lấy session (3.2)
//  - has_mfa=true, no secret     → raise SessionError thiếu secret (2.3)
//  - sai password                → fail-fast login error (3.3)
```

### Counterexamples (Concrete)

- `has_mfa=true, has_secret=true, mfa_redirect_delay=8.0s` (proxy chậm) → **hiện tại**: miss MFA tại mốc 3s, timeout cookies tại `/mfa-challenge`. **Đúng**: poll, detect MFA ở ~8s, nhập TOTP, trả session JSON.
- `has_mfa=true, has_secret=true, mfa_redirect_delay=15.0s` (proxy rất chậm) → hiện tại timeout; đúng: vẫn detect trong deadline, lấy session.
- `has_mfa=true, has_secret=true, mfa_redirect_delay=1.0s` (direct) → **preserve**: detect MFA, nhập TOTP, lấy session (không hồi quy).
- `has_mfa=false` → **preserve**: redirect chatgpt.com, không TOTP, lấy session.
- `has_mfa=true, has_secret=false` → **preserve**: raise `SessionError` thiếu secret, không đợi tới timeout.

## Notes / Scope

- Root cause khu trú trong `session_phase.py`, đoạn Step 4 (MFA detection) + cách nối sang Step 5 của `_drive_session_flow`. Sửa bằng cách thay "sleep 3s + check một lần" bằng **polling chờ terminal state**.
- Get Link (`payment_link.py`) **không sửa** trong spec này — fail của nó là hệ quả của Get Session fail. Sau fix, nếu Get Link vẫn lỗi độc lập qua proxy thì mở spec riêng.
- Không đụng cơ chế retry launch (`_browser_retry.py`) — semantics "không retry sau submit password" giữ nguyên.
