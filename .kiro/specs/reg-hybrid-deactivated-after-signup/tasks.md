# Implementation Plan

> Tiền đề (design): `chatgpt_camoufox` là golden — **CẤM sửa**. Mọi fix chỉ trong `reg_hybrid/`.
> Theo AGENTS.md: mọi test nằm trong `test/`, chạy `python3 test/<file>.py`. **Cấm** inline `python3 -c`.
> Hai nhánh root cause: A (request layer, `relay.py`) fix vô điều kiện; B (oracle/token layer) fix
> CÓ ĐIỀU KIỆN — chỉ commit sau khi bước phân loại (task 2) xác nhận ban xảy ra trên happy path.

- [x] 1. Viết test reproduce drift hybrid vs golden (TRƯỚC khi fix)
  - **Property 1: Bug Condition** - Request & token signature lệch golden
  - File: `test/check_hybrid_drift.py` (theo AGENTS.md — cấm inline `python3 -c`)
  - **CRITICAL**: Test này PHẢI FAIL/quan sát được counterexample trên code CHƯA fix — xác nhận bug tồn tại
  - **DO NOT** sửa test hay code khi nó fail ở task này
  - **NOTE**: Test encode hành vi đúng (Property 1) — sẽ validate fix khi PASS sau khi implement
  - **GOAL**: Surface counterexample chứng minh `isBugCondition(X)` thành lập với `mode="hybrid"`
  - **Scoped PBT Approach**: scope vào case deterministic để reproduce ổn định
  - Setup offline: `HybridChatGPTRelay` với `session` giả (fake curl recorder ghi `method+url+body+headers`),
    `tokens` giả (stub `mint_token`/`mint_so` đếm số lần gọi), `mail_provider` giả trả OTP theo kịch bản;
    song song golden `ChatGPTRelay` cùng fake session + OTP về ngay (không network/Camoufox thật)
  - Case A1 — double-POST: kịch bản OTP đầu sai → assert recorder ghi `create_account` **>1 lần** trên code cũ (counterexample nhánh A)
  - Case A2 — step diff happy path: OTP về ngay → so `relay.steps` hybrid vs golden, assert lệch ở chỗ ngoài OTP loop (nếu có drift) / xác nhận giống
  - Case B1 — synthetic feeder: assert đường mint `so` của `browser_pool` có inject `_OBSERVER_FEEDER_JS`/`_OBSERVER_BURST_JS` trên code cũ (counterexample nhánh B: token từ event giả)
  - Case B2 — shared browser: assert pool tái dùng cùng browser instance cho 2 lần `acquire` khác signup (counterexample cluster fingerprint) trên code cũ
  - Assertion khớp Expected Behavior Properties (Property 1) trong design: create_account 1 lần, step sequence ngoài OTP loop = golden, `so` không từ synthetic events
  - Chạy: `python3 test/check_hybrid_drift.py` trên code CHƯA fix
  - **EXPECTED OUTCOME**: Test FAIL (đúng — chứng minh drift tồn tại)
  - Document counterexample (create_account POST 2 lần khi OTP retry; `so` từ synthetic events; browser dùng lại giữa signup)
  - _Requirements: 2.1, 2.2, 2.3_

- [x] 2. Bước phân loại root cause (BẮT BUỘC — gate cho nhánh B)
  - File: `test/check_happy_path_request_diff.py` (theo AGENTS.md — cấm inline `python3 -c`)
  - **IMPORTANT**: Đây là gate quyết định nhánh A đủ hay cần commit nhánh B — chạy TRƯỚC khi đụng oracle layer
  - Với cùng input happy path (OTP về ngay, 1× send + 1× validate, không retry): ghi đầy đủ
    `method + url + body + header order + cookie` của MỌI request hybrid và golden, rồi diff
  - **Quyết định**:
    - Nếu happy path **giống hệt** golden nhưng account thực vẫn bị ban → loại trừ nhánh A, root cause là nhánh B → **mở gate** commit task 5
    - Nếu happy path **khác** golden (double-POST chỉ xuất hiện khi retry, happy path sạch) → root cause là nhánh A → fix task 4 là đủ, **KHÔNG** đụng oracle layer (tránh hi sinh tốc độ pool)
  - Chạy: `python3 test/check_happy_path_request_diff.py`
  - **EXPECTED OUTCOME**: In ra diff rõ ràng + kết luận nhánh; ghi quyết định gate cho task 5
  - _Requirements: 2.1, 2.3_

- [x] 3. Viết preservation property test baseline (TRƯỚC khi fix)
  - **Property 2: Preservation** - Hành vi golden + smart OTP loop không đổi
  - File: `test/test_hybrid_relay_preservation.py` (theo AGENTS.md — cấm inline `python3 -c`)
  - **IMPORTANT**: Theo observation-first — chạy code CHƯA fix với input non-bug (`isBugCondition` = false), ghi output thật rồi chốt thành assert
  - **Why PBT**: preservation là property phổ quát ("for all non-buggy inputs") — property-based sinh nhiều case, bắt edge case unit test bỏ sót
  - Observe + chốt: smart OTP loop bất biến — resend / multi-code `poll_all_codes` / `prefer_second_code` / verify-retry `_otp_validate_soft` / human-like delay 2–4s / `prefer_newest_untried_otp_sync` (3.2)
  - Observe + chốt: kế thừa bất biến — `_dd_s`/`oai-did`/sentinel/device_id/headers vẫn từ `super().__init__()`, subclass không override (3.3)
  - Observe + chốt: happy path request y hệt golden từ đầu tới `get_session` (3.4)
  - Observe + chốt: no-pre-mint — sentinel `oauth_create_account` chỉ mint tại `create_account` sau OTP validate OK, không spawn pre-mint thread (`on_otp_poll_start=None`) (3.5)
  - Observe + chốt: `SignupResult` đủ `session_token`/`access_token`/`account_id`/`cookies` theo schema (3.6)
  - Observe + chốt: không file nào trong `chatgpt_camoufox/` bị sửa (3.1)
  - Dùng property-based (sinh kịch bản OTP / thứ tự bước / số lần retry) cho preservation
  - Chạy: `python3 test/test_hybrid_relay_preservation.py` trên code CHƯA fix
  - **EXPECTED OUTCOME**: Tất cả preservation test PASS (chốt baseline cần giữ)
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

- [x] 4. Fix nhánh A — `reg_hybrid/relay.py` (VÔ ĐIỀU KIỆN)

  - [x] 4.1 Bỏ `_create_account_with_retry` double-POST
    - Trong `run()`, thay `callback_url = self._create_account_with_retry()` bằng `callback_url = self.create_account()` — gọi đúng method golden kế thừa, **đúng 1 lần**/session
    - Xóa hẳn helper `_create_account_with_retry` (dead-path: dựa trên cache `_patched_mint_token` không còn active vì `on_otp_poll_start=None`)
    - Nếu `create_account` raise → để propagate lên `run_hybrid_signup` outer-loop classify như golden, KHÔNG re-POST trên cùng session
    - _Bug_Condition: isBugCondition(X) — createAccountPostCount(X) > 1_
    - _Expected_Behavior: createAccountPostCount(result) = 1 như golden_
    - _Preservation: smart OTP loop, kế thừa golden, no-pre-mint, schema không đổi_
    - _Requirements: 2.1, 2.2, 2.3_

  - [x] 4.2 Thu hẹp delta `run()` về đúng OTP loop
    - Cấu trúc lại `run()` (giữ override vì CẤM sửa golden) để mọi bước ngoài OTP gọi nguyên method kế thừa (`get_csrf`/`signin`/`authorize`/`register`/`otp_send`/`create_account`/`callback`/`get_session`) — không thêm side-effect
    - Tách smart OTP thành method riêng `_acquire_and_validate_otp()` để `run()` đọc ra đúng skeleton golden + 1 điểm khác
    - Mục tiêu: diff `run()` hybrid vs golden chỉ còn ở block OTP
    - _Bug_Condition: isBugCondition(X) — stepSequence(X, exclude=OTP_LOOP) != golden_
    - _Expected_Behavior: stepSequence(result, exclude=OTP_LOOP) = golden_stepSequence_
    - _Preservation: smart OTP loop (delta hợp lệ duy nhất) không đổi_
    - _Requirements: 2.1, 2.3_

  - [x] 4.3 Test guard chống drift tương lai
    - Thêm test (trong `test/check_happy_path_request_diff.py` hoặc file guard riêng) so khớp `relay.steps` hybrid (happy path, OTP 1 lần) với golden `ChatGPTRelay.run()` — bất kỳ bước/thứ tự lệch nào ngoài OTP loop sẽ FAIL (thay cho template-method không thể chèn vào golden)
    - _Bug_Condition: isBugCondition(X) — drift bước ngoài OTP loop_
    - _Expected_Behavior: step sequence ngoài OTP loop luôn khớp golden_
    - _Requirements: 2.1, 2.3_

  - [x] 4.4 Verify drift test (task 1) — phần nhánh A giờ PASS
    - **Property 1: Expected Behavior** - create_account 1 lần + step sequence khớp golden
    - **IMPORTANT**: Chạy LẠI cùng test `test/check_hybrid_drift.py` từ task 1 — KHÔNG viết test mới
    - Chạy: `python3 test/check_hybrid_drift.py`
    - **EXPECTED OUTCOME**: Case A1 (double-POST) và A2 (step diff) PASS (xác nhận drift nhánh A đã fix)
    - _Requirements: 2.1, 2.2, 2.3_

- [x] 5. Fix nhánh B — oracle/token layer (CÓ ĐIỀU KIỆN — GATED bởi task 2)
  - **GATE**: Chỉ commit task này khi bước phân loại (task 2) xác nhận ban xảy ra trên happy path (loại trừ nhánh A). Nếu task 2 kết luận root cause là nhánh A → **BỎ QUA** task 5, KHÔNG đụng oracle layer.
  - Files: `reg_hybrid/browser_pool.py`, `reg_hybrid/camoufox_factory.py`, `reg_hybrid/runner.py`

  - [x] 5.1 Loại synthetic Observer feeder (ứng viên #1)
    - Bỏ inject `_OBSERVER_FEEDER_JS` + `_OBSERVER_BURST_JS` khỏi `_acquire_context_in_thread`/`_mint_so_in_thread` trong `browser_pool.py`
    - Để `sessionObserverToken` chạy đúng như golden `CamoufoxTokenGenerator` (page tĩnh, không feed event)
    - Nếu `so` empty vì page tĩnh headless → giải bằng cơ chế golden-compatible (non-headless cho oracle, hoặc dùng thẳng `CamoufoxTokenGenerator` golden), KHÔNG bù bằng event giả
    - _Bug_Condition: isBugCondition(X) — soTokenFromSyntheticEvents(X)_
    - _Expected_Behavior: NOT soTokenFromSyntheticEvents(result) — `so` bám golden_
    - _Preservation: package chatgpt_camoufox, proxy, TLS, profile không đổi_
    - _Requirements: 2.1, 2.3_

  - [x] 5.2 Cân nhắc bỏ shared-browser pool (ứng viên #2)
    - Cho hybrid dùng path `_NoPoolThreadAffinityWrapper(CamoufoxTokenGenerator(...))` (đã có khi `HYBRID_POOL_DISABLED=1`) làm **mặc định** trong `runner.py`/`camoufox_factory.py` — mỗi signup launch browser golden riêng, khớp lifecycle golden, tránh cluster fingerprint
    - Đánh đổi tốc độ (cold launch ~5–10s/signup); chỉ bật pool lại khi xác nhận fingerprint per-context đủ khác
    - _Bug_Condition: isBugCondition(X) — browserFingerprintReusedAcrossSignups(X)_
    - _Expected_Behavior: mỗi signup browser golden tươi, không cluster fingerprint_
    - _Preservation: schema SignupResult, smart OTP loop không đổi_
    - _Requirements: 2.1, 2.3_

  - [x] 5.3 Giữ nguyên các khía cạnh đã khớp golden
    - Giữ proxy (cùng proxy Camoufox + curl), TLS verify (mặc định ON, không insecure-by-default), profile Firefox 135 Windows, header order, no-pre-mint
    - _Preservation: các khía cạnh này đã khớp golden — không đổi_
    - _Requirements: 3.1, 3.3, 3.5_

  - [x] 5.4 Verify drift test (task 1) — phần nhánh B giờ PASS
    - **Property 1: Expected Behavior** - `so` không từ synthetic feeder + browser không share
    - **IMPORTANT**: Chạy LẠI cùng test `test/check_hybrid_drift.py` từ task 1 — KHÔNG viết test mới
    - Chạy: `python3 test/check_hybrid_drift.py`
    - **EXPECTED OUTCOME**: Case B1 (feeder) và B2 (shared browser) PASS (xác nhận drift nhánh B đã fix)
    - _Requirements: 2.1, 2.3_

- [x] 6. Verify Property 1 + Property 2

  - [x] 6.1 Verify Property 1 — toàn bộ drift test PASS
    - **Property 1: Expected Behavior** - Request & token signature đồng nhất golden
    - **IMPORTANT**: Chạy LẠI `test/check_hybrid_drift.py` từ task 1 — KHÔNG viết test mới
    - Chạy: `python3 test/check_hybrid_drift.py`
    - **EXPECTED OUTCOME**: Tất cả case PASS (nhánh A bắt buộc; nhánh B nếu gate task 2 mở) — bug đã fix
    - _Requirements: 2.1, 2.2, 2.3_

  - [x] 6.2 Verify Property 2 — preservation test vẫn PASS
    - **Property 2: Preservation** - Hành vi golden + smart OTP loop không đổi
    - **IMPORTANT**: Chạy LẠI `test/test_hybrid_relay_preservation.py` từ task 3 — KHÔNG viết test mới
    - Chạy: `python3 test/test_hybrid_relay_preservation.py`
    - **EXPECTED OUTCOME**: Tất cả PASS (không hồi quy package golden, smart OTP loop, kế thừa, no-pre-mint, schema)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

- [x] 7. Checkpoint - Đảm bảo toàn bộ test pass
  - Chạy lại toàn bộ: `python3 test/check_hybrid_drift.py`, `python3 test/check_happy_path_request_diff.py`, `python3 test/test_hybrid_relay_preservation.py` — xác nhận tất cả PASS
  - Chạy `python3 test/syntax_check.py` (parse AST mọi file Python đã sửa trong `reg_hybrid/`) để chắc không lỗi cú pháp
  - Xác nhận không file nào trong `chatgpt_camoufox/` bị sửa (golden bất biến)
  - Nếu phát sinh vấn đề ngoài dự kiến, dừng và hỏi user
