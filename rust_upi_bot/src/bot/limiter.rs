//! Per-user state — anti-spam, cooldown, in-flight tracking.
//!
//! Chính sách:
//!   * 1 job active per user. Submission tiếp khi đang chạy → reject.
//!   * Cooldown N giây sau khi job xong (kể cả PASS hoặc FAIL) → chống spam.
//!   * Message rate limit: max M message/phút per user. Vượt → ignore tạm.
//!   * Counter tự reset sau 60s sliding window.
//!
//! Quota concurrent process per user (`max_per_user`) hot-reload được:
//!   * Default toàn cục lưu trong `Arc<AtomicU32>` — admin đổi 1 lần áp ngay
//!     cho mọi user không có override.
//!   * Override per-user lưu trong cache `HashMap<user_id, u32>` — pre-load
//!     từ SQLite ở boot, write-through khi admin set/xóa.
//!   * Effective limit khi admit = override.unwrap_or(default). Job đang chạy
//!     không bị kill khi admin giảm — chỉ submit kế bị block tới khi xuống
//!     dưới mức mới.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::Mutex;

/// Hard min/max enforce ở mọi setter (atomic + override). Trùng với
/// `Settings::MAX_PER_USER_{MIN,MAX}` — khai báo lại ở đây để limiter không
/// phụ thuộc settings module (giữ test self-contained).
pub const MAX_PER_USER_MIN: u32 = 1;
pub const MAX_PER_USER_MAX: u32 = 10;

#[inline]
fn clamp_max(n: u32) -> u32 {
    n.clamp(MAX_PER_USER_MIN, MAX_PER_USER_MAX)
}

#[derive(Debug, Default, Clone)]
struct UserState {
    in_flight: u32,
    last_done_at: Option<Instant>,
    /// Sliding window: timestamps các message gần đây (giữ trong 60s).
    recent_messages: Vec<Instant>,
}

#[derive(Clone)]
pub struct UserLimiter {
    inner: Arc<Mutex<HashMap<i64, UserState>>>,
    cooldown: Duration,
    msg_rate_per_min: u32,
    /// Default toàn cục — atomic để hot-reload không cần lock.
    max_per_user_default: Arc<AtomicU32>,
    /// Override per-user. `None` (không có entry) → dùng default.
    user_overrides: Arc<Mutex<HashMap<i64, u32>>>,
}

#[derive(Debug, Clone)]
pub enum AdmitDecision {
    /// Cho phép submit job mới.
    Allow,
    /// Đã đạt giới hạn tiến trình đồng thời của user.
    MaxConcurrent { max: u32 },
    /// Đang trong cooldown — phải đợi N giây nữa.
    Cooldown { remaining_secs: u64 },
}

#[derive(Debug, Clone)]
pub enum MessageDecision {
    Allow,
    /// Vượt rate limit — bot bỏ qua, log warning.
    Drop { observed: u32, limit: u32 },
}

impl UserLimiter {
    /// Constructor. `default_max_per_user` = giá trị áp cho user không có
    /// override (sẽ clamp 1..=10). `initial_overrides` = pre-load từ SQLite
    /// để hot-reload từ lần đầu boot — caller (main) lấy qua
    /// `store.list_user_limits()`.
    pub fn new(
        cooldown: Duration,
        msg_rate_per_min: u32,
        default_max_per_user: u32,
        initial_overrides: Vec<(i64, u32)>,
    ) -> Self {
        let overrides: HashMap<i64, u32> = initial_overrides
            .into_iter()
            .map(|(uid, n)| (uid, clamp_max(n)))
            .collect();
        Self {
            inner: Arc::new(Mutex::new(HashMap::new())),
            cooldown,
            msg_rate_per_min,
            max_per_user_default: Arc::new(AtomicU32::new(clamp_max(default_max_per_user))),
            user_overrides: Arc::new(Mutex::new(overrides)),
        }
    }

    /// Đọc default toàn cục hiện tại (atomic, không lock).
    pub fn default_max_per_user(&self) -> u32 {
        self.max_per_user_default.load(Ordering::Relaxed)
    }

    /// Cập nhật default toàn cục — áp ngay cho mọi user không có override.
    /// Caller PHẢI ghi DB qua `Settings::set_max_per_user_default` trước/sau
    /// để persist (write-through).
    pub fn set_default_max_per_user(&self, n: u32) {
        self.max_per_user_default
            .store(clamp_max(n), Ordering::Relaxed);
    }

    /// Đọc override hiện tại của 1 user. `None` = không có override.
    pub async fn get_user_override(&self, user_id: i64) -> Option<u32> {
        self.user_overrides.lock().await.get(&user_id).copied()
    }

    /// Set override cho 1 user. Caller PHẢI ghi DB qua `Settings::set_user_limit`
    /// để persist. Hot-reload: lần `try_admit` kế tiếp đọc giá trị mới.
    pub async fn set_user_override(&self, user_id: i64, max_per_user: u32) {
        self.user_overrides
            .lock()
            .await
            .insert(user_id, clamp_max(max_per_user));
    }

    /// Xóa override → user dùng lại default toàn cục. Caller PHẢI gọi
    /// `Settings::remove_user_limit` để persist.
    pub async fn clear_user_override(&self, user_id: i64) -> bool {
        self.user_overrides
            .lock()
            .await
            .remove(&user_id)
            .is_some()
    }

    /// Effective limit cho 1 user — override nếu có, ngược lại default toàn
    /// cục. Public để admit handler / `/my_limit` đọc cùng nguồn.
    pub async fn effective_max(&self, user_id: i64) -> u32 {
        if let Some(n) = self.user_overrides.lock().await.get(&user_id).copied() {
            return n;
        }
        self.default_max_per_user()
    }

    /// Snapshot toàn bộ override (sort theo user_id) — cho lệnh admin liệt kê.
    pub async fn snapshot_overrides(&self) -> Vec<(i64, u32)> {
        let g = self.user_overrides.lock().await;
        let mut v: Vec<(i64, u32)> = g.iter().map(|(k, v)| (*k, *v)).collect();
        v.sort_by_key(|(uid, _)| *uid);
        v
    }

    /// Đăng ký 1 message từ user → trả AdmitDecision cho message đó (anti-flood).
    pub async fn register_message(&self, user_id: i64) -> MessageDecision {
        let mut g = self.inner.lock().await;
        let st = g.entry(user_id).or_default();
        let now = Instant::now();
        // Drop entries cũ hơn 60s
        st.recent_messages.retain(|t| now.duration_since(*t) < Duration::from_secs(60));
        st.recent_messages.push(now);
        let observed = st.recent_messages.len() as u32;
        if observed > self.msg_rate_per_min {
            return MessageDecision::Drop {
                observed,
                limit: self.msg_rate_per_min,
            };
        }
        MessageDecision::Allow
    }

    /// Check + RESERVE slot atomic trong 1 lần lock. Nếu `Allow` thì đã tăng
    /// `in_flight` luôn → đóng khe TOCTOU giữa check và submit (preflight proxy
    /// await ở giữa không còn cho phép user vượt effective max). Caller PHẢI
    /// `release` nếu sau đó submit thất bại, hoặc để `mark_done` trả slot khi
    /// job hoàn tất.
    pub async fn try_admit(&self, user_id: i64) -> AdmitDecision {
        // Đọc effective max NGOÀI lock `inner` để giảm contention. Race chấp
        // nhận được: nếu admin đổi giữa 2 dòng, lần admit này dùng giá trị cũ
        // 1 nhịp — lần kế áp giá trị mới ngay. Không có invariant nào bị phá.
        let max = self.effective_max(user_id).await;
        let mut g = self.inner.lock().await;
        let st = g.entry(user_id).or_default();
        if st.in_flight >= max {
            return AdmitDecision::MaxConcurrent { max };
        }
        if st.in_flight == 0 {
            if let Some(last) = st.last_done_at {
                let elapsed = last.elapsed();
                if elapsed < self.cooldown {
                    return AdmitDecision::Cooldown {
                        remaining_secs: (self.cooldown - elapsed).as_secs() + 1,
                    };
                }
            }
        }
        st.in_flight += 1; // reserve ngay
        AdmitDecision::Allow
    }

    /// Rollback 1 reservation khi job KHÔNG vào được queue (proxy chết, trùng
    /// tài khoản, queue đầy...). Chỉ giảm `in_flight`, KHÔNG set `last_done_at`
    /// → không áp cooldown oan cho job chưa từng chạy.
    pub async fn release(&self, user_id: i64) {
        let mut g = self.inner.lock().await;
        if let Some(st) = g.get_mut(&user_id) {
            st.in_flight = st.in_flight.saturating_sub(1);
        }
    }

    /// Reset CỨNG `in_flight=0` cho user (dùng cho `/stop`, `/stopall`, nút
    /// Stop trên board). Lý do: cancel_token + registry clear ngay khi /stop,
    /// nhưng worker `mark_done` chạy bất đồng bộ — nếu user gửi combo mới
    /// trước khi worker kết thúc cancel handshake, limiter vẫn còn đếm cũ →
    /// MaxConcurrent block oan. Force reset ở /stop diệt khe race này.
    /// `mark_done` của worker sau đó giảm `saturating_sub(0)` → không underflow.
    /// KHÔNG set `last_done_at` (user dừng chủ động, không phạt cooldown).
    pub async fn force_reset_user(&self, user_id: i64) {
        let mut g = self.inner.lock().await;
        if let Some(st) = g.get_mut(&user_id) {
            st.in_flight = 0;
        }
    }

    /// Reset CỨNG TẤT CẢ user (admin `/stopall` / `/flushall`). Drain map →
    /// memory không giữ lại entry rỗng. Tương đương `force_reset_user` cho mọi user.
    pub async fn force_reset_everyone(&self) {
        let mut g = self.inner.lock().await;
        for (_, st) in g.iter_mut() {
            st.in_flight = 0;
        }
    }

    pub async fn mark_done(&self, user_id: i64) {
        let mut g = self.inner.lock().await;
        let st = g.entry(user_id).or_default();
        st.in_flight = st.in_flight.saturating_sub(1);
        st.last_done_at = Some(Instant::now());
    }

    /// Maintenance — drop entries không hoạt động > 1h để tránh leak HashMap.
    pub async fn vacuum(&self) {
        let mut g = self.inner.lock().await;
        let now = Instant::now();
        g.retain(|_, st| {
            if st.in_flight > 0 {
                return true;
            }
            let recent_recent = st
                .recent_messages
                .last()
                .map(|t| now.duration_since(*t) < Duration::from_secs(3600))
                .unwrap_or(false);
            let recent_done = st
                .last_done_at
                .map(|t| now.duration_since(t) < Duration::from_secs(3600))
                .unwrap_or(false);
            recent_recent || recent_done
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn cooldown_blocks_then_allows() {
        let lim = UserLimiter::new(Duration::from_millis(200), 100, 2, vec![]);
        // try_admit reserve ngay khi Allow.
        assert!(matches!(lim.try_admit(1).await, AdmitDecision::Allow));
        // Vẫn còn slot thứ 2 → Allow (reserve tiếp).
        assert!(matches!(lim.try_admit(1).await, AdmitDecision::Allow));
        // Đã đủ 2 → MaxConcurrent.
        assert!(matches!(
            lim.try_admit(1).await,
            AdmitDecision::MaxConcurrent { max: 2 }
        ));
        lim.mark_done(1).await;
        lim.mark_done(1).await;
        // Idle + ngay sau done → cooldown.
        assert!(matches!(
            lim.try_admit(1).await,
            AdmitDecision::Cooldown { .. }
        ));
        tokio::time::sleep(Duration::from_millis(220)).await;
        assert!(matches!(lim.try_admit(1).await, AdmitDecision::Allow));
    }

    #[tokio::test]
    async fn release_rolls_back_reservation_without_cooldown() {
        let lim = UserLimiter::new(Duration::from_secs(60), 1, 1, vec![]);
        assert!(matches!(lim.try_admit(9).await, AdmitDecision::Allow));
        // Đã đầy slot.
        assert!(matches!(
            lim.try_admit(9).await,
            AdmitDecision::MaxConcurrent { max: 1 }
        ));
        // Rollback (submit fail) → slot trả lại, KHÔNG cooldown.
        lim.release(9).await;
        assert!(matches!(lim.try_admit(9).await, AdmitDecision::Allow));
    }

    #[tokio::test]
    async fn rate_limit_drops_after_threshold() {
        let lim = UserLimiter::new(Duration::from_secs(0), 3, 2, vec![]);
        for _ in 0..3 {
            assert!(matches!(
                lim.register_message(7).await,
                MessageDecision::Allow
            ));
        }
        // Cái thứ 4 vượt
        assert!(matches!(
            lim.register_message(7).await,
            MessageDecision::Drop { .. }
        ));
    }

    #[tokio::test]
    async fn user_override_wins_over_default() {
        // Default 2, user 42 override 5 → user 42 chạy được tới 5, user khác 2.
        let lim = UserLimiter::new(Duration::from_secs(0), 100, 2, vec![(42, 5)]);
        for _ in 0..5 {
            assert!(matches!(lim.try_admit(42).await, AdmitDecision::Allow));
        }
        assert!(matches!(
            lim.try_admit(42).await,
            AdmitDecision::MaxConcurrent { max: 5 }
        ));
        // User 99 không override → giới hạn 2.
        assert!(matches!(lim.try_admit(99).await, AdmitDecision::Allow));
        assert!(matches!(lim.try_admit(99).await, AdmitDecision::Allow));
        assert!(matches!(
            lim.try_admit(99).await,
            AdmitDecision::MaxConcurrent { max: 2 }
        ));
    }

    #[tokio::test]
    async fn hot_reload_default_takes_effect_next_admit() {
        let lim = UserLimiter::new(Duration::from_secs(0), 100, 2, vec![]);
        assert!(matches!(lim.try_admit(1).await, AdmitDecision::Allow));
        assert!(matches!(lim.try_admit(1).await, AdmitDecision::Allow));
        // Đã đầy 2.
        assert!(matches!(
            lim.try_admit(1).await,
            AdmitDecision::MaxConcurrent { max: 2 }
        ));
        // Admin nâng default lên 4 → admit kế tiếp Allow ngay.
        lim.set_default_max_per_user(4);
        assert!(matches!(lim.try_admit(1).await, AdmitDecision::Allow));
        assert!(matches!(lim.try_admit(1).await, AdmitDecision::Allow));
        assert!(matches!(
            lim.try_admit(1).await,
            AdmitDecision::MaxConcurrent { max: 4 }
        ));
    }

    #[tokio::test]
    async fn override_clamped_to_range() {
        // Override out-of-range bị clamp về biên — không panic, không drop user.
        let lim = UserLimiter::new(Duration::from_secs(0), 100, 2, vec![(7, 999)]);
        assert_eq!(lim.effective_max(7).await, MAX_PER_USER_MAX);
        lim.set_user_override(7, 0).await;
        assert_eq!(lim.effective_max(7).await, MAX_PER_USER_MIN);
    }

    #[tokio::test]
    async fn clear_override_falls_back_to_default() {
        let lim = UserLimiter::new(Duration::from_secs(0), 100, 3, vec![(5, 8)]);
        assert_eq!(lim.effective_max(5).await, 8);
        assert!(lim.clear_user_override(5).await);
        assert_eq!(lim.effective_max(5).await, 3);
        // Idempotent: lần 2 trả false.
        assert!(!lim.clear_user_override(5).await);
    }
}
