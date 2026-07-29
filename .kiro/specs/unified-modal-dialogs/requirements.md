# Requirements Document

## Introduction

Hợp nhất toàn bộ hộp thoại giao diện web (`web/static/`) về một bộ modal duy nhất, thay thế các lệnh `alert()` và `confirm()` mặc định của trình duyệt. Hệ thống cung cấp một namespace toàn cục `window.Dialog` với ba hàm bất đồng bộ (`alert`, `confirm`, `choice`) tái sử dụng cấu trúc DOM modal đã có ở phần HME (`#hme-feedback-modal` trong `web/static/index.html` và CSS `.modal*` trong `web/static/style.css`). Toàn bộ call site hiện tại trong các file JS frontend được chuyển đổi sang `Dialog.*` với `async/await`, giữ nguyên text gốc của thông báo, đảm bảo logic revert UI (ví dụ headless toggle) chạy đúng trong async flow, và không thay đổi backend Python hay Settings Store.

## Glossary

- **Dialog_Module**: Module JavaScript mới nằm tại `web/static/dialog.js`, định nghĩa và export đối tượng `window.Dialog`.
- **Dialog_API**: Đối tượng toàn cục `window.Dialog` với ba phương thức: `alert`, `confirm`, `choice`.
- **Dialog_Alert**: Phương thức `Dialog.alert({title, message, detail?})` trả về `Promise<true>` khi người dùng đóng modal.
- **Dialog_Confirm**: Phương thức `Dialog.confirm({title, message, detail?, confirmLabel?, cancelLabel?, danger?})` trả về `Promise<boolean>` (`true` nếu xác nhận, `false` nếu huỷ).
- **Dialog_Choice**: Phương thức `Dialog.choice({title, message, actions:[{label, value, className?, autofocus?}], cancelLabel?})` trả về `Promise<value | null>` (giá trị của action được chọn hoặc `null` nếu huỷ).
- **Dialog_Modal_Element**: Phần tử DOM modal được Dialog_Module sử dụng để render hộp thoại, dựa trên cấu trúc `#hme-feedback-modal` đã có sẵn.
- **Singleton_Behavior**: Quy tắc tại mọi thời điểm chỉ có tối đa một Dialog_Modal_Element đang mở; khi mở dialog mới, dialog cũ bị đóng và `Promise` của dialog cũ được giải quyết theo trạng thái huỷ.
- **Migration_Scope**: Tập các file JS frontend phải chuyển đổi từ `alert()`/`confirm()` sang Dialog_API: `web/static/app.js`, `web/static/session.js`, `web/static/link.js`, `web/static/autoreg.js`, `web/static/hotmail.js`, `web/static/hme.js`.
- **Original_Text**: Text gốc (tiếng Việt hoặc tiếng Anh) của tham số truyền vào `alert()` hoặc `confirm()` ở call site hiện tại, được giữ nguyên không dịch và không reword.
- **Default_Alert_Title**: Chuỗi `"Thông báo"` được dùng làm tiêu đề mặc định cho Dialog_Alert khi caller không truyền `title`.
- **Default_Confirm_Title**: Chuỗi `"Xác nhận"` được dùng làm tiêu đề mặc định cho Dialog_Confirm khi caller không truyền `title`.
- **CopyText_Function**: Hàm `copyText()` trong `web/static/app.js` báo lỗi qua `alert(...)` rồi `throw` để bên gọi có thể `.catch()` xử lý tiếp.
- **Loading_Order**: Thứ tự nạp `<script>` trong `web/static/index.html`, trong đó `dialog.js` phải được nạp trước `settings.js`, `app.js`, `session.js`, `link.js`, `hme.js`, `autoreg.js`, `hotmail.js`.

## Requirements

### Requirement 1 — API contract của `window.Dialog`

**User Story:** Là một developer frontend, tôi muốn một API thống nhất `window.Dialog` với ba hàm trả về Promise, để mọi luồng UI đều dùng cùng một primitive bất đồng bộ thay cho `alert`/`confirm` mặc định.

#### Acceptance Criteria

1. THE Dialog_Module SHALL gán đối tượng Dialog_API vào `window.Dialog` ngay khi script được nạp.
2. THE Dialog_API SHALL expose đúng ba phương thức `alert`, `confirm`, `choice` và không expose phương thức nào khác ngoài ba phương thức này.
3. WHEN caller gọi `Dialog.alert({title, message, detail})`, THE Dialog_Module SHALL hiển thị Dialog_Modal_Element với đúng một nút đóng và trả về `Promise` chỉ resolve thành `true` sau khi modal đóng.
4. WHEN caller gọi `Dialog.confirm({title, message, detail, confirmLabel, cancelLabel, danger})`, THE Dialog_Module SHALL hiển thị Dialog_Modal_Element với hai nút (xác nhận và huỷ) và trả về `Promise` resolve thành `true` khi nút xác nhận được kích hoạt, hoặc `false` khi nút huỷ được kích hoạt hoặc modal bị đóng theo cơ chế huỷ.
5. WHEN caller gọi `Dialog.choice({title, message, actions, cancelLabel})`, THE Dialog_Module SHALL render mỗi phần tử trong `actions` thành một nút riêng và trả về `Promise` resolve thành giá trị `value` của action được chọn, hoặc `null` khi nút huỷ được kích hoạt hoặc modal bị đóng theo cơ chế huỷ.
6. WHEN caller gọi `Dialog.confirm` với `danger` bằng `true`, THE Dialog_Module SHALL áp dụng style nguy hiểm cho nút xác nhận theo CSS modal đã có.
7. WHEN caller không truyền `title` cho `Dialog.alert`, THE Dialog_Module SHALL dùng Default_Alert_Title làm tiêu đề.
8. WHEN caller không truyền `title` cho `Dialog.confirm`, THE Dialog_Module SHALL dùng Default_Confirm_Title làm tiêu đề.
9. WHEN caller truyền trường `detail` chuỗi không rỗng, THE Dialog_Module SHALL render `detail` thành một khối phụ bên dưới `message` trong Dialog_Modal_Element.
10. IF caller gọi bất kỳ phương thức nào của Dialog_API với tham số không phải object hoặc thiếu trường `message`, THEN THE Dialog_Module SHALL ném lỗi đồng bộ mô tả tham số sai và không hiển thị modal.

### Requirement 2 — Singleton và focus

**User Story:** Là người dùng cuối, tôi muốn chỉ có một hộp thoại hiển thị tại một thời điểm và phím tắt làm việc nhất quán, để không bị chồng lớp modal hoặc mất focus.

#### Acceptance Criteria

1. THE Dialog_Module SHALL đảm bảo Singleton_Behavior bằng cách giữ tham chiếu tới dialog đang mở và đóng dialog đó trước khi mở dialog mới.
2. WHEN một dialog đang mở và caller gọi tiếp một phương thức Dialog_API khác, THE Dialog_Module SHALL resolve `Promise` của dialog cũ theo trạng thái huỷ tương ứng (`false` cho `confirm`, `null` cho `choice`, `true` cho `alert`) trước khi hiển thị dialog mới.
3. WHEN Dialog_Modal_Element được hiển thị, THE Dialog_Module SHALL chuyển focus vào nút mặc định: nút xác nhận đối với `Dialog.confirm`, nút đóng đối với `Dialog.alert`, hoặc action có `autofocus: true` đối với `Dialog.choice` (nếu không có thì action đầu tiên).
4. WHEN người dùng nhấn phím `Escape` trong khi Dialog_Modal_Element đang mở, THE Dialog_Module SHALL đóng modal và resolve `Promise` theo trạng thái huỷ tương ứng với loại dialog.
5. WHEN người dùng nhấn phím `Enter` trong khi Dialog_Modal_Element đang mở và một nút đang giữ focus, THE Dialog_Module SHALL kích hoạt nút đang giữ focus.
6. WHEN Dialog_Modal_Element đóng, THE Dialog_Module SHALL khôi phục focus về phần tử đã giữ focus ngay trước khi modal mở.

### Requirement 3 — Render nội dung và message nhiều dòng

**User Story:** Là developer migrate code, tôi muốn các message gốc chứa ký tự xuống dòng (`\n`, `\n\n`) hiển thị nguyên vẹn trong modal, để không phải sửa lại text gốc khi chuyển từ `confirm()` sang `Dialog.confirm`.

#### Acceptance Criteria

1. THE Dialog_Module SHALL render trường `message` dưới dạng text thuần (không parse HTML) và áp dụng quy tắc CSS `white-space: pre-line` để bảo toàn các ký tự xuống dòng `\n`.
2. THE Dialog_Module SHALL render trường `title` dưới dạng text thuần và không parse HTML.
3. WHEN caller truyền `message` chứa ký tự `\n` hoặc `\n\n`, THE Dialog_Module SHALL hiển thị các đoạn xuống dòng tương ứng trong Dialog_Modal_Element.
4. THE Dialog_Module SHALL không escape hoặc rút gọn nội dung `message`, `title`, `detail` ngoài các thao tác cần thiết để render an toàn dưới dạng text.
5. THE Dialog_Module SHALL tái sử dụng cấu trúc DOM dựa trên `#hme-feedback-modal` của `web/static/index.html` và các class CSS `.modal*` đã có trong `web/static/style.css` mà không thêm theme hoặc layout mới.

### Requirement 4 — Backward compatibility với handler hiện tại

**User Story:** Là developer migrate code, tôi muốn các handler hiện đang phụ thuộc vào tính chất chặn của `alert()`/`confirm()` (đặc biệt là logic revert UI và `copyText`) tiếp tục hoạt động đúng sau khi chuyển sang `Dialog.*`.

#### Acceptance Criteria

1. THE Dialog_API SHALL trả về Promise để các handler có thể dùng `await Dialog.alert(...)` và `await Dialog.confirm(...)` nhằm thay thế hành vi chặn của `alert()`/`confirm()`.
2. WHEN một handler thực hiện logic revert UI sau khi `Dialog.confirm` resolve thành `false`, THE Dialog_Module SHALL đảm bảo handler nhận được kết quả `false` đúng một lần và logic revert chạy trong cùng async flow.
3. WHEN CopyText_Function dùng Dialog_Alert để báo lỗi sao chép, THE CopyText_Function SHALL `await Dialog.alert(...)` xong và sau đó `throw` lỗi như hành vi gốc, để bên gọi giữ nguyên kiểu xử lý `.catch()`.
4. THE Migration_Scope SHALL không thêm fallback im lặng (silent fallback) cho trường hợp `Dialog` chưa load — handler phải fail-fast nếu `window.Dialog` không tồn tại tại thời điểm chạy.
5. THE Dialog_Module SHALL không thay đổi giá trị trả về của bất kỳ business logic nào khác ngoài việc thay thế primitive UI `alert`/`confirm`.

### Requirement 5 — Phạm vi migration call site

**User Story:** Là maintainer dự án, tôi muốn không còn bất kỳ lời gọi `alert()` hoặc `confirm()` mặc định nào trong các file JS frontend, để toàn bộ UI cảnh báo và xác nhận đi qua cùng một bộ modal.

#### Acceptance Criteria

1. THE Migration_Scope SHALL bao phủ toàn bộ lời gọi `alert(...)` và `confirm(...)` trong `web/static/app.js`, `web/static/session.js`, `web/static/link.js`, `web/static/autoreg.js`, `web/static/hotmail.js`, `web/static/hme.js`.
2. WHEN một lời gọi `alert(message)` được chuyển đổi, THE call site SHALL gọi `await Dialog.alert({message: <Original_Text>})` và giữ nguyên Original_Text.
3. WHEN một lời gọi `confirm(message)` được chuyển đổi, THE call site SHALL gọi `await Dialog.confirm({message: <Original_Text>})` và giữ nguyên Original_Text.
4. WHEN một lời gọi `confirm` hiện tại có thông điệp nhiều dòng (chứa `\n` hoặc `\n\n`, ví dụ luồng headless toggle trong `web/static/app.js`), THE call site SHALL chuyển nguyên Original_Text vào trường `message` của `Dialog.confirm` mà không tách dòng hoặc cắt bớt nội dung.
5. WHEN một handler đang dùng `confirm(...)` đồng bộ, THE handler SHALL được khai báo `async` (hoặc trả về Promise) để có thể dùng `await Dialog.confirm(...)`, kể cả các handler có logic revert UI.
6. THE engine modal cũ trong IIFE của `web/static/hme.js` (`showHmeDialog`, `hmeAlert`, `hmeConfirm`, `hmeChoice`) SHALL được thay thế bằng Dialog_API; các call site nội bộ trong `hme.js` cũng phải dùng `Dialog.alert`, `Dialog.confirm`, `Dialog.choice`.
7. THE Migration_Scope SHALL không thêm hàm wrapper riêng theo từng file (mỗi file gọi trực tiếp `Dialog.*`).

### Requirement 6 — Thứ tự nạp script trong index.html

**User Story:** Là người tích hợp frontend, tôi muốn `dialog.js` luôn sẵn sàng trước khi bất kỳ file phụ thuộc nào chạy, để không xảy ra `Dialog is undefined` tại runtime.

#### Acceptance Criteria

1. THE `web/static/index.html` SHALL nạp `web/static/dialog.js` trước `web/static/settings.js`, `web/static/app.js`, `web/static/session.js`, `web/static/link.js`, `web/static/hme.js`, `web/static/autoreg.js`, `web/static/hotmail.js` theo Loading_Order.
2. WHEN trình duyệt nạp `web/static/index.html`, THE `web/static/dialog.js` SHALL được parse và thực thi trước khi bất kỳ file nào trong Migration_Scope thực thi lời gọi `Dialog.*` đầu tiên.
3. THE `web/static/dialog.js` SHALL không phụ thuộc vào module nào khác trong `web/static/` (đứng độc lập, chỉ phụ thuộc DOM và CSS đã có).
4. IF `window.Dialog` chưa tồn tại tại thời điểm một file trong Migration_Scope gọi `Dialog.*`, THEN THE call site SHALL ném lỗi runtime do truy cập thuộc tính `undefined` (không có fallback im lặng).

### Requirement 7 — Giữ nguyên text gốc

**User Story:** Là người dùng cuối, tôi muốn nội dung cảnh báo và xác nhận giữ nguyên như bản hiện tại, để hành vi quen thuộc và không phát sinh sai lệch ngôn nghĩa khi migrate.

#### Acceptance Criteria

1. THE Migration_Scope SHALL truyền Original_Text vào trường `message` của Dialog_API mà không dịch, không reword, không thêm hoặc bớt từ ngữ.
2. THE Migration_Scope SHALL không thêm trường `title` tuỳ biến cho các call site đang chỉ dùng `alert(text)` hoặc `confirm(text)` mặc định, mà để Dialog_Module áp dụng Default_Alert_Title hoặc Default_Confirm_Title.
3. WHERE một call site cần label nút khác mặc định để khớp với ngữ cảnh hiện tại (ví dụ "Xoá", "Tiếp tục"), THE call site SHALL truyền `confirmLabel`/`cancelLabel` rõ ràng, vẫn giữ Original_Text trong `message`.
4. THE Migration_Scope SHALL không thay đổi cấp độ thông báo (alert vs confirm) so với code gốc — `alert()` luôn map sang `Dialog.alert`, `confirm()` luôn map sang `Dialog.confirm`.

### Requirement 8 — Phạm vi loại trừ (out-of-scope)

**User Story:** Là maintainer, tôi muốn phạm vi thay đổi được giới hạn rõ ràng trong UI dialog primitive, để tránh kéo theo thay đổi backend hoặc cấu hình runtime ngoài chủ đích.

#### Acceptance Criteria

1. THE Dialog_Module SHALL không thay thế hoặc bao bọc `window.prompt()` (codebase hiện không dùng `prompt`, không thêm primitive mới cho `prompt`).
2. THE feature SHALL không sửa đổi mã backend Python (`db/`, `automation/`, `web/server*.py`, `web/api*.py`, `app/` hoặc tương đương).
3. THE feature SHALL không thêm key mới vào Settings Store (`SettingsRepository`) và không đọc/ghi `localStorage` cho cấu hình runtime của dialog.
4. THE feature SHALL không tạo loại dialog mới ngoài `alert`, `confirm`, `choice` (không thêm toast, snackbar, banner, prompt).
5. THE feature SHALL không refactor business logic của handler — chỉ thay primitive UI và bọc handler trong `async/await` ở mức tối thiểu cần thiết để chờ Promise.
6. THE feature SHALL không đổi class CSS hiện có của modal (`.modal*` trong `web/static/style.css`) ngoài các bổ sung tối thiểu để render `detail` và áp dụng `white-space: pre-line` cho `message` nếu chưa có.
