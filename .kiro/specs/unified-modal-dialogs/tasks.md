# Implementation Plan: Unified Modal Dialogs

## Overview

Triển khai theo design.md: tạo module `web/static/dialog.js` (IIFE expose `window.Dialog`), bổ sung CSS `white-space: pre-line`, chèn `<script>` đúng vị trí trong `index.html`, refactor `hme.js` (xoá engine modal cũ), rồi migrate 5 file frontend còn lại từ `alert()`/`confirm()` sang `await Dialog.*`. Verification = code review + grep + manual smoke (KHÔNG tạo file test mới). Backend Python và Settings Store không đổi.

Convert the feature design into a series of prompts for a code-generation LLM that will implement each step with incremental progress. Make sure that each prompt builds on the previous prompts, and ends with wiring things together. There should be no hanging or orphaned code that isn't integrated into a previous step. Focus ONLY on tasks that involve writing, modifying, or testing code.

## Tasks

- [x] 1. Tạo module `web/static/dialog.js` (IIFE expose `window.Dialog`)
  - File mới `web/static/dialog.js`, IIFE `(function(){ "use strict"; ... })()`, gán `window.Dialog = { alert, confirm, choice }` (chỉ ba method).
  - State nội bộ: `_currentResolve`, `_currentCancelValue`, `_lastFocus`, `_keyHandler`.
  - Helper nội bộ: `_validate(opts, opts2)`, `_resolveNodes()` (lấy 6 node `#hme-feedback-modal/title/close/message/detail/actions`, throw `Error("Dialog modal DOM contract violated: missing #<id>")` nếu thiếu), `_renderActions(actions, withCancel, cancelLabel)` (reset `innerHTML`, dựng nút Cancel + nút theo từng action, gắn `onclick` → `_close(value)`, trả về `defaultButton`), `_open(opts)` (singleton: nếu `_currentResolve` còn → `_close(_currentCancelValue)` trước; lưu `_lastFocus = document.activeElement`; set title/message/detail textContent; ẩn detail nếu rỗng; `_keyHandler = _onKeyDown`; `addEventListener("keydown", _keyHandler, true)`; `modal.style.display = "flex"`; focus `defaultButton`; trả Promise và set `_currentResolve` + `_currentCancelValue` ngay trong executor đồng bộ), `_close(value)` (ẩn modal, gỡ keydown listener capture-phase, gọi `_currentResolve(value)` đúng một lần, clear state, `_lastFocus.focus()` nếu còn DOM), `_onKeyDown(e)` (Esc → `_close(_currentCancelValue)`; Enter → click button đang focus nếu thuộc modal là `<button>`, ngược lại click default button; preventDefault + stopPropagation).
  - `Dialog.alert(opts)` → `_validate`; `_open` với `cancelValue = true`, action duy nhất `{ label: "OK", value: true, className: "btn btn-primary", autofocus: true }`, không Cancel; default title `"Thông báo"`; trả `Promise<true>`.
  - `Dialog.confirm(opts)` → `_validate`; `_open` với `cancelValue = false`; render `[Cancel (btn-ghost, value=false, label = cancelLabel || "Huỷ")]` + `[Confirm (autofocus, value=true, label = confirmLabel || "Tiếp tục", class = danger ? "btn btn-danger" : "btn btn-primary")]`; default title `"Xác nhận"`; trả `Promise<boolean>`.
  - `Dialog.choice(opts)` → `_validate(opts, { requireActions: true })`; `_open` với `cancelValue = null`; render `[Cancel (btn-ghost, label = cancelLabel || "Huỷ", value=null)]` + `actions.map(...)`; default focus = action đầu tiên có `autofocus: true`, fallback action đầu tiên; default title `"Thông báo"`; trả `Promise<value | null>`.
  - `_validate`: throw `TypeError` đồng bộ nếu `opts` không phải object hoặc `null`, hoặc `message` không phải string non-empty; `title`/`detail`/`confirmLabel`/`cancelLabel` nếu có phải là string; `requireActions=true` thì `actions` phải là Array length ≥ 1, mỗi phần tử có `label` string non-empty (`value` là any).
  - Cấm: KHÔNG tạo DOM động, KHÔNG đăng ký fallback im lặng, KHÔNG export thêm method nào ngoài 3 method trên.
  - _Implements Req 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 1.10, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 3.1, 3.2, 3.3, 3.4, 3.5, 4.1, 4.4, 4.5, 6.3_

- [x] 2. Bổ sung `white-space: pre-line` cho `.hme-feedback-message` trong `web/static/style.css`
  - Tìm rule `.hme-feedback-message` đang có (margin/color/font-size/line-height) và thêm đúng MỘT property `white-space: pre-line;` vào trong khối rule.
  - KHÔNG tạo class mới, KHÔNG đổi class khác, KHÔNG động vào các rule `.modal*` còn lại; idempotent nếu rule đã có sẵn `white-space: pre-line`.
  - _Implements Req 3.1, 3.3, 3.5, 8.6_

- [x] 3. Chèn `<script src="/static/dialog.js?v=__ASSET_VERSION__"></script>` ngay TRƯỚC `settings.js` trong `web/static/index.html`
  - Thêm đúng MỘT dòng `<script>` này, đặt ngay trước thẻ `<script src="/static/settings.js?v=__ASSET_VERSION__"></script>` cuối body.
  - KHÔNG đổi markup `#hme-feedback-modal`, KHÔNG đổi thứ tự các script khác (`settings.js` → `app.js` → `session.js` → `link.js` → `hme.js` → `autoreg.js` → `hotmail.js`).
  - _Implements Req 6.1, 6.2_

- [x] 4. Refactor `web/static/hme.js`: xoá engine modal cũ và đổi mọi call site nội bộ sang `Dialog.*`
  - Xoá khỏi IIFE: biến `let _hmeDialogResolve = null;`, `let _hmeDialogCancelValue = null;`; hàm `closeHmeDialog(value)`, `showHmeDialog(options)`, `hmeAlert(title, message, detail)`, `hmeConfirm(title, message, options)`, `hmeChoice(title, message, actions, cancelLabel)`.
  - Đổi mọi call site nội bộ trong file: `await hmeAlert(t, m, d)` → `await Dialog.alert({ title: t, message: m, detail: d })`; `await hmeConfirm(t, m, { confirmLabel, cancelLabel, danger })` → `await Dialog.confirm({ title: t, message: m, confirmLabel, cancelLabel, danger })`; `await hmeChoice(t, m, actions, cancelLabel)` → `await Dialog.choice({ title: t, message: m, actions, cancelLabel })`.
  - Nếu IIFE của `hme.js` đang `return`/expose các symbol `hmeAlert`/`hmeConfirm`/`hmeChoice` → xoá khỏi return.
  - Giữ NGUYÊN Original_Text của mọi `title`/`message`/`detail`; KHÔNG dịch, KHÔNG reword, KHÔNG thêm wrapper riêng.
  - Sau task này: grep `\b(hmeAlert|hmeConfirm|hmeChoice|showHmeDialog|closeHmeDialog|_hmeDialogResolve|_hmeDialogCancelValue)\b` trong `web/static/hme.js` phải rỗng.
  - _Implements Req 5.1, 5.6, 5.7, 7.1, 7.2_

- [x] 5. Migrate `web/static/app.js`: thay toàn bộ `alert()`/`confirm()` theo bảng mapping trong design.md
  - Mapping `alert(...)`: `alert('Paste combos first.')` → `await Dialog.alert({ message: 'Paste combos first.' })`; `alert('Error: ' + err.message)` → `await Dialog.alert({ message: 'Error: ' + err.message })`; mọi `alert(err.message)` còn lại → `await Dialog.alert({ message: err.message })`.
  - Mapping `confirm(...)`: `if (!confirm('Retry this job?')) return;` → handler `async`, `if (!(await Dialog.confirm({ message: 'Retry this job?' }))) return;`; áp dụng tương tự cho `'Stop this running job?'`, `'Remove this job from the list and textarea?'`, `'Stop all running or queued jobs?'`, `'Retry tất cả jobs error & cancelled?'`.
  - Clear All (danger): `if (!confirm('Xoá TẤT CẢ jobs (mọi trạng thái)? Hành động không thể hoàn tác.')) return;` → `if (!(await Dialog.confirm({ message: 'Xoá TẤT CẢ jobs (mọi trạng thái)? Hành động không thể hoàn tác.', danger: true, confirmLabel: 'Xoá' }))) return;`.
  - Headless toggle multi-line + revert: thay `confirm(<text gốc nhiều dòng có \\n\\n>)` → `await Dialog.confirm({ message: <text gốc giữ nguyên \\n\\n> })`; handler đổi sang `async`; logic revert checkbox `#headless-toggle` khi resolve `false` giữ nguyên.
  - `copyText()`: trong nhánh clipboard fail, đổi `alert('Copy failed.'); throw new Error('Copy failed');` → `await Dialog.alert({ message: 'Copy failed.' }); throw new Error('Copy failed');` (giữ throw để caller `.catch()` không đổi).
  - Đổi handler liên quan sang `async` ở mức tối thiểu cần thiết để `await`. KHÔNG refactor business logic, KHÔNG đổi text gốc, KHÔNG đổi cấp độ alert↔confirm.
  - _Implements Req 4.2, 4.3, 5.1, 5.2, 5.3, 5.4, 5.5, 5.7, 7.1, 7.2, 7.3, 7.4_

- [x] 6. Migrate `web/static/session.js`: thay 7 `alert(...)`
  - Đổi từng `alert(<msg>)` → `await Dialog.alert({ message: <msg> })`, giữ nguyên Original_Text.
  - Handler chứa các alert đa số đã `async`; với handler còn đồng bộ, đổi sang `async` ở mức tối thiểu cần thiết để `await`.
  - KHÔNG thêm wrapper riêng cho file, gọi trực tiếp `Dialog.alert`.
  - _Implements Req 5.1, 5.2, 5.5, 5.7, 7.1, 7.2, 7.4_

- [x] 7. Migrate `web/static/link.js`: thay 7 `alert(...)`, chuẩn hoá pattern `.catch(err => alert(err.message))` theo Mẫu A
  - Đổi từng `alert(<msg>)` → `await Dialog.alert({ message: <msg> })`, giữ nguyên Original_Text.
  - Áp dụng đồng nhất Mẫu A trong file: `fetch(...).then(...).catch((err) => alert(err.message))` → `fetch(...).then(...).catch(async (err) => { await Dialog.alert({ message: err.message }); })`.
  - KHÔNG đổi business logic của chuỗi promise, KHÔNG dùng Mẫu B (try/catch) trong task này.
  - _Implements Req 4.1, 4.2, 5.1, 5.2, 5.5, 5.7, 7.1, 7.2, 7.4_

- [x] 8. Migrate `web/static/autoreg.js`: thay 2 `alert(...)`
  - `alert('Start failed: ' + ...)` → `await Dialog.alert({ message: 'Start failed: ' + ... })`.
  - `alert('Start request failed: ' + ...)` → `await Dialog.alert({ message: 'Start request failed: ' + ... })`.
  - Handler đã `async`, chỉ thay primitive. Giữ Original_Text.
  - _Implements Req 5.1, 5.2, 5.7, 7.1, 7.2, 7.4_

- [x] 9. Migrate `web/static/hotmail.js`: thay 1 `alert('Copy failed')`
  - Trong `.catch()` của clipboard, đổi `alert('Copy failed')` → `await Dialog.alert({ message: 'Copy failed' })`; nếu callback hiện đồng bộ → đổi `async (err) => { ... }`.
  - Giữ nguyên cấu trúc `.then().catch()`.
  - _Implements Req 5.1, 5.2, 5.5, 5.7, 7.1, 7.2, 7.4_

- [x] 10. Verification (code review + grep + manual smoke, KHÔNG tạo file test)
  - Grep `\balert\(` và `\bconfirm\(` trên 6 file `web/static/{app,session,link,autoreg,hotmail,hme}.js` → kết quả phải rỗng.
  - Grep `\b(hmeAlert|hmeConfirm|hmeChoice|showHmeDialog|closeHmeDialog|_hmeDialogResolve|_hmeDialogCancelValue)\b` trên `web/static/hme.js` → rỗng.
  - Diff `web/static/index.html` → chỉ thêm đúng 1 dòng `<script src="/static/dialog.js?v=__ASSET_VERSION__"></script>` ngay trước `settings.js`; markup `#hme-feedback-modal` không đổi.
  - Diff `web/static/style.css` → chỉ bổ sung `white-space: pre-line` trong rule `.hme-feedback-message`.
  - Diff backend Python (`db/`, `automation/`, `web/server*.py`, `web/api*.py`, `app/`) → KHÔNG thay đổi.
  - Diff `db/repositories.py` → `_EXACT_KEYS` không có key mới; `_validate_type_constraint()` không đổi.
  - Manual smoke (chạy server, mở UI):
    1. Reg → Run với combos rỗng → alert "Paste combos first.", OK đóng, focus quay về Run.
    2. Reg → toggle `#headless-toggle` khi đang chạy → confirm multi-line xuống dòng đúng; bấm Huỷ → checkbox revert đúng một lần.
    3. Reg → Stop All → confirm hiển thị, Confirm gửi request.
    4. Reg → Clear All → confirm `danger` đỏ + label "Xoá", Huỷ không xoá.
    5. Session/Link/Hotmail → trigger 1 alert mỗi tab → modal hiển thị, đóng được.
    6. HME → Add/Open profile, reactivate → focus đúng nút Confirm, mở 2 dialog liên tiếp ở console (`Dialog.confirm(...).then(v => console.log(v)); Dialog.alert(...);`) → dialog đầu log `false`, dialog sau hiển thị bình thường.
    7. Console: `Dialog.alert(null)` và `Dialog.choice({ message: 'x', actions: [] })` → throw đồng bộ.
    8. Tạm xoá dòng `<script src=".../dialog.js...">` trong `index.html`, reload, gọi `Dialog.alert(...)` ở console → throw `TypeError` (fail-fast); KHÔI PHỤC lại sau khi xác nhận.
  - _Implements Req 5.1, 5.6, 6.1, 6.4, 7.1, 8.1, 8.2, 8.3, 8.4, 8.5, 8.6_

## Notes

- Không tạo file test tự động (theo chỉ định user). Verification = code review + grep + manual smoke.
- Toàn bộ Original_Text giữ nguyên — không dịch, không reword.
- Không thêm wrapper riêng theo từng file: mọi call site gọi trực tiếp `Dialog.*`.
- Không có fallback im lặng cho `window.Dialog` chưa load — fail-fast bằng `TypeError` runtime (Req 4.4 + 6.4).
- Phạm vi loại trừ (Req 8): không sửa backend Python, không thêm key Settings Store, không tạo prompt/toast/banner/snackbar mới, không refactor business logic.
- Mẫu A áp dụng đồng nhất trong `link.js`; các file khác đổi handler sang `async` ở mức tối thiểu cần thiết để `await`.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1"] },
    { "id": 1, "tasks": ["2", "3"] },
    { "id": 2, "tasks": ["4", "5", "6", "7", "8", "9"] },
    { "id": 3, "tasks": ["10"] }
  ]
}
```

Diễn giải:

- Wave 0 — `1` (tạo `dialog.js`) là root, mọi task khác phụ thuộc vào sự tồn tại của `window.Dialog`.
- Wave 1 — `2` (CSS `white-space: pre-line`) và `3` (chèn `<script>` vào `index.html`) chạy song song sau khi module `dialog.js` đã có (cùng phụ thuộc T1, không đụng nhau về file).
- Wave 2 — `4` (refactor `hme.js`), `5` (migrate `app.js`), `6` (`session.js`), `7` (`link.js`), `8` (`autoreg.js`), `9` (`hotmail.js`) chạy song song sau T1 + T3 (cần `Dialog` runtime + `<script>` đã được nạp đúng thứ tự); mỗi task động đến 1 file riêng nên không xung đột.
- Wave 3 — `10` (verification) chạy cuối, phụ thuộc tất cả các task trước.
