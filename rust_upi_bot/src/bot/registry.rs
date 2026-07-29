//! Per-user job registry — track tokens để `/stop` chỉ cancel job của
//! đúng user đó, không đụng đến người khác.
//!
//! Mỗi job 1 `CancellationToken`. Khi user gửi `/stop`:
//!   1. Lấy tất cả token của user → `cancel()` từng cái
//!   2. Remove khỏi map → memory không grow
//!
//! Khi job tự kết thúc (Done/Timeout/Cancel) → caller phải gọi `unregister`
//! để dọn entry. Vacuum định kỳ làm thêm 1 lớp safety.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use tokio::sync::Mutex;
use tokio_util::sync::CancellationToken;

#[derive(Clone)]
pub struct JobRegistry {
    inner: Arc<Inner>,
}

struct Inner {
    map: Mutex<HashMap<i64, Vec<Entry>>>,
    next_id: AtomicU64,
}

struct Entry {
    id: u64,
    token: CancellationToken,
    /// Khóa định danh tài khoản (email normalized) — chống 1 user gửi trùng
    /// tài khoản đang chạy/queue.
    email_key: String,
}

impl JobRegistry {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Inner {
                map: Mutex::new(HashMap::new()),
                next_id: AtomicU64::new(1),
            }),
        }
    }

    /// Đăng ký job mới NHƯNG từ chối nếu user đã có job đang chạy/queue cho
    /// cùng `email_key`. Atomic dưới mutex — không có khe TOCTOU giữa check và
    /// insert. Trả `None` khi trùng tài khoản. Worker phải `tokio::select!`
    /// cùng `token.cancelled()` để stop sớm khi user gửi `/stop`.
    pub async fn try_register(
        &self,
        user_id: i64,
        email_key: &str,
    ) -> Option<(u64, CancellationToken)> {
        let mut g = self.inner.map.lock().await;
        if let Some(v) = g.get(&user_id) {
            if v.iter().any(|e| e.email_key == email_key) {
                return None;
            }
        }
        let id = self.inner.next_id.fetch_add(1, Ordering::Relaxed);
        let token = CancellationToken::new();
        g.entry(user_id).or_default().push(Entry {
            id,
            token: token.clone(),
            email_key: email_key.to_string(),
        });
        Some((id, token))
    }

    /// Gọi khi job tự kết thúc (Done/Timeout/Cancel) — dọn entry.
    /// No-op nếu user đã /stop trước đó (entry đã được clear).
    pub async fn unregister(&self, user_id: i64, job_id: u64) {
        let mut g = self.inner.map.lock().await;
        if let Some(v) = g.get_mut(&user_id) {
            v.retain(|e| e.id != job_id);
            if v.is_empty() {
                g.remove(&user_id);
            }
        }
    }

    /// Cancel TẤT CẢ job đang chạy/queue của user. Trả số job bị cancel.
    /// Không đụng job của user khác.
    pub async fn stop_user(&self, user_id: i64) -> usize {
        let mut g = self.inner.map.lock().await;
        let Some(entries) = g.remove(&user_id) else {
            return 0;
        };
        let n = entries.len();
        for e in entries {
            e.token.cancel();
        }
        n
    }

    /// Cancel ĐÚNG 1 job theo (user_id, job_id) — cho nút Stop per-process.
    /// Trả true nếu tìm thấy và đã cancel.
    pub async fn stop_job(&self, user_id: i64, job_id: u64) -> bool {
        let mut g = self.inner.map.lock().await;
        let Some(v) = g.get_mut(&user_id) else {
            return false;
        };
        let mut found = false;
        v.retain(|e| {
            if e.id == job_id {
                e.token.cancel();
                found = true;
                false
            } else {
                true
            }
        });
        if v.is_empty() {
            g.remove(&user_id);
        }
        found
    }

    /// Cancel TẤT CẢ job của MỌI user (admin `/stopall` & `/flushall`). Trả
    /// tổng số job bị cancel. Drain sạch map → memory không giữ lại entry.
    pub async fn stop_everyone(&self) -> usize {
        let mut g = self.inner.map.lock().await;
        let mut n = 0usize;
        for (_, entries) in g.drain() {
            for e in entries {
                e.token.cancel();
                n += 1;
            }
        }
        n
    }

    /// Cancel ĐÚNG 1 job theo `job_id` BẤT KỂ user nào sở hữu — chỉ admin được
    /// gọi (caller phải verify admin trước). Trả `true` nếu tìm thấy và cancel.
    pub async fn stop_job_any(&self, job_id: u64) -> bool {
        let mut g = self.inner.map.lock().await;
        let mut found = false;
        let mut empty_user: Option<i64> = None;
        for (uid, v) in g.iter_mut() {
            let before = v.len();
            v.retain(|e| {
                if e.id == job_id {
                    e.token.cancel();
                    false
                } else {
                    true
                }
            });
            if v.len() != before {
                found = true;
                if v.is_empty() {
                    empty_user = Some(*uid);
                }
                break;
            }
        }
        if let Some(u) = empty_user {
            g.remove(&u);
        }
        found
    }

    /// Số user còn entry (memory metric).
    #[allow(dead_code)]
    pub async fn user_count(&self) -> usize {
        self.inner.map.lock().await.len()
    }

    /// Vacuum entries có token đã cancelled (safety cleanup nếu unregister
    /// quên gọi). Gọi định kỳ.
    pub async fn vacuum(&self) {
        let mut g = self.inner.map.lock().await;
        for (_, v) in g.iter_mut() {
            v.retain(|e| !e.token.is_cancelled());
        }
        g.retain(|_, v| !v.is_empty());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn stop_user_only_cancels_own_jobs() {
        let reg = JobRegistry::new();
        let (id_a1, token_a1) = reg.try_register(100, "a@x.com").await.unwrap();
        let (id_a2, token_a2) = reg.try_register(100, "b@x.com").await.unwrap();
        let (id_b1, token_b1) = reg.try_register(200, "a@x.com").await.unwrap();
        assert_eq!(reg.user_count().await, 2);

        let stopped = reg.stop_user(100).await;
        assert_eq!(stopped, 2);
        assert!(token_a1.is_cancelled());
        assert!(token_a2.is_cancelled());
        // User 200 KHÔNG bị cancel
        assert!(!token_b1.is_cancelled());

        // Map cleaned for user 100, retained for 200
        assert_eq!(reg.user_count().await, 1);
        let _ = (id_a1, id_a2, id_b1);
    }

    #[tokio::test]
    async fn unregister_cleans_entry() {
        let reg = JobRegistry::new();
        let (id, _token) = reg.try_register(7, "a@x.com").await.unwrap();
        assert_eq!(reg.user_count().await, 1);
        reg.unregister(7, id).await;
        assert_eq!(reg.user_count().await, 0);
    }

    #[tokio::test]
    async fn try_register_rejects_duplicate_email() {
        let reg = JobRegistry::new();
        let first = reg.try_register(5, "dup@x.com").await;
        assert!(first.is_some());
        // Cùng user + cùng email đang chạy → từ chối.
        assert!(reg.try_register(5, "dup@x.com").await.is_none());
        // Email khác của cùng user → cho phép.
        assert!(reg.try_register(5, "other@x.com").await.is_some());
        // User khác cùng email → cho phép (giới hạn theo từng user).
        assert!(reg.try_register(6, "dup@x.com").await.is_some());
        // Sau khi job đầu kết thúc → email cũ được phép lại.
        let (id, _) = first.unwrap();
        reg.unregister(5, id).await;
        assert!(reg.try_register(5, "dup@x.com").await.is_some());
    }

    #[tokio::test]
    async fn stop_user_returns_zero_when_no_jobs() {
        let reg = JobRegistry::new();
        assert_eq!(reg.stop_user(999).await, 0);
    }

    #[tokio::test]
    async fn stop_everyone_cancels_all_users() {
        let reg = JobRegistry::new();
        let (_, t1) = reg.try_register(1, "a@x.com").await.unwrap();
        let (_, t2) = reg.try_register(1, "b@x.com").await.unwrap();
        let (_, t3) = reg.try_register(2, "c@x.com").await.unwrap();
        assert_eq!(reg.user_count().await, 2);
        let n = reg.stop_everyone().await;
        assert_eq!(n, 3);
        assert!(t1.is_cancelled() && t2.is_cancelled() && t3.is_cancelled());
        assert_eq!(reg.user_count().await, 0);
        // Idempotent: lần 2 không còn job.
        assert_eq!(reg.stop_everyone().await, 0);
    }

    #[tokio::test]
    async fn vacuum_drops_cancelled_entries() {
        let reg = JobRegistry::new();
        let (_, token) = reg.try_register(42, "a@x.com").await.unwrap();
        token.cancel();
        // Entry vẫn còn vì chưa unregister
        assert_eq!(reg.user_count().await, 1);
        reg.vacuum().await;
        assert_eq!(reg.user_count().await, 0);
    }
}
