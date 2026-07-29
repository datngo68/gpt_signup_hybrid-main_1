//! SendGate — bộ điều phối gửi tin Telegram chống 429.
//!
//! Telegram giới hạn ~1 message/giây/chat (sustained) + ~30/giây toàn bot;
//! `editMessageText`/`sendPhoto` đều tính vào limit. Bot trước đây bắn thẳng
//! không tiết chế → flood per-chat → 429, và vì KHÔNG đọc `retry_after` nên
//! mỗi lần gửi tiếp trong cửa sổ phạt lại bị Telegram tăng hình phạt (penalty
//! phình tới 20+ phút).
//!
//! Gate này:
//!   1. **Per-chat pacing**: mỗi chat tối thiểu `per_chat` (1.1s) giữa 2 lần
//!      gửi. Nhiều task gửi cùng chat → mỗi task "đặt chỗ" 1 slot tăng dần →
//!      tự xếp hàng có giãn cách, KHÔNG bao giờ vượt trần per-chat.
//!   2. **Global pacing**: trần tổng `global` (~35ms ≈ 28 msg/s) cho toàn bot.
//!   3. **Honor `retry_after`**: khi dính 429, đẩy mốc `next_allowed` của chat
//!      tới hết cửa sổ phạt → KHÔNG bắn tiếp → penalty không phình.
//!
//! Reserve slot dưới `Mutex` ngắn (chỉ tính toán mốc), `sleep` NGOÀI lock nên
//! không chặn task khác. Dùng `tokio::sync::Mutex` để await an toàn.

use std::collections::HashMap;
use std::time::Duration;

use tokio::sync::Mutex;
use tokio::time::Instant;

pub struct SendGate {
    inner: Mutex<GateInner>,
    /// Giãn cách tối thiểu giữa 2 message cùng 1 chat.
    per_chat: Duration,
    /// Giãn cách tối thiểu toàn bot (trần tổng).
    global: Duration,
}

struct GateInner {
    /// Mốc sớm nhất được phép gửi tiếp cho từng chat.
    chat_next: HashMap<i64, Instant>,
    /// Mốc sớm nhất được phép gửi tiếp toàn cục.
    global_next: Instant,
}

impl SendGate {
    pub fn new() -> Self {
        Self {
            inner: Mutex::new(GateInner {
                chat_next: HashMap::new(),
                global_next: Instant::now(),
            }),
            // 1.1s > trần 1 msg/s của Telegram → biên an toàn.
            per_chat: Duration::from_millis(1100),
            // ~28 msg/s < trần ~30/s toàn bot.
            global: Duration::from_millis(35),
        }
    }

    /// Đặt chỗ 1 slot gửi cho `chat_id`, trả về sau khi đã chờ đủ giãn cách.
    /// Reserve tuyến tính: 2 lời gọi liên tiếp cùng chat nhận 2 mốc cách nhau
    /// `per_chat` → tự xếp hàng. Lock chỉ giữ khi tính mốc, sleep ngoài lock.
    pub async fn acquire(&self, chat_id: i64) {
        let wait_until = {
            let mut g = self.inner.lock().await;
            let now = Instant::now();
            let chat_at = g.chat_next.get(&chat_id).copied().unwrap_or(now).max(now);
            let global_at = g.global_next.max(now);
            let at = chat_at.max(global_at);
            g.chat_next.insert(chat_id, at + self.per_chat);
            g.global_next = at + self.global;
            at
        };
        let now = Instant::now();
        if wait_until > now {
            tokio::time::sleep(wait_until - now).await;
        }
    }

    /// Dính 429 → đẩy mốc gửi tiếp của chat (và global) tới hết cửa sổ phạt
    /// `retry_after` (+1s đệm). Mọi lời `acquire` sau sẽ tự chờ tới đó.
    pub async fn penalize(&self, chat_id: i64, retry_after_secs: u64) {
        let until = Instant::now() + Duration::from_secs(retry_after_secs.saturating_add(1));
        let mut g = self.inner.lock().await;
        let e = g.chat_next.entry(chat_id).or_insert(until);
        if until > *e {
            *e = until;
        }
        if until > g.global_next {
            g.global_next = until;
        }
    }

    /// Dọn mốc chat đã quá hạn (chống map phình). Gọi định kỳ từ vacuum loop.
    pub async fn vacuum(&self) {
        let mut g = self.inner.lock().await;
        let now = Instant::now();
        g.chat_next.retain(|_, t| *t > now);
    }
}
