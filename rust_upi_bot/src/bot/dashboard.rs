//! Dashboard per-user — 1 message sống / user gom MỌI tiến trình của user đó.
//!
//! Thay cho mô hình cũ "mỗi job 1 message tự edit mỗi 2.5s" (N job cùng chat →
//! flood → 429). Giờ:
//!   - Mỗi user = 1 message dashboard duy nhất.
//!   - Job event chỉ cập nhật `JobBoard` + `mark_dirty(user)`.
//!   - 1 ticker toàn cục (`flush`) định kỳ render bảng từ board → edit 1
//!     message qua `TelegramClient` (đã có SendGate pace per-chat + honor
//!     retry_after) → tải gửi giảm ~N lần.
//!
//! Lock `Mutex` chỉ giữ khi đọc/ghi state ngắn; mọi network call thực hiện
//! NGOÀI lock (collect snapshot → release → send → re-lock store kết quả).

use std::collections::hash_map::DefaultHasher;
use std::collections::HashMap;
use std::hash::{Hash, Hasher};
use std::sync::Arc;

use serde_json::Value;
use tokio::sync::Mutex;

use crate::bot::board::JobBoard;
use crate::bot::i18n::{self, Lang};
use crate::bot::proc_view;
use crate::bot::telegram::TelegramClient;

/// Số nút Stop tối đa trên 1 dashboard (chống vượt giới hạn inline keyboard).
const STOP_BTN_CAP: usize = 20;

#[derive(Clone)]
pub struct DashboardManager {
    inner: Arc<Mutex<HashMap<i64, UserDash>>>,
}

struct UserDash {
    chat_id: i64,
    lang: Lang,
    /// Message dashboard hiện tại. None = chưa gửi lần nào (ticker sẽ gửi mới).
    msg_id: Option<i64>,
    /// Hash nội dung lần render gần nhất — skip edit khi không đổi.
    last_hash: u64,
    /// Có thay đổi cần render lại không.
    dirty: bool,
}

/// Snapshot state 1 user để xử lý ngoài lock.
struct DashJob {
    user_id: i64,
    chat_id: i64,
    lang: Lang,
    msg_id: Option<i64>,
    last_hash: u64,
}

impl DashboardManager {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    /// Đảm bảo có entry dashboard cho user + đánh dấu dirty. Gọi khi user submit
    /// job mới (cập nhật chat_id/lang theo lần gần nhất).
    pub async fn touch(&self, user_id: i64, chat_id: i64, lang: Lang) {
        let mut g = self.inner.lock().await;
        let e = g.entry(user_id).or_insert(UserDash {
            chat_id,
            lang,
            msg_id: None,
            last_hash: 0,
            dirty: true,
        });
        e.chat_id = chat_id;
        e.lang = lang;
        e.dirty = true;
    }

    /// Đánh dấu user cần render lại (job event). No-op nếu user chưa có entry.
    pub async fn mark_dirty(&self, user_id: i64) {
        let mut g = self.inner.lock().await;
        if let Some(e) = g.get_mut(&user_id) {
            e.dirty = true;
        }
    }

    /// Ticker: render + edit/send dashboard cho mọi user dirty. Board rỗng →
    /// render dòng "không còn tiến trình" 1 lần rồi gỡ entry (batch sau tạo
    /// dashboard mới). `clock` = giờ VN hiển thị (now_hms).
    pub async fn flush(&self, tg: &TelegramClient, board: &JobBoard, clock: &str) {
        // 1. Collect user dirty (mark dirty=false optimistic) — không await trong lock.
        let jobs: Vec<DashJob> = {
            let mut g = self.inner.lock().await;
            let mut out = Vec::new();
            for (uid, d) in g.iter_mut() {
                if d.dirty {
                    d.dirty = false;
                    out.push(DashJob {
                        user_id: *uid,
                        chat_id: d.chat_id,
                        lang: d.lang,
                        msg_id: d.msg_id,
                        last_hash: d.last_hash,
                    });
                }
            }
            out
        };
        if jobs.is_empty() {
            return;
        }

        // 2. Xử lý từng user ngoài lock.
        for j in jobs {
            let entries = board.snapshot_entries_for_user(j.user_id).await;
            let html = proc_view::render_dashboard_html(&entries, j.lang, clock);
            let mut hasher = DefaultHasher::new();
            html.hash(&mut hasher);
            let hash = hasher.finish();

            // Không đổi nội dung → bỏ qua (tiết kiệm 1 API call).
            if hash == j.last_hash && j.msg_id.is_some() {
                continue;
            }

            let kb = stop_keyboard(&entries, j.lang);
            let empty = entries.is_empty();

            if let Some(mid) = j.msg_id {
                tg.edit_message_kb_html(j.chat_id, mid, &html, kb).await.ok();
                // Board rỗng → đã render dòng idle, gỡ entry để batch sau tạo mới.
                if empty {
                    self.inner.lock().await.remove(&j.user_id);
                    continue;
                }
                let mut g = self.inner.lock().await;
                if let Some(e) = g.get_mut(&j.user_id) {
                    e.last_hash = hash;
                }
            } else {
                // Chưa có message → gửi mới (trừ khi board đã rỗng — không tạo
                // dashboard rỗng vô nghĩa).
                if empty {
                    self.inner.lock().await.remove(&j.user_id);
                    continue;
                }
                match tg.send_message_kb_html(j.chat_id, &html, kb).await {
                    Ok(mid) => {
                        let mut g = self.inner.lock().await;
                        if let Some(e) = g.get_mut(&j.user_id) {
                            e.msg_id = Some(mid);
                            e.last_hash = hash;
                        }
                    }
                    Err(e) => {
                        tracing::warn!(user_id = j.user_id, "dashboard send fail: {}", e);
                        // Gửi fail (vd 429) → đánh dirty lại để ticker sau thử lại.
                        let mut g = self.inner.lock().await;
                        if let Some(en) = g.get_mut(&j.user_id) {
                            en.dirty = true;
                        }
                    }
                }
            }
        }
    }
}

/// Keyboard nút Stop cho dashboard — callback `dstop:<job_id>` (khác `bstop`
/// của /board admin để tách luồng re-render). Cap số nút.
fn stop_keyboard(entries: &[(u64, crate::bot::board::JobStatus)], lang: Lang) -> Value {
    let mut rows: Vec<Value> = Vec::new();
    for (job_id, s) in entries.iter().take(STOP_BTN_CAP) {
        rows.push(serde_json::json!([{
            "text": i18n::btn_board_stop(lang, &s.email_masked),
            "callback_data": format!("dstop:{}", job_id),
        }]));
    }
    Value::Array(rows)
}
