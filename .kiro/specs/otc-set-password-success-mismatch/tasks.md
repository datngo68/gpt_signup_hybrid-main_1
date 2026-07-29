# Implementation Plan

## Overview

Fix triển khai theo 7 bước: thêm helper validation, refactor 2 callsite gọi `_REGISTER_USER_JS` (password_create giữ semantics, OTC chuyển sang strict fail-fast), viết 2 file test, chạy test, và review regression password_create. Mỗi task tham chiếu requirement (`2.x`, `3.x`) và property (1=Fix Checking, 2=Preservation, 3=Regression) tương ứng từ `bugfix.md` + `design.md`.

## Tasks

- [ ] 1. Add `_assert_register_success` helper in `browser_phase.py`
  - Insert helper function right after `_REGISTER_USER_JS` constant (~line 82, before `# Helper functions` section header).
  - Signature: `def _assert_register_success(result, *, context: str, accept_already_exists: bool = False) -> tuple[str, dict]`.
  - Implement validation logic per design spec:
    - Reject non-dict result → raise `BrowserPhaseError(f"{context}: unexpected result shape: {result!r}")`.
    - Extract `status` (int) and `body` (dict | str | None, default `{}`); compute `body_str` via `json.dumps` for dict or `str(body)` for non-dict.
    - Compute `is_2xx = isinstance(status, int) and 200 <= status < 300`.
    - Compute `looks_already_exists = (status == 409) or "already" in body_str.lower() or "exists" in body_str.lower()`.
    - If not `is_2xx`: when `accept_already_exists and looks_already_exists` → return `("already_exists", body if isinstance(body, dict) else {})`; else raise `BrowserPhaseError(f"{context}: HTTP {status}: {body_str[:200]}")`.
    - If `is_2xx` and `body` is dict: for each key in `("error", "detail")`, if `body.get(key)` truthy → raise `BrowserPhaseError(f"{context}: HTTP {status} but body.{key}: {body_str[:200]}")`.
    - Return `("success", body if isinstance(body, dict) else {})`.
  - Helper is sync (validation only, no I/O); do not mark `async`.
  - _Validates: Property 1 (Fix Checking) input contract — helper is the single source of validation reused by both callsites._ _Requirements: 2.1, 2.2, 2.3, 3.1, 3.2_

- [ ] 2. Refactor `password_create` branch in `_drive_signup_flow` (browser_phase.py:453-481)
  - Replace the existing inline validation block (from `if not isinstance(result, dict): raise ...` through `raise BrowserPhaseError(f"register failed HTTP {status}: {body_str[:200]}")`).
  - Call `outcome, body = _assert_register_success(result, context="register password_create", accept_already_exists=True)` immediately after `register_attempted = True`.
  - Branch on `outcome`:
    - `"already_exists"` → log `"[flow] account already exists — page sẽ chuyển login"`, `await asyncio.sleep(1.5)`, `continue`.
    - `"success"` → extract `continue_url = body.get("continue_url") if isinstance(body, dict) else None`, log `"[flow] register OK → continue_url={continue_url}"`, normalize relative URL to absolute (`https://auth.openai.com{continue_url}` if starts with `/`), `await page.goto(continue_url, wait_until="domcontentloaded")` if set, then `await asyncio.sleep(1.0)` and `continue`.
  - Keep `register_attempted = True` BEFORE calling helper so a raise still marks the attempt (prevents infinite loop on screen re-detect).
  - Do not catch `BrowserPhaseError` from helper — let it propagate to caller chain.
  - _Validates: Property 3 (password_create regression-free)._ _Requirements: 3.2_

- [ ] 3. Fix OTC set-password block in `_drive_signup_flow` `about_you` branch (browser_phase.py:701-715)
  - Inside `if screen == "about_you":` → `if one_time_code_mode:` block, replace the entire `try/except Exception` block with:
    1. `log(f"[flow] POST /api/accounts/user/register (set password OTC, email={request.email})")`.
    2. `reg = await page.evaluate(_REGISTER_USER_JS, {"username": request.email, "password": request.password})` — bare await, no try/except.
    3. `_outcome, _body = _assert_register_success(reg, context="set_password OTC (about_you)", accept_already_exists=False)`.
    4. `log("[flow] set password OTC OK")`.
  - Do not introduce any try/except wrapper. Exceptions from `page.evaluate` (network drop, page closed, JS exception) and `BrowserPhaseError` from helper must propagate out of `_drive_signup_flow` so `run_browser_phase` raises and caller marks job failed.
  - Subsequent statements (`_wait_oai_sc`, `_fill_about_you`, `_wait_chatgpt_session`, `return callback_url, otp_seconds_total`) MUST only execute when helper returned `"success"`. Verify by reading the surrounding block — no early return / fallthrough that bypasses the helper.
  - _Validates: Property 1 (Fix Checking) — bug condition path now raises before fill_about_you / return callback_url._ _Requirements: 2.1, 2.2, 2.3, 2.4, 3.1, 3.4_

- [ ] 4. Write `test/test_otc_register_assert.py` — unit tests for helper
  - File header: shebang `#!/usr/bin/env python3`, docstring describing the test scope (validates `_assert_register_success` against bugfix.md counterexamples).
  - Import: `from gpt_signup_hybrid.browser_phase import _assert_register_success, BrowserPhaseError`. Add `sys.path` bootstrap to repo root if needed (mirror pattern from existing `test/test_*.py`).
  - Implement 15 test cases per design Testing Strategy table:
    - 6 success / shape error cases shared between both `accept_already_exists` modes (cases 1–6).
    - 4 HTTP-error / body-error cases shared (cases 7–10).
    - 4 already-exists matrix cases (11–14: HTTP 409 + HTTP 422 with "already exists" message, each tested with both `True` and `False` flag values).
    - 1 string-body 200 case (15).
  - Each test = a top-level function `def test_<descriptive_name>():` containing concrete input dict, call to helper, and `assert` on returned tuple OR `try/except BrowserPhaseError` with `assert "<expected substring>" in str(exc)`.
  - At end of file, add `if __name__ == "__main__":` block that calls every test function in order, prints `"PASS: <name>"` per test, prints final `"All N tests passed"` summary.
  - Do not import pytest; project rule prefers runnable scripts.
  - Do not add fixtures, parametrize decorators, or class wrappers — keep flat and grep-friendly.
  - _Validates: Property 1 input contract — helper raises for every counterexample in bugfix.md._ _Requirements: 2.1, 2.2, 2.3, 3.1, 3.2_

- [ ] 5. Write `test/check_otc_branch_raise.py` — smoke for OTC branch propagation
  - File header: shebang + docstring stating goal: confirm exception from `page.evaluate` and helper failures propagate out of OTC branch (no swallowing).
  - Build a minimal `FakePage` async class with `evaluate(js, args)` coroutine method whose return value is set per-test via constructor arg. Variants: returns dict, returns non-dict, raises `RuntimeError("network down")`.
  - Test 1 — exception propagation: instantiate `FakePage(side_effect=RuntimeError("network down"))`. Run an async wrapper that mimics the post-fix OTC block (`reg = await page.evaluate(...)` → `_assert_register_success(reg, context="set_password OTC (about_you)", accept_already_exists=False)`). Assert `RuntimeError` is raised (not swallowed).
  - Test 2 — HTTP 400: `FakePage(return_value={"status": 400, "body": {"error": "weak"}})`. Assert `BrowserPhaseError` with `"set_password OTC"` in message.
  - Test 3 — HTTP 200 with body.error: `FakePage(return_value={"status": 200, "body": {"error": "session_invalid"}})`. Assert `BrowserPhaseError` raised, message contains `"body.error"`.
  - Test 4 — HTTP 200 success: `FakePage(return_value={"status": 200, "body": {}})`. Assert wrapper completes without raising and returns expected sentinel ("ok").
  - Use `asyncio.run(main())` for execution; print PASS/FAIL per test and exit non-zero on any failure.
  - Do not invoke real Playwright, do not start a browser. Do not import `_drive_signup_flow` directly — replicate the 4-line OTC block inline in the test wrapper, since the production block is not standalone.
  - _Validates: Property 1 — exceptions and helper raises propagate; Property 3.4 — success path still returns._ _Requirements: 2.1, 2.2, 2.3, 2.4, 3.1_

- [ ] 6. Run new tests and capture outcomes
  - From repo root run `python3 test/test_otc_register_assert.py`. Confirm exit code 0 and all 15 cases print PASS.
  - From repo root run `python3 test/check_otc_branch_raise.py`. Confirm exit code 0 and all 4 smoke tests print PASS.
  - If any test fails, fix the helper or branch (Tasks 1–3) before proceeding. Do not edit tests to match buggy behavior.
  - Do not add tests to a runner / CI config in this spec — out of scope.
  - _Validates: Properties 1, 2, 3 (executable evidence)._ _Requirements: 2.1, 2.2, 2.3, 2.4, 3.1, 3.2, 3.3, 3.4, 3.5_

- [ ] 7. Verify `password_create` branch regression with manual trace
  - Open `browser_phase.py` and read the refactored `password_create` block end-to-end alongside the original (use git diff or before/after side-by-side).
  - For each input class in the design Risk section, confirm post-refactor behavior matches pre-refactor:
    - HTTP 200 + `continue_url` → success path with `page.goto(continue_url)`.
    - HTTP 200 + body has `error`/`detail` → NOW raises (documented behavior change; record this in commit message).
    - HTTP 409 → `"already_exists"` outcome → log + continue.
    - Body string contains "already" / "exists" → `"already_exists"`.
    - Other non-2xx → raise.
  - No code change in this task. Output: a short paragraph in the commit body or PR description summarizing the verified delta. If unsafe drift discovered, open follow-up task to introduce `lenient_body_error_for_200` flag (out of scope here).
  - _Validates: Property 3 (password_create regression-free, with documented intentional delta)._ _Requirements: 3.2_

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "tasks": ["1"],
      "rationale": "Helper là single source of truth, mọi task khác import hoặc nhân bản logic của nó."
    },
    {
      "wave": 2,
      "tasks": ["2", "3", "4", "5"],
      "rationale": "Sau khi helper sẵn sàng, hai callsite refactor (2, 3) và hai test file (4, 5) độc lập với nhau — chạy song song được."
    },
    {
      "wave": 3,
      "tasks": ["6"],
      "rationale": "Chạy test cần cả helper, callsite refactor, và test file đã land."
    },
    {
      "wave": 4,
      "tasks": ["7"],
      "rationale": "Manual regression trace cho password_create chỉ làm sau khi test xanh."
    }
  ]
}
```

## Notes

- Tất cả thay đổi code production gói gọn trong `browser_phase.py`. Không sửa caller chain (`signup.py`, `cli.py`, `web/manager.py`) — đã đúng.
- File test mới đặt trong `test/` đúng project rule. Không thêm pytest dep, không inline `python3 -c`.
- Behavior change được biết: HTTP 200 + `body.error` / `body.detail` ở `password_create` giờ raise (trước đây pass-through). Document trong commit message + monitor logs sau deploy. Nếu phát sinh false positive, mở spec follow-up cho flag `lenient_body_error_for_200`.
- Out of scope: dead-code audit `_register_with_password` (line 121-217), retry/login logic, DB schema change.
