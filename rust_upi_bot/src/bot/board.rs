//! Live job board — snapshot trung tâm của mọi job đang trong hệ thống, phục
//! vụ bảng trạng thái realtime cho `/board`.
//!
//! Khác `JobRegistry` (chỉ giữ cancel token để `/stop`), board giữ thêm trạng
//! thái hiển thị: user, email (đã mask), state, **bước thân thiện** (StepKind
//! đã dịch từ log thô — KHÔNG lộ http/proxy/internal step code), mốc thời gian.
//!
//! Vòng đời 1 entry (key = `job_id` u64 từ `JobRegistry::register`):
//!   1. submit thành công  → `insert_queued`           (StepKind::Idle)
//!   2. worker pickup       → `mark_running`
//!   3. mỗi log line        → `set_step` (parse → StepKind, chỉ tiến không lùi)
//!   4. Done/Timeout/Cancel → `remove`
//!
//! Map sync + ngắn, không giữ guard qua `.await`. Render là pure function trên
//! snapshot đã clone.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Instant;

use tokio::sync::Mutex;

use crate::bot::i18n::Lang;

/// Trạng thái 1 job trong board.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JobState {
    Queued,
    Running,
}

impl JobState {
    /// Nhãn ngắn cho cột "Trạng thái" trong /board (icon + chữ).
    pub fn label(self, lang: Lang) -> &'static str {
        match (self, lang) {
            (JobState::Running, Lang::Vi) => "▶️ Chạy",
            (JobState::Running, Lang::En) => "▶️ Run",
            (JobState::Queued, Lang::Vi) => "⏳ Chờ",
            (JobState::Queued, Lang::En) => "⏳ Wait",
        }
    }
}

/// Bước hiện tại — DỊCH từ log thô của runner thành label thân thiện.
/// Tuyệt đối KHÔNG mang chi tiết kỹ thuật (http=200, [5b], proxy=...) lên
/// /board, tránh lộ logic nội bộ và overload user.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum StepKind {
    /// Vừa submit / chưa có log nào.
    #[default]
    Idle,
    Login,
    Prepare,
    Confirm,
    /// Vòng kích hoạt (`try N/M`). `max=0` khi mới vào phase chưa parse được counter.
    Activate { cur: u32, max: u32 },
    Qr,
}

impl StepKind {
    /// Rank để so sánh "tiến/lùi". Phase chỉ tiến lên — riêng Activate cùng
    /// rank với chính nó để cập nhật counter (cur/max) liên tục.
    fn rank(self) -> u8 {
        match self {
            StepKind::Idle => 0,
            StepKind::Login => 1,
            StepKind::Prepare => 2,
            StepKind::Confirm => 3,
            StepKind::Activate { .. } => 4,
            StepKind::Qr => 5,
        }
    }

    /// Label song ngữ — chỉ chữ thân thiện, không http/proxy/code.
    pub fn label(self, lang: Lang) -> String {
        let vi = matches!(lang, Lang::Vi);
        match self {
            StepKind::Idle => {
                if vi { "Đang chờ".into() } else { "Queued".into() }
            }
            StepKind::Login => {
                if vi { "Đang đăng nhập".into() } else { "Signing in".into() }
            }
            StepKind::Prepare => {
                if vi { "Đang chuẩn bị".into() } else { "Preparing".into() }
            }
            StepKind::Confirm => {
                if vi { "Đang xác nhận".into() } else { "Confirming".into() }
            }
            StepKind::Activate { cur, max } => {
                if max == 0 {
                    if vi { "Đang kích hoạt".into() } else { "Activating".into() }
                } else if vi {
                    format!("Đang thử {}/{}", cur, max)
                } else {
                    format!("Trying {}/{}", cur, max)
                }
            }
            StepKind::Qr => {
                if vi { "Đang tạo mã QR".into() } else { "Generating QR".into() }
            }
        }
    }
}

/// Map 1 dòng log thô → `StepKind`. Trả `None` khi log không thuộc nhóm bước.
/// Pattern khớp với `proc_view::detect_phase` để 2 view (card user và board)
/// đồng nhất bước, nhưng KHÔNG giữ chi tiết.
pub fn parse_step(line: &str) -> Option<StepKind> {
    // QR thành công (xuất hiện muộn nhất — ưu tiên trước).
    if (line.contains("[QR]") && line.contains("OK"))
        || line.contains("approve     OK")
        || line.contains("approved at")
    {
        return Some(StepKind::Qr);
    }
    // "try N/M ..." — vòng kích hoạt với counter.
    let trimmed = line.trim_start();
    if let Some(rest) = trimmed.strip_prefix("try ") {
        let mut sp = rest.split('/');
        let cur_part = sp.next()?;
        let max_part = sp.next()?;
        let cur: u32 = cur_part.trim().parse().ok()?;
        let max: u32 = max_part.split_whitespace().next()?.trim().parse().ok()?;
        return Some(StepKind::Activate { cur, max });
    }
    if line.contains("[6/6]") {
        return Some(StepKind::Activate { cur: 0, max: 0 });
    }
    if line.contains("[5b") || line.contains("[5c") {
        return Some(StepKind::Confirm);
    }
    if line.contains("[2/6")
        || line.contains("[3/6")
        || line.contains("[4/6")
        || line.contains("[5a]")
    {
        return Some(StepKind::Prepare);
    }
    if line.contains("[1/6] login")
        || line.contains("[login]")
        || line.starts_with("Account:")
    {
        return Some(StepKind::Login);
    }
    None
}

#[derive(Debug, Clone)]
pub struct JobStatus {
    pub user_id: i64,
    pub username: Option<String>,
    /// Email đã mask sẵn (không lưu plaintext).
    pub email_masked: String,
    pub state: JobState,
    /// Bước hiện tại (đã dịch thân thiện — không có log thô).
    pub step: StepKind,
    /// Mốc tính tuổi: thời điểm submit (Queued) hoặc thời điểm chạy (Running).
    pub since: Instant,
}

#[derive(Clone)]
pub struct JobBoard {
    inner: Arc<Mutex<HashMap<u64, JobStatus>>>,
}

impl JobBoard {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    /// Thêm job vừa submit vào board (state = Queued, step = Idle).
    pub async fn insert_queued(
        &self,
        job_id: u64,
        user_id: i64,
        username: Option<String>,
        email_masked: String,
    ) {
        let mut g = self.inner.lock().await;
        g.insert(
            job_id,
            JobStatus {
                user_id,
                username,
                email_masked,
                state: JobState::Queued,
                step: StepKind::Idle,
                since: Instant::now(),
            },
        );
    }

    /// Worker pickup → chuyển sang Running, reset mốc tuổi về thời điểm chạy.
    pub async fn mark_running(&self, job_id: u64) {
        let mut g = self.inner.lock().await;
        if let Some(s) = g.get_mut(&job_id) {
            s.state = JobState::Running;
            s.since = Instant::now();
        }
    }

    /// Cập nhật bước từ 1 dòng log thô. CHỈ tiến lên (rank cao hơn); riêng
    /// phase Activate được cập nhật counter `(cur, max)` liên tục cùng rank.
    pub async fn set_step(&self, job_id: u64, line: String) {
        let Some(parsed) = parse_step(&line) else { return };
        let mut g = self.inner.lock().await;
        let Some(s) = g.get_mut(&job_id) else { return };
        let same_activate = matches!(parsed, StepKind::Activate { .. })
            && matches!(s.step, StepKind::Activate { .. });
        if parsed.rank() > s.step.rank() || same_activate {
            s.step = parsed;
        }
    }

    /// Job kết thúc (Done/Timeout/Cancelled) → bỏ khỏi board.
    pub async fn remove(&self, job_id: u64) {
        self.inner.lock().await.remove(&job_id);
    }

    /// Xóa SẠCH board (admin `/flushall`). Trả số entry đã xóa.
    pub async fn clear_all(&self) -> usize {
        let mut g = self.inner.lock().await;
        let n = g.len();
        g.clear();
        n
    }

    /// Snapshot kèm `job_id`, sắp xếp ổn định: Running trước Queued, rồi theo
    /// tuổi giảm dần. Clone để render không giữ lock.
    pub async fn snapshot_entries(&self) -> Vec<(u64, JobStatus)> {
        let g = self.inner.lock().await;
        let mut out: Vec<(u64, JobStatus)> = g.iter().map(|(k, v)| (*k, v.clone())).collect();
        sort_entries(&mut out);
        out
    }

    /// Như `snapshot_entries` nhưng CHỈ job của `user_id` — dùng cho `/board`
    /// của user thường (bảo mật: không lộ tiến trình của người khác).
    pub async fn snapshot_entries_for_user(&self, user_id: i64) -> Vec<(u64, JobStatus)> {
        let g = self.inner.lock().await;
        let mut out: Vec<(u64, JobStatus)> = g
            .iter()
            .filter(|(_, v)| v.user_id == user_id)
            .map(|(k, v)| (*k, v.clone()))
            .collect();
        sort_entries(&mut out);
        out
    }

    /// Chủ sở hữu (user_id) của 1 job — dùng để verify quyền khi cần.
    #[allow(dead_code)]
    pub async fn owner_of(&self, job_id: u64) -> Option<i64> {
        self.inner.lock().await.get(&job_id).map(|s| s.user_id)
    }
}

/// Sắp xếp entries: Running trước Queued, rồi job lâu nhất lên đầu.
fn sort_entries(out: &mut [(u64, JobStatus)]) {
    out.sort_by(|a, b| match (a.1.state, b.1.state) {
        (JobState::Running, JobState::Queued) => std::cmp::Ordering::Less,
        (JobState::Queued, JobState::Running) => std::cmp::Ordering::Greater,
        _ => b.1.since.cmp(&a.1.since).reverse(),
    });
}

/// Escape ký tự đặc biệt HTML — Telegram parse rất chặt, mọi giá trị động
/// (email, username, age...) PHẢI qua hàm này trước khi nhúng template HTML.
pub fn html_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#39;"),
            _ => out.push(c),
        }
    }
    out
}

/// Cắt chuỗi theo số ký tự (char-safe), thêm '…' nếu bị cắt.
#[allow(dead_code)]
pub fn truncate_chars(s: &str, max: usize) -> String {
    let trimmed = s.trim();
    if trimmed.chars().count() <= max {
        return trimmed.to_string();
    }
    let mut out: String = trimmed.chars().take(max.saturating_sub(1)).collect();
    out.push('…');
    out
}

/// Định dạng tuổi job dạng mm:ss (hoặc h:mm:ss khi >= 1 giờ).
pub fn fmt_age(secs: u64) -> String {
    let h = secs / 3600;
    let m = (secs % 3600) / 60;
    let s = secs % 60;
    if h > 0 {
        format!("{}:{:02}:{:02}", h, m, s)
    } else {
        format!("{:02}:{:02}", m, s)
    }
}
