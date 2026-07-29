//! SQLite Settings Store — đồng bộ project rule (single source of truth).
//!
//! Ngoài bảng `settings` (key/value dot-namespace, schema giống Python
//! `db/repositories.py::SettingsRepository`), store còn giữ:
//!   - `bot_users`  — mọi user đã kết nối bot (nguồn cho broadcast `/notify`).
//!   - `bot_bans`   — danh sách ban theo `user_id` (định danh bền vững, user
//!                    đổi username vẫn dính ban).
//!
//! `Connection` của rusqlite là `Send` nhưng không `Sync`; bọc trong
//! `Mutex<Connection>` để `Arc<Settings>` chia sẻ được giữa các tokio task.
//! Mọi method DB sync + ngắn, không giữ guard qua `.await` → an toàn trong async.

use anyhow::Result;
use rusqlite::{params, Connection, OptionalExtension};
use std::path::Path;
use std::sync::Mutex;

/// Settings key cho login proxy global (admin-only). Dot-namespace theo rule.
const LOGIN_PROXY_KEY: &str = "proxy.login";

/// Settings key cho kênh nhận thông báo "QR success" do admin cấu hình. Khác
/// `--admin-chat-id` (DM của admin): notify target có thể là supergroup/topic
/// để cả team theo dõi success real-time.
const NOTIFY_CHAT_ID_KEY: &str = "notify.chat_id";
/// Optional `message_thread_id` (topic ID trong forum-supergroup). 0/missing =
/// gửi vào root chat. Telegram chỉ chấp nhận thread_id > 0.
const NOTIFY_THREAD_ID_KEY: &str = "notify.thread_id";

/// 1 dòng trong bảng `bot_bans`, dùng để render `/banlist`.
#[derive(Debug, Clone)]
pub struct BanRecord {
    pub user_id: i64,
    pub username: Option<String>,
    pub reason: Option<String>,
    #[allow(dead_code)]
    pub banned_at: i64,
}

pub struct Settings {
    conn: Mutex<Connection>,
}

impl Settings {
    pub fn open(path: &Path) -> Result<Self> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let conn = Connection::open(path)?;
        conn.execute_batch(
            r#"
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
            );

            CREATE TABLE IF NOT EXISTS bot_users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                first_name TEXT,
                chat_id    INTEGER NOT NULL,
                first_seen INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                last_seen  INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
            );
            CREATE INDEX IF NOT EXISTS idx_bot_users_username
                ON bot_users(username COLLATE NOCASE);

            CREATE TABLE IF NOT EXISTS bot_bans (
                user_id   INTEGER PRIMARY KEY,
                username  TEXT,
                reason    TEXT,
                banned_by INTEGER,
                banned_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
            );

            -- Per-user proxy override. user_id PK → 1 user = 1 proxy line. Lưu
            -- raw line/template (host:port[:user[:pass]] hoặc scheme URL) — mọi
            -- consumer phải materialize trước khi feed reqwest. Proxy của user
            -- KHÔNG share sang user khác (private per-user store).
            CREATE TABLE IF NOT EXISTS user_proxies (
                user_id INTEGER PRIMARY KEY,
                raw     TEXT NOT NULL,
                set_at  INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
            );

            -- Ngôn ngữ giao diện theo user (vi/en). User phải chọn lần đầu.
            CREATE TABLE IF NOT EXISTS user_lang (
                user_id INTEGER PRIMARY KEY,
                lang    TEXT NOT NULL,
                set_at  INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
            );

            -- Per-user override cho `max_per_user` (số tiến trình đồng thời).
            -- Mặc định global đặt qua key `limits.max_per_user_default` trong
            -- bảng `settings`. User có row ở đây thì giá trị override thắng.
            -- Range hợp lệ: 1..=10 (caller validate trước khi insert).
            CREATE TABLE IF NOT EXISTS user_limits (
                user_id      INTEGER PRIMARY KEY,
                max_per_user INTEGER NOT NULL,
                set_at       INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
            );
            "#,
        )?;
        Ok(Self {
            conn: Mutex::new(conn),
        })
    }

    /// Lock helper. Với `panic = "abort"` (release profile) poisoning không thể
    /// xảy ra; `expect` giữ đúng tinh thần fail-fast nếu gặp ở debug.
    fn lock(&self) -> std::sync::MutexGuard<'_, Connection> {
        self.conn.lock().expect("settings mutex poisoned")
    }

    // ── settings key/value ────────────────────────────────────────────────
    pub fn get(&self, key: &str) -> Option<String> {
        self.lock()
            .query_row(
                "SELECT value FROM settings WHERE key = ?1",
                params![key],
                |r| r.get::<_, Option<String>>(0),
            )
            .ok()
            .flatten()
    }

    pub fn set(&self, key: &str, value: &str) -> Result<()> {
        self.lock().execute(
            "INSERT INTO settings(key, value) VALUES(?1, ?2)
             ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                            updated_at=CAST(strftime('%s','now') AS INTEGER)",
            params![key, value],
        )?;
        Ok(())
    }

    #[allow(dead_code)]
    pub fn get_u32(&self, key: &str) -> Option<u32> {
        self.get(key).and_then(|s| s.parse().ok())
    }

    // ── login proxy (admin-only, global) ──────────────────────────────────
    /// Số dòng login proxy tối đa admin được set. Tương tự `USER_PROXY_MAX_LINES`
    /// nhưng cho login segment (step < proxy_from_step). 1 admin = 1 pool nhỏ,
    /// mỗi job pick random 1 line.
    pub const LOGIN_PROXY_MAX_LINES: usize = 10;

    /// Đọc danh sách login proxy raw (newline-separated, key `proxy.login`).
    /// Trim + dedupe + cap `LOGIN_PROXY_MAX_LINES`. Empty = chưa set.
    /// Mọi consumer PHẢI materialize trước khi feed client.
    pub fn get_login_proxies(&self) -> Vec<String> {
        let raw = match self.get(LOGIN_PROXY_KEY) {
            Some(s) => s,
            None => return Vec::new(),
        };
        let mut out: Vec<String> = Vec::new();
        let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
        for line in raw.lines() {
            let t = line.trim();
            if t.is_empty() {
                continue;
            }
            if seen.insert(t.to_string()) {
                out.push(t.to_string());
                if out.len() >= Self::LOGIN_PROXY_MAX_LINES {
                    break;
                }
            }
        }
        out
    }

    /// Set danh sách login proxy (tối đa `LOGIN_PROXY_MAX_LINES`). Caller PHẢI
    /// validate từng line bằng `proxy_format::validate_and_mask` trước. Lưu
    /// dạng newline-separated trong cột `value`. `lines` rỗng → xóa entry
    /// (caller nên gọi `remove_login_proxy` cho rõ ràng).
    pub fn set_login_proxies(&self, lines: &[String]) -> Result<()> {
        let cleaned: Vec<&str> = lines
            .iter()
            .map(|s| s.trim())
            .filter(|s| !s.is_empty())
            .take(Self::LOGIN_PROXY_MAX_LINES)
            .collect();
        if cleaned.is_empty() {
            self.remove_login_proxy()?;
            return Ok(());
        }
        let joined = cleaned.join("\n");
        self.set(LOGIN_PROXY_KEY, &joined)
    }

    /// Xóa toàn bộ login proxy. Trả `true` nếu có row bị xóa.
    pub fn remove_login_proxy(&self) -> Result<bool> {
        let n = self.lock().execute(
            "DELETE FROM settings WHERE key = ?1",
            params![LOGIN_PROXY_KEY],
        )?;
        Ok(n > 0)
    }

    // ── notify target (admin-only, global) ────────────────────────────────
    /// (chat_id, thread_id) — thread_id `Some(n)` chỉ khi > 0. None = chưa cấu
    /// hình. Caller fallback về `--admin-chat-id` nếu None.
    pub fn get_notify_target(&self) -> Option<(i64, Option<i64>)> {
        let chat_id: i64 = self.get(NOTIFY_CHAT_ID_KEY)?.parse().ok()?;
        if chat_id == 0 {
            return None;
        }
        let thread_id: Option<i64> = self
            .get(NOTIFY_THREAD_ID_KEY)
            .and_then(|s| s.parse::<i64>().ok())
            .filter(|t| *t > 0);
        Some((chat_id, thread_id))
    }

    pub fn set_notify_target(&self, chat_id: i64, thread_id: Option<i64>) -> Result<()> {
        self.set(NOTIFY_CHAT_ID_KEY, &chat_id.to_string())?;
        match thread_id {
            Some(t) if t > 0 => self.set(NOTIFY_THREAD_ID_KEY, &t.to_string())?,
            _ => {
                self.lock().execute(
                    "DELETE FROM settings WHERE key = ?1",
                    params![NOTIFY_THREAD_ID_KEY],
                )?;
            }
        }
        Ok(())
    }

    /// Xóa notify target. Trả `true` nếu có row bị xóa.
    pub fn remove_notify_target(&self) -> Result<bool> {
        let n1 = self.lock().execute(
            "DELETE FROM settings WHERE key = ?1",
            params![NOTIFY_CHAT_ID_KEY],
        )?;
        let n2 = self.lock().execute(
            "DELETE FROM settings WHERE key = ?1",
            params![NOTIFY_THREAD_ID_KEY],
        )?;
        Ok(n1 + n2 > 0)
    }

    // ── bot_users ───────────────────────────────────────────────────────
    /// Upsert user mỗi lần nhận message. `username`/`first_name` cập nhật theo
    /// lần gần nhất; `user_id` là khóa bền vững. `first_seen` giữ nguyên.
    pub fn record_user(
        &self,
        user_id: i64,
        username: Option<&str>,
        first_name: Option<&str>,
        chat_id: i64,
    ) -> Result<()> {
        self.lock().execute(
            "INSERT INTO bot_users(user_id, username, first_name, chat_id)
             VALUES(?1, ?2, ?3, ?4)
             ON CONFLICT(user_id) DO UPDATE SET
                 username   = excluded.username,
                 first_name = excluded.first_name,
                 chat_id    = excluded.chat_id,
                 last_seen  = CAST(strftime('%s','now') AS INTEGER)",
            params![user_id, username, first_name, chat_id],
        )?;
        Ok(())
    }

    /// Resolve username (có/không có '@', case-insensitive) → user_id từ
    /// `bot_users`. None nếu user chưa từng kết nối bot.
    pub fn resolve_username(&self, username: &str) -> Result<Option<i64>> {
        let u = username.trim_start_matches('@');
        let id = self
            .lock()
            .query_row(
                "SELECT user_id FROM bot_users WHERE username = ?1 COLLATE NOCASE",
                params![u],
                |r| r.get::<_, i64>(0),
            )
            .optional()?;
        Ok(id)
    }

    /// Username gần nhất biết được của 1 user_id (cho hiển thị).
    pub fn known_username(&self, user_id: i64) -> Result<Option<String>> {
        let name = self
            .lock()
            .query_row(
                "SELECT username FROM bot_users WHERE user_id = ?1",
                params![user_id],
                |r| r.get::<_, Option<String>>(0),
            )
            .optional()?
            .flatten();
        Ok(name)
    }

    /// chat_id đã lưu của user (cho DM trực tiếp). None nếu user chưa từng
    /// kết nối bot. Với private chat, chat_id thường == user_id nhưng vẫn đọc
    /// từ store để chính xác (group/forwarded edge case).
    pub fn chat_id_for_user(&self, user_id: i64) -> Result<Option<i64>> {
        let cid = self
            .lock()
            .query_row(
                "SELECT chat_id FROM bot_users WHERE user_id = ?1",
                params![user_id],
                |r| r.get::<_, i64>(0),
            )
            .optional()?;
        Ok(cid)
    }

    /// (user_id, chat_id) của mọi user KHÔNG bị ban — danh sách đích broadcast.
    pub fn broadcast_targets(&self) -> Result<Vec<(i64, i64)>> {
        let conn = self.lock();
        let mut stmt = conn.prepare(
            "SELECT user_id, chat_id FROM bot_users
             WHERE user_id NOT IN (SELECT user_id FROM bot_bans)
             ORDER BY user_id",
        )?;
        let rows = stmt.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?)))?;
        let mut out = Vec::new();
        for r in rows {
            out.push(r?);
        }
        Ok(out)
    }

    /// Gỡ user khỏi `bot_users` — gọi khi broadcast phát hiện user đã block bot
    /// (Telegram trả "bot was blocked" / "user is deactivated").
    pub fn remove_user(&self, user_id: i64) -> Result<()> {
        self.lock()
            .execute("DELETE FROM bot_users WHERE user_id = ?1", params![user_id])?;
        Ok(())
    }

    // ── bot_bans ──────────────────────────────────────────────────────────
    pub fn is_banned(&self, user_id: i64) -> Result<bool> {
        let n: i64 = self.lock().query_row(
            "SELECT COUNT(*) FROM bot_bans WHERE user_id = ?1",
            params![user_id],
            |r| r.get(0),
        )?;
        Ok(n > 0)
    }

    /// Ban theo user_id (idempotent upsert). `username` chỉ lưu để hiển thị —
    /// định danh ban là user_id nên đổi username không thoát ban.
    pub fn ban(
        &self,
        user_id: i64,
        username: Option<&str>,
        reason: Option<&str>,
        banned_by: i64,
    ) -> Result<()> {
        self.lock().execute(
            "INSERT INTO bot_bans(user_id, username, reason, banned_by)
             VALUES(?1, ?2, ?3, ?4)
             ON CONFLICT(user_id) DO UPDATE SET
                 username  = excluded.username,
                 reason    = excluded.reason,
                 banned_by = excluded.banned_by,
                 banned_at = CAST(strftime('%s','now') AS INTEGER)",
            params![user_id, username, reason, banned_by],
        )?;
        Ok(())
    }

    /// Gỡ ban theo user_id. Trả true nếu có row bị xóa.
    pub fn unban(&self, user_id: i64) -> Result<bool> {
        let n = self
            .lock()
            .execute("DELETE FROM bot_bans WHERE user_id = ?1", params![user_id])?;
        Ok(n > 0)
    }

    /// Resolve username → user_id từ chính bảng ban (fallback cho `/unban` khi
    /// user đã rời `bot_users` hoặc đổi username sau khi bị ban).
    pub fn banned_user_id_by_username(&self, username: &str) -> Result<Option<i64>> {
        let u = username.trim_start_matches('@');
        let id = self
            .lock()
            .query_row(
                "SELECT user_id FROM bot_bans WHERE username = ?1 COLLATE NOCASE",
                params![u],
                |r| r.get::<_, i64>(0),
            )
            .optional()?;
        Ok(id)
    }

    pub fn list_bans(&self) -> Result<Vec<BanRecord>> {
        let conn = self.lock();
        let mut stmt = conn.prepare(
            "SELECT user_id, username, reason, banned_at
             FROM bot_bans ORDER BY banned_at DESC",
        )?;
        let rows = stmt.query_map([], |r| {
            Ok(BanRecord {
                user_id: r.get(0)?,
                username: r.get(1)?,
                reason: r.get(2)?,
                banned_at: r.get(3)?,
            })
        })?;
        let mut out = Vec::new();
        for r in rows {
            out.push(r?);
        }
        Ok(out)
    }

    // ── user_proxies ──────────────────────────────────────────────────────
    /// Số dòng proxy tối đa user được set (yêu cầu sản phẩm). 1 user = 1
    /// pool nhỏ, mỗi dòng = 1 proxy line/template.
    pub const USER_PROXY_MAX_LINES: usize = 10;

    /// Set danh sách proxy cho user (tối đa `USER_PROXY_MAX_LINES`). Caller
    /// PHẢI validate từng line bằng `proxy_format::validate_and_mask` trước.
    /// Lưu dạng newline-separated trong cột `raw` để giữ schema hiện tại.
    /// `lines` rỗng → xóa entry (gọi `remove_user_proxy` cho rõ ràng hơn).
    pub fn set_user_proxies(&self, user_id: i64, lines: &[String]) -> Result<()> {
        let cleaned: Vec<&str> = lines
            .iter()
            .map(|s| s.trim())
            .filter(|s| !s.is_empty())
            .take(Self::USER_PROXY_MAX_LINES)
            .collect();
        if cleaned.is_empty() {
            self.remove_user_proxy(user_id)?;
            return Ok(());
        }
        let joined = cleaned.join("\n");
        self.set_user_proxy(user_id, &joined)
    }

    /// Đọc danh sách proxy của user (newline-separated). Trim + dedupe + cap
    /// `USER_PROXY_MAX_LINES`. Empty list nếu user chưa set.
    pub fn get_user_proxies(&self, user_id: i64) -> Result<Vec<String>> {
        let raw = match self.get_user_proxy(user_id)? {
            Some(s) => s,
            None => return Ok(Vec::new()),
        };
        let mut out: Vec<String> = Vec::new();
        let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
        for line in raw.lines() {
            let t = line.trim();
            if t.is_empty() {
                continue;
            }
            if seen.insert(t.to_string()) {
                out.push(t.to_string());
                if out.len() >= Self::USER_PROXY_MAX_LINES {
                    break;
                }
            }
        }
        Ok(out)
    }

    /// Set proxy raw line cho user (upsert). Caller PHẢI validate qua
    /// `proxy_format::validate_and_mask` trước. `raw` lưu nguyên (template/SID
    /// cho rotate sticky session ở consumer). Cho multi-line: dùng
    /// `set_user_proxies`.
    pub fn set_user_proxy(&self, user_id: i64, raw: &str) -> Result<()> {
        self.lock().execute(
            "INSERT INTO user_proxies(user_id, raw) VALUES(?1, ?2)
             ON CONFLICT(user_id) DO UPDATE SET
                 raw    = excluded.raw,
                 set_at = CAST(strftime('%s','now') AS INTEGER)",
            params![user_id, raw],
        )?;
        Ok(())
    }

    /// Lấy raw proxy line của user (None nếu chưa set).
    pub fn get_user_proxy(&self, user_id: i64) -> Result<Option<String>> {
        let raw = self
            .lock()
            .query_row(
                "SELECT raw FROM user_proxies WHERE user_id = ?1",
                params![user_id],
                |r| r.get::<_, String>(0),
            )
            .optional()?;
        Ok(raw)
    }

    /// Xóa proxy của user. Trả `true` nếu có row bị xóa.
    pub fn remove_user_proxy(&self, user_id: i64) -> Result<bool> {
        let n = self
            .lock()
            .execute("DELETE FROM user_proxies WHERE user_id = ?1", params![user_id])?;
        Ok(n > 0)
    }

    // ── user_lang ─────────────────────────────────────────────────────────
    /// Ngôn ngữ đã chọn của user ("vi"/"en"). None nếu chưa chọn lần nào.
    pub fn get_user_lang(&self, user_id: i64) -> Option<String> {
        self.lock()
            .query_row(
                "SELECT lang FROM user_lang WHERE user_id = ?1",
                params![user_id],
                |r| r.get::<_, String>(0),
            )
            .optional()
            .ok()
            .flatten()
    }

    /// Đặt ngôn ngữ cho user (upsert).
    pub fn set_user_lang(&self, user_id: i64, lang: &str) -> Result<()> {
        self.lock().execute(
            "INSERT INTO user_lang(user_id, lang) VALUES(?1, ?2)
             ON CONFLICT(user_id) DO UPDATE SET
                 lang   = excluded.lang,
                 set_at = CAST(strftime('%s','now') AS INTEGER)",
            params![user_id, lang],
        )?;
        Ok(())
    }

    // ── max_per_user (global default + per-user override) ─────────────────
    /// Min/max cho `max_per_user` cả global lẫn per-user. Caller PHẢI clamp
    /// trước khi gọi setter — setter sẽ KHÔNG tự clamp (fail-fast nếu vượt).
    pub const MAX_PER_USER_MIN: u32 = 1;
    pub const MAX_PER_USER_MAX: u32 = 10;
    /// Giá trị mặc định ban đầu khi key chưa được set lần nào (boot lần đầu).
    /// Sẵn để main caller dùng nếu CLI flag không xài. Hiện tại main đang
    /// dùng `cli.max_per_user` (đã default 2 ở clap) → const này giữ làm
    /// reference cho tài liệu/test.
    #[allow(dead_code)]
    pub const MAX_PER_USER_DEFAULT_FALLBACK: u32 = 2;
    /// Settings key cho global default. Mọi user không có override đọc giá trị này.
    pub const KEY_MAX_PER_USER_DEFAULT: &'static str = "limits.max_per_user_default";

    /// Đọc global default từ settings store. Trả `None` nếu chưa set lần nào
    /// → caller chịu trách nhiệm seed `MAX_PER_USER_DEFAULT_FALLBACK`.
    pub fn get_max_per_user_default(&self) -> Option<u32> {
        self.get_u32(Self::KEY_MAX_PER_USER_DEFAULT)
            .map(|n| n.clamp(Self::MAX_PER_USER_MIN, Self::MAX_PER_USER_MAX))
    }

    /// Set global default. Caller PHẢI clamp 1..=10 trước. Trả `Err` nếu DB
    /// fail. Effect: chỉ persist — caller phải gọi `UserLimiter::set_default`
    /// để áp ngay (không restart).
    pub fn set_max_per_user_default(&self, n: u32) -> Result<()> {
        debug_assert!(
            (Self::MAX_PER_USER_MIN..=Self::MAX_PER_USER_MAX).contains(&n),
            "set_max_per_user_default: out of range {}",
            n
        );
        self.set(Self::KEY_MAX_PER_USER_DEFAULT, &n.to_string())
    }

    /// Lấy override cho 1 user — `None` nếu user chưa có override.
    /// Hiện tại main pre-load qua `list_user_limits` rồi giữ trong cache
    /// `UserLimiter::user_overrides`; method này dành cho tool/test trực tiếp.
    #[allow(dead_code)]
    pub fn get_user_limit(&self, user_id: i64) -> Result<Option<u32>> {
        let n = self
            .lock()
            .query_row(
                "SELECT max_per_user FROM user_limits WHERE user_id = ?1",
                params![user_id],
                |r| r.get::<_, u32>(0),
            )
            .optional()?
            .map(|n| n.clamp(Self::MAX_PER_USER_MIN, Self::MAX_PER_USER_MAX));
        Ok(n)
    }

    /// Set override cho 1 user (upsert). Caller PHẢI clamp 1..=10 trước.
    pub fn set_user_limit(&self, user_id: i64, max_per_user: u32) -> Result<()> {
        debug_assert!(
            (Self::MAX_PER_USER_MIN..=Self::MAX_PER_USER_MAX).contains(&max_per_user),
            "set_user_limit: out of range {}",
            max_per_user
        );
        self.lock().execute(
            "INSERT INTO user_limits(user_id, max_per_user) VALUES(?1, ?2)
             ON CONFLICT(user_id) DO UPDATE SET
                 max_per_user = excluded.max_per_user,
                 set_at       = CAST(strftime('%s','now') AS INTEGER)",
            params![user_id, max_per_user],
        )?;
        Ok(())
    }

    /// Xóa override của 1 user (sau đó user dùng global default). Trả `true`
    /// nếu có row bị xóa.
    pub fn remove_user_limit(&self, user_id: i64) -> Result<bool> {
        let n = self
            .lock()
            .execute("DELETE FROM user_limits WHERE user_id = ?1", params![user_id])?;
        Ok(n > 0)
    }

    /// Liệt kê tất cả override per-user — `(user_id, max_per_user)` sort theo
    /// user_id. Dùng để boot pre-load cache trong `UserLimiter` + lệnh admin
    /// xem danh sách user có quota cao hơn default.
    pub fn list_user_limits(&self) -> Result<Vec<(i64, u32)>> {
        let conn = self.lock();
        let mut stmt = conn.prepare(
            "SELECT user_id, max_per_user FROM user_limits ORDER BY user_id",
        )?;
        let rows = stmt
            .query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, u32>(1)?)))?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        Ok(rows
            .into_iter()
            .map(|(uid, n)| (uid, n.clamp(Self::MAX_PER_USER_MIN, Self::MAX_PER_USER_MAX)))
            .collect())
    }
}
