//! Global proxy status cache — kết quả probe sống được tái dùng trong TTL để
//! tránh probe lại mỗi job. Đồng thời single-flight: nhiều job cùng cần 1
//! proxy line chỉ tốn ĐÚNG 1 lần probe thật (các job khác chờ cùng kết quả).
//!
//! Key cache = raw proxy line (KHÔNG materialize). Proxy có `{SID}` xoay IP mỗi
//! lần materialize nhưng credential/host giống nhau → probe theo raw line phản
//! ánh đúng "line còn sống/auth ok hay không".
//!
//! Singleton process-wide qua [`PROXY_STATUS`] — cache là state toàn cục thuần
//! (không phụ thuộc config), không cần plumbing qua mọi handler.

use crate::bot::proxy_probe::{probe_proxy_line, ProbeResult};
use once_cell::sync::Lazy;
use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::Mutex;

/// TTL cache trạng thái proxy (5 phút — chốt theo yêu cầu).
pub const STATUS_TTL: Duration = Duration::from_secs(300);

/// Singleton cache toàn process.
pub static PROXY_STATUS: Lazy<ProxyStatusCache> = Lazy::new(ProxyStatusCache::new);

struct CacheEntry {
    result: Arc<ProbeResult>,
    at: Instant,
}

pub struct ProxyStatusCache {
    /// raw line → kết quả probe gần nhất + thời điểm.
    cache: Mutex<HashMap<String, CacheEntry>>,
    /// raw line → lock single-flight (chỉ 1 probe thật/line tại 1 thời điểm).
    locks: Mutex<HashMap<String, Arc<Mutex<()>>>>,
}

impl ProxyStatusCache {
    fn new() -> Self {
        Self {
            cache: Mutex::new(HashMap::new()),
            locks: Mutex::new(HashMap::new()),
        }
    }

    /// Đọc cache nếu còn tươi.
    async fn fresh(&self, raw: &str) -> Option<Arc<ProbeResult>> {
        let g = self.cache.lock().await;
        g.get(raw).and_then(|e| {
            if e.at.elapsed() < STATUS_TTL {
                Some(e.result.clone())
            } else {
                None
            }
        })
    }

    async fn store(&self, raw: &str, result: Arc<ProbeResult>) {
        let mut g = self.cache.lock().await;
        g.insert(
            raw.to_string(),
            CacheEntry {
                result,
                at: Instant::now(),
            },
        );
    }

    /// Lấy trạng thái proxy: dùng cache nếu còn tươi, ngược lại probe thật
    /// (single-flight). Dùng cho pre-flight job.
    pub async fn get_or_probe(&self, raw: &str) -> Arc<ProbeResult> {
        if let Some(r) = self.fresh(raw).await {
            return r;
        }
        // Single-flight: acquire per-key lock.
        let key_lock = {
            let mut locks = self.locks.lock().await;
            locks
                .entry(raw.to_string())
                .or_insert_with(|| Arc::new(Mutex::new(())))
                .clone()
        };
        let _guard = key_lock.lock().await;
        // Re-check: task khác có thể đã probe xong trong lúc ta chờ lock.
        if let Some(r) = self.fresh(raw).await {
            return r;
        }
        let result = Arc::new(probe_proxy_line(raw).await);
        self.store(raw, result.clone()).await;
        result
    }

    /// Probe tươi, BỎ QUA cache, ghi đè kết quả. Dùng cho nút check thủ công /
    /// `/proxy_login_set` để admin sửa proxy xong verify lại ngay.
    pub async fn refresh(&self, raw: &str) -> Arc<ProbeResult> {
        let result = Arc::new(probe_proxy_line(raw).await);
        self.store(raw, result.clone()).await;
        result
    }
}
