# Design Document

## Overview

Hợp nhất toàn bộ hộp thoại web frontend về một module duy nhất `web/static/dialog.js` (IIFE) expose `window.Dialog` với ba phương thức bất đồng bộ `alert`, `confirm`, `choice`. Module **không tạo DOM động** — tái sử dụng nguyên element `#hme-feedback-modal` đã có trong `web/static/index.html` cùng các class `.modal*`, `.hme-feedback-*` đã có trong `web/static/style.css`. Toàn bộ call site `alert(...)` / `confirm(...)` trong `app.js`, `session.js`, `link.js`, `autoreg.js`, `hotmail.js`, `hme.js` chuyển sang `await Dialog.*`. Engine modal cũ trong `hme.js` (`showHmeDialog`, `hmeAlert`, `hmeConfirm`, `hmeChoice`) bị xoá hoàn toàn.

Phạm vi thay đổi:

- Tạo mới: `web/static/dialog.js`.
- Bổ sung CSS tối thiểu: rule `.hme-feedback-message { white-space: pre-line; }` (chỉ thêm khai báo `white-space`, giữ nguyên các property khác đang có).
- Sửa `web/static/index.html`: thêm `<script src="/static/dialog.js?v=__ASSET_VERSION__"></script>` đặt **ngay trước** `settings.js`. Phần modal HTML giữ nguyên.
- Refactor 6 file JS theo Migration_Scope.

Out-of-scope (giữ nguyên Requirement 8): không sửa backend Python, không thêm key Settings Store, không tạo prompt/toast/banner mới, không refactor business logic, không đổi class CSS hiện có ngoài bổ sung `white-space: pre-line`.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    web/static/index.html                │
│                                                         │
│  <div id="hme-feedback-modal" class="modal">            │
│    h3#hme-feedback-title                                │
│    button#hme-feedback-close                            │
│    p#hme-feedback-message                               │
│    pre#hme-feedback-detail                              │
│    footer#hme-feedback-actions                          │
│  </div>                                                 │
└──────────────────────┬──────────────────────────────────┘
                       │ DOM contract (id sẵn)
                       ▼
┌─────────────────────────────────────────────────────────┐
│              web/static/dialog.js (IIFE)                │
│                                                         │
│   Internal state:                                       │
│     _currentResolve, _currentCancelValue,               │
│     _lastFocus, _keyHandler                             │
│                                                         │
│   Internal: _validate, _renderActions,                  │
│             _open, _close, _onKeyDown                   │
│                                                         │
│   Public:   Dialog.alert / .confirm / .choice           │
└──────────────────────┬──────────────────────────────────┘
                       │ window.Dialog
                       ▼
┌─────────────────────────────────────────────────────────┐
│  Migration_Scope (consumers, async/await)               │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐    │
│  │  app.js  │ │session.js│ │ link.js  │ │autoreg.js│    │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘    │
│  ┌──────────┐ ┌──────────┐                              │
│  │hotmail.js│ │  hme.js  │ (xoá hmeAlert/Confirm/Choice)│
│  └──────────┘ └──────────┘                              │
└─────────────────────────────────────────────────────────┘
```

Loading order trong `index.html` (đoạn cuối body):

```html
<script src="/static/dialog.js?v=__ASSET_VERSION__"></script>   <!-- MỚI -->
<script src="/static/settings.js?v=__ASSET_VERSION__"></script>
<script src="/static/app.js?v=__ASSET_VERSION__"></script>
<script src="/static/session.js?v=__ASSET_VERSION__"></script>
<script src="/static/link.js?v=__ASSET_VERSION__"></script>
<script src="/static/hme.js?v=__ASSET_VERSION__"></script>
<script src="/static/autoreg.js?v=__ASSET_VERSION__"></script>
<script src="/static/hotmail.js?v=__ASSET_VERSION__"></script>
```

## Components and Interfaces

### 1. DOM contract (đã tồn tại, không sửa cấu trúc)

| Selector | Vai trò |
|----------|---------|
| `#hme-feedback-modal` | Container modal (root, đặt `display: flex` để mở, `none` để đóng) |
| `#hme-feedback-title` | `<h3>` tiêu đề (textContent) |
| `#hme-feedback-close` | Nút `×` (close icon) |
| `#hme-feedback-message` | `<p>` chứa message (textContent + CSS `white-space: pre-line`) |
| `#hme-feedback-detail` | `<pre>` chứa detail (textContent, default `display:none`) |
| `#hme-feedback-actions` | `<footer>` chứa các nút action (innerHTML reset mỗi lần `_open`) |

Nếu **bất kỳ** node trên thiếu trong DOM tại lần `_open` đầu tiên → throw `Error("Dialog modal DOM contract violated: missing #<id>")`.

### 2. Public API — `window.Dialog`

```js
// Chỉ ba method, không method nào khác.
window.Dialog = { alert, confirm, choice };
```

#### `Dialog.alert(opts) → Promise<true>`

```js
// opts: { title?: string, message: string, detail?: string }
// Default title: "Thông báo".
// Render: 1 nút "OK" (autofocus, class "btn btn-primary").
// Resolve: true khi user bấm OK / Esc / nút × → cancelValue = true.
await Dialog.alert({ message: "Paste combos first." });
```

#### `Dialog.confirm(opts) → Promise<boolean>`

```js
// opts: {
//   title?: string,
//   message: string,
//   detail?: string,
//   confirmLabel?: string,    // default: "Tiếp tục"
//   cancelLabel?: string,     // default: "Huỷ"
//   danger?: boolean,         // default: false → confirm class "btn btn-primary"
//                             // true  → confirm class "btn btn-danger"
// }
// Default title: "Xác nhận".
// Render: [Cancel (btn-ghost)] [Confirm (autofocus, primary | danger)].
// Resolve: true nếu Confirm; false nếu Cancel / Esc / × → cancelValue = false.
const ok = await Dialog.confirm({
  message: "Xoá TẤT CẢ jobs (mọi trạng thái)? Hành động không thể hoàn tác.",
  danger: true,
  confirmLabel: "Xoá",
});
```

#### `Dialog.choice(opts) → Promise<value | null>`

```js
// opts: {
//   title?: string,
//   message: string,
//   detail?: string,
//   actions: Array<{
//     label: string,
//     value: any,           // resolve về giá trị này khi action được chọn
//     className?: string,   // default: "btn btn-ghost"
//     autofocus?: boolean,
//   }>,
//   cancelLabel?: string,   // default: "Huỷ"
// }
// Render: [Cancel (btn-ghost)] [action1] [action2] ... [actionN]
//   - Nếu có ≥1 action với autofocus=true: focus action đầu tiên có autofocus.
//   - Nếu không có autofocus: focus action đầu tiên trong list.
// Resolve: action.value khi action được chọn; null nếu Cancel / Esc / × → cancelValue = null.
const ans = await Dialog.choice({
  title: "Sync profile?",
  message: "Chọn cách sync.",
  actions: [
    { label: "Dry run", value: "dry", className: "btn btn-ghost", autofocus: true },
    { label: "Run sync", value: "run", className: "btn btn-primary" },
  ],
});
```

### 3. Module shape (`web/static/dialog.js`)

```js
(function () {
  "use strict";

  // ── Internal state ────────────────────────────────────
  let _currentResolve = null;        // function | null
  let _currentCancelValue = null;    // any (true | false | null)
  let _lastFocus = null;             // Element | null
  let _keyHandler = null;            // function | null

  // ── Internal helpers ──────────────────────────────────

  // Throw đồng bộ nếu opts không hợp lệ. requireActions=true cho choice().
  function _validate(opts, opts2) { /* ... */ }

  // Reset innerHTML, render Cancel + buttons; gắn click handler resolve.
  // Trả về { defaultButton } để _open set focus.
  function _renderActions(actions) { /* ... */ }

  // Mở modal: validate DOM, đóng dialog cũ (singleton), set state,
  // render title/message/detail, render actions, gắn _keyHandler,
  // lưu _lastFocus, focus default button. Trả Promise.
  function _open(opts) { /* ... */ }

  // Đóng modal: ẩn modal, gỡ _keyHandler, restore focus, clear state,
  // gọi resolve(value) một lần.
  function _close(value) { /* ... */ }

  // Document-level keydown (capture phase) khi modal mở.
  function _onKeyDown(e) { /* ... */ }

  // ── Public API ────────────────────────────────────────
  function alert(opts)   { /* ... */ }
  function confirm(opts) { /* ... */ }
  function choice(opts)  { /* ... */ }

  window.Dialog = { alert: alert, confirm: confirm, choice: choice };
})();
```

## Data Models

### `AlertOpts`

| Field | Kiểu | Bắt buộc | Mặc định | Ghi chú |
|-------|------|----------|----------|---------|
| `title` | `string` | không | `"Thông báo"` | Default_Alert_Title |
| `message` | `string` (non-empty) | có | — | Render textContent |
| `detail` | `string` | không | `""` | Nếu rỗng → ẩn `#hme-feedback-detail` |

### `ConfirmOpts`

| Field | Kiểu | Bắt buộc | Mặc định |
|-------|------|----------|----------|
| `title` | `string` | không | `"Xác nhận"` |
| `message` | `string` (non-empty) | có | — |
| `detail` | `string` | không | `""` |
| `confirmLabel` | `string` | không | `"Tiếp tục"` |
| `cancelLabel` | `string` | không | `"Huỷ"` |
| `danger` | `boolean` | không | `false` |

### `ChoiceOpts`

| Field | Kiểu | Bắt buộc | Mặc định |
|-------|------|----------|----------|
| `title` | `string` | không | `"Thông báo"` |
| `message` | `string` (non-empty) | có | — |
| `detail` | `string` | không | `""` |
| `actions` | `Array<ChoiceAction>` (length ≥ 1) | có | — |
| `cancelLabel` | `string` | không | `"Huỷ"` |

### `ChoiceAction`

| Field | Kiểu | Bắt buộc | Mặc định |
|-------|------|----------|----------|
| `label` | `string` | có | — |
| `value` | `any` | có | — |
| `className` | `string` | không | `"btn btn-ghost"` |
| `autofocus` | `boolean` | không | `false` |

### Cancel value mapping

| Method | cancelValue | Resolve khi Esc / × / mở dialog mới |
|--------|-------------|-------------------------------------|
| `alert` | `true` | `true` |
| `confirm` | `false` | `false` |
| `choice` | `null` | `null` |

## Sequence cho từng public method

### `alert(opts)`

1. `_validate(opts)` — throw đồng bộ nếu sai.
2. Tạo `Promise` mới với resolve.
3. Gọi `_open({ title, message, detail, cancelValue: true, defaultButton: <OK>, actions: [{ label: "OK", className: "btn btn-primary", value: true, autofocus: true }] })` (mapping nội bộ thành 1 action).
4. Khi `_close(value)` chạy → `resolve(value)`.
5. Trả `Promise<true>` (mọi đường thoát đều resolve `true`).

### `confirm(opts)`

1. `_validate(opts)`.
2. Tạo Promise.
3. `_open` với:
   - `cancelValue = false`.
   - Render `[Cancel (btn-ghost, value=false)]` + `[Confirm (autofocus, value=true, class = danger ? "btn btn-danger" : "btn btn-primary")]`.
4. `resolve(value)` khi `_close`.
5. Trả `Promise<boolean>`.

### `choice(opts)`

1. `_validate(opts, { requireActions: true })` — throw nếu `actions` rỗng / thiếu.
2. Tạo Promise.
3. `_open` với:
   - `cancelValue = null`.
   - Render `[Cancel (btn-ghost, value=null)]` + `actions.map(a => button(a))`.
   - Default focus = action đầu tiên có `autofocus: true`, fallback action đầu tiên.
4. `resolve(value)` khi `_close`.
5. Trả `Promise<value | null>`.

## Singleton flow

```
caller A: Dialog.confirm(...)   ──┐
                                  │   _currentResolve = resolve_A
                                  │   _currentCancelValue = false
                                  ▼
                          [modal hiển thị, A pending]

caller B: Dialog.alert(...)
   │
   ▼
   _open() detect _currentResolve != null
   → _close(_currentCancelValue)        // resolve_A(false), gỡ listener, ẩn modal
   → set state cho B (cancelValue=true)
   → render lại actions, hiển thị modal
   → return Promise mới

[caller A nhận false đúng 1 lần]
[caller B chờ user]
```

Bất biến: tại mọi thời điểm chỉ có ≤ 1 modal hiển thị. `_currentResolve` chỉ được gọi đúng một lần per dialog (sau đó set về `null` trong `_close`).

## Esc / Enter handling

`_keyHandler` được gắn lên `document` ở **capture phase** (`addEventListener('keydown', _onKeyDown, true)`) khi `_open`, gỡ khi `_close`.

```
keydown event
   │
   ├── key === "Escape"
   │     → e.preventDefault(); e.stopPropagation();
   │     → _close(_currentCancelValue)
   │
   ├── key === "Enter"
   │     → if (document.activeElement nằm trong #hme-feedback-modal
   │          && là <button>):
   │           e.preventDefault(); activeElement.click()
   │       else:
   │           e.preventDefault();
   │           default button (Confirm | OK | autofocus action).click()
   │
   └── khác: bỏ qua
```

Restore focus: trong `_open`, lưu `_lastFocus = document.activeElement`. Trong `_close`, nếu `_lastFocus && typeof _lastFocus.focus === "function"` → `_lastFocus.focus()`; sau đó `_lastFocus = null`.

## Render flow chi tiết (`_open`)

```
1. _validate(opts) — throw nếu sai
2. Resolve modal nodes:
     modal   = document.getElementById("hme-feedback-modal")
     title   = ...title
     message = ...message
     detail  = ...detail
     actions = ...actions
     close   = ...close
   Nếu BẤT KỲ node nào null → throw Error mô tả id thiếu
3. Singleton: nếu _currentResolve != null → _close(_currentCancelValue)
4. _lastFocus = document.activeElement
5. title.textContent = opts.title (đã apply default)
6. message.textContent = opts.message
7. Nếu opts.detail truthy:
     detail.textContent = opts.detail; detail.style.display = "block"
   Ngược lại:
     detail.textContent = ""; detail.style.display = "none"
8. _renderActions(opts.actions):
     - actions.innerHTML = ""
     - Tạo <button> Cancel (chỉ với confirm/choice)
     - Tạo <button> cho mỗi action; gắn click → _close(action.value)
     - Trả về defaultButton (autofocus đầu tiên hoặc fallback)
9. close.onclick = () => _close(_currentCancelValue)
10. _keyHandler = _onKeyDown; document.addEventListener("keydown", _keyHandler, true)
11. modal.style.display = "flex"
12. defaultButton.focus()
13. Return Promise(resolve => _currentResolve = resolve;
                              _currentCancelValue = <theo loại dialog>)
```

Lưu ý thứ tự: `_currentResolve` và `_currentCancelValue` phải được set **trước khi** modal có thể đóng (vì tay người dùng có thể click trước khi step 13 chạy nếu code chia làm 2 promise). Trong thực tế, executor của `new Promise` chạy đồng bộ, nên đặt step 13 ở đầu (`new Promise((resolve) => { _currentResolve = resolve; ... rest })`) để đảm bảo singleton đúng.

Triển khai đúng:

```js
function _open(opts) {
  // Validate DOM
  const nodes = _resolveNodes();   // throw nếu thiếu
  // Singleton: đóng dialog cũ trước
  if (_currentResolve) _close(_currentCancelValue);
  _lastFocus = document.activeElement;

  return new Promise((resolve) => {
    _currentResolve = resolve;
    _currentCancelValue = opts.cancelValue;
    // ... render title/message/detail/actions ...
    document.addEventListener("keydown", _keyHandler = _onKeyDown, true);
    nodes.modal.style.display = "flex";
    defaultButton.focus();
  });
}
```

## Validation rules (`_validate`)

```
_validate(opts, opts2 = { requireActions: false })
  if (typeof opts !== "object" || opts === null)
    throw new TypeError("Dialog: opts must be a non-null object");
  if (typeof opts.message !== "string" || opts.message.length === 0)
    throw new TypeError("Dialog: opts.message must be a non-empty string");
  // title/detail/labels: nếu có thì phải là string
  for (key of ["title", "detail", "confirmLabel", "cancelLabel"])
    if (opts[key] !== undefined && typeof opts[key] !== "string")
      throw new TypeError("Dialog: opts." + key + " must be a string");
  if (opts2.requireActions) {
    if (!Array.isArray(opts.actions) || opts.actions.length === 0)
      throw new Error("Dialog.choice: opts.actions must be a non-empty array");
    for (a of opts.actions) {
      if (typeof a !== "object" || a === null) throw new Error(...);
      if (typeof a.label !== "string" || a.label.length === 0) throw ...;
      // a.value: any (kể cả null/undefined cũng OK)
    }
  }
```

Tất cả lỗi validate là đồng bộ → caller `await Dialog.alert(...)` sẽ thấy throw trước khi rơi vào async, đúng yêu cầu fail-fast.

## Error Handling

| Tình huống | Hành vi |
|-----------|---------|
| `opts` không phải object hoặc thiếu `message` | Throw `TypeError` đồng bộ, không hiển thị modal |
| `opts.message` rỗng / không phải string | Throw `TypeError` đồng bộ |
| `Dialog.choice` nhưng `actions` rỗng / thiếu / phần tử lỗi | Throw `Error` đồng bộ |
| Modal node thiếu (DOM contract bị phá) | Throw `Error("Dialog modal DOM contract violated: missing #<id>")` đồng bộ tại lần `_open` đầu tiên |
| `window.Dialog` chưa load khi caller chạy | Caller throw `TypeError` (truy cập `.alert` của `undefined`) — fail-fast theo Requirement 4.4 + 6.4. Module **không** đăng ký fallback im lặng. |
| 2 lần `Dialog.confirm` liên tiếp | Lần 1 resolve `false`, lần 2 hiển thị bình thường. Không có race. |
| User huỷ bằng nút × | `_close(_currentCancelValue)` → resolve theo cancelValue của loại dialog. |
| Esc khi modal mở | Như nút × |
| Enter khi modal mở | Click default button hoặc nút đang focus |
| `copyText()` (app.js) khi clipboard fail | `await Dialog.alert({ message: "Copy failed." }); throw new Error("Copy failed");` — bên ngoài giữ `.catch()` |

## Migration plan từng file

Mỗi call site dưới đây chỉ thay primitive UI, **không refactor business logic**. Handler nào còn đồng bộ phải đổi thành `async` để dùng `await`. Nếu caller bên ngoài đang `.catch()` chuỗi — giữ nguyên kiểu trả Promise.

### `web/static/app.js`

| Pattern hiện tại | Sau migration |
|------------------|---------------|
| `alert('Paste combos first.')` | `await Dialog.alert({ message: 'Paste combos first.' })` |
| `alert('Error: ' + err.message)` | `await Dialog.alert({ message: 'Error: ' + err.message })` |
| `alert(err.message)` | `await Dialog.alert({ message: err.message })` |
| `if (!confirm('Retry this job?')) return;` | handler đổi `async`, `if (!(await Dialog.confirm({ message: 'Retry this job?' }))) return;` |
| `if (!confirm('Stop this running job?')) return;` | tương tự |
| `if (!confirm('Remove this job from the list and textarea?')) return;` | tương tự |
| `if (!confirm('Stop all running or queued jobs?')) return;` | tương tự |
| `if (!confirm('Xoá TẤT CẢ jobs (mọi trạng thái)? Hành động không thể hoàn tác.')) return;` | `if (!(await Dialog.confirm({ message: 'Xoá TẤT CẢ jobs (mọi trạng thái)? Hành động không thể hoàn tác.', danger: true, confirmLabel: 'Xoá' }))) return;` |
| `if (!confirm('Retry tất cả jobs error & cancelled?')) return;` | tương tự (không `danger`) |
| Headless toggle confirm (multi-line, có `\n\n`, sau đó revert checkbox khi false) | Handler đã `async`. Đổi `confirm(<text gốc>)` → `await Dialog.confirm({ message: <text gốc nguyên xi, giữ \\n\\n> })`. Logic revert checkbox khi resolve `false` giữ nguyên. |
| `copyText()` khi clipboard fail | `await Dialog.alert({ message: 'Copy failed.' }); throw new Error('Copy failed');` (giữ throw để caller `.catch()` xử lý) |

### `web/static/session.js`

7 lời gọi `alert(...)` → tất cả đổi sang `await Dialog.alert({ message: <text gốc> })`. Handler chứa các alert này đa số đã `async` (event handler từ button + fetch). Không xoá hoặc thêm logic.

### `web/static/link.js`

7 lời gọi `alert(...)` → tương tự `session.js`.

Lưu ý đặc thù: chuỗi promise dạng

```js
fetch(...).then(...).catch((err) => alert(err.message));
```

→ chuyển thành **một** trong hai mẫu (phase Tasks chốt mẫu áp dụng đồng nhất):

- Mẫu A (giữ `.catch()`):
  ```js
  fetch(...).then(...).catch(async (err) => {
    await Dialog.alert({ message: err.message });
  });
  ```
- Mẫu B (chuyển handler sang `try/catch`):
  ```js
  try {
    const r = await fetch(...);
    ...
  } catch (err) {
    await Dialog.alert({ message: err.message });
  }
  ```

### `web/static/autoreg.js`

2 lời gọi `alert(...)`:
- `alert('Start failed: ' + ...)` → `await Dialog.alert({ message: 'Start failed: ' + ... })`
- `alert('Start request failed: ' + ...)` → `await Dialog.alert({ message: 'Start request failed: ' + ... })`

Handler đã `async` sẵn — chỉ thay primitive.

### `web/static/hotmail.js`

1 lời gọi `alert('Copy failed')` trong `.catch()` của clipboard → `await Dialog.alert({ message: 'Copy failed' })`.

### `web/static/hme.js`

Refactor IIFE:

1. **Xoá**:
   - `let _hmeDialogResolve = null;`
   - `let _hmeDialogCancelValue = null;`
   - `function closeHmeDialog(value) { ... }`
   - `function showHmeDialog(options) { ... }`
   - `function hmeAlert(title, message, detail) { ... }`
   - `function hmeConfirm(title, message, options) { ... }`
   - `function hmeChoice(title, message, actions, cancelLabel) { ... }`

2. **Đổi mọi call site nội bộ** trong `hme.js`:
   - `await hmeAlert(title, message, detail)` → `await Dialog.alert({ title, message, detail })`
   - `await hmeConfirm(title, message, { confirmLabel, cancelLabel, danger })` → `await Dialog.confirm({ title, message, confirmLabel, cancelLabel, danger })`
   - `await hmeChoice(title, message, actions, cancelLabel)` → `await Dialog.choice({ title, message, actions, cancelLabel })`

3. **Không export** `hmeAlert/hmeConfirm/hmeChoice` ra IIFE return value (nếu có).

Toàn bộ tham số object truyền vào `Dialog.*` giữ nguyên text gốc (Original_Text) — không dịch, không reword.

## CSS bổ sung (`web/static/style.css`)

Hiện tại rule `.hme-feedback-message` đã có (margin/color/font-size/line-height) nhưng **chưa có** `white-space`. Bổ sung **đúng một property**:

```css
.hme-feedback-message {
  /* các property hiện có giữ nguyên */
  white-space: pre-line;
}
```

Không tạo class mới, không đổi class khác. Nếu rule đã được bổ sung trước đó (idempotent re-run) → giữ nguyên.

## index.html change

Thêm đúng một dòng `<script>` ngay trước `settings.js`:

```html
<script src="/static/dialog.js?v=__ASSET_VERSION__"></script>
<script src="/static/settings.js?v=__ASSET_VERSION__"></script>
<script src="/static/app.js?v=__ASSET_VERSION__"></script>
...
```

Phần markup của `#hme-feedback-modal` và các modal HME khác giữ nguyên.

## Backward compatibility & invariants

1. **`copyText()`**: caller bên ngoài đang dùng `.catch()` để xử lý lỗi copy. Giữ `throw new Error('Copy failed')` **sau** `await Dialog.alert(...)` — Promise vẫn reject như cũ.
2. **Fail-fast**: nếu `window.Dialog === undefined` tại runtime → caller throw `TypeError`. **Không** thêm fallback im lặng (Requirement 4.4 + 6.4).
3. **Giá trị return của business logic**: không thay đổi. `Dialog.confirm` resolve `false` ↔ `confirm()` cũ trả `false` → nhánh `if (!ok) return;` vẫn đúng.
4. **Singleton ↔ headless toggle revert**: dialog 1 `confirm` resolve `false` đúng một lần → handler revert checkbox đúng một lần.
5. **Original_Text**: 100% text gốc giữ nguyên (kể cả tiếng Việt, kể cả `\n\n` trong message multi-line).
6. **Loại dialog**: `alert()` cũ → `Dialog.alert`; `confirm()` cũ → `Dialog.confirm`. Không reclass.

## Edge cases

| Trường hợp | Hành vi mong đợi |
|-----------|------------------|
| `Dialog.alert(null)` / `Dialog.alert("text")` / `Dialog.alert({})` | Throw `TypeError` đồng bộ |
| `Dialog.alert({ message: 0 })` / `Dialog.alert({ message: "" })` | Throw `TypeError` đồng bộ |
| `Dialog.choice({ message: "x", actions: [] })` | Throw `Error` đồng bộ |
| `Dialog.choice({ message: "x" })` (thiếu `actions`) | Throw `Error` đồng bộ |
| `#hme-feedback-modal` thiếu khỏi DOM | Throw `Error` đồng bộ tại lần `_open` đầu tiên |
| 2 lần `Dialog.confirm` liên tiếp (chưa kịp đóng dialog 1) | Dialog 1 resolve `false`; dialog 2 hiển thị bình thường |
| User bấm × | `_close(_currentCancelValue)` → resolve cancelValue (alert→true, confirm→false, choice→null) |
| User nhấn Esc | Như × |
| User nhấn Enter trong khi nút Cancel đang focus | Click Cancel → resolve cancelValue |
| `detail` rỗng / undefined | `#hme-feedback-detail` ẩn (`display: none`) |
| `message` chứa `\n\n` (vd headless toggle) | Render xuống dòng nhờ `white-space: pre-line` |
| Caller throw trong handler bằng `await Dialog.confirm(...)` chưa resolve, sau đó caller khác mở dialog | Singleton cancel dialog cũ; Promise resolve `false` đúng một lần |

## Verification (manual smoke, không tạo file test)

Chạy server, mở UI, thực hiện các kịch bản sau và quan sát hành vi:

1. **Reg tab → Start với combos rỗng**: bấm Run khi `#combo-input` trống → modal alert "Paste combos first." hiển thị, bấm OK → modal đóng, focus quay về nút Run.
2. **Reg tab → Headless toggle khi có job running**:
   - Bật Run với combos hợp lệ.
   - Trong khi job chạy, click toggle `#headless-toggle`.
   - Modal confirm multi-line hiển thị xuống dòng đúng.
   - Bấm Huỷ → modal đóng, checkbox revert về trạng thái cũ.
3. **Reg tab → Stop All**: confirm hiển thị, bấm Confirm → request gửi đi.
4. **Reg tab → Clear All**: confirm với nút **danger** đỏ + label "Xoá", bấm Huỷ → không xoá.
5. **Session tab**: trigger một alert (vd combos rỗng) → modal hiển thị, đóng được.
6. **Link tab**: tương tự — trigger 1 alert.
7. **Hotmail tab**: trigger Copy fail (vd disable clipboard API trên devtools) → alert "Copy failed".
8. **HME tab**:
   - Click Add Profile / Open Profile → flow → Cancel.
   - Bấm reactivate trên profile → modal confirm hiển thị, focus đúng nút Confirm.
   - Mở 2 dialog liên tiếp (debug bằng console: `Dialog.confirm(...).then(...); Dialog.alert(...);`) → dialog đầu nhận `false`, dialog sau hiển thị bình thường.
9. **Edge**: trong console gọi `Dialog.alert(null)` → throw đồng bộ; gọi `Dialog.choice({ message: 'x', actions: [] })` → throw đồng bộ.
10. **Loading order**: tạm xoá `dialog.js` khỏi `index.html` → gọi `Dialog.alert(...)` ở console → throw `TypeError` (xác nhận fail-fast). Khôi phục lại sau.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system.*

Theo phân tích prework, toàn bộ acceptance criteria của feature này thuộc một trong các nhóm sau:

- **Contract API tĩnh** (Req 1.1, 1.2, 6.1–6.4): kiểm tra một lần qua code review hoặc smoke test, hành vi không biến thiên theo input.
- **Render UI** (Req 1.3–1.9, 2.3, 2.6, 3.1–3.5): kiểm tra qua manual smoke. Các message text khác nhau không thay đổi luồng render đáng kể.
- **State transition đơn lẻ** (Req 1.10, 2.1–2.5, 4.1–4.5): edge case / example test.
- **Migration scope** (Req 5.1–5.7, 7.1–7.4, 8.1–8.6): code review + grep, không phải logic runtime.

Không có pure function nào với input/output biến thiên đáng kể để PBT 100 iterations đem lại giá trị hơn 2–3 example tests. User cũng đã chỉ rõ "không tạo file test, chỉ manual smoke".

**Kết luận**: Phần lớn acceptance criteria không phù hợp với PBT. Tuy nhiên hai bất biến dưới đây mang tính universal trên mọi chuỗi gọi `Dialog.*` và được ghi lại để làm "specification" cho code reviewer + manual smoke (không sinh thành test tự động vì user chỉ định manual smoke):

### Property 1: Singleton — đúng một dialog tại một thời điểm

*For any* chuỗi N lần gọi liên tiếp `Dialog.alert | Dialog.confirm | Dialog.choice` (N ≥ 1) trong cùng một tick, sau khi tất cả lệnh được thực thi đồng bộ:
- Đúng **một** modal `#hme-feedback-modal` đang ở trạng thái `display: flex` (modal cuối cùng).
- Promise của (N − 1) lần gọi trước đã resolve **đúng một lần** với cancelValue tương ứng (`alert → true`, `confirm → false`, `choice → null`).
- Promise của lần gọi thứ N vẫn đang pending cho tới khi user thao tác.

**Validates: Requirements 2.1, 2.2**

### Property 2: cancelValue mapping — Esc và nút × resolve đúng giá trị huỷ

*For any* dialog đang mở (bất kể message/title/detail), khi user nhấn `Escape` **hoặc** click `#hme-feedback-close`, Promise đang chờ resolve **đúng một lần** với giá trị:
- `true` nếu là `Dialog.alert`.
- `false` nếu là `Dialog.confirm`.
- `null` nếu là `Dialog.choice`.

Và sau khi resolve: `_currentResolve === null`, `_lastFocus.focus()` đã được gọi (nếu `_lastFocus` còn trong DOM).

**Validates: Requirements 1.3, 1.4, 1.5, 2.4, 2.6**

## Testing Strategy

User đã chỉ rõ **không tạo file test tự động** cho feature này. Chiến lược verify gồm hai lớp:

### 1. Manual smoke testing (chính)

Thực hiện toàn bộ kịch bản trong mục **Verification** ở trên. Mỗi kịch bản phủ một hoặc nhiều acceptance criteria:

| Kịch bản smoke | Phủ requirement |
|----------------|-----------------|
| Alert "Paste combos first." | 1.1, 1.2, 1.3, 1.7, 2.3, 2.6, 5.2 |
| Headless toggle multi-line confirm + revert | 1.4, 3.1, 3.3, 4.2, 5.4, 5.5 |
| Stop All confirm | 1.4, 7.1 |
| Clear All danger confirm | 1.6, 7.3 |
| Session/Link/Hotmail alert | 5.1, 5.2, 7.1 |
| HME open profile / save / cancel / reactivate | 5.6, 1.4, 1.5 |
| 2 dialog liên tiếp | 2.1, 2.2 |
| Edge throw đồng bộ | 1.10, 6.4 |
| Loading order kiểm tra fail-fast | 6.1, 6.2, 6.3, 6.4 |

### 2. Code review (bổ sung)

- Grep `\balert\(` và `\bconfirm\(` trong `web/static/{app,session,link,autoreg,hotmail,hme}.js` → kết quả phải rỗng (Req 5.1).
- Diff text trước/sau migration trên các call site → đảm bảo Original_Text không đổi (Req 7.1).
- Diff `web/static/index.html` → chỉ thêm đúng một dòng `<script src=".../dialog.js...">` trước `settings.js` (Req 6.1).
- Diff `web/static/style.css` → chỉ bổ sung `white-space: pre-line` trong rule `.hme-feedback-message` (Req 8.6).
- Diff `web/static/hme.js` → các symbol `_hmeDialogResolve`, `_hmeDialogCancelValue`, `closeHmeDialog`, `showHmeDialog`, `hmeAlert`, `hmeConfirm`, `hmeChoice` đã bị xoá (Req 5.6).
- Không có file Python (`db/`, `automation/`, `web/server*.py`, `web/api*.py`) thay đổi (Req 8.2).
- Không có key mới trong `_EXACT_KEYS` của `db/repositories.py` (Req 8.3).

### 3. Property-based testing — không áp dụng

Như phân tích ở **Correctness Properties**, feature này không có pure function với input space đáng kể để PBT đem lại giá trị. Mọi acceptance criteria đều rơi vào nhóm rendering UI, contract API, hoặc migration scope — phù hợp với manual smoke + code review hơn là test tự động.
