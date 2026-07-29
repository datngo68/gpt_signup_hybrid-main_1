//! UPI QR Bot — Rust binary cho OpenWrt aarch64.
//!
//! Pipeline:
//!   1. User /start → bot greeting.
//!   2. User upload session.json (Telegram document) → parse access_token + cookies.
//!   3. Job vào FIFO queue. Worker pool (max-concurrent) pickup khi có slot.
//!   4. Worker chạy UPI flow (steps 2-6) → render QR PNG → gửi cho user.
//!   5. Realtime log progress qua editMessageText (rate-limited).

mod bot;
mod http;
mod proxy_format;
mod random_profile;
mod settings;
mod stripe;
mod stripe_token;
mod upi;
mod user_agent;
mod auth;

use anyhow::{anyhow, Context, Result};
use clap::Parser;
use serde_json::Value;
use std::collections::HashSet;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::mpsc;
use tracing::{error, info, warn};

use crate::bot::board::{JobBoard, JobStatus};
use crate::bot::dashboard::DashboardManager;
use crate::bot::i18n::{self, Lang};
use crate::bot::limiter::{AdmitDecision, MessageDecision, UserLimiter};
use crate::bot::queue::{spawn_workers, Job, JobEvent, JobQueue, SubmitError, WorkerConfig};
use crate::bot::registry::JobRegistry;
use crate::bot::telegram::{CallbackQuery, Message, TelegramClient};
use crate::http::HttpClient;
use crate::upi::runner::{AuthSource, UpiJobConfig};

#[derive(Parser, Debug, Clone)]
#[command(name = "upi-qr-bot", version)]
struct Cli {
    /// Sub-command. Empty = chạy bot (default).
    #[command(subcommand)]
    cmd: Option<SubCmd>,

    /// Telegram bot token (from @BotFather)
    #[arg(long, env = "TELEGRAM_TOKEN", default_value = "", global = true)]
    telegram_token: String,

    /// Comma-separated whitelist của user_id Telegram được phép. Empty = ai cũng dùng.
    #[arg(long, env = "ALLOWED_USERS", default_value = "", global = true)]
    allowed_users: String,

    /// Số worker chạy đồng thời. Job mới sẽ vào queue khi đầy.
    #[arg(long, env = "MAX_CONCURRENT", default_value = "100", global = true)]
    max_concurrent: usize,

    /// Số tiến trình tối đa 1 user chạy đồng thời. Đây là giá trị **seed** ban
    /// đầu cho DB key `limits.max_per_user_default` (chỉ dùng nếu DB chưa có
    /// row). Sau đó admin đổi qua `/set_max_per_user <n>` — thay đổi persist
    /// vào DB và áp ngay (không restart). Mỗi user có thể được override
    /// riêng qua `/set_user_limit @user <n>`. Range hợp lệ 1..=10.
    #[arg(long, env = "MAX_PER_USER", default_value = "2", global = true)]
    max_per_user: u32,

    /// Hard cap số job pending trong queue. Khi đầy, job mới bị reject. Bảo vệ RAM.
    #[arg(long, env = "QUEUE_CAPACITY", default_value = "50", global = true)]
    queue_capacity: usize,

    /// Số lần retry approve (per job).
    #[arg(long, env = "APPROVE_RETRIES", default_value = "200", global = true)]
    approve_retries: u32,

    /// Delay giữa các approve attempt (giây). Range hợp lệ [2, 60]. Dưới 2s sẽ
    /// bị Stripe rate-limit → result=blocked spam. Mặc định 3s đồng bộ Python.
    #[arg(long, env = "APPROVE_DELAY_SECS", default_value = "3", global = true)]
    approve_delay_secs: u64,

    /// Restart threshold — số `result=exception` LIÊN TIẾP trước khi restart checkout. 0 = disabled.
    #[arg(long, env = "RESTART_THRESHOLD", default_value = "20", global = true)]
    restart_threshold: u32,

    /// Số lần restart tối đa trong 1 job. 0 = disabled.
    #[arg(long, env = "MAX_RESTARTS", default_value = "3", global = true)]
    max_restarts: u32,

    /// Comma-separated proxy URLs. VD: "http://u:p@h1:8080,http://u:p@h2:8080".
    #[arg(long, env = "PROXY_POOL", default_value = "", global = true)]
    proxy_pool: String,

    /// 1-6, step bắt đầu áp proxy. Default 3 (login + checkout DIRECT).
    #[arg(long, env = "PROXY_FROM_STEP", default_value = "3", global = true)]
    proxy_from_step: u32,

    /// Cooldown (seconds) giữa 2 job cùng 1 user. Chống spam.
    #[arg(long, env = "USER_COOLDOWN_SECONDS", default_value = "10", global = true)]
    user_cooldown_seconds: u64,

    /// Max message tiếp nhận từ 1 user trong cửa sổ 60s. Vượt → tạm bỏ qua.
    /// Default 60 để cho paste JSON dài bị Telegram split thành nhiều chunks
    /// vẫn không bị drop.
    #[arg(long, env = "USER_MSG_RATE_PER_MIN", default_value = "60", global = true)]
    user_msg_rate_per_min: u32,

    /// Job hard timeout (seconds). Quá → kill job, free worker slot.
    #[arg(long, env = "JOB_TIMEOUT_SECONDS", default_value = "1800", global = true)]
    job_timeout_seconds: u64,

    /// Watermark text vẽ phía dưới QR PNG (không đè lên QR). Empty = không vẽ.
    #[arg(long, env = "QR_WATERMARK", default_value = "@prr9293", global = true)]
    qr_watermark: String,

    /// Telegram user ID để nhận thông báo khi user KHÁC tạo QR thành công.
    /// 0 = disabled. Job của chính admin (cùng id) sẽ KHÔNG gửi notification.
    #[arg(long, env = "ADMIN_CHAT_ID", default_value = "0", global = true)]
    admin_chat_id: i64,

    /// SQLite Settings Store path.
    #[arg(long, env = "DB_PATH", default_value = "/overlay/upi-bot/state.db", global = true)]
    db_path: PathBuf,

    /// Output directory cho QR PNG.
    #[arg(long, env = "QR_OUT_DIR", default_value = "/tmp/upi-qr", global = true)]
    qr_out_dir: PathBuf,

    /// Cache directory cho Stripe bundles.
    #[arg(long, env = "BUNDLES_CACHE_DIR", default_value = "/tmp/upi-bot-bundles", global = true)]
    bundles_cache_dir: PathBuf,

    /// HTTP timeout (seconds).
    #[arg(long, env = "HTTP_TIMEOUT", default_value = "60", global = true)]
    http_timeout: u64,

    /// Ngưỡng latency probe proxy (ms). Proxy của user/login probe ra latency
    /// LỚN HƠN ngưỡng này → bot ƯU TIÊN xoay sang proxy khác trong pool.
    /// NHƯNG: nếu sau khi lọc KHÔNG còn proxy nhanh nào → vẫn dùng proxy slow
    /// (đỡ hơn block job hoàn toàn). Probe lỗi/proxy chết VẪN block.
    /// Per-process: mỗi job đọc cache hoặc probe lại — không ảnh hưởng job khác.
    #[arg(long, env = "PROXY_LATENCY_LIMIT_MS", default_value = "3000", global = true)]
    proxy_latency_limit_ms: u64,
}

#[derive(clap::Subcommand, Debug, Clone)]
enum SubCmd {
    /// Live probe: fetch Stripe bundles + extract token config. Verify
    /// TLS + token extraction trước khi run bot.
    StripeProbe,
    /// Run UPI flow 1 lần với session.json file local — không cần Telegram.
    /// Output JSON kết quả + path tới QR PNG.
    /// Run UPI flow 1 lần — nhận session.json HOẶC combo email|pass|2fa.
    /// Không cần Telegram. Output JSON kết quả + path tới QR PNG.
    RunOnce {
        /// Path tới session.json (dùng session có sẵn).
        #[arg(long)]
        session_json: Option<PathBuf>,
        /// Combo "email|password|totp_secret" (login HTTP lấy session).
        #[arg(long, default_value = "")]
        combo: String,
        /// Path xuất QR PNG (nếu có).
        #[arg(long, default_value = "/tmp/upi-runonce.png")]
        qr_out: PathBuf,
    },
    /// Test login HTTP từ combo `email|password|2fa` → in kết quả session.
    /// Không cần Telegram. Dùng để verify flow login JA3 (wreq/BoringSSL).
    LoginOnce {
        /// Combo "email|password|totp_secret"
        #[arg(long)]
        combo: String,
        /// Proxy URL cho login (optional). Empty = direct.
        #[arg(long, default_value = "")]
        proxy: String,
    },
}

fn parse_allowed_users(s: &str) -> HashSet<i64> {
    s.split(',')
        .filter_map(|t| t.trim().parse::<i64>().ok())
        .collect()
}

fn parse_proxy_pool(s: &str) -> Vec<String> {
    s.split(',')
        .map(|t| t.trim().to_string())
        .filter(|t| !t.is_empty())
        .collect()
}

/// Ngôn ngữ user (None nếu chưa chọn lần nào).
fn user_lang(store: &settings::Settings, user_id: i64) -> Option<Lang> {
    store.get_user_lang(user_id).and_then(|s| Lang::from_code(&s))
}

/// Fallback khi chưa rõ (chỉ dùng ở chỗ hiếm); mặc định tiếng Việt.
fn lang_or_default(store: &settings::Settings, user_id: i64) -> Lang {
    user_lang(store, user_id).unwrap_or(Lang::Vi)
}

/// Ngôn ngữ admin — đọc từ store theo `admin_chat_id`. Mọi tin admin/system →
/// admin (notify DM, banner cảnh báo) PHẢI dùng hàm này để khớp ngôn ngữ admin
/// đã chọn (qua /language). Default Vi khi admin chưa chọn hoặc admin disabled.
fn admin_lang(store: &settings::Settings, admin_chat_id: i64) -> Lang {
    if admin_chat_id == 0 {
        return Lang::Vi;
    }
    lang_or_default(store, admin_chat_id)
}

/// Keyboard chọn ngôn ngữ (song ngữ).
fn language_keyboard() -> Value {
    serde_json::json!([[
        {"text": "🇻🇳 Tiếng Việt", "callback_data": "setlang:vi"},
        {"text": "🇬🇧 English", "callback_data": "setlang:en"}
    ]])
}

/// Keyboard menu cài đặt.
fn settings_keyboard(lang: Lang) -> Value {
    serde_json::json!([[
        {"text": i18n::btn_language(lang), "callback_data": "cmd:language"}
    ]])
}

/// Danh sách lệnh menu Telegram localized (set per-chat sau khi chọn ngôn ngữ).
/// Đầy đủ lệnh user; nếu là admin thì kèm nhóm lệnh admin.
fn localized_commands(lang: Lang, is_admin: bool) -> Vec<(&'static str, &'static str)> {
    let mut v = match lang {
        Lang::Vi => vec![
            ("start", "Bắt đầu / hướng dẫn"),
            ("status", "Trạng thái bot + hàng chờ"),
            ("stop", "Dừng tất cả tiến trình của bạn"),
            ("board", "Bảng tiến trình + nút Dừng"),
            ("cancel", "Xóa bộ đệm văn bản đang chờ"),
            ("proxy_set", "Đặt proxy riêng của bạn"),
            ("proxy_remove", "Xóa proxy của bạn"),
            ("proxy_check", "Kiểm tra live proxy của bạn"),
            ("login_proxy_check", "Kiểm tra live login proxy"),
            ("my_limit", "Xem giới hạn tiến trình của bạn"),
            ("2fa", "Lấy mã 2FA (TOTP)"),
            ("settings", "Cài đặt"),
            ("language", "Đổi ngôn ngữ"),
            ("help", "Trợ giúp"),
        ],
        Lang::En => vec![
            ("start", "Start / instructions"),
            ("status", "Bot status + queue"),
            ("stop", "Stop all your processes"),
            ("board", "Process board + Stop buttons"),
            ("cancel", "Clear pending text buffer"),
            ("proxy_set", "Set your own proxy"),
            ("proxy_remove", "Remove your proxy"),
            ("proxy_check", "Live check your proxy"),
            ("login_proxy_check", "Live check login proxy"),
            ("my_limit", "Show your concurrent process limit"),
            ("2fa", "Get a 2FA (TOTP) code"),
            ("settings", "Settings"),
            ("language", "Change language"),
            ("help", "Help"),
        ],
    };
    if is_admin {
        let admin = match lang {
            Lang::Vi => vec![
                ("notify", "Gửi thông báo tới mọi user (admin)"),
                ("chat", "Nhắn riêng 1 user (admin)"),
                ("ban", "Cấm user (admin)"),
                ("unban", "Gỡ cấm user (admin)"),
                ("banlist", "Danh sách user bị cấm (admin)"),
                ("stopall", "Dừng TẤT CẢ tiến trình mọi user (admin)"),
                ("flushall", "Xóa sạch hàng chờ + board (admin)"),
                ("set_notify", "Đặt kênh thông báo success (admin)"),
                ("notify_remove", "Xóa kênh thông báo (admin)"),
                ("notify_test", "Test kênh thông báo (admin)"),
                ("proxy_login_set", "Đặt login proxy chung (admin)"),
                ("proxy_login_remove", "Xóa login proxy chung (admin)"),
                ("proxy_check_user", "Xem proxy của user (admin, raw)"),
                ("set_max_per_user", "Đặt giới hạn tiến trình/user toàn cục (admin)"),
                ("set_user_limit", "Override giới hạn cho 1 user (admin)"),
            ],
            Lang::En => vec![
                ("notify", "Broadcast to all users (admin)"),
                ("chat", "Direct message a user (admin)"),
                ("ban", "Ban a user (admin)"),
                ("unban", "Unban a user (admin)"),
                ("banlist", "List banned users (admin)"),
                ("stopall", "Stop ALL processes of all users (admin)"),
                ("flushall", "Clear queue + board (admin)"),
                ("set_notify", "Set success-notify channel (admin)"),
                ("notify_remove", "Remove notify channel (admin)"),
                ("notify_test", "Test notify channel (admin)"),
                ("proxy_login_set", "Set shared login proxy (admin)"),
                ("proxy_login_remove", "Remove shared login proxy (admin)"),
                ("proxy_check_user", "Show user's proxy (admin, raw)"),
                ("set_max_per_user", "Set global concurrent process limit (admin)"),
                ("set_user_limit", "Override per-user limit (admin)"),
            ],
        };
        v.extend(admin);
    }
    v
}

/// Validate JSON đúng shape của https://chatgpt.com/api/auth/session
/// (phải có `accessToken` non-empty + object `user`).
fn is_auth_session_json(v: &Value) -> bool {
    let has_token = v
        .get("accessToken")
        .and_then(|t| t.as_str())
        .map(|s| !s.is_empty())
        .unwrap_or(false);
    let has_user = v.get("user").map(|u| u.is_object()).unwrap_or(false);
    has_token && has_user
}

#[tokio::main(flavor = "multi_thread", worker_threads = 4)]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into()),
        )
        .with_target(false)
        .compact()
        .init();

    let cli = Cli::parse();
    info!(
        "starting upi-qr-bot — max_concurrent={} approve_retries={} restart={}/{} proxy_pool={} proxy_from_step={}",
        cli.max_concurrent,
        cli.approve_retries,
        cli.restart_threshold,
        cli.max_restarts,
        cli.proxy_pool.split(',').filter(|s| !s.trim().is_empty()).count(),
        cli.proxy_from_step,
    );

    let allowed_users = parse_allowed_users(&cli.allowed_users);
    let proxy_pool = parse_proxy_pool(&cli.proxy_pool);

    // Nâng giới hạn file descriptor cho nhiều tiến trình đồng thời (mỗi job mở
    // socket UPI + login). Chạy root nên set được cả hard limit (tới fs.nr_open).
    let want_nofile: u64 = (cli.max_concurrent as u64 * 64).clamp(8192, 262144);
    if let Err(e) = rlimit::Resource::NOFILE.set(want_nofile, want_nofile) {
        // Fallback: ít nhất nâng soft tới hard hiện có.
        let _ = rlimit::increase_nofile_limit(want_nofile);
        warn!("set nofile hard limit fail: {} (đã nâng soft tới hard)", e);
    }
    if let Ok((soft, hard)) = rlimit::Resource::NOFILE.get() {
        info!("nofile limit: soft={} hard={}", soft, hard);
    }

    std::fs::create_dir_all(&cli.qr_out_dir).ok();
    std::fs::create_dir_all(&cli.bundles_cache_dir).ok();
    if let Some(p) = cli.db_path.parent() {
        std::fs::create_dir_all(p).ok();
    }

    // Open Settings Store — giữ sống suốt runtime: chứa settings + danh sách
    // user kết nối (broadcast) + danh sách ban (theo user_id).
    let store = Arc::new(
        settings::Settings::open(&cli.db_path).context("failed to open SQLite settings store")?,
    );
    info!("settings store opened: {}", cli.db_path.display());

    let client = HttpClient::new(cli.http_timeout)?;

    // Sub-commands
    if let Some(cmd) = cli.cmd.clone() {
        match cmd {
            SubCmd::StripeProbe => return run_stripe_probe(client, cli.bundles_cache_dir.clone()).await,
            SubCmd::RunOnce { session_json, combo, qr_out } => {
                return run_once(client, &cli, &proxy_pool, session_json.as_deref(), &combo, &qr_out).await;
            }
            SubCmd::LoginOnce { combo, proxy } => {
                return run_login_once(&combo, &proxy).await;
            }
        }
    }

    if cli.telegram_token.is_empty() {
        return Err(anyhow!(
            "--telegram-token is required in bot mode (or use sub-command stripe-probe / run-once)"
        ));
    }
    let tg = Arc::new(TelegramClient::new(&cli.telegram_token)?);

    let (queue, queue_rx) = JobQueue::new(cli.queue_capacity);
    let queue = Arc::new(queue);

    // ── max_per_user: resolve effective default + load overrides từ DB ────
    // Ưu tiên: DB key `limits.max_per_user_default` win. Nếu key chưa có,
    // seed = CLI flag (đã clamp 1..=10) — sau đó mọi thay đổi qua
    // `/set_max_per_user` ghi DB → restart vẫn nhớ. Override per-user load
    // bulk vào limiter cache (write-through khi admin set/xóa qua lệnh).
    let initial_default_max = match store.get_max_per_user_default() {
        Some(n) => n,
        None => {
            let seed = cli
                .max_per_user
                .clamp(settings::Settings::MAX_PER_USER_MIN, settings::Settings::MAX_PER_USER_MAX);
            if let Err(e) = store.set_max_per_user_default(seed) {
                warn!("seed limits.max_per_user_default fail: {}", e);
            } else {
                info!(
                    "seeded limits.max_per_user_default={} (DB lần đầu)",
                    seed
                );
            }
            seed
        }
    };
    let initial_overrides = match store.list_user_limits() {
        Ok(v) => v,
        Err(e) => {
            warn!("list_user_limits fail (boot với cache rỗng): {}", e);
            Vec::new()
        }
    };
    info!(
        "max_per_user default={} overrides={}",
        initial_default_max,
        initial_overrides.len()
    );

    let limiter = UserLimiter::new(
        Duration::from_secs(cli.user_cooldown_seconds),
        cli.user_msg_rate_per_min,
        initial_default_max,
        initial_overrides,
    );
    let registry = JobRegistry::new();
    let board = JobBoard::new();
    let dashboard = DashboardManager::new();
    let limiter_for_done = limiter.clone();
    let registry_for_done = registry.clone();
    let on_done: Arc<dyn Fn(i64, u64) + Send + Sync> = Arc::new(move |user_id: i64, job_id: u64| {
        let lim = limiter_for_done.clone();
        let reg = registry_for_done.clone();
        tokio::spawn(async move {
            lim.mark_done(user_id).await;
            reg.unregister(user_id, job_id).await;
        });
    });
    spawn_workers(
        client.clone(),
        queue_rx,
        on_done,
        WorkerConfig {
            max_concurrent: cli.max_concurrent,
            job_timeout: Duration::from_secs(cli.job_timeout_seconds),
        },
    );

    // Vacuum limiter + registry + send-gate mỗi 10 phút.
    {
        let lim = limiter.clone();
        let reg = registry.clone();
        let tg_v = tg.clone();
        tokio::spawn(async move {
            let mut interval = tokio::time::interval(Duration::from_secs(600));
            interval.tick().await;
            loop {
                interval.tick().await;
                lim.vacuum().await;
                reg.vacuum().await;
                tg_v.gate().vacuum().await;
            }
        });
    }

    // Dashboard ticker — render + edit 1 message/user mỗi 3s (chỉ user dirty,
    // chỉ khi nội dung đổi). Thay cho per-job message tự edit → chống flood 429.
    {
        let dash = dashboard.clone();
        let board = board.clone();
        let tg_d = tg.clone();
        tokio::spawn(async move {
            let mut interval = tokio::time::interval(Duration::from_secs(3));
            interval.tick().await;
            loop {
                interval.tick().await;
                dash.flush(&tg_d, &board, &now_hms()).await;
            }
        });
    }

    // Set bot commands menu (mặc định cho mọi user)
    let bot_commands: &[(&str, &str)] = &[
        ("start", "Open menu / Mở menu"),
        ("status", "Bot status"),
        ("stop", "Stop my processes"),
        ("board", "Process board / Bảng tiến trình"),
        ("settings", "Settings / Cài đặt"),
        ("language", "Language / Ngôn ngữ"),
        ("proxy_set", "Set your own proxy"),
        ("proxy_remove", "Remove your proxy"),
        ("proxy_check", "Live check your proxy"),
        ("login_proxy_check", "Live check login proxy"),
        ("my_limit", "Show your concurrent process limit"),
        ("2fa", "Get a 2FA (TOTP) code"),
        ("help", "Show help"),
    ];
    if let Err(e) = tg.set_my_commands(bot_commands).await {
        warn!("setMyCommands warn: {}", e);
    }

    // Menu lệnh admin (scope theo chat admin) — chỉ admin thấy /notify, /ban...
    if cli.admin_chat_id != 0 {
        let admin_commands: &[(&str, &str)] = &[
            ("start", "Open menu"),
            ("status", "Bot status"),
            ("stop", "Cancel my running jobs"),
            ("cancel", "Clear pending text buffer"),
            ("proxy_set", "Set your own proxy"),
            ("proxy_remove", "Remove your proxy"),
            ("proxy_check", "Live check your proxy"),
            ("login_proxy_check", "Live check login proxy"),
            ("my_limit", "Show your concurrent process limit"),
            ("2fa", "Get a 2FA (TOTP) code"),
            ("help", "Show help"),
            ("notify", "Broadcast to all users (admin)"),
            ("chat", "Direct message a user (admin)"),
            ("ban", "Ban user by @username/id (admin)"),
            ("unban", "Unban by @username/id (admin)"),
            ("banlist", "List banned users (admin)"),
            ("board", "Process board + Stop buttons"),
            ("stopall", "Stop ALL processes of all users (admin)"),
            ("flushall", "Clear queue + board (admin)"),
            ("set_notify", "Set success-notify channel (admin)"),
            ("notify_remove", "Remove notify channel (admin)"),
            ("notify_test", "Test notify channel (admin)"),
            ("proxy_login_set", "Set shared login proxy (admin)"),
            ("proxy_login_remove", "Remove shared login proxy (admin)"),
            ("proxy_check_user", "Show user's proxy (admin, raw)"),
            ("set_max_per_user", "Set global concurrent process limit (admin)"),
            ("set_user_limit", "Override per-user limit (admin)"),
        ];
        if let Err(e) = tg
            .set_my_commands_for_chat(cli.admin_chat_id, admin_commands)
            .await
        {
            warn!("setMyCommands(admin) warn: {}", e);
        }
    }

    info!("bot ready, polling Telegram getUpdates...");
    let mut offset: i64 = 0;
    loop {
        match tg.get_updates(offset, 25).await {
            Ok(updates) => {
                for u in updates {
                    offset = u.update_id + 1;
                    if let Some(msg) = u.message {
                        let tg = tg.clone();
                        let queue = queue.clone();
                        let limiter = limiter.clone();
                        let registry = registry.clone();
                        let store = store.clone();
                        let allowed = allowed_users.clone();
                        let proxy_pool = proxy_pool.clone();
                        let cli = cli.clone();
                        let board = board.clone();
                        let dashboard = dashboard.clone();
                        tokio::spawn(async move {
                            if let Err(e) = handle_message(
                                tg,
                                queue,
                                limiter,
                                registry,
                                store,
                                msg,
                                &allowed,
                                &proxy_pool,
                                &cli,
                                board,
                                dashboard,
                            )
                            .await
                            {
                                error!("handle_message error: {}", e);
                            }
                        });
                    } else if let Some(cb) = u.callback_query {
                        let tg = tg.clone();
                        let registry = registry.clone();
                        let limiter = limiter.clone();
                        let store = store.clone();
                        let allowed = allowed_users.clone();
                        let admin_chat_id = cli.admin_chat_id;
                        let board = board.clone();
                        let dashboard = dashboard.clone();
                        tokio::spawn(async move {
                            if let Err(e) = handle_callback(
                                tg,
                                registry,
                                limiter,
                                store,
                                cb,
                                &allowed,
                                admin_chat_id,
                                board,
                                dashboard,
                            )
                            .await
                            {
                                error!("handle_callback error: {}", e);
                            }
                        });
                    }
                }
            }
            Err(e) => {
                warn!("getUpdates error: {} — sleeping 5s", e);
                tokio::time::sleep(Duration::from_secs(5)).await;
            }
        }
    }
}

async fn handle_message(
    tg: Arc<TelegramClient>,
    queue: Arc<JobQueue>,
    limiter: UserLimiter,
    registry: JobRegistry,
    store: Arc<settings::Settings>,
    msg: Message,
    allowed: &HashSet<i64>,
    proxy_pool: &[String],
    cli: &Cli,
    board: JobBoard,
    dashboard: DashboardManager,
) -> Result<()> {
    let user_id = msg.from.as_ref().map(|u| u.id).unwrap_or(0);
    let username = msg.from.as_ref().and_then(|u| u.username.clone());
    let first_name = msg.from.as_ref().and_then(|u| u.first_name.clone());

    // Ghi nhận user kết nối (nguồn danh sách broadcast). Username có thể đổi —
    // lưu theo user_id bền vững. Bỏ qua message không có sender (channel, ...).
    if user_id != 0 {
        if let Err(e) = store.record_user(
            user_id,
            username.as_deref(),
            first_name.as_deref(),
            msg.chat.id,
        ) {
            tracing::warn!(user_id, "record_user fail: {}", e);
        }
    }

    let is_admin = cli.admin_chat_id != 0 && user_id == cli.admin_chat_id;

    // Anti-flood: register message → drop nếu vượt rate. Mọi message đều count
    // (không còn skip "chunk continuation" sau khi bỏ session_buffer).
    match limiter.register_message(user_id).await {
        MessageDecision::Allow => {}
        MessageDecision::Drop { observed, limit } => {
            tracing::warn!(
                user_id,
                observed,
                limit,
                "message dropped (rate limit exceeded)"
            );
            return Ok(());
        }
    }

    // Ban check — banned user (theo user_id) bị chặn hoàn toàn. Admin miễn nhiễm.
    if !is_admin {
        match store.is_banned(user_id) {
            Ok(true) => {
                tg.send_message(
                    msg.chat.id,
                    &i18n::banned(lang_or_default(&store, user_id)),
                    Some(msg.message_id),
                )
                .await
                .ok();
                return Ok(());
            }
            Ok(false) => {}
            Err(e) => tracing::warn!(user_id, "is_banned check fail: {}", e),
        }
    }

    let lang_opt = user_lang(&store, user_id);
    if !allowed.is_empty() && !allowed.contains(&user_id) {
        tg.send_message(
            msg.chat.id,
            &i18n::not_whitelisted(lang_opt.unwrap_or(Lang::Vi)),
            Some(msg.message_id),
        )
        .await
        .ok();
        return Ok(());
    }

    // ── Cổng ngôn ngữ: user PHẢI chọn Việt/Anh trước khi dùng bot ──
    let lang = match lang_opt {
        Some(l) => l,
        None => {
            tg.send_message_kb(
                msg.chat.id,
                i18n::choose_language(),
                None,
                language_keyboard(),
            )
            .await
            .ok();
            return Ok(());
        }
    };

    // Commands
    if let Some(text) = &msg.text {
        let trimmed = text.trim();
        if trimmed.starts_with("/start") {
            // Refresh menu lệnh localized đầy đủ (kèm admin nếu là admin).
            tg.set_my_commands_for_chat(msg.chat.id, &localized_commands(lang, is_admin))
                .await
                .ok();
            send_welcome(&tg, msg.chat.id, msg.message_id, lang).await;
            return Ok(());
        }
        if trimmed.starts_with("/language") || trimmed.starts_with("/lang") {
            tg.send_message_kb(msg.chat.id, i18n::choose_language(), None, language_keyboard())
                .await
                .ok();
            return Ok(());
        }
        if trimmed.starts_with("/settings") {
            tg.send_message_kb(
                msg.chat.id,
                &i18n::settings_title(lang),
                None,
                settings_keyboard(lang),
            )
            .await
            .ok();
            return Ok(());
        }
        if trimmed.starts_with("/help") {
            send_help(&tg, &store, msg.chat.id, msg.message_id, user_id, is_admin, lang).await;
            return Ok(());
        }
        if trimmed.starts_with("/status") {
            let pending = queue.pending();
            tg.send_message(
                msg.chat.id,
                &format!("{} · queue {}/{}", i18n::status_online(lang), pending, cli.queue_capacity),
                None,
            )
            .await
            .ok();
            return Ok(());
        }
        if trimmed.starts_with("/board") {
            // /board CHỈ dành cho admin (toàn hệ thống). User thường có
            // dashboard tự cập nhật → nudge refresh + báo ngắn.
            if is_admin {
                handle_board(&tg, &board, msg.chat.id, user_id, is_admin, lang).await;
            } else {
                dashboard.touch(user_id, msg.chat.id, lang).await;
                tg.send_message(msg.chat.id, &i18n::dashboard_auto_note(lang), None)
                    .await
                    .ok();
            }
            return Ok(());
        }
        if trimmed.starts_with("/cancel") {
            // /cancel là alias mềm cho /stop trong scope text input. Không
            // còn buffer ghép chunk để clear → chỉ phản hồi để user biết.
            tg.send_message(msg.chat.id, &i18n::stopped_all(lang, 0), None)
                .await
                .ok();
            return Ok(());
        }
        if trimmed.starts_with("/stop") {
            let stopped = registry.stop_user(user_id).await;
            limiter.force_reset_user(user_id).await;
            let body = i18n::stopped_all(lang, stopped);
            tg.send_message(msg.chat.id, &body, Some(msg.message_id))
                .await
                .ok();
            return Ok(());
        }

        // ── Admin commands ────────────────────────────────────────────────
        // Match chính xác token lệnh (strip @botname) để /ban không nuốt /banlist.
        let cmd_base = trimmed
            .split_whitespace()
            .next()
            .unwrap_or("")
            .split('@')
            .next()
            .unwrap_or("");

        // ── Proxy commands (mọi user) ─────────────────────────────────────
        match cmd_base {
            "/proxy_set" => {
                handle_proxy_set(
                    &tg,
                    &store,
                    &msg,
                    text,
                    user_id,
                    &username,
                    cli.admin_chat_id,
                    cli.proxy_from_step,
                    lang,
                )
                .await;
                return Ok(());
            }
            "/proxy_remove" => {
                handle_proxy_remove(&tg, &store, &msg, user_id, lang).await;
                return Ok(());
            }
            "/2fa" => {
                handle_2fa(&tg, &msg, text, lang).await;
                return Ok(());
            }
            "/proxy_check" => {
                handle_proxy_check(&tg, &store, &msg, user_id, lang).await;
                return Ok(());
            }
            "/login_proxy_check" => {
                handle_login_proxy_check(&tg, &store, &msg, lang, cli.proxy_from_step).await;
                return Ok(());
            }
            "/my_limit" => {
                handle_my_limit(&tg, &limiter, &msg, user_id, lang).await;
                return Ok(());
            }
            _ => {}
        }

        match cmd_base {
            "/notify" | "/chat" | "/ban" | "/unban" | "/banlist"
            | "/proxy_login_set" | "/proxy_login_remove" | "/stopall" | "/flushall"
            | "/set_notify" | "/notify_remove" | "/notify_test"
            | "/proxy_check_user"
            | "/set_max_per_user" | "/set_user_limit" => {
                if !is_admin {
                    tg.send_message(
                        msg.chat.id,
                        &i18n::admin_only(lang),
                        Some(msg.message_id),
                    )
                    .await
                    .ok();
                    return Ok(());
                }
                match cmd_base {
                    "/notify" => handle_notify(&tg, &store, &msg, text).await,
                    "/chat" => handle_chat(&tg, &store, &msg, text).await,
                    "/ban" => {
                        handle_ban(&tg, &store, &registry, &limiter, &msg, text, cli.admin_chat_id).await
                    }
                    "/unban" => handle_unban(&tg, &store, &msg, text).await,
                    "/banlist" => handle_banlist(&tg, &store, &msg).await,
                    "/proxy_login_set" => {
                        handle_proxy_login_set(&tg, &store, &msg, text, cli.proxy_from_step).await
                    }
                    "/proxy_login_remove" => {
                        handle_proxy_login_remove(&tg, &store, &msg).await
                    }
                    "/stopall" => handle_stopall(&tg, &registry, &limiter, &store, msg.chat.id).await,
                    "/flushall" => {
                        handle_flushall(
                            &tg,
                            &registry,
                            &limiter,
                            &board,
                            &store,
                            msg.chat.id,
                        )
                        .await
                    }
                    "/set_notify" => handle_set_notify(&tg, &store, &msg, text).await,
                    "/notify_remove" => handle_notify_remove(&tg, &store, &msg).await,
                    "/notify_test" => {
                        handle_notify_test(&tg, &store, &msg, cli.admin_chat_id).await
                    }
                    "/proxy_check_user" => {
                        handle_proxy_check_user(&tg, &store, &msg, text, cli.admin_chat_id).await
                    }
                    "/set_max_per_user" => {
                        handle_set_max_per_user(
                            &tg, &store, &limiter, &msg, text, cli.admin_chat_id,
                        )
                        .await
                    }
                    "/set_user_limit" => {
                        handle_set_user_limit(
                            &tg, &store, &limiter, &msg, text, cli.admin_chat_id,
                        )
                        .await
                    }
                    _ => unreachable!(),
                }
                return Ok(());
            }
            _ => {}
        }

        if trimmed.starts_with('/') {
            let cmd = trimmed.split_whitespace().next().unwrap_or(trimmed);
            tg.send_message(msg.chat.id, &i18n::unknown_command(lang, cmd), Some(msg.message_id))
                .await
                .ok();
            return Ok(());
        }

        // Text thường — combo `email|password|2fa` được chấp nhận paste (luôn
        // ngắn). Còn JSON dài → từ chối + hint upload file (Telegram cắt > 4096
        // ký tự, paste JSON là nguồn gây stuck → fail-fast, clear).
        if !trimmed.is_empty() {
            // Auto-detect combo email|password|2fa (không phải JSON) → login HTTP.
            // Hỗ trợ NHIỀU dòng: mỗi dòng = 1 tài khoản → 1 tiến trình riêng.
            if looks_like_combo_input(trimmed) {
                let (combos, invalid) = parse_account_combos(trimmed);
                if combos.is_empty() {
                    tg.send_message(msg.chat.id, &i18n::invalid_combo(lang), Some(msg.message_id))
                        .await
                        .ok();
                    return Ok(());
                }

                // Cap số dòng xử lý 1 lần = effective max tiến trình/user (chống
                // flood khi dán quá nhiều dòng). Đọc qua limiter để áp đúng
                // override per-user khi user trả phí được mức cao hơn default.
                // Phần dư bị bỏ, báo rõ trong header.
                let cap = limiter.effective_max(user_id).await.max(1) as usize;
                let dropped = combos.len().saturating_sub(cap);
                let combos: Vec<AccountCombo> = combos.into_iter().take(cap).collect();

                if combos.len() > 1 || invalid > 0 || dropped > 0 {
                    tg.send_message(
                        msg.chat.id,
                        &i18n::combo_batch_received(lang, combos.len(), invalid, dropped),
                        Some(msg.message_id),
                    )
                    .await
                    .ok();
                }

                // Khi user dán batch nhiều combo: probe proxy 1 LẦN duy nhất ở
                // đây + gửi 1 card preflight cho cả batch — sau đó từng job tái
                // dùng `outcome.clone()` (random pick từ pool đã materialize +
                // shuffle, không probe lại). Single combo: pass None để giữ
                // hành vi inner preflight cũ. Pool toàn dead → fallback DIRECT
                // (không block batch).
                let shared_outcome: Option<PreflightOutcome> = if combos.len() > 1 {
                    let outcome = run_user_preflight(user_id, &store, cli, lang).await;
                    if !outcome.proxy_lines.is_empty() {
                        tg.send_message(
                            msg.chat.id,
                            &bot::proc_view::render_preflight_ok_batch(
                                lang,
                                combos.len(),
                                &outcome.proxy_lines,
                            ),
                            None,
                        )
                        .await
                        .ok();
                    }
                    Some(outcome)
                } else {
                    None
                };

                // Mỗi tài khoản 1 job độc lập — admission (cap/user) + chống
                // trùng tài khoản tự áp trong enqueue_and_track. Lỗi 1 dòng
                // không chặn các dòng còn lại.
                for combo in combos {
                    if let Err(e) = process_account_combo(
                        tg.clone(),
                        queue.clone(),
                        limiter.clone(),
                        registry.clone(),
                        store.clone(),
                        msg.chat.id,
                        msg.message_id,
                        user_id,
                        username.clone(),
                        combo,
                        proxy_pool,
                        cli,
                        board.clone(),
                        dashboard.clone(),
                        lang,
                        shared_outcome.clone(),
                    )
                    .await
                    {
                        error!("process_account_combo (batch) error: {}", e);
                    }
                }
                return Ok(());
            }

            // Text không phải combo → coi như user đang paste session.json
            // thẳng vào chat. Telegram cắt tin nhắn dài >4096 ký tự nên paste
            // JSON là nguồn gây stuck (chunks không bao giờ ghép đủ). Trả hint
            // chi tiết yêu cầu upload file thay vì paste — fail-fast, clear.
            tg.send_message_kb_html(
                msg.chat.id,
                &i18n::session_must_be_file(lang),
                Value::Array(vec![]),
            )
            .await
            .ok();
            return Ok(());
        }
        return Ok(());
    }

    // Document upload
    let Some(doc) = msg.document.clone() else {
        tg.send_message_kb_html(
            msg.chat.id,
            &i18n::need_input(lang),
            Value::Array(vec![]),
        )
        .await
        .ok();
        return Ok(());
    };

    let file_name = doc.file_name.unwrap_or_else(|| "session.json".into());
    let lower = file_name.to_lowercase();
    // Chấp nhận file .json (chuẩn) hoặc .txt (user export bằng IDM/Save As
    // sai đuôi vẫn dùng được — nội dung phải là JSON hợp lệ, validate ở dưới).
    if !(lower.ends_with(".json") || lower.ends_with(".txt")) {
        tg.send_message_kb_html(
            msg.chat.id,
            &i18n::session_must_be_file(lang),
            Value::Array(vec![]),
        )
        .await
        .ok();
        return Ok(());
    }
    if doc.file_size.unwrap_or(0) > 1_500_000 {
        tg.send_message(
            msg.chat.id,
            &i18n::invalid_session_json(lang),
            Some(msg.message_id),
        )
        .await
        .ok();
        return Ok(());
    }

    let file_path = tg.get_file_path(&doc.file_id).await?;
    let bytes = tg.download_file(&file_path).await?;
    let raw = String::from_utf8(bytes.to_vec())
        .unwrap_or_else(|e| String::from_utf8_lossy(e.as_bytes()).into_owned());
    process_session_json(
        tg,
        queue,
        limiter,
        registry,
        store,
        msg.chat.id,
        msg.message_id,
        user_id,
        username,
        raw,
        proxy_pool,
        cli,
        board,
        dashboard,
        lang,
        None,
    )
    .await
}

/// Kết quả pre-flight proxy: pool URLs đã probe + materialize, để caller
/// (per-job) tự pick random 1 login + shuffle user pool. Đảm bảo MỖI tài
/// khoản trong batch nhận login + work proxy random ĐỘC LẬP, không bám 1 IP.
///
/// **Không có nhãn "dead/block"**: mọi pool toàn dead đều fallback DIRECT.
/// Job luôn được phép chạy; admin/user chỉ thấy warning trong card.
#[derive(Debug, Clone)]
struct PreflightOutcome {
    /// Pool URLs LOGIN proxy live (pass probe + materialize OK). Empty =
    /// admin chưa set, hoặc set nhưng cả pool dead/fail materialize → caller
    /// fallback DIRECT cho login segment.
    login_live_urls: Vec<String>,
    /// Pool URLs USER proxy live (pass probe + materialize OK).
    user_live_urls: Vec<String>,
    /// `true` khi user đã set ≥1 proxy line ban đầu (kể cả toàn dead). Cần
    /// để caller cascade đúng: user set pool + all dead → DIRECT (không
    /// fallback sang login proxy / env pool); user chưa set → mới cascade.
    user_has_pool: bool,
    /// Dòng trạng thái cho card pre-flight (đã sanitize). Empty = không có gì
    /// đáng hiển thị (user không pool + admin không set login proxy).
    proxy_lines: Vec<String>,
}

/// Probe pool proxy của 1 user (login proxy admin + pool riêng). Pure data
/// (không gửi tin nhắn) — caller render card. Cache 5' của `PROXY_STATUS`
/// đảm bảo 2 lần gọi liên tiếp với cùng user thường hit cache, nhưng tách
/// hàm này ra để batch caller có thể gọi 1 lần rồi share cho N job.
async fn run_user_preflight(
    user_id: i64,
    store: &Arc<settings::Settings>,
    cli: &Cli,
    lang: Lang,
) -> PreflightOutcome {
    // ── Pool nguồn: login (admin global, multi) + user (private, multi) ──
    let login_raw_lines: Vec<String> = store.get_login_proxies();
    let user_proxy_lines: Vec<String> = match store.get_user_proxies(user_id) {
        Ok(v) => v,
        Err(e) => {
            tracing::warn!(user_id, "get_user_proxies fail: {}", e);
            Vec::new()
        }
    };
    let user_has_pool = !user_proxy_lines.is_empty();

    // Probe SONG SONG: mỗi login line + mỗi user pool line (qua cache 5').
    let limit_ms = cli.proxy_latency_limit_ms;
    let login_probe_futs = login_raw_lines.iter().map(|raw| {
        let raw = raw.clone();
        async move { bot::proxy_status::PROXY_STATUS.get_or_probe(&raw).await }
    });
    let user_probe_futs = user_proxy_lines.iter().map(|raw| {
        let raw = raw.clone();
        async move { bot::proxy_status::PROXY_STATUS.get_or_probe(&raw).await }
    });
    let (login_statuses, user_statuses) = tokio::join!(
        futures_util::future::join_all(login_probe_futs),
        futures_util::future::join_all(user_probe_futs)
    );

    // ── Classify LOGIN pool: fast / slow_ok / dead ─────────────────────
    let mut login_fast_raw: Vec<String> = Vec::new();
    let mut login_slow_raw: Vec<String> = Vec::new();
    let mut login_dead = 0usize;
    for (raw, st) in login_raw_lines.iter().zip(login_statuses.iter()) {
        if !st.ok {
            login_dead += 1;
            continue;
        }
        if st.latency_ms > limit_ms {
            login_slow_raw.push(raw.clone());
        } else {
            login_fast_raw.push(raw.clone());
        }
    }
    let (login_live_raw, login_used_slow): (Vec<String>, bool) = if !login_fast_raw.is_empty() {
        (login_fast_raw, false)
    } else {
        (login_slow_raw.clone(), !login_slow_raw.is_empty())
    };

    // Materialize TẤT CẢ live login → URLs. Pool đầy đủ để caller (per-job)
    // pick random 1 → mỗi tài khoản 1 IP login khác nhau.
    let mut login_live_urls: Vec<String> = Vec::new();
    for raw in &login_live_raw {
        match proxy_format::materialize_for_client(raw, 8) {
            Ok(url) => login_live_urls.push(url),
            Err(e) => {
                tracing::warn!(user_id, raw = %proxy_format::mask_proxy(raw), "login proxy materialize fail: {}", e);
                login_dead += 1;
            }
        }
    }

    let login_pool_all_dead = !login_raw_lines.is_empty() && login_live_urls.is_empty();
    if login_pool_all_dead {
        tracing::warn!(
            user_id,
            total = login_raw_lines.len(),
            dead = login_dead,
            "login pool — toàn dead/không materialize được → fallback DIRECT"
        );
    }

    // ── Classify USER pool: fast / slow_ok / dead ──────────────────────
    let mut user_pool_fast_raw: Vec<String> = Vec::new();
    let mut user_pool_slow_raw: Vec<String> = Vec::new();
    let mut user_pool_dead = 0usize;
    for (raw, st) in user_proxy_lines.iter().zip(user_statuses.iter()) {
        if !st.ok {
            user_pool_dead += 1;
            continue;
        }
        if st.latency_ms > limit_ms {
            user_pool_slow_raw.push(raw.clone());
        } else {
            user_pool_fast_raw.push(raw.clone());
        }
    }
    let (user_pool_live_raw, used_slow_fallback): (Vec<String>, bool) =
        if !user_pool_fast_raw.is_empty() {
            (user_pool_fast_raw, false)
        } else {
            (user_pool_slow_raw.clone(), !user_pool_slow_raw.is_empty())
        };
    let user_pool_slow_count = user_pool_slow_raw.len();

    // Materialize live USER raw → URLs (giữ nguyên thứ tự ban đầu — caller
    // shuffle per-job để mỗi tài khoản có pool work order riêng).
    let mut user_live_urls: Vec<String> = Vec::new();
    for raw in &user_pool_live_raw {
        match proxy_format::materialize_for_client(raw, 8) {
            Ok(url) => user_live_urls.push(url),
            Err(e) => {
                tracing::warn!(user_id, raw = %proxy_format::mask_proxy(raw), "materialize fail: {}", e);
                user_pool_dead += 1;
            }
        }
    }

    if !user_live_urls.is_empty() {
        tracing::info!(
            user_id,
            live = user_live_urls.len(),
            slow_used = used_slow_fallback,
            slow = user_pool_slow_count,
            dead = user_pool_dead,
            "user proxy pool active"
        );
    } else if user_has_pool {
        tracing::warn!(
            user_id,
            total = user_proxy_lines.len(),
            dead = user_pool_dead,
            "user pool — toàn dead → fallback DIRECT"
        );
    }

    // ── Render dòng trạng thái proxy cho card pre-flight ────────────────
    let mut proxy_lines: Vec<String> = Vec::new();
    let multi_login = login_statuses.len() > 1;
    for (i, st) in login_statuses.iter().enumerate() {
        let label = if multi_login {
            format!("Login #{}", i + 1)
        } else {
            "Login".to_string()
        };
        let mut line = build_proxy_line(lang, &label, st);
        if st.ok && st.latency_ms > limit_ms {
            if login_used_slow {
                line.push_str(&i18n::proxy_slow_fallback(lang, st.latency_ms, limit_ms));
            } else {
                line.push_str(&i18n::proxy_skip_slow(lang, st.latency_ms, limit_ms));
            }
        }
        proxy_lines.push(line);
    }
    for (i, st) in user_statuses.iter().enumerate() {
        let label = format!("User #{}", i + 1);
        let mut line = build_proxy_line(lang, &label, st);
        if st.ok && st.latency_ms > limit_ms {
            if used_slow_fallback {
                line.push_str(&i18n::proxy_slow_fallback(lang, st.latency_ms, limit_ms));
            } else {
                line.push_str(&i18n::proxy_skip_slow(lang, st.latency_ms, limit_ms));
            }
        }
        proxy_lines.push(line);
    }
    if user_has_pool && user_live_urls.is_empty() {
        tracing::warn!(
            user_id,
            total = user_proxy_lines.len(),
            dead = user_pool_dead,
            slow = user_pool_slow_count,
            "user pool toàn dead — job sẽ chạy DIRECT"
        );
        proxy_lines.push(i18n::proxy_pool_all_dead_direct(lang));
    }
    if login_pool_all_dead {
        proxy_lines.push(i18n::login_pool_all_dead_direct(lang));
    }

    PreflightOutcome {
        login_live_urls,
        user_live_urls,
        user_has_pool,
        proxy_lines,
    }
}

#[allow(clippy::too_many_arguments)]
async fn enqueue_and_track(
    tg: Arc<TelegramClient>,
    queue: Arc<JobQueue>,
    limiter: UserLimiter,
    registry: JobRegistry,
    store: Arc<settings::Settings>,
    chat_id: i64,
    reply_to: i64,
    user_id: i64,
    username: Option<String>,
    email: String,
    auth: AuthSource,
    proxy_pool: &[String],
    cli: &Cli,
    board: JobBoard,
    dashboard: DashboardManager,
    lang: Lang,
    // Khi `Some`, dùng kết quả preflight đã probe sẵn (caller — vd batch
    // handler — đã probe + gửi card 1 lần, không lặp). Khi `None`, hành xử
    // nguyên bản: probe + gửi card cho job này.
    precomputed: Option<PreflightOutcome>,
) -> Result<()> {
    // Admission: tối đa N tiến trình/user (mặc định 10). try_admit RESERVE slot
    // atomic ngay khi Allow → không còn khe TOCTOU với preflight proxy phía sau.
    match limiter.try_admit(user_id).await {
        AdmitDecision::Allow => {}
        AdmitDecision::MaxConcurrent { max } => {
            tg.send_message(chat_id, &i18n::max_concurrent(lang, max), Some(reply_to))
                .await
                .ok();
            return Ok(());
        }
        AdmitDecision::Cooldown { remaining_secs } => {
            tg.send_message(chat_id, &i18n::cooldown(lang, remaining_secs), Some(reply_to))
                .await
                .ok();
            return Ok(());
        }
    }

    // ── Chống trùng tài khoản: 1 user không được chạy/queue cùng 1 email 2 lần.
    // Đăng ký job NGAY (atomic) để giữ chỗ email_key trước mọi await (preflight
    // proxy) → không có khe TOCTOU. Mọi đường return sớm sau đây PHẢI nhả cả
    // reservation (limiter.release) lẫn entry registry (unregister).
    let masked_email = upi::runner::mask_email(&email);
    let email_key = email.trim().to_lowercase();
    let (job_id, cancel_token) = match registry.try_register(user_id, &email_key).await {
        Some(v) => v,
        None => {
            limiter.release(user_id).await;
            tg.send_message(
                chat_id,
                &i18n::duplicate_account(lang, &masked_email),
                Some(reply_to),
            )
            .await
            .ok();
            return Ok(());
        }
    };

    // ── Pre-flight proxy: probe TRỪ KHI caller (batch handler) đã probe sẵn.
    // Khi precomputed = Some → dùng kết quả share, KHÔNG gửi card lại (đã gửi
    // 1 lần ở batch caller). Khi None → probe trong job này + gửi card.
    //
    // Mọi pool toàn dead đều fallback DIRECT (job luôn được phép chạy) —
    // không còn trường hợp block từ pre-flight.
    let card_already_sent = precomputed.is_some();
    let preflight = match precomputed {
        Some(o) => o,
        None => run_user_preflight(user_id, &store, cli, lang).await,
    };
    let proxy_lines = preflight.proxy_lines.clone();

    // ── PER-JOB random pick ──────────────────────────────────────────────
    // MỖI tài khoản random độc lập:
    //   - login: pick 1 URL random từ pool live → 2 job liên tiếp KHÔNG bám
    //     cùng 1 IP login (giảm rate-limit per-IP).
    //   - work pool: clone + shuffle riêng cho job này → runner round-robin
    //     theo pool đó với order khác mỗi job.
    let login_proxy_effective: Option<String> = {
        use rand::seq::SliceRandom;
        let mut rng = rand::thread_rng();
        preflight.login_live_urls.choose(&mut rng).cloned()
    };
    let mut user_pool_per_job: Vec<String> = preflight.user_live_urls.clone();
    {
        use rand::seq::SliceRandom;
        let mut rng = rand::thread_rng();
        user_pool_per_job.shuffle(&mut rng);
    }
    let effective_pool: Vec<String> = if !user_pool_per_job.is_empty() {
        user_pool_per_job
    } else if preflight.user_has_pool {
        // User đã set proxy nhưng cả pool dead/fail materialize → DIRECT cho
        // segment work (không cascade về login proxy hay env pool — đó là
        // ý đồ user khi set proxy riêng).
        Vec::new()
    } else {
        // User chưa set proxy → cascade: login proxy của job này / env global
        // / DIRECT.
        match &login_proxy_effective {
            Some(u) => vec![u.clone()],
            None => proxy_pool.to_vec(),
        }
    };

    if !card_already_sent && !proxy_lines.is_empty() {
        tg.send_message(
            chat_id,
            &bot::proc_view::render_preflight_ok(lang, &masked_email, &proxy_lines),
            None,
        )
        .await
        .ok();
    }

    // Đăng ký job đã thực hiện ở đầu hàm (try_register) — job_id/cancel_token
    // đã có sẵn. Chỉ tạo channel sự kiện ở đây.
    let (event_tx, mut event_rx) = mpsc::unbounded_channel::<JobEvent>();
    let qr_path = cli
        .qr_out_dir
        .join(format!("qr_{}_{}.png", user_id, job_id));

    // Nhãn nguồn auth (để báo admin) — lấy trước khi move vào job_config.
    let auth_kind = match &auth {
        AuthSource::Login { .. } => "combo (email|pass|2fa)",
        AuthSource::Session { .. } => "session.json",
    };

    let job_config = UpiJobConfig {
        email: email.clone(),
        auth,
        proxy_pool: effective_pool,
        login_proxy: login_proxy_effective,
        approve_retries: cli.approve_retries,
        approve_delay_ms: Some(cli.approve_delay_secs.saturating_mul(1000)),
        restart_threshold: cli.restart_threshold,
        max_restarts: cli.max_restarts,
        proxy_from_step: cli.proxy_from_step,
        qr_out_path: qr_path.clone(),
        bundles_cache_dir: cli.bundles_cache_dir.clone(),
        qr_watermark: cli.qr_watermark.clone(),
    };

    let username_for_log = username.clone();
    let job = Job {
        user_id,
        job_id,
        chat_id,
        username,
        config: job_config,
        log_tx: event_tx,
        cancel: cancel_token.clone(),
    };

    match queue.try_submit(job) {
        Ok(position) => {
            // Slot đã reserve ở try_admit — KHÔNG tăng lại. on_done → mark_done
            // sẽ trả slot khi job kết thúc. Job vào board (Queued) + đánh thức
            // dashboard per-user (1 message sống, ticker tự render).
            board
                .insert_queued(job_id, user_id, username_for_log.clone(), masked_email.clone())
                .await;
            dashboard.touch(user_id, chat_id, lang).await;
            // Báo admin (DM) khi user KHÁC tạo tiến trình mới — KHÔNG đi qua
            // notify target (notify target dành riêng cho QR success ở topic).
            if cli.admin_chat_id != 0 && user_id != cli.admin_chat_id {
                let alang = admin_lang(&store, cli.admin_chat_id);
                let note = i18n::admin_note_new_process(
                    alang,
                    username_for_log.as_deref().unwrap_or("-"),
                    user_id,
                    &masked_email,
                    auth_kind,
                    position,
                );
                if let Err(e) = tg.send_message(cli.admin_chat_id, &note, None).await {
                    tracing::warn!(admin = cli.admin_chat_id, "admin new-process notify fail: {}", e);
                }
            }
        }
        Err(SubmitError::QueueFull { pending, capacity }) => {
            registry.unregister(user_id, job_id).await;
            limiter.release(user_id).await;
            tg.send_message(chat_id, &i18n::queue_full(lang, pending, capacity), Some(reply_to))
                .await
                .ok();
            return Ok(());
        }
        Err(SubmitError::Closed) => {
            registry.unregister(user_id, job_id).await;
            limiter.release(user_id).await;
            tg.send_message(chat_id, &i18n::queue_closed(lang), Some(reply_to))
                .await
                .ok();
            return Ok(());
        }
    }

    let tg_for_log = tg.clone();
    let qr_path_for_send = qr_path.clone();
    let admin_chat_id = cli.admin_chat_id;
    let store_for_notify = store.clone();
    let board_for_task = board;
    let dash_for_task = dashboard;
    let board_job_id = job_id;
    let email_for_done = masked_email.clone();
    tokio::spawn(async move {
        // Không còn message per-job: job event chỉ cập nhật board + đánh thức
        // dashboard (1 message/user, ticker render). Terminal: QR (success) /
        // tin lý do (fail/timeout) — gửi lẻ, đều qua SendGate (chống 429).
        let mut started_at = std::time::Instant::now();

        while let Some(event) = event_rx.recv().await {
            match event {
                JobEvent::Queued { .. } => {
                    dash_for_task.mark_dirty(user_id).await;
                }
                JobEvent::Started => {
                    started_at = std::time::Instant::now();
                    board_for_task.mark_running(board_job_id).await;
                    dash_for_task.mark_dirty(user_id).await;
                }
                JobEvent::Log(line) => {
                    // Board parse log → StepKind thân thiện; dashboard ticker
                    // sẽ render lại (không edit ngay ở đây → không flood).
                    board_for_task.set_step(board_job_id, line).await;
                    dash_for_task.mark_dirty(user_id).await;
                }
                JobEvent::Done(result) => {
                    board_for_task.remove(board_job_id).await;
                    dash_for_task.mark_dirty(user_id).await;
                    let expires = format_expires(result.qr_expires_at);
                    let attempts = result.approve_attempts.len();

                    if result.ok {
                        // Link thanh toán: ưu tiên payment_link Stripe, fallback
                        // return_url checkout đổi host → pay.openai.com.
                        let pay_url = result.payment_link.clone().or_else(|| {
                            if result.return_url.contains("/c/pay/") {
                                Some(
                                    result
                                        .return_url
                                        .replace("checkout.stripe.com", "pay.openai.com"),
                                )
                            } else {
                                None
                            }
                        });
                        // Ảnh QR + caption gộp (email + hạn + link + attempts)
                        // → 1 tin duy nhất cho success (bớt spam).
                        let caption = bot::proc_view::qr_caption_full(
                            lang,
                            &email_for_done,
                            &expires,
                            pay_url.as_deref(),
                            attempts,
                        );
                        let mut sent_photo = false;
                        if let Some(qr) = result.qr_path.as_deref() {
                            let path = std::path::Path::new(qr);
                            if path.exists() {
                                match tg_for_log
                                    .send_photo(chat_id, path, Some(&caption), None)
                                    .await
                                {
                                    Ok(_) => sent_photo = true,
                                    Err(e) => tracing::warn!("sendPhoto fail: {}", e),
                                }
                            }
                        }
                        // Không có ảnh (hiếm) → gửi caption dạng text để user vẫn
                        // nhận link/thông tin.
                        if !sent_photo {
                            tg_for_log.send_message(chat_id, &caption, None).await.ok();
                        }
                    } else {
                        // Fail → 1 tin lý do thân thiện.
                        let raw = result
                            .error
                            .clone()
                            .or_else(|| result.qr_reason.clone())
                            .unwrap_or_default();
                        let body = bot::proc_view::render_done_fail(
                            lang,
                            &email_for_done,
                            result.elapsed_seconds,
                            &raw,
                            attempts,
                        );
                        tg_for_log.send_message(chat_id, &body, None).await.ok();
                    }

                    // QR success → admin DM + notify target (topic). Tránh dup
                    // khi notify target trùng admin chat (cùng chat, no thread).
                    if result.ok && user_id != admin_chat_id {
                        let alang = admin_lang(&store_for_notify, admin_chat_id);
                        let summary = i18n::admin_note_qr_success(
                            alang,
                            username_for_log.as_deref().unwrap_or("-"),
                            user_id,
                            &result.email,
                            result.elapsed_seconds,
                        );
                        let mut sent_admin = false;
                        if let Some((chat, thread)) = store_for_notify.get_notify_target() {
                            if let Err(e) = tg_for_log
                                .send_message_to_thread(chat, &summary, thread)
                                .await
                            {
                                tracing::warn!(chat, "notify success fail: {}", e);
                            }
                            if chat == admin_chat_id && thread.is_none() {
                                sent_admin = true;
                            }
                        }
                        if admin_chat_id != 0 && !sent_admin {
                            if let Err(e) =
                                tg_for_log.send_message(admin_chat_id, &summary, None).await
                            {
                                tracing::warn!(admin_chat_id, "admin success notify fail: {}", e);
                            }
                        }
                    }
                    crate::bot::queue::cleanup_qr_artifacts(&qr_path_for_send);
                    break;
                }
                JobEvent::Timeout => {
                    board_for_task.remove(board_job_id).await;
                    dash_for_task.mark_dirty(user_id).await;
                    let body = bot::proc_view::render_timeout(
                        lang,
                        &email_for_done,
                        started_at.elapsed().as_secs_f64(),
                    );
                    tg_for_log.send_message(chat_id, &body, None).await.ok();
                    crate::bot::queue::cleanup_qr_artifacts(&qr_path_for_send);
                    break;
                }
                JobEvent::Cancelled => {
                    // User chủ động /stop → dashboard tự bỏ dòng job. Không gửi
                    // tin terminal riêng (user đã biết) → bớt 1 message.
                    board_for_task.remove(board_job_id).await;
                    dash_for_task.mark_dirty(user_id).await;
                    crate::bot::queue::cleanup_qr_artifacts(&qr_path_for_send);
                    break;
                }
            }
        }
    });

    Ok(())
}

/// Combo tài khoản dạng `email|password|totp_secret`.
struct AccountCombo {
    email: String,
    password: String,
    totp_secret: String,
}

/// Heuristic: text trông giống combo (không phải JSON, có '|' và '@').
fn looks_like_combo_input(text: &str) -> bool {
    let t = text.trim();
    !t.starts_with('{') && !t.starts_with('[') && t.contains('|') && t.contains('@')
}

/// Parse 1 dòng `email|password|totp_secret`. Yêu cầu đúng 1 tài khoản/lần
/// (multi-line bị từ chối ở caller). Trả None nếu format sai.
fn parse_account_combo(text: &str) -> Option<AccountCombo> {
    let lines: Vec<&str> = text
        .lines()
        .map(|l| l.trim())
        .filter(|l| !l.is_empty())
        .collect();
    if lines.len() != 1 {
        return None;
    }
    let parts: Vec<&str> = lines[0].split('|').collect();
    if parts.len() < 3 {
        return None;
    }
    let email = parts[0].trim();
    let password = parts[1].trim();
    let secret = parts[2].trim();
    if email.is_empty() || password.is_empty() || secret.is_empty() || !email.contains('@') {
        return None;
    }
    Some(AccountCombo {
        email: email.to_string(),
        password: password.to_string(),
        totp_secret: secret.to_string(),
    })
}

/// Parse NHIỀU dòng combo `email|password|2fa` (mỗi dòng 1 tài khoản). Bỏ qua
/// dòng rỗng, dedupe theo email (giữ lần đầu) để 1 lần dán không tạo 2 job
/// trùng. Trả `(combos hợp lệ, số dòng sai định dạng)`.
fn parse_account_combos(text: &str) -> (Vec<AccountCombo>, usize) {
    let mut out: Vec<AccountCombo> = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    let mut invalid = 0usize;
    for line in text.lines() {
        let l = line.trim();
        if l.is_empty() {
            continue;
        }
        match parse_account_combo(l) {
            Some(c) => {
                let key = c.email.trim().to_lowercase();
                if seen.insert(key) {
                    out.push(c);
                }
            }
            None => invalid += 1,
        }
    }
    (out, invalid)
}

/// Nhận session.json (file hoặc paste text) → build job với session có sẵn.
#[allow(clippy::too_many_arguments)]
async fn process_session_json(
    tg: Arc<TelegramClient>,
    queue: Arc<JobQueue>,
    limiter: UserLimiter,
    registry: JobRegistry,
    store: Arc<settings::Settings>,
    chat_id: i64,
    reply_to: i64,
    user_id: i64,
    username: Option<String>,
    raw: String,
    proxy_pool: &[String],
    cli: &Cli,
    board: JobBoard,
    dashboard: DashboardManager,
    lang: Lang,
    precomputed: Option<PreflightOutcome>,
) -> Result<()> {
    // Chỉ nhận JSON đúng shape của https://chatgpt.com/api/auth/session.
    let session_json: Value = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(_) => {
            tg.send_message(chat_id, &i18n::invalid_session_json(lang), Some(reply_to))
                .await
                .ok();
            return Ok(());
        }
    };
    if !is_auth_session_json(&session_json) {
        tg.send_message(chat_id, &i18n::invalid_session_json(lang), Some(reply_to))
            .await
            .ok();
        return Ok(());
    }
    let access_token = session_json
        .get("accessToken")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    // Session BẮT BUỘC có email thật (user.email) — đây là khóa định danh để
    // chống trùng tài khoản. Không fallback "unknown@unknown" (sẽ làm dedupe sai
    // và job vô danh). Thiếu/email sai định dạng → từ chối, báo user.
    let email = match session_json
        .get("user")
        .and_then(|u| u.get("email"))
        .and_then(|e| e.as_str())
        .map(|s| s.trim())
        .filter(|s| !s.is_empty() && s.contains('@'))
    {
        Some(e) => e.to_string(),
        None => {
            tg.send_message(chat_id, &i18n::session_no_email(lang), Some(reply_to))
                .await
                .ok();
            return Ok(());
        }
    };
    let cookie_header = build_cookie_header(&session_json);

    enqueue_and_track(
        tg,
        queue,
        limiter,
        registry,
        store,
        chat_id,
        reply_to,
        user_id,
        username,
        email,
        AuthSource::Session {
            access_token,
            cookie_header,
        },
        proxy_pool,
        cli,
        board,
        dashboard,
        lang,
        precomputed,
    )
    .await
}

/// Nhận combo `email|password|2fa` → login HTTP ở step [1/6] rồi chạy UPI.
#[allow(clippy::too_many_arguments)]
async fn process_account_combo(
    tg: Arc<TelegramClient>,
    queue: Arc<JobQueue>,
    limiter: UserLimiter,
    registry: JobRegistry,
    store: Arc<settings::Settings>,
    chat_id: i64,
    reply_to: i64,
    user_id: i64,
    username: Option<String>,
    combo: AccountCombo,
    proxy_pool: &[String],
    cli: &Cli,
    board: JobBoard,
    dashboard: DashboardManager,
    lang: Lang,
    precomputed: Option<PreflightOutcome>,
) -> Result<()> {
    enqueue_and_track(
        tg,
        queue,
        limiter,
        registry,
        store,
        chat_id,
        reply_to,
        user_id,
        username,
        combo.email,
        AuthSource::Login {
            password: combo.password,
            totp_secret: combo.totp_secret,
        },
        proxy_pool,
        cli,
        board,
        dashboard,
        lang,
        precomputed,
    )
    .await
}

/// Giờ VN dạng HH:MM:SS cho nhãn cập nhật của board.
fn now_hms() -> String {
    let vn = chrono::FixedOffset::east_opt(7 * 3600).unwrap();
    chrono::Utc::now()
        .with_timezone(&vn)
        .format("%H:%M:%S")
        .to_string()
}

/// Tách secret TOTP từ input của `/2fa`: nếu là combo `email|pass|secret` →
/// lấy field thứ 3; ngược lại coi cả chuỗi là secret. Normalize + validate
/// base32 (qua `totp::normalize_secret`). Trả secret đã chuẩn hoá hoặc Err.
fn extract_totp_secret(input: &str) -> Result<String> {
    let raw = if input.contains('|') {
        input.split('|').nth(2).map(|s| s.trim()).unwrap_or("")
    } else {
        input.trim()
    };
    crate::auth::totp::normalize_secret(raw)
}

/// Build (html, keyboard) cho card 2FA từ secret đã normalize. Nút 🔄 mang
/// theo secret trong `callback_data` (giới hạn 64 byte Telegram) để regenerate
/// khi bấm — secret quá dài thì bỏ nút (user gõ lại /2fa).
fn build_2fa_view(secret: &str, lang: Lang) -> (String, Value) {
    let (code, secs_left) = crate::auth::totp::now_code_with_ttl(secret)
        .unwrap_or_else(|_| ("------".to_string(), 0));
    let html = i18n::twofa_card(lang, &code, secs_left, &now_hms());
    let cb = format!("2fa:{}", secret);
    let kb = if cb.len() <= 64 {
        serde_json::json!([[{
            "text": i18n::btn_2fa_reload(lang),
            "callback_data": cb,
        }]])
    } else {
        Value::Array(vec![])
    };
    (html, kb)
}

/// `/2fa <secret | email|pass|secret>` — sinh mã TOTP hiện tại + đếm ngược +
/// nút 🔄 lấy mã mới. Mọi user dùng được (không cần whitelist riêng).
async fn handle_2fa(tg: &Arc<TelegramClient>, msg: &Message, text: &str, lang: Lang) {
    let Some((arg, _)) = command_body(text) else {
        tg.send_message_kb_html(msg.chat.id, &i18n::twofa_usage(lang), Value::Array(vec![]))
            .await
            .ok();
        return;
    };
    match extract_totp_secret(arg) {
        Ok(secret) => {
            let (html, kb) = build_2fa_view(&secret, lang);
            tg.send_message_kb_html(msg.chat.id, &html, kb).await.ok();
        }
        Err(e) => {
            tg.send_message_kb_html(
                msg.chat.id,
                &i18n::twofa_invalid(lang, &e.to_string()),
                Value::Array(vec![]),
            )
            .await
            .ok();
        }
    }
}

/// Đích nhận thông báo "QR success" / "tiến trình mới" / "user set proxy".
/// Ưu tiên `notify.chat_id` admin tự cấu hình (có thể là supergroup/topic),
/// fallback về `--admin-chat-id` (DM admin) khi chưa set. None = tắt thông báo.
fn resolve_notify_target(
    store: &settings::Settings,
    admin_chat_id: i64,
) -> Option<(i64, Option<i64>)> {
    if let Some(t) = store.get_notify_target() {
        return Some(t);
    }
    if admin_chat_id != 0 {
        return Some((admin_chat_id, None));
    }
    None
}

/// Parse arg cho `/set_notify`. Hỗ trợ:
///   - `<chat_id>`              → (chat_id, None)
///   - `<chat_id> <thread_id>`  → (chat_id, Some(thread_id))
///   - link Telegram supergroup `https://t.me/c/<gid>/<tid>[/<msg>]` →
///     `(-100<gid>, Some(<tid>))`
///   - link Telegram supergroup `https://t.me/c/<gid>/<msg>` (không topic) →
///     `(-100<gid>, None)` — heuristic: nếu chỉ có 1 số sau `/c/<gid>/` thì
///     coi là message_id (gửi root chat).
fn parse_notify_arg(arg: &str) -> Option<(i64, Option<i64>)> {
    let arg = arg.trim();
    if arg.is_empty() {
        return None;
    }
    // Link supergroup `/c/<gid>/<tid>/<msg>` hoặc `/c/<gid>/<tid>`.
    if let Some(rest) = arg
        .strip_prefix("https://t.me/c/")
        .or_else(|| arg.strip_prefix("http://t.me/c/"))
        .or_else(|| arg.strip_prefix("t.me/c/"))
    {
        let parts: Vec<&str> = rest
            .split(['/', '?', '#'])
            .filter(|p| !p.is_empty())
            .collect();
        let gid: i64 = parts.first()?.parse().ok()?;
        // chat_id của supergroup theo Bot API = -100<gid>.
        let chat_id: i64 = format!("-100{}", gid).parse().ok()?;
        let thread = match parts.len() {
            1 => None,
            // chỉ có 1 số sau gid → message_id, không phải topic.
            2 => None,
            // gid/tid/msg → tid là topic.
            _ => parts.get(1).and_then(|s| s.parse::<i64>().ok()).filter(|t| *t > 0),
        };
        return Some((chat_id, thread));
    }
    // 2 số: `<chat_id> <thread_id>`.
    let toks: Vec<&str> = arg.split_whitespace().collect();
    let chat_id: i64 = toks.first()?.parse().ok()?;
    if chat_id == 0 {
        return None;
    }
    let thread_id = toks
        .get(1)
        .and_then(|s| s.parse::<i64>().ok())
        .filter(|t| *t > 0);
    Some((chat_id, thread_id))
}

/// `/set_notify` (admin) — đặt kênh nhận thông báo "QR success".
/// Usage:
///   /set_notify                           → show trạng thái + hướng dẫn
///   /set_notify <chat_id>                 → root chat (group/DM)
///   /set_notify <chat_id> <thread_id>     → topic trong forum-supergroup
///   /set_notify https://t.me/c/<gid>/<tid>[/<msg>]  → parse từ link
async fn handle_set_notify(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    msg: &Message,
    text: &str,
) {
    let admin_id = msg.from.as_ref().map(|u| u.id).unwrap_or(0);
    let lang = lang_or_default(store, admin_id);
    let arg = text
        .strip_prefix("/set_notify")
        .map(|s| s.trim())
        .unwrap_or("");
    if arg.is_empty() {
        let cur = store.get_notify_target();
        let body = match cur {
            Some((cid, Some(tid))) => i18n::admin_notify_show_with_topic(lang, cid, tid),
            Some((cid, None)) => i18n::admin_notify_show_root(lang, cid),
            None => i18n::admin_notify_show_unset(lang),
        };
        tg.send_message(msg.chat.id, &body, Some(msg.message_id))
            .await
            .ok();
        return;
    }

    let Some((chat_id, thread_id)) = parse_notify_arg(arg) else {
        tg.send_message(
            msg.chat.id,
            &i18n::admin_notify_set_bad_format(lang),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    };

    if let Err(e) = store.set_notify_target(chat_id, thread_id) {
        tg.send_message(
            msg.chat.id,
            &i18n::db_error(lang, &e.to_string()),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    }

    // Test luôn — chứng minh bot có quyền gửi vào target đó.
    let probe = i18n::admin_notify_set_probe_text(lang, chat_id, thread_id);
    let test_result = tg.send_message_to_thread(chat_id, &probe, thread_id).await;
    let body = match test_result {
        Ok(_) => i18n::admin_notify_set_ok(lang, chat_id, thread_id),
        Err(e) => i18n::admin_notify_set_fail(lang, chat_id, thread_id, &e.to_string()),
    };
    tg.send_message(msg.chat.id, &body, Some(msg.message_id))
        .await
        .ok();
}

/// `/notify_remove` (admin) — xóa notify target, fallback về ADMIN_CHAT_ID.
async fn handle_notify_remove(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    msg: &Message,
) {
    let admin_id = msg.from.as_ref().map(|u| u.id).unwrap_or(0);
    let lang = lang_or_default(store, admin_id);
    let body = match store.remove_notify_target() {
        Ok(true) => i18n::admin_notify_remove_ok(lang),
        Ok(false) => i18n::admin_notify_remove_none(lang),
        Err(_) => i18n::admin_notify_remove_db_err(lang),
    };
    tg.send_message(msg.chat.id, &body, Some(msg.message_id))
        .await
        .ok();
}

/// `/notify_test` (admin) — gửi 1 test message vào notify target hiện tại.
async fn handle_notify_test(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    msg: &Message,
    admin_chat_id: i64,
) {
    let admin_id = msg.from.as_ref().map(|u| u.id).unwrap_or(0);
    let lang = lang_or_default(store, admin_id);
    let target = resolve_notify_target(store, admin_chat_id);
    let Some((chat_id, thread_id)) = target else {
        tg.send_message(
            msg.chat.id,
            &i18n::admin_notify_test_unset(lang),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    };
    let body = i18n::admin_notify_test_body(lang, chat_id, thread_id, &now_hms());
    let res = tg.send_message_to_thread(chat_id, &body, thread_id).await;
    let report = match res {
        Ok(_) => i18n::admin_notify_test_ok(lang, chat_id, thread_id),
        Err(e) => i18n::admin_notify_test_fail(lang, &e.to_string()),
    };
    tg.send_message(msg.chat.id, &report, Some(msg.message_id))
        .await
        .ok();
}

/// `/stopall` (admin) — cancel MỌI job của MỌI user. Slot limiter tự trả qua
/// `on_done` khi worker xử lý xong các job đã cancel.
async fn handle_stopall(
    tg: &Arc<TelegramClient>,
    registry: &JobRegistry,
    limiter: &UserLimiter,
    store: &Arc<settings::Settings>,
    chat_id: i64,
) {
    let n = registry.stop_everyone().await;
    limiter.force_reset_everyone().await;
    let lang = admin_lang(store, chat_id);
    tg.send_message(chat_id, &i18n::admin_stopall_done(lang, n), None)
        .await
        .ok();
}

/// `/flushall` (admin) — reset toàn bộ trạng thái tạm: cancel mọi job, xóa
/// board entries. Force reset limiter để chống lệch counter.
async fn handle_flushall(
    tg: &Arc<TelegramClient>,
    registry: &JobRegistry,
    limiter: &UserLimiter,
    board: &JobBoard,
    store: &Arc<settings::Settings>,
    chat_id: i64,
) {
    let jobs = registry.stop_everyone().await;
    limiter.force_reset_everyone().await;
    let cards = board.clear_all().await;
    let lang = admin_lang(store, chat_id);
    tg.send_message(
        chat_id,
        &i18n::admin_flushall_done(lang, jobs, cards),
        None,
    )
    .await
    .ok();
}

/// Số nút Stop tối đa render trên 1 board (chống vượt giới hạn inline keyboard
/// của Telegram). Danh sách text vẫn liệt kê đủ; nút chỉ cho N process đầu.
const BOARD_STOP_BTN_CAP: usize = 25;

/// Render board thành Rich HTML (Bot API 10.1+) — dùng `<h3>` heading,
/// `<table bordered striped>` với cột thẳng hàng, `<details>` cho ghi chú phụ.
/// Cột "Bước hiện tại" CHỈ hiển thị label thân thiện từ `StepKind` —
/// KHÔNG render log thô (http=200, [5b], proxy=...) để tránh lộ logic nội bộ.
///
/// `scope_all` = true: admin xem toàn hệ thống (có thêm cột "Chủ"); false:
/// chỉ tiến trình của viewer (giấu cột chủ).
fn render_board_rich_html(entries: &[(u64, JobStatus)], scope_all: bool, lang: Lang) -> String {
    use crate::bot::board::{html_escape, JobState};
    let running = entries
        .iter()
        .filter(|(_, s)| s.state == JobState::Running)
        .count();
    let queued = entries.len() - running;

    let (heading, run_label, queue_label, empty_msg, footer_hint) = match (scope_all, lang) {
        (true, Lang::Vi) => (
            "📊 Bảng tiến trình — toàn hệ thống",
            "đang chạy",
            "đang chờ",
            "Hiện chưa có tiến trình nào.",
            "Bấm 🔄 để cập nhật bảng. Bấm nút 🛑 để dừng tiến trình tương ứng.",
        ),
        (true, Lang::En) => (
            "📊 Process board — system-wide",
            "running",
            "queued",
            "No processes right now.",
            "Tap 🔄 to refresh. Tap 🛑 to stop a process.",
        ),
        (false, Lang::Vi) => (
            "📊 Tiến trình của bạn",
            "đang chạy",
            "đang chờ",
            "Bạn chưa có tiến trình nào.",
            "Bấm 🔄 để cập nhật bảng. Bấm nút 🛑 để dừng tiến trình của bạn.",
        ),
        (false, Lang::En) => (
            "📊 Your processes",
            "running",
            "queued",
            "You have no processes.",
            "Tap 🔄 to refresh. Tap 🛑 to stop a process.",
        ),
    };

    let mut out = String::with_capacity(512 + entries.len() * 220);
    out.push_str("<h3>");
    out.push_str(heading);
    out.push_str("</h3>");
    out.push_str(&format!(
        "<p><i>▶️ <b>{}</b> {} · ⏳ <b>{}</b> {} · 🕒 {}</i></p>",
        running,
        run_label,
        queued,
        queue_label,
        html_escape(&now_hms())
    ));

    if entries.is_empty() {
        out.push_str(&format!("<p><i>{}</i></p>", empty_msg));
        return out;
    }

    let (h_no, h_st, h_age, h_owner, h_email, h_step) = match lang {
        Lang::Vi => ("#", "Trạng thái", "Tuổi", "Chủ", "Email", "Bước hiện tại"),
        Lang::En => ("#", "State", "Age", "Owner", "Email", "Current step"),
    };

    out.push_str("<table bordered striped>");
    if scope_all {
        out.push_str(&format!(
            "<tr><th>{}</th><th>{}</th><th>{}</th><th>{}</th><th>{}</th><th>{}</th></tr>",
            h_no, h_st, h_age, h_owner, h_email, h_step
        ));
    } else {
        out.push_str(&format!(
            "<tr><th>{}</th><th>{}</th><th>{}</th><th>{}</th><th>{}</th></tr>",
            h_no, h_st, h_age, h_email, h_step
        ));
    }

    for (i, (_, s)) in entries.iter().enumerate() {
        let age = crate::bot::board::fmt_age(s.since.elapsed().as_secs());
        let st_label = s.state.label(lang);
        let email_html = html_escape(&s.email_masked);
        let step_html = html_escape(&s.step.label(lang));
        let age_html = html_escape(&age);

        if scope_all {
            let owner_text = match s.username.as_deref() {
                Some(u) if !u.is_empty() => format!("@{}", u),
                _ => format!("id{}", s.user_id),
            };
            out.push_str(&format!(
                "<tr><td>{}</td><td>{}</td><td><code>{}</code></td>\
                 <td>{}</td><td><code>{}</code></td><td>{}</td></tr>",
                i + 1,
                st_label,
                age_html,
                html_escape(&owner_text),
                email_html,
                step_html
            ));
        } else {
            out.push_str(&format!(
                "<tr><td>{}</td><td>{}</td><td><code>{}</code></td>\
                 <td><code>{}</code></td><td>{}</td></tr>",
                i + 1,
                st_label,
                age_html,
                email_html,
                step_html
            ));
        }
    }
    out.push_str("</table>");
    out.push_str(&format!("<p><i>{}</i></p>", footer_hint));
    out
}

/// Keyboard board: 1 nút Stop / process (cap) + nút Refresh.
fn board_keyboard(entries: &[(u64, JobStatus)], lang: Lang) -> Value {
    let mut rows: Vec<Value> = Vec::new();
    for (job_id, s) in entries.iter().take(BOARD_STOP_BTN_CAP) {
        rows.push(serde_json::json!([{
            "text": i18n::btn_board_stop(lang, &s.email_masked),
            "callback_data": format!("bstop:{}", job_id),
        }]));
    }
    rows.push(serde_json::json!([{
        "text": i18n::btn_board_refresh(lang),
        "callback_data": "board:refresh",
    }]));
    Value::Array(rows)
}

/// Lấy snapshot (admin = tất cả, user = riêng) rồi build (rich_html, keyboard).
/// Filter theo `viewer` khi không phải admin → KHÔNG lộ tiến trình người khác.
async fn build_board_view(
    board: &JobBoard,
    viewer: i64,
    is_admin: bool,
    lang: Lang,
) -> (String, Value) {
    let entries = if is_admin {
        board.snapshot_entries().await
    } else {
        board.snapshot_entries_for_user(viewer).await
    };
    let html = render_board_rich_html(&entries, is_admin, lang);
    let kb = board_keyboard(&entries, lang);
    (html, kb)
}

/// `/board` — bảng tiến trình kèm nút Stop. Admin xem toàn hệ thống; user
/// thường CHỈ thấy tiến trình của chính mình (bảo mật). Snapshot tĩnh + nút
/// 🔄 Refresh để cập nhật. Render Rich HTML (Bot API 10.1+) — `<table>` thật.
async fn handle_board(
    tg: &Arc<TelegramClient>,
    board: &JobBoard,
    chat_id: i64,
    viewer: i64,
    is_admin: bool,
    lang: Lang,
) {
    let (html, kb) = build_board_view(board, viewer, is_admin, lang).await;
    if let Err(e) = tg.send_rich_message_kb(chat_id, &html, kb).await {
        tracing::warn!("send board (rich) fail: {}", e);
    }
}

fn build_cookie_header(session_json: &Value) -> String {
    let mut pairs: Vec<String> = Vec::new();
    let cookies_v = session_json
        .get("__cookies")
        .or_else(|| session_json.get("cookies"));
    if let Some(arr) = cookies_v.and_then(|v| v.as_array()) {
        for c in arr {
            let Some(name) = c.get("name").and_then(|v| v.as_str()) else { continue };
            let Some(value) = c.get("value").and_then(|v| v.as_str()) else { continue };
            let domain = c
                .get("domain")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim_start_matches('.')
                .to_lowercase();
            if !domain.is_empty() && !domain.contains("chatgpt.com") && !domain.contains("openai.com") {
                continue;
            }
            pairs.push(format!("{}={}", name, value));
        }
    } else if let Some(obj) = cookies_v.and_then(|v| v.as_object()) {
        for (k, v) in obj {
            if let Some(s) = v.as_str() {
                pairs.push(format!("{}={}", k, s));
            }
        }
    }
    pairs.join("; ")
}

fn format_expires(ts_opt: Option<i64>) -> String {
    let Some(ts) = ts_opt else {
        return "—".into();
    };
    let Some(utc) = chrono::DateTime::<chrono::Utc>::from_timestamp(ts, 0) else {
        return ts.to_string();
    };
    let vn_offset = chrono::FixedOffset::east_opt(7 * 3600).unwrap();
    let vn = utc.with_timezone(&vn_offset);
    let now = chrono::Utc::now();
    let delta = utc - now;
    let suffix = if delta.num_seconds() < 0 {
        format!(" · expired {}m ago", -delta.num_minutes())
    } else if delta.num_minutes() < 60 {
        format!(" · in {}m", delta.num_minutes().max(0))
    } else {
        format!(
            " · in {}h{:02}m",
            delta.num_hours(),
            (delta.num_minutes() % 60).abs()
        )
    };
    format!("{} (UTC+7){}", vn.format("%d/%m/%Y %H:%M"), suffix)
}

async fn run_stripe_probe(client: Arc<HttpClient>, cache_dir: PathBuf) -> Result<()> {
    let cache = stripe::bundles::BundleCache::new(cache_dir);
    println!("→ fetch_bundles_live ...");
    let started = std::time::Instant::now();
    let cfg = stripe::bundles::extract_config_live(&client, &cache).await?;
    let elapsed = started.elapsed().as_secs_f64();
    println!(
        "✓ extract_config_live OK ({:.2}s)\n  shift = {}\n  rv_ts = {}\n  rv    = {} (len={})\n  sv    = {} (len={})\n  bundle_hash = {}…",
        elapsed,
        cfg.shift,
        cfg.rv_ts,
        &cfg.rv[..cfg.rv.len().min(16)],
        cfg.rv.len(),
        &cfg.sv[..cfg.sv.len().min(16)],
        cfg.sv.len(),
        &cfg.bundle_hash[..16],
    );
    // Test compute với dummy ppage_id
    let js = stripe_token::compute_js_checksum("test_ppage_id_abc", cfg.shift);
    let rv_ts = stripe_token::compute_rv_timestamp(&cfg);
    println!(
        "  js_checksum(test_ppage_id_abc) = {}\n  rv_timestamp                  = {}",
        js, rv_ts
    );
    Ok(())
}

async fn run_once(
    client: Arc<HttpClient>,
    cli: &Cli,
    proxy_pool: &[String],
    session_json: Option<&std::path::Path>,
    combo: &str,
    qr_out: &PathBuf,
) -> Result<()> {
    // Build (email, auth) từ combo email|pass|2fa HOẶC session.json.
    let (email, auth) = if !combo.trim().is_empty() {
        let parts: Vec<&str> = combo.split('|').collect();
        if parts.len() < 3 {
            return Err(anyhow!("combo phải là email|password|totp_secret"));
        }
        let (e, p, s) = (parts[0].trim(), parts[1].trim(), parts[2].trim());
        if e.is_empty() || p.is_empty() || s.is_empty() {
            return Err(anyhow!("combo có field rỗng"));
        }
        (
            e.to_string(),
            AuthSource::Login {
                password: p.to_string(),
                totp_secret: s.to_string(),
            },
        )
    } else {
        let path = session_json.ok_or_else(|| anyhow!("cần --session-json hoặc --combo"))?;
        let raw = std::fs::read(path).context("failed to read session.json")?;
        let v: Value = serde_json::from_slice(&raw).context("session.json is not valid JSON")?;
        let access_token = v
            .get("accessToken")
            .and_then(|x| x.as_str())
            .ok_or_else(|| anyhow!("session.json missing accessToken"))?
            .to_string();
        let email = v
            .get("user")
            .and_then(|u| u.get("email"))
            .and_then(|e| e.as_str())
            .unwrap_or("unknown@unknown")
            .to_string();
        let cookie_header = build_cookie_header(&v);
        (
            email,
            AuthSource::Session {
                access_token,
                cookie_header,
            },
        )
    };

    let job = UpiJobConfig {
        email,
        auth,
        proxy_pool: proxy_pool.to_vec(),
        login_proxy: None,
        approve_retries: cli.approve_retries,
        approve_delay_ms: Some(cli.approve_delay_secs.saturating_mul(1000)),
        restart_threshold: cli.restart_threshold,
        max_restarts: cli.max_restarts,
        proxy_from_step: cli.proxy_from_step,
        qr_out_path: qr_out.clone(),
        bundles_cache_dir: cli.bundles_cache_dir.clone(),
        qr_watermark: cli.qr_watermark.clone(),
    };
    let log: crate::upi::runner::LogFn = Arc::new(|line: &str| {
        println!("{}", line);
    });
    let result = crate::upi::runner::run_upi_qr(client, job, log).await;
    println!("\n=== RESULT ===");
    println!("{}", serde_json::to_string_pretty(&result)?);
    Ok(())
}

/// Test login HTTP từ combo `email|password|2fa` → in kết quả (không in token).
async fn run_login_once(combo: &str, proxy: &str) -> Result<()> {
    let parts: Vec<&str> = combo.split('|').collect();
    if parts.len() < 3 {
        return Err(anyhow!("combo phải là email|password|totp_secret"));
    }
    let email = parts[0].trim();
    let password = parts[1].trim();
    let secret = parts[2].trim();
    if email.is_empty() || password.is_empty() || secret.is_empty() {
        return Err(anyhow!("combo có field rỗng"));
    }
    let proxy_opt = if proxy.trim().is_empty() {
        None
    } else {
        Some(proxy.trim())
    };

    let log: crate::upi::runner::LogFn = Arc::new(|line: &str| println!("{}", line));
    match crate::auth::login_pure_request(email, password, secret, proxy_opt, &log).await {
        Ok(sess) => {
            println!("\n=== LOGIN OK ===");
            println!(
                "accessToken_len={} cookie_count={} cookie_header_len={}",
                sess.access_token.len(),
                sess.cookie_count,
                sess.cookie_header.len()
            );
            Ok(())
        }
        Err(e) => {
            println!("\n=== LOGIN FAIL ===\n{}", e);
            Err(anyhow!("login failed"))
        }
    }
}


/// Welcome message with inline keyboard (localized).
async fn send_welcome(tg: &Arc<TelegramClient>, chat_id: i64, reply_to: i64, lang: Lang) {
    let info = i18n::welcome(lang);
    let kb = serde_json::json!([
        [
            {"text": i18n::btn_status(lang), "callback_data": "cmd:status"},
            {"text": i18n::btn_stop_all(lang), "callback_data": "cmd:stop"}
        ],
        [
            {"text": i18n::btn_settings(lang), "callback_data": "cmd:settings"},
            {"text": i18n::btn_help(lang), "callback_data": "cmd:help"}
        ],
        [
            {"text": "💬 Contact", "url": "https://t.me/prr9293"},
            {"text": "👥 Join Group", "url": "https://t.me/+QOsyt6bh5341YWM9"}
        ]
    ]);
    if let Err(e) = tg.send_message_kb(chat_id, &info, Some(reply_to), kb).await {
        tracing::warn!("send_welcome fail: {}", e);
        tg.send_message(chat_id, &info, Some(reply_to)).await.ok();
    }
}

/// Inline button click handler. Maps `cmd:<action>` → bot action for clicker.
async fn handle_callback(
    tg: Arc<TelegramClient>,
    registry: JobRegistry,
    limiter: UserLimiter,
    store: Arc<settings::Settings>,
    cb: CallbackQuery,
    allowed: &HashSet<i64>,
    admin_chat_id: i64,
    board: JobBoard,
    dashboard: DashboardManager,
) -> Result<()> {
    let user_id = cb.from.id;
    let is_admin = admin_chat_id != 0 && user_id == admin_chat_id;
    let lang = lang_or_default(&store, user_id);

    if !is_admin {
        if let Ok(true) = store.is_banned(user_id) {
            tg.answer_callback_query(&cb.id, Some(&i18n::toast_blocked(lang)))
                .await
                .ok();
            return Ok(());
        }
    }

    if !allowed.is_empty() && !allowed.contains(&user_id) {
        tg.answer_callback_query(&cb.id, Some(&i18n::toast_not_whitelisted(lang)))
            .await
            .ok();
        return Ok(());
    }

    let chat_id = cb.message.as_ref().map(|m| m.chat.id).unwrap_or(0);
    let data = cb.data.clone().unwrap_or_default();

    // Chọn ngôn ngữ (có thể xảy ra trước khi user có lang).
    if let Some(code) = data.strip_prefix("setlang:") {
        if let Some(l) = Lang::from_code(code) {
            store.set_user_lang(user_id, l.code()).ok();
            tg.answer_callback_query(&cb.id, Some(&i18n::language_set(l))).await.ok();
            // Đặt menu lệnh localized cho riêng chat này (đầy đủ + admin nếu có).
            let cmds = localized_commands(l, is_admin);
            tg.set_my_commands_for_chat(chat_id, &cmds).await.ok();
            send_welcome(&tg, chat_id, 0, l).await;
        } else {
            tg.answer_callback_query(&cb.id, None).await.ok();
        }
        return Ok(());
    }

    // Reload mã 2FA — callback `2fa:<secret>`. Regenerate mã + đếm ngược, edit
    // tại chỗ. Secret nằm trong callback_data (≤64 byte) nên không cần state.
    if let Some(secret) = data.strip_prefix("2fa:") {
        let message_id = cb.message.as_ref().map(|m| m.message_id).unwrap_or(0);
        match crate::auth::totp::normalize_secret(secret) {
            Ok(norm) => {
                let (html, kb) = build_2fa_view(&norm, lang);
                if message_id != 0 {
                    tg.edit_message_kb_html(chat_id, message_id, &html, kb).await.ok();
                }
                tg.answer_callback_query(&cb.id, Some(&i18n::toast_2fa_reloaded(lang)))
                    .await
                    .ok();
            }
            Err(_) => {
                tg.answer_callback_query(&cb.id, Some(&i18n::toast_2fa_expired(lang)))
                    .await
                    .ok();
            }
        }
        return Ok(());
    }

    // Dashboard: nút Stop 1 process (callback `dstop:<job_id>`). Admin stop job
    // bất kỳ; user thường CHỈ job của mình (`stop_job` verify ownership). Sau
    // khi stop → đánh dirty dashboard của chủ job để ticker render lại (bỏ dòng).
    if let Some(id_str) = data.strip_prefix("dstop:") {
        if let Ok(job_id) = id_str.parse::<u64>() {
            let owner = board.owner_of(job_id).await;
            let ok = if is_admin {
                registry.stop_job_any(job_id).await
            } else {
                registry.stop_job(user_id, job_id).await
            };
            if ok {
                if let Some(uid) = owner {
                    limiter.release(uid).await;
                    dashboard.mark_dirty(uid).await;
                }
            }
            tg.answer_callback_query(&cb.id, Some(&i18n::board_stopped_toast(lang, ok)))
                .await
                .ok();
        } else {
            tg.answer_callback_query(&cb.id, None).await.ok();
        }
        return Ok(());
    }

    // Board: làm mới snapshot (admin = toàn hệ thống; user = riêng).
    if data == "board:refresh" {
        let message_id = cb.message.as_ref().map(|m| m.message_id).unwrap_or(0);
        if message_id != 0 {
            let (html, kb) = build_board_view(&board, user_id, is_admin, lang).await;
            tg.edit_rich_message_kb(chat_id, message_id, &html, kb).await.ok();
        }
        tg.answer_callback_query(&cb.id, None).await.ok();
        return Ok(());
    }

    // Board: nút Stop 1 process. Admin dừng được job bất kỳ; user thường CHỈ
    // dừng job của mình — `stop_job` verify ownership theo user_id nên user
    // KHÔNG thể dừng job người khác kể cả khi forge job_id (bảo mật).
    if let Some(id_str) = data.strip_prefix("bstop:") {
        if let Ok(job_id) = id_str.parse::<u64>() {
            // Lấy chủ sở hữu job để release slot đúng user (admin có thể stop
            // job của user khác — slot phải trả về user đó, không phải admin).
            let owner = board.owner_of(job_id).await;
            let ok = if is_admin {
                registry.stop_job_any(job_id).await
            } else {
                registry.stop_job(user_id, job_id).await
            };
            if ok {
                if let Some(uid) = owner {
                    // Force release 1 slot cho user owner — diệt khe race
                    // giữa cancel_token và worker mark_done.
                    limiter.release(uid).await;
                }
            }
            tg.answer_callback_query(&cb.id, Some(&i18n::board_stopped_toast(lang, ok)))
                .await
                .ok();
            let message_id = cb.message.as_ref().map(|m| m.message_id).unwrap_or(0);
            if message_id != 0 {
                let (html, kb) = build_board_view(&board, user_id, is_admin, lang).await;
                tg.edit_rich_message_kb(chat_id, message_id, &html, kb).await.ok();
            }
        } else {
            tg.answer_callback_query(&cb.id, None).await.ok();
        }
        return Ok(());
    }

    match data.as_str() {
        "cmd:status" => {
            tg.answer_callback_query(&cb.id, None).await.ok();
            tg.send_message(chat_id, &i18n::status_online(lang), None).await.ok();
        }
        "cmd:stop" => {
            let n = registry.stop_user(user_id).await;
            limiter.force_reset_user(user_id).await;
            let body = i18n::stopped_all(lang, n);
            tg.answer_callback_query(&cb.id, Some(&body)).await.ok();
            tg.send_message(chat_id, &body, None).await.ok();
        }
        "cmd:settings" => {
            tg.answer_callback_query(&cb.id, None).await.ok();
            tg.send_message_kb(chat_id, &i18n::settings_title(lang), None, settings_keyboard(lang))
                .await
                .ok();
        }
        "cmd:language" => {
            tg.answer_callback_query(&cb.id, None).await.ok();
            tg.send_message_kb(chat_id, i18n::choose_language(), None, language_keyboard())
                .await
                .ok();
        }
        "cmd:cancel" => {
            // Không còn buffer text để clear sau khi bỏ session_buffer.
            // Giữ ack để callback button vẫn responsive.
            tg.answer_callback_query(&cb.id, None).await.ok();
        }
        "cmd:help" => {
            tg.answer_callback_query(&cb.id, None).await.ok();
            send_help(&tg, &store, chat_id, 0, user_id, is_admin, lang).await;
        }
        "proxy:check" => {
            // Probe live status pool proxy của chính user — không cho user
            // khác probe proxy của ai khác. Probe SONG SONG mọi line, render
            // gọn từng dòng.
            tg.answer_callback_query(&cb.id, Some(&i18n::toast_probing(lang))).await.ok();
            let lines = match store.get_user_proxies(user_id) {
                Ok(v) if !v.is_empty() => v,
                Ok(_) => {
                    tg.send_message(chat_id, &i18n::proxy_not_set(lang), None).await.ok();
                    return Ok(());
                }
                Err(e) => {
                    tg.send_message(chat_id, &i18n::db_error(lang, &e.to_string()), None).await.ok();
                    return Ok(());
                }
            };
            // Refresh tất cả (bypass cache 5') — user bấm Check thường muốn số mới.
            let probes = lines.iter().map(|raw| {
                let raw = raw.clone();
                async move { bot::proxy_status::PROXY_STATUS.refresh(&raw).await }
            });
            let results = futures_util::future::join_all(probes).await;
            let body = render_pool_probe_result(lang, &lines, &results, true);
            tg.send_message_kb_html(chat_id, &body, proxy_keyboard(lang))
                .await
                .ok();
        }
        "proxy:remove" => {
            match store.remove_user_proxy(user_id) {
                Ok(true) => {
                    tg.answer_callback_query(&cb.id, Some(&i18n::toast_removed(lang))).await.ok();
                    tg.send_message(chat_id, &i18n::proxy_removed_direct(lang), None)
                        .await
                        .ok();
                }
                Ok(false) => {
                    tg.answer_callback_query(&cb.id, Some(&i18n::toast_nothing_to_remove(lang))).await.ok();
                    tg.send_message(chat_id, &i18n::proxy_not_set(lang), None).await.ok();
                }
                Err(e) => {
                    tg.answer_callback_query(&cb.id, Some(&i18n::toast_db_error(lang))).await.ok();
                    tg.send_message(chat_id, &i18n::proxy_remove_failed(lang, &e.to_string()), None).await.ok();
                }
            }
        }
        _ => {
            tg.answer_callback_query(&cb.id, Some(&i18n::toast_unknown_action(lang)))
                .await
                .ok();
        }
    }
    Ok(())
}

// ── Admin command helpers ──────────────────────────────────────────────

/// Tách phần nội dung sau token lệnh. Trả `(body, body_start_byte)` với `body`
/// đã trim khoảng trắng 2 đầu. None nếu lệnh không có nội dung.
/// `body_start_byte` = vị trí byte trong `text` nơi body bắt đầu (trước trim
/// phải) — dùng để tính offset entity cần dịch.
fn command_body(text: &str) -> Option<(&str, usize)> {
    let ws = text.char_indices().find(|(_, c)| c.is_whitespace())?.0;
    let rest = &text[ws..];
    let body_rel = rest.char_indices().find(|(_, c)| !c.is_whitespace())?.0;
    let start = ws + body_rel;
    let body = text[start..].trim_end();
    if body.is_empty() {
        return None;
    }
    Some((body, start))
}

/// Dịch entities của message admin về body sau khi cắt prefix lệnh. Offset/length
/// theo UTF-16 code unit (chuẩn Telegram). Entity nằm hẳn trong prefix bị bỏ;
/// entity vắt qua ranh giới bị clip cho khớp body.
fn shift_entities(entities: &[Value], prefix_utf16: usize, body_utf16: usize) -> Vec<Value> {
    let p = prefix_utf16 as i64;
    let blen = body_utf16 as i64;
    let mut out = Vec::new();
    for e in entities {
        let off = e.get("offset").and_then(|v| v.as_i64()).unwrap_or(0);
        let len = e.get("length").and_then(|v| v.as_i64()).unwrap_or(0);
        let nstart = (off - p).max(0);
        let nend = (off + len - p).min(blen);
        if nend <= nstart {
            continue;
        }
        let mut ne = e.clone();
        if let Some(obj) = ne.as_object_mut() {
            obj.insert("offset".into(), serde_json::json!(nstart));
            obj.insert("length".into(), serde_json::json!(nend - nstart));
        }
        out.push(ne);
    }
    out
}

/// `/notify <nội dung>` — broadcast tới mọi user (trừ user bị ban), giữ nguyên
/// xuống dòng + format chữ admin đã gõ. Prune user đã block bot.
async fn handle_notify(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    msg: &Message,
    text: &str,
) {
    let Some((body, body_start)) = command_body(text) else {
        tg.send_message(
            msg.chat.id,
            &i18n::admin_notify_broadcast_usage(lang_or_default(store, msg.from.as_ref().map(|u| u.id).unwrap_or(0))),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    };

    let prefix_utf16 = text[..body_start].encode_utf16().count();
    let body_utf16 = body.encode_utf16().count();
    let entities: Vec<Value> = msg
        .entities
        .as_ref()
        .map(|es| shift_entities(es, prefix_utf16, body_utf16))
        .unwrap_or_default();

    let targets = match store.broadcast_targets() {
        Ok(t) => t,
        Err(e) => {
            tg.send_message(
                msg.chat.id,
                &i18n::admin_user_list_read_err(lang_or_default(store, msg.from.as_ref().map(|u| u.id).unwrap_or(0)), &e.to_string()),
                Some(msg.message_id),
            )
            .await
            .ok();
            return;
        }
    };

    let total = targets.len();
    let status_id = tg
        .send_message(
            msg.chat.id,
            &i18n::admin_notify_broadcast_start(lang_or_default(store, msg.from.as_ref().map(|u| u.id).unwrap_or(0)), total),
            Some(msg.message_id),
        )
        .await
        .unwrap_or(0);

    let mut ok = 0usize;
    let mut fail = 0usize;
    let mut pruned = 0usize;
    for (uid, chat_id) in targets {
        let ents = if entities.is_empty() {
            None
        } else {
            Some(entities.clone())
        };
        match tg.send_message_entities(chat_id, body, ents).await {
            Ok(_) => ok += 1,
            Err(e) => {
                fail += 1;
                let s = e.to_string().to_lowercase();
                if s.contains("bot was blocked")
                    || s.contains("user is deactivated")
                    || s.contains("chat not found")
                {
                    if let Err(e2) = store.remove_user(uid) {
                        tracing::warn!(uid, "prune user fail: {}", e2);
                    } else {
                        pruned += 1;
                    }
                }
            }
        }
        // Tôn trọng giới hạn ~30 msg/s của Telegram broadcast.
        tokio::time::sleep(Duration::from_millis(40)).await;
    }

    let summary = i18n::admin_notify_broadcast_done(lang_or_default(store, msg.from.as_ref().map(|u| u.id).unwrap_or(0)), ok, fail, pruned);
    if status_id != 0 {
        tg.edit_message_text(msg.chat.id, status_id, &summary)
            .await
            .ok();
    } else {
        tg.send_message(msg.chat.id, &summary, None).await.ok();
    }
}

/// `/chat <@username | id> <message>` — gửi DM trực tiếp tới 1 user. Resolve
/// target (số = user_id, còn lại = username qua bot_users), giữ nguyên format
/// (entities) admin gõ giống `/notify`. Report kết quả về admin.
async fn handle_chat(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    msg: &Message,
    text: &str,
) {
    let Some((arg, arg_start)) = command_body(text) else {
        tg.send_message(
            msg.chat.id,
            &i18n::admin_chat_usage_long(lang_or_default(store, msg.from.as_ref().map(|u| u.id).unwrap_or(0))),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    };

    // Token đầu = target; phần còn lại = nội dung tin nhắn.
    let token = arg.split_whitespace().next().unwrap_or("");
    if token.is_empty() {
        tg.send_message(
            msg.chat.id,
            &i18n::admin_chat_usage_short(lang_or_default(store, msg.from.as_ref().map(|u| u.id).unwrap_or(0))),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    }

    // Byte offset của nội dung tin nhắn trong `text` gốc (sau target + whitespace)
    // — để shift entities chính xác (UTF-16). target bắt đầu tại arg_start.
    let after_target = &arg[token.len()..];
    let Some((body, body_rel)) = after_target
        .char_indices()
        .find(|(_, c)| !c.is_whitespace())
        .map(|(i, _)| (after_target[i..].trim_end(), i))
    else {
        tg.send_message(
            msg.chat.id,
            &i18n::admin_chat_empty_message(lang_or_default(store, msg.from.as_ref().map(|u| u.id).unwrap_or(0))),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    };
    if body.is_empty() {
        tg.send_message(
            msg.chat.id,
            &i18n::admin_chat_empty_message(lang_or_default(store, msg.from.as_ref().map(|u| u.id).unwrap_or(0))),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    }
    let msg_start = arg_start + token.len() + body_rel;

    // Resolve target → user_id. Số (có/không '@') = user_id; còn lại = username.
    let stripped = token.trim_start_matches('@');
    let (target_id, uname): (i64, Option<String>) = match stripped.parse::<i64>() {
        Ok(id) => (id, store.known_username(id).ok().flatten()),
        Err(_) => match store.resolve_username(stripped) {
            Ok(Some(id)) => (id, Some(stripped.to_string())),
            Ok(None) => {
                tg.send_message(
                    msg.chat.id,
                    &i18n::admin_username_not_seen_chat(
                        lang_or_default(store, msg.from.as_ref().map(|u| u.id).unwrap_or(0)),
                        stripped,
                    ),
                    Some(msg.message_id),
                )
                .await
                .ok();
                return;
            }
            Err(e) => {
                tg.send_message(
                    msg.chat.id,
                    &i18n::admin_username_resolve_err(
                        lang_or_default(store, msg.from.as_ref().map(|u| u.id).unwrap_or(0)),
                        &e.to_string(),
                    ),
                    Some(msg.message_id),
                )
                .await
                .ok();
                return;
            }
        },
    };

    // chat_id để gửi: ưu tiên chat_id đã lưu, fallback user_id (private chat).
    let target_chat = store
        .chat_id_for_user(target_id)
        .ok()
        .flatten()
        .unwrap_or(target_id);

    // Shift entities (giữ format admin gõ) về body sau khi cắt "/chat <target> ".
    let prefix_utf16 = text[..msg_start].encode_utf16().count();
    let body_utf16 = body.encode_utf16().count();
    let entities: Option<Vec<Value>> = msg
        .entities
        .as_ref()
        .map(|es| shift_entities(es, prefix_utf16, body_utf16))
        .filter(|v| !v.is_empty());

    match tg.send_message_entities(target_chat, body, entities).await {
        Ok(_) => {
            let uname_disp = uname
                .as_deref()
                .map(|u| format!(" (@{})", u))
                .unwrap_or_default();
            tg.send_message(
                msg.chat.id,
                &i18n::admin_msg_sent(lang_or_default(store, msg.from.as_ref().map(|u| u.id).unwrap_or(0)), target_id, &uname_disp),
                Some(msg.message_id),
            )
            .await
            .ok();
        }
        Err(e) => {
            let s = e.to_string().to_lowercase();
            let lang = lang_or_default(store, msg.from.as_ref().map(|u| u.id).unwrap_or(0));
            let hint = if s.contains("bot was blocked") {
                match lang {
                    Lang::Vi => " (user đã chặn bot)",
                    Lang::En => " (user has blocked the bot)",
                }
            } else if s.contains("chat not found") {
                match lang {
                    Lang::Vi => " (user chưa từng start bot)",
                    Lang::En => " (user has never started the bot)",
                }
            } else if s.contains("user is deactivated") {
                match lang {
                    Lang::Vi => " (tài khoản đã bị vô hiệu hóa)",
                    Lang::En => " (account deactivated)",
                }
            } else {
                ""
            };
            tg.send_message(
                msg.chat.id,
                &i18n::admin_send_failed(lang, hint, &e.to_string()),
                Some(msg.message_id),
            )
            .await
            .ok();
        }
    }
}

/// `/ban <@username | id> [lý do]` — resolve về user_id rồi lưu ban theo user_id
/// (đổi username vẫn dính ban). Cũng stop mọi job đang chạy của user đó.
async fn handle_ban(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    registry: &JobRegistry,
    limiter: &UserLimiter,
    msg: &Message,
    text: &str,
    admin_id: i64,
) {
    let Some((arg, _)) = command_body(text) else {
        tg.send_message(
            msg.chat.id,
            &i18n::admin_ban_usage(lang_or_default(store, msg.from.as_ref().map(|u| u.id).unwrap_or(0))),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    };
    let token = arg.split_whitespace().next().unwrap_or("");
    let reason = arg[token.len()..].trim();
    let reason_opt = if reason.is_empty() { None } else { Some(reason) };

    // Token toàn chữ số (có/không '@') → coi là user_id; còn lại → username.
    let stripped = token.trim_start_matches('@');
    let (target_id, uname): (i64, Option<String>) = match stripped.parse::<i64>() {
        Ok(id) => (id, store.known_username(id).ok().flatten()),
        Err(_) => match store.resolve_username(stripped) {
            Ok(Some(id)) => (id, Some(stripped.to_string())),
            Ok(None) => {
                tg.send_message(
                    msg.chat.id,
                    &i18n::admin_username_not_seen_ban(
                        lang_or_default(store, admin_id),
                        stripped,
                    ),
                    Some(msg.message_id),
                )
                .await
                .ok();
                return;
            }
            Err(e) => {
                tg.send_message(
                    msg.chat.id,
                    &i18n::admin_username_resolve_err(
                        lang_or_default(store, admin_id),
                        &e.to_string(),
                    ),
                    Some(msg.message_id),
                )
                .await
                .ok();
                return;
            }
        },
    };

    if target_id == admin_id {
        tg.send_message(
            msg.chat.id,
            &i18n::admin_ban_cant_ban_admin(lang_or_default(store, admin_id)),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    }

    match store.ban(target_id, uname.as_deref(), reason_opt, admin_id) {
        Ok(_) => {
            let stopped = registry.stop_user(target_id).await;
            limiter.force_reset_user(target_id).await;
            let lang = lang_or_default(store, admin_id);
            let uname_disp = uname
                .as_deref()
                .map(|u| format!(" (@{})", u))
                .unwrap_or_default();
            let reason_disp = match reason_opt {
                Some(r) => match lang {
                    Lang::Vi => format!("\nLý do: {}", r),
                    Lang::En => format!("\nReason: {}", r),
                },
                None => String::new(),
            };
            tg.send_message(
                msg.chat.id,
                &i18n::admin_ban_ok(lang, target_id, &uname_disp, &reason_disp, stopped),
                Some(msg.message_id),
            )
            .await
            .ok();
        }
        Err(e) => {
            tg.send_message(
                msg.chat.id,
                &i18n::admin_ban_failed(lang_or_default(store, admin_id), &e.to_string()),
                Some(msg.message_id),
            )
            .await
            .ok();
        }
    }
}

/// `/unban <@username | id>` — gỡ ban. Resolve username qua bot_users, fallback
/// sang username lưu trong chính bảng ban (phòng user đã rời/đổi tên).
async fn handle_unban(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    msg: &Message,
    text: &str,
) {
    let Some((arg, _)) = command_body(text) else {
        tg.send_message(
            msg.chat.id,
            &i18n::admin_unban_usage(lang_or_default(store, msg.from.as_ref().map(|u| u.id).unwrap_or(0))),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    };
    let token = arg.split_whitespace().next().unwrap_or("");
    let stripped = token.trim_start_matches('@');

    let target_id: Option<i64> = match stripped.parse::<i64>() {
        Ok(id) => Some(id),
        Err(_) => match store.resolve_username(stripped) {
            Ok(Some(id)) => Some(id),
            Ok(None) => store.banned_user_id_by_username(stripped).ok().flatten(),
            Err(_) => store.banned_user_id_by_username(stripped).ok().flatten(),
        },
    };

    let Some(target_id) = target_id else {
        tg.send_message(
            msg.chat.id,
            &i18n::admin_user_id_not_found(lang_or_default(store, msg.from.as_ref().map(|u| u.id).unwrap_or(0)), token),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    };

    match store.unban(target_id) {
        Ok(true) => {
            tg.send_message(
                msg.chat.id,
                &i18n::admin_unban_ok(lang_or_default(store, msg.from.as_ref().map(|u| u.id).unwrap_or(0)), target_id),
                Some(msg.message_id),
            )
            .await
            .ok();
        }
        Ok(false) => {
            tg.send_message(
                msg.chat.id,
                &format!("ℹ️ user_id {} is not in the ban list.", target_id),
                Some(msg.message_id),
            )
            .await
            .ok();
        }
        Err(e) => {
            tg.send_message(
                msg.chat.id,
                &i18n::admin_unban_failed(lang_or_default(store, msg.from.as_ref().map(|u| u.id).unwrap_or(0)), &e.to_string()),
                Some(msg.message_id),
            )
            .await
            .ok();
        }
    }
}

/// `/banlist` — liệt kê user bị ban (mới nhất trước).
async fn handle_banlist(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    msg: &Message,
) {
    let bans = match store.list_bans() {
        Ok(b) => b,
        Err(e) => {
            tg.send_message(
                msg.chat.id,
                &i18n::admin_banlist_read_err(lang_or_default(store, msg.from.as_ref().map(|u| u.id).unwrap_or(0)), &e.to_string()),
                Some(msg.message_id),
            )
            .await
            .ok();
            return;
        }
    };
    if bans.is_empty() {
        tg.send_message(
            msg.chat.id,
            &i18n::admin_banlist_empty(lang_or_default(store, msg.from.as_ref().map(|u| u.id).unwrap_or(0))),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    }
    let mut body = i18n::admin_banlist_header(lang_or_default(store, msg.from.as_ref().map(|u| u.id).unwrap_or(0)), bans.len());
    for b in &bans {
        let uname = b
            .username
            .as_deref()
            .map(|u| format!(" @{}", u))
            .unwrap_or_default();
        let reason = b
            .reason
            .as_deref()
            .map(|r| format!(" — {}", r))
            .unwrap_or_default();
        body.push_str(&format!("• {}{}{}\n", b.user_id, uname, reason));
    }
    if body.len() > 3800 {
        body.truncate(3800);
        body.push('…');
    }
    tg.send_message(msg.chat.id, &body, Some(msg.message_id))
        .await
        .ok();
}

// ── Proxy command helpers ─────────────────────────────────────────────

/// Inline keyboard cho thao tác proxy (check live + remove). Dùng chung cho
/// `/proxy_set` (sau khi save) và callback `proxy:check` (sau khi probe xong).
fn proxy_keyboard(lang: Lang) -> Value {
    serde_json::json!([
        [
            {"text": i18n::btn_proxy_check(lang), "callback_data": "proxy:check"},
            {"text": i18n::btn_proxy_remove(lang), "callback_data": "proxy:remove"},
        ]
    ])
}

/// Render probe result CHO POOL nhiều proxy thành 1 card gọn: header summary
/// (live/dead/slow) + danh sách đánh số (line + status + latency).
///
/// `mask = true` → che credential (<code>host:port:***:***</code>) cho nhãn user
/// thường. `mask = false` → in NGUYÊN raw line (cho admin `/proxy_check_user`
/// để admin tap-copy đi đặt lại). Caller chịu trách nhiệm ngữ cảnh hiển thị.
fn render_pool_probe_result(
    lang: Lang,
    lines: &[String],
    results: &[std::sync::Arc<bot::proxy_probe::ProbeResult>],
    mask: bool,
) -> String {
    let limit_ms = 2000u64; // hiển thị heuristic; threshold thực dùng `cli.proxy_latency_limit_ms`.
    let mut live = 0usize;
    let mut slow = 0usize;
    let mut dead = 0usize;
    for r in results {
        if !r.ok {
            dead += 1;
        } else if r.latency_ms > limit_ms {
            slow += 1;
        } else {
            live += 1;
        }
    }
    let mut body = match lang {
        Lang::Vi => format!(
            "🌐 Pool {} dòng — ✅ {} sống · 🐢 {} chậm · ❌ {} chết\n",
            lines.len(),
            live,
            slow,
            dead
        ),
        Lang::En => format!(
            "🌐 Pool of {} lines — ✅ {} live · 🐢 {} slow · ❌ {} dead\n",
            lines.len(),
            live,
            slow,
            dead
        ),
    };
    for (i, (raw, r)) in lines.iter().zip(results.iter()).enumerate() {
        let status = proxy_status_text(lang, r);
        let icon = if !r.ok {
            "❌"
        } else if r.latency_ms > limit_ms {
            "🐢"
        } else {
            "✅"
        };
        let display = if mask {
            proxy_format::mask_proxy(raw)
        } else {
            raw.clone()
        };
        body.push_str(&format!(
            "\n{}. {} <code>{}</code> · {} · {}ms",
            i + 1,
            icon,
            bot::board::html_escape(&display),
            status,
            r.latency_ms
        ));
    }
    body
}

/// Map ProbeResult → nhãn trạng thái ngắn song ngữ. Hiển thị trên probe card
/// + pre-flight + pool listing — KHÔNG để user VN thấy text English thô.
fn proxy_status_text(lang: Lang, r: &bot::proxy_probe::ProbeResult) -> &'static str {
    use bot::proxy_probe::ProbeReason;
    match (&r.reason, r.ok) {
        (ProbeReason::Ok, true) => match lang {
            Lang::Vi => "SỐNG",
            Lang::En => "ALIVE",
        },
        (ProbeReason::Auth, _) => match lang {
            Lang::Vi => "LỖI AUTH",
            Lang::En => "AUTH FAIL",
        },
        (ProbeReason::Ip, _) => match lang {
            Lang::Vi => "LỖI IP",
            Lang::En => "IP-LEVEL FAIL",
        },
        (ProbeReason::BadFormat, _) => match lang {
            Lang::Vi => "SAI ĐỊNH DẠNG",
            Lang::En => "BAD FORMAT",
        },
        _ => match lang {
            Lang::Vi => "KHÔNG RÕ",
            Lang::En => "UNKNOWN",
        },
    }
}

/// 1 dòng trạng thái proxy cho pre-flight card (mask + sanitize detail).
fn build_proxy_line(lang: Lang, label: &str, r: &bot::proxy_probe::ProbeResult) -> String {
    let detail = if r.ok {
        r.detail.clone()
    } else {
        proxy_format::sanitize_proxy_text(&r.detail)
    };
    bot::proc_view::render_proxy_line(lang, label, r.ok, proxy_status_text(lang, r), &detail, r.latency_ms)
}

/// `/proxy_set <line>` — set proxy private cho user. Không có arg → show
/// trạng thái + keyboard nếu user đã có proxy, hoặc usage nếu chưa.
///
/// Format hỗ trợ (đồng bộ Python `materialize_proxy`):
///   - host:port
///   - host:port:user
///   - host:port:user:pass
///   - scheme://user:pass@host:port
///   - placeholder `{SID}` / `{sid}` cho sticky session
async fn handle_proxy_set(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    msg: &Message,
    text: &str,
    user_id: i64,
    username: &Option<String>,
    admin_chat_id: i64,
    proxy_from_step: u32,
    lang: Lang,
) {
    let body_opt = command_body(text);
    if body_opt.is_none() {
        // No arg → show pool hiện tại + keyboard, hoặc usage nếu chưa có.
        match store.get_user_proxies(user_id) {
            Ok(lines) if !lines.is_empty() => {
                let masked: Vec<String> =
                    lines.iter().map(|s| proxy_format::mask_proxy(s)).collect();
                let body = i18n::proxy_show_pool(lang, &masked);
                tg.send_message_kb(msg.chat.id, &body, Some(msg.message_id), proxy_keyboard(lang))
                    .await
                    .ok();
            }
            Ok(_) => {
                tg.send_message(msg.chat.id, &i18n::proxy_set_usage_multi(lang), Some(msg.message_id))
                    .await
                    .ok();
            }
            Err(e) => {
                tg.send_message(
                    msg.chat.id,
                    &i18n::db_error(lang, &e.to_string()),
                    Some(msg.message_id),
                )
                .await
                .ok();
            }
        }
        return;
    }

    let (arg, _) = body_opt.unwrap();
    // Multi-line: 1 dòng/proxy, tối đa USER_PROXY_MAX_LINES (10). Trim, bỏ dòng
    // rỗng. Cap quá → bỏ phần dư, báo `dropped`. Validate từng dòng → invalid
    // bị skip + đếm.
    let cap = settings::Settings::USER_PROXY_MAX_LINES;
    let raw_lines: Vec<&str> = arg
        .lines()
        .map(|l| l.trim())
        .filter(|l| !l.is_empty())
        .collect();
    let dropped = raw_lines.len().saturating_sub(cap);
    let raw_lines: Vec<&str> = raw_lines.into_iter().take(cap).collect();
    if raw_lines.is_empty() {
        tg.send_message(msg.chat.id, &i18n::proxy_empty_line(lang), Some(msg.message_id))
            .await
            .ok();
        return;
    }

    let mut accepted: Vec<String> = Vec::new();
    let mut first_invalid_err: Option<String> = None;
    let mut invalid = 0usize;
    for line in &raw_lines {
        match proxy_format::validate_and_mask(line) {
            Ok(_) => accepted.push((*line).to_string()),
            Err(e) => {
                invalid += 1;
                if first_invalid_err.is_none() {
                    first_invalid_err = Some(format!("`{}`: {}", line, e));
                }
            }
        }
    }
    if accepted.is_empty() {
        tg.send_message(
            msg.chat.id,
            &i18n::proxy_invalid_format(
                lang,
                &first_invalid_err.unwrap_or_else(|| "no valid line".into()),
            ),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    }

    if let Err(e) = store.set_user_proxies(user_id, &accepted) {
        tg.send_message(
            msg.chat.id,
            &i18n::proxy_save_failed(lang, &e.to_string()),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    }

    let masked: Vec<String> = accepted
        .iter()
        .map(|s| proxy_format::mask_proxy(s))
        .collect();
    let body = i18n::proxy_set_ok_pool(lang, &masked, proxy_from_step, dropped, invalid);
    tg.send_message_kb(msg.chat.id, &body, Some(msg.message_id), proxy_keyboard(lang))
        .await
        .ok();

    // Notify admin — full info để verify proxy đáng ngờ. Gửi HTML + <code>
    // từng dòng để admin tap-copy nhanh. Skip khi admin tự set.
    if admin_chat_id != 0 && user_id != admin_chat_id {
        let alang = admin_lang(store, admin_chat_id);
        let summary = i18n::admin_note_user_set_proxy(
            alang,
            accepted.len(),
            username.as_deref().unwrap_or("-"),
            user_id,
            &accepted,
            &masked,
        );
        if let Err(e) = tg
            .send_message_kb_html(admin_chat_id, &summary, Value::Array(vec![]))
            .await
        {
            tracing::warn!(admin_chat_id, "admin notify proxy_set fail: {}", e);
        }
    }
}

/// `/proxy_remove` — xóa proxy của user. Job tiếp theo sẽ dùng pool global
/// (hoặc DIRECT nếu pool global rỗng).
async fn handle_proxy_remove(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    msg: &Message,
    user_id: i64,
    lang: Lang,
) {
    match store.remove_user_proxy(user_id) {
        Ok(true) => {
            tg.send_message(
                msg.chat.id,
                &i18n::proxy_removed_global(lang),
                Some(msg.message_id),
            )
            .await
            .ok();
        }
        Ok(false) => {
            tg.send_message(
                msg.chat.id,
                &i18n::proxy_none_to_remove(lang),
                Some(msg.message_id),
            )
            .await
            .ok();
        }
        Err(e) => {
            tg.send_message(
                msg.chat.id,
                &i18n::proxy_remove_failed(lang, &e.to_string()),
                Some(msg.message_id),
            )
            .await
            .ok();
        }
    }
}

/// `/proxy_login_set <line(s)>` — ADMIN set POOL login proxy global. Áp cho
/// segment login (step < `proxy_from_step`) của TẤT CẢ user. Multi-line: mỗi
/// dòng = 1 proxy, tối đa `LOGIN_PROXY_MAX_LINES`. Mỗi job pick RANDOM 1 line
/// từ pool live → spread tải, đỡ rate-limit. Validate + probe tươi để admin
/// verify ngay sau khi set.
async fn handle_proxy_login_set(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    msg: &Message,
    text: &str,
    proxy_from_step: u32,
) {
    let admin_lang_v = lang_or_default(store, msg.from.as_ref().map(|u| u.id).unwrap_or(0));
    let upper_step = proxy_from_step.saturating_sub(1).max(1);

    let body_opt = command_body(text);
    if body_opt.is_none() {
        // Không arg → show pool hiện tại (mask), hoặc usage nếu chưa set.
        let lines = store.get_login_proxies();
        if lines.is_empty() {
            tg.send_message(
                msg.chat.id,
                "ℹ️ Chưa set login proxy.\n\nUsage (1 hoặc nhiều dòng, tối đa 10):\n\
                 /proxy_login_set host:port\n\
                 host:port:user:pass\n\
                 http://user:pass@host:port\n\
                 socks5://user:pass@host:1080\n\n\
                 Mỗi job pick random 1 line từ pool. Hỗ trợ {SID} cho sticky session.",
                Some(msg.message_id),
            )
            .await
            .ok();
            return;
        }
        let masked: Vec<String> = lines.iter().map(|s| proxy_format::mask_proxy(s)).collect();
        let mut listing = String::new();
        for (i, m) in masked.iter().enumerate() {
            listing.push_str(&format!("{}. {}\n", i + 1, m));
        }
        tg.send_message(
            msg.chat.id,
            &format!(
                "🌐 Login proxy pool hiện tại ({}/{}):\n{}\nÁp cho step 1..{} (login). \
                 Đổi: /proxy_login_set <dòng 1>\\n<dòng 2>...  ·  Xóa: /proxy_login_remove",
                masked.len(),
                settings::Settings::LOGIN_PROXY_MAX_LINES,
                listing,
                upper_step,
            ),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    }

    let (arg, _) = body_opt.unwrap();
    let cap = settings::Settings::LOGIN_PROXY_MAX_LINES;
    let raw_lines: Vec<&str> = arg
        .lines()
        .map(|l| l.trim())
        .filter(|l| !l.is_empty())
        .collect();
    let dropped = raw_lines.len().saturating_sub(cap);
    let raw_lines: Vec<&str> = raw_lines.into_iter().take(cap).collect();
    if raw_lines.is_empty() {
        tg.send_message(msg.chat.id, &i18n::proxy_empty_line(admin_lang_v), Some(msg.message_id))
            .await
            .ok();
        return;
    }

    let mut accepted: Vec<String> = Vec::new();
    let mut first_invalid_err: Option<String> = None;
    let mut invalid = 0usize;
    for line in &raw_lines {
        match proxy_format::validate_and_mask(line) {
            Ok(_) => accepted.push((*line).to_string()),
            Err(e) => {
                invalid += 1;
                if first_invalid_err.is_none() {
                    first_invalid_err = Some(format!("`{}`: {}", line, e));
                }
            }
        }
    }
    if accepted.is_empty() {
        tg.send_message(
            msg.chat.id,
            &i18n::admin_invalid_proxy_format(
                admin_lang_v,
                &first_invalid_err.unwrap_or_else(|| "no valid line".into()),
            ),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    }

    if let Err(e) = store.set_login_proxies(&accepted) {
        tg.send_message(
            msg.chat.id,
            &i18n::proxy_save_failed(admin_lang_v, &e.to_string()),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    }

    // Probe tươi (bypass cache) cho TỪNG line song song để admin verify ngay.
    let probe_futs = accepted.iter().map(|raw| {
        let raw = raw.clone();
        async move { (raw.clone(), bot::proxy_status::PROXY_STATUS.refresh(&raw).await) }
    });
    let probes: Vec<(String, Arc<bot::proxy_probe::ProbeResult>)> =
        futures_util::future::join_all(probe_futs).await;

    let masked: Vec<String> = accepted.iter().map(|s| proxy_format::mask_proxy(s)).collect();
    let body = i18n::admin_login_proxy_set_ok(
        admin_lang_v,
        upper_step,
        &masked,
        &probes,
        dropped,
        invalid,
    );
    tg.send_message(msg.chat.id, &body, Some(msg.message_id))
        .await
        .ok();
}

/// `/proxy_login_remove` — ADMIN xóa login proxy global. Sau đó segment login
/// chạy DIRECT (hoặc pool global env nếu user không có proxy riêng).
async fn handle_proxy_login_remove(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    msg: &Message,
) {
    match store.remove_login_proxy() {
        Ok(true) => {
            tg.send_message(
                msg.chat.id,
                &i18n::admin_login_proxy_remove_ok(lang_or_default(store, msg.from.as_ref().map(|u| u.id).unwrap_or(0))),
                Some(msg.message_id),
            )
            .await
            .ok();
        }
        Ok(false) => {
            tg.send_message(
                msg.chat.id,
                "ℹ️ Chưa có login proxy để xóa.",
                Some(msg.message_id),
            )
            .await
            .ok();
        }
        Err(e) => {
            tg.send_message(
                msg.chat.id,
                &i18n::admin_remove_failed(lang_or_default(store, msg.from.as_ref().map(|u| u.id).unwrap_or(0)), &e.to_string()),
                Some(msg.message_id),
            )
            .await
            .ok();
        }
    }
}

/// `/proxy_check` — user kiểm tra LIVE pool proxy của chính mình. Là shortcut
/// text cho callback `proxy:check` (nút trên card `/proxy_set`). Mask credential.
async fn handle_proxy_check(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    msg: &Message,
    user_id: i64,
    lang: Lang,
) {
    let lines = match store.get_user_proxies(user_id) {
        Ok(v) if !v.is_empty() => v,
        Ok(_) => {
            tg.send_message(msg.chat.id, &i18n::proxy_not_set(lang), Some(msg.message_id))
                .await
                .ok();
            return;
        }
        Err(e) => {
            tg.send_message(
                msg.chat.id,
                &i18n::db_error(lang, &e.to_string()),
                Some(msg.message_id),
            )
            .await
            .ok();
            return;
        }
    };
    let probes = lines.iter().map(|raw| {
        let raw = raw.clone();
        async move { bot::proxy_status::PROXY_STATUS.refresh(&raw).await }
    });
    let results = futures_util::future::join_all(probes).await;
    let mut body = i18n::proxy_check_header_self(lang);
    body.push_str(&render_pool_probe_result(lang, &lines, &results, true));
    tg.send_message_kb_html(msg.chat.id, &body, proxy_keyboard(lang))
        .await
        .ok();
}

/// `/login_proxy_check` — bất kỳ user nào (cả admin) check LIVE pool login
/// proxy admin set. Mask credential — user thường không cần thấy raw, admin
/// vẫn có thể đọc lại pool qua `/proxy_login_set` (no arg).
async fn handle_login_proxy_check(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    msg: &Message,
    lang: Lang,
    proxy_from_step: u32,
) {
    let lines = store.get_login_proxies();
    if lines.is_empty() {
        tg.send_message(
            msg.chat.id,
            &i18n::login_proxy_check_empty(lang),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    }
    let probes = lines.iter().map(|raw| {
        let raw = raw.clone();
        async move { bot::proxy_status::PROXY_STATUS.refresh(&raw).await }
    });
    let results = futures_util::future::join_all(probes).await;
    let upper_step = proxy_from_step.saturating_sub(1).max(1);
    let mut body = i18n::login_proxy_check_header(lang, upper_step);
    body.push_str(&render_pool_probe_result(lang, &lines, &results, true));
    tg.send_message_kb_html(msg.chat.id, &body, Value::Array(vec![]))
        .await
        .ok();
}

/// `/proxy_check_user <@username | id>` — ADMIN xem RAW pool proxy của 1 user
/// (kèm credential để admin tap-copy đặt lại) + check live song song.
/// Resolve target giống `/chat` `/ban`. KHÔNG kèm keyboard remove — admin
/// không nên vô tình xóa proxy của user khác.
async fn handle_proxy_check_user(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    msg: &Message,
    text: &str,
    admin_id: i64,
) {
    let alang = lang_or_default(store, admin_id);

    let Some((arg, _)) = command_body(text) else {
        tg.send_message(
            msg.chat.id,
            &i18n::admin_check_proxy_usage(alang),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    };
    let token = arg.split_whitespace().next().unwrap_or("");
    if token.is_empty() {
        tg.send_message(
            msg.chat.id,
            &i18n::admin_check_proxy_usage(alang),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    }

    // Resolve target → user_id. Số (có/không '@') = user_id; còn lại = username.
    let stripped = token.trim_start_matches('@');
    let (target_id, uname): (i64, Option<String>) = match stripped.parse::<i64>() {
        Ok(id) => (id, store.known_username(id).ok().flatten()),
        Err(_) => match store.resolve_username(stripped) {
            Ok(Some(id)) => (id, Some(stripped.to_string())),
            Ok(None) => {
                tg.send_message(
                    msg.chat.id,
                    &i18n::admin_username_not_seen_chat(alang, stripped),
                    Some(msg.message_id),
                )
                .await
                .ok();
                return;
            }
            Err(e) => {
                tg.send_message(
                    msg.chat.id,
                    &i18n::admin_username_resolve_err(alang, &e.to_string()),
                    Some(msg.message_id),
                )
                .await
                .ok();
                return;
            }
        },
    };
    let uname_disp = uname
        .as_deref()
        .map(|u| format!(" (@{})", u))
        .unwrap_or_default();

    let lines = match store.get_user_proxies(target_id) {
        Ok(v) => v,
        Err(e) => {
            tg.send_message(
                msg.chat.id,
                &i18n::db_error(alang, &e.to_string()),
                Some(msg.message_id),
            )
            .await
            .ok();
            return;
        }
    };
    if lines.is_empty() {
        tg.send_message_kb_html(
            msg.chat.id,
            &i18n::admin_check_proxy_no_proxy(alang, target_id, &uname_disp),
            Value::Array(vec![]),
        )
        .await
        .ok();
        return;
    }

    let probes = lines.iter().map(|raw| {
        let raw = raw.clone();
        async move { bot::proxy_status::PROXY_STATUS.refresh(&raw).await }
    });
    let results = futures_util::future::join_all(probes).await;

    let mut body = i18n::admin_check_proxy_target_header(alang, target_id, &uname_disp);
    // mask=false → in raw để admin copy. Caller chấp nhận chia sẻ credential
    // (đã có cảnh báo trong header).
    body.push_str(&render_pool_probe_result(alang, &lines, &results, false));
    tg.send_message_kb_html(msg.chat.id, &body, Value::Array(vec![]))
        .await
        .ok();
}

/// `/set_max_per_user [n]` — ADMIN đổi default toàn cục cho `max_per_user`.
/// No-arg: hiển thị giá trị + số user có override. Có arg: validate 1..=10,
/// ghi DB + push atomic vào limiter (hot reload, áp ngay cho mọi user không
/// có override).
async fn handle_set_max_per_user(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    limiter: &UserLimiter,
    msg: &Message,
    text: &str,
    admin_id: i64,
) {
    let alang = lang_or_default(store, admin_id);
    let body_opt = command_body(text);
    if body_opt.is_none() {
        let current = limiter.default_max_per_user();
        let overrides = limiter.snapshot_overrides().await.len();
        tg.send_message_kb_html(
            msg.chat.id,
            &i18n::limit_show_global(alang, current, overrides),
            Value::Array(vec![]),
        )
        .await
        .ok();
        return;
    }

    let (arg, _) = body_opt.unwrap();
    let token = arg.split_whitespace().next().unwrap_or("");
    let parsed: Option<u32> = token.parse().ok();
    let Some(n) = parsed else {
        tg.send_message_kb_html(
            msg.chat.id,
            &i18n::limit_invalid_range(
                alang,
                token,
                settings::Settings::MAX_PER_USER_MIN,
                settings::Settings::MAX_PER_USER_MAX,
            ),
            Value::Array(vec![]),
        )
        .await
        .ok();
        return;
    };
    if !(settings::Settings::MAX_PER_USER_MIN..=settings::Settings::MAX_PER_USER_MAX)
        .contains(&n)
    {
        tg.send_message_kb_html(
            msg.chat.id,
            &i18n::limit_invalid_range(
                alang,
                token,
                settings::Settings::MAX_PER_USER_MIN,
                settings::Settings::MAX_PER_USER_MAX,
            ),
            Value::Array(vec![]),
        )
        .await
        .ok();
        return;
    }

    let old = limiter.default_max_per_user();
    // Persist trước, rồi push vào limiter — fail DB → không thay đổi runtime
    // (caller thấy lỗi rõ ràng, không có drift giữa atomic và DB).
    if let Err(e) = store.set_max_per_user_default(n) {
        tg.send_message(
            msg.chat.id,
            &i18n::db_error(alang, &e.to_string()),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    }
    limiter.set_default_max_per_user(n);
    tg.send_message_kb_html(
        msg.chat.id,
        &i18n::limit_set_global_ok(alang, old, n),
        Value::Array(vec![]),
    )
    .await
    .ok();
}

/// `/set_user_limit <@user|id> [n|default]` — ADMIN set/show/xóa override
/// per-user. Hỗ trợ:
///   - `<target>` — show effective + override + default global.
///   - `<target> <n>` — set override (1..=10).
///   - `<target> default` — xóa override (về default global). Cũng chấp nhận
///     `0` / `none` / `clear` cho cùng nghĩa.
async fn handle_set_user_limit(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    limiter: &UserLimiter,
    msg: &Message,
    text: &str,
    admin_id: i64,
) {
    let alang = lang_or_default(store, admin_id);
    let Some((arg, _)) = command_body(text) else {
        tg.send_message_kb_html(
            msg.chat.id,
            &i18n::limit_user_set_usage(alang),
            Value::Array(vec![]),
        )
        .await
        .ok();
        return;
    };
    let mut it = arg.split_whitespace();
    let token_target = it.next().unwrap_or("");
    let token_value = it.next().map(|s| s.to_string());
    if token_target.is_empty() {
        tg.send_message_kb_html(
            msg.chat.id,
            &i18n::limit_user_set_usage(alang),
            Value::Array(vec![]),
        )
        .await
        .ok();
        return;
    }

    // Resolve target → user_id (giống /chat /ban /proxy_check_user).
    let stripped = token_target.trim_start_matches('@');
    let (target_id, uname): (i64, Option<String>) = match stripped.parse::<i64>() {
        Ok(id) => (id, store.known_username(id).ok().flatten()),
        Err(_) => match store.resolve_username(stripped) {
            Ok(Some(id)) => (id, Some(stripped.to_string())),
            Ok(None) => {
                tg.send_message(
                    msg.chat.id,
                    &i18n::admin_username_not_seen_chat(alang, stripped),
                    Some(msg.message_id),
                )
                .await
                .ok();
                return;
            }
            Err(e) => {
                tg.send_message(
                    msg.chat.id,
                    &i18n::admin_username_resolve_err(alang, &e.to_string()),
                    Some(msg.message_id),
                )
                .await
                .ok();
                return;
            }
        },
    };
    let uname_disp = uname
        .as_deref()
        .map(|u| format!(" (@{})", u))
        .unwrap_or_default();

    // Branch theo có/không token_value.
    let Some(value) = token_value else {
        // Show only.
        let override_some = limiter.get_user_override(target_id).await;
        let default_global = limiter.default_max_per_user();
        tg.send_message_kb_html(
            msg.chat.id,
            &i18n::limit_user_show(alang, target_id, &uname_disp, override_some, default_global),
            Value::Array(vec![]),
        )
        .await
        .ok();
        return;
    };

    // Token "default" / "none" / "clear" / "0" → xóa override.
    let lc = value.to_lowercase();
    if matches!(lc.as_str(), "default" | "none" | "clear" | "0") {
        // Persist trước, rồi xóa cache.
        match store.remove_user_limit(target_id) {
            Ok(removed) => {
                limiter.clear_user_override(target_id).await;
                let default_global = limiter.default_max_per_user();
                let body = if removed {
                    i18n::limit_user_clear_ok(alang, target_id, &uname_disp, default_global)
                } else {
                    i18n::limit_user_clear_none(alang, target_id, &uname_disp)
                };
                tg.send_message_kb_html(msg.chat.id, &body, Value::Array(vec![]))
                    .await
                    .ok();
            }
            Err(e) => {
                tg.send_message(
                    msg.chat.id,
                    &i18n::db_error(alang, &e.to_string()),
                    Some(msg.message_id),
                )
                .await
                .ok();
            }
        }
        return;
    }

    // Else parse u32.
    let parsed: Option<u32> = value.parse().ok();
    let Some(n) = parsed else {
        tg.send_message_kb_html(
            msg.chat.id,
            &i18n::limit_invalid_range(
                alang,
                &value,
                settings::Settings::MAX_PER_USER_MIN,
                settings::Settings::MAX_PER_USER_MAX,
            ),
            Value::Array(vec![]),
        )
        .await
        .ok();
        return;
    };
    if !(settings::Settings::MAX_PER_USER_MIN..=settings::Settings::MAX_PER_USER_MAX)
        .contains(&n)
    {
        tg.send_message_kb_html(
            msg.chat.id,
            &i18n::limit_invalid_range(
                alang,
                &value,
                settings::Settings::MAX_PER_USER_MIN,
                settings::Settings::MAX_PER_USER_MAX,
            ),
            Value::Array(vec![]),
        )
        .await
        .ok();
        return;
    }

    // Persist trước, rồi push cache. DB fail → không thay đổi runtime.
    if let Err(e) = store.set_user_limit(target_id, n) {
        tg.send_message(
            msg.chat.id,
            &i18n::db_error(alang, &e.to_string()),
            Some(msg.message_id),
        )
        .await
        .ok();
        return;
    }
    limiter.set_user_override(target_id, n).await;
    tg.send_message_kb_html(
        msg.chat.id,
        &i18n::limit_user_set_ok(alang, target_id, &uname_disp, n),
        Value::Array(vec![]),
    )
    .await
    .ok();
}

/// `/my_limit` — user xem giới hạn tiến trình đồng thời của chính mình +
/// default toàn cục để biết mình có override admin cấp riêng không.
async fn handle_my_limit(
    tg: &Arc<TelegramClient>,
    limiter: &UserLimiter,
    msg: &Message,
    user_id: i64,
    lang: Lang,
) {
    let override_some = limiter.get_user_override(user_id).await;
    let effective = override_some.unwrap_or_else(|| limiter.default_max_per_user());
    let default_global = limiter.default_max_per_user();
    let body = i18n::my_limit_card(lang, effective, override_some.is_some(), default_global);
    tg.send_message_kb_html(msg.chat.id, &body, Value::Array(vec![]))
        .await
        .ok();
}

/// `/help` — show full command list. Section admin chỉ hiện khi user là admin.
/// Cũng đính kèm trạng thái proxy hiện tại của user (mask).
async fn send_help(
    tg: &Arc<TelegramClient>,
    store: &Arc<settings::Settings>,
    chat_id: i64,
    reply_to: i64,
    user_id: i64,
    is_admin: bool,
    lang: Lang,
) {
    let vi = matches!(lang, Lang::Vi);
    let mut help = if vi {
        String::from(
            "📚 <b>Danh sách lệnh</b>\n\n\
             <b>Người dùng:</b>\n\
             /start — mở menu / hướng dẫn\n\
             /status — trạng thái bot + hàng chờ\n\
             /stop — dừng TẤT CẢ tiến trình của bạn\n\
             /board — bảng tiến trình của bạn + nút Dừng\n\
             /proxy_set &lt;line&gt; — đặt proxy riêng của bạn\n\
             /proxy_remove — xóa proxy của bạn\n\
             /proxy_check — kiểm tra LIVE pool proxy của bạn\n\
             /login_proxy_check — kiểm tra LIVE pool login proxy (admin set)\n\
             /my_limit — xem giới hạn tiến trình đồng thời của bạn\n\
             /2fa &lt;secret | email|pass|secret&gt; — lấy mã 2FA (TOTP) + nút làm mới\n\
             /settings — cài đặt\n\
             /language — đổi ngôn ngữ\n\
             /help — bảng này\n",
        )
    } else {
        String::from(
            "📚 <b>Commands</b>\n\n\
             <b>User:</b>\n\
             /start — open menu / instructions\n\
             /status — bot status + queue\n\
             /stop — stop ALL your processes\n\
             /board — your process board + Stop buttons\n\
             /proxy_set &lt;line&gt; — set your own proxy\n\
             /proxy_remove — remove your proxy\n\
             /proxy_check — LIVE check your proxy pool\n\
             /login_proxy_check — LIVE check the login proxy pool (admin set)\n\
             /my_limit — show your concurrent process limit\n\
             /2fa &lt;secret | email|pass|secret&gt; — get a 2FA (TOTP) code + reload button\n\
             /settings — settings\n\
             /language — change language\n\
             /help — this message\n",
        )
    };
    if is_admin {
        help.push_str(if vi {
            "\n<b>Admin:</b>\n\
             /notify &lt;nội dung&gt; — gửi thông báo tới mọi user (giữ format)\n\
             /chat &lt;@user | id&gt; &lt;nội dung&gt; — nhắn riêng 1 user\n\
             /ban &lt;@user | id&gt; [lý do] — cấm theo user_id\n\
             /unban &lt;@user | id&gt; — gỡ cấm\n\
             /banlist — danh sách user bị cấm\n\
             /stopall — dừng TẤT CẢ tiến trình của mọi user\n\
             /flushall — xóa sạch hàng chờ + board\n\
             /set_notify &lt;chat_id&gt; [thread_id] — đặt kênh thông báo success\n\
             /notify_remove — xóa kênh thông báo\n\
             /notify_test — gửi test message tới kênh thông báo\n\
             /proxy_login_set &lt;line(s)&gt; — đặt POOL login proxy (multi-line, mỗi job pick random)\n\
             /proxy_login_remove — xóa toàn bộ pool login proxy\n\
             /proxy_check_user &lt;@user | id&gt; — xem RAW pool proxy của user (kèm credential) + LIVE check\n\
             /set_max_per_user &lt;1-10&gt; — đổi default toàn cục số tiến trình/user (no-arg để xem)\n\
             /set_user_limit &lt;@user | id&gt; [n|default] — set/show/xóa override quota per-user\n"
        } else {
            "\n<b>Admin:</b>\n\
             /notify &lt;message&gt; — broadcast to all users (keeps formatting)\n\
             /chat &lt;@user | id&gt; &lt;message&gt; — direct message one user\n\
             /ban &lt;@user | id&gt; [reason] — ban by user_id\n\
             /unban &lt;@user | id&gt; — unban\n\
             /banlist — list banned users\n\
             /stopall — stop ALL processes of all users\n\
             /flushall — clear queue + board\n\
             /set_notify &lt;chat_id&gt; [thread_id] — set success-notify channel\n\
             /notify_remove — remove notify channel\n\
             /notify_test — send a test message to notify channel\n\
             /proxy_login_set &lt;line(s)&gt; — set login proxy POOL (multi-line, each job picks random)\n\
             /proxy_login_remove — remove the entire login proxy pool\n\
             /proxy_check_user &lt;@user | id&gt; — show RAW user proxy pool (with credentials) + LIVE check\n\
             /set_max_per_user &lt;1-10&gt; — change global default concurrent processes/user (no-arg to view)\n\
             /set_user_limit &lt;@user | id&gt; [n|default] — set/show/clear per-user quota override\n"
        });
    }

    // Section gửi tài khoản — luôn hiển thị, hướng dẫn cụ thể từng bước.
    help.push_str(if vi {
        "\n📥 <b>Gửi tài khoản cho bot</b>\n\n\
         <b>Cách 1 — FILE session.json</b> (khuyên dùng, không cần password)\n\
         1. Mở Chrome đã đăng nhập ChatGPT\n\
         2. Vào: <code>https://chatgpt.com/api/auth/session</code>\n\
         3. Chuột phải → <i>Save As</i> → lưu thành <code>session.json</code>\n\
         4. Kéo-thả file vào chat (icon 📎 → File / Document)\n\
         5. Bot chấp nhận đuôi <code>.json</code> hoặc <code>.txt</code>, tối đa 1.5 MB\n\n\
         <b>Cách 2 — Combo text</b> (login bằng password + 2FA)\n\
         • Định dạng: <code>email|password|2fa_secret</code>\n\
         • Mỗi dòng = 1 tài khoản, paste thẳng vào chat\n\
         • Ví dụ:\n\
         <code>foo@gmail.com|MyPass123|JBSWY3DPEHPK3PXP\nbar@yahoo.com|Pass456|MFRGGZDFMZTWQ2LK</code>\n\n\
         ⚠️ <b>KHÔNG paste session.json thẳng vào chat</b> — Telegram cắt tin nhắn dài quá 4096 ký tự, JSON sẽ vỡ và bot không xử lý được.\n"
    } else {
        "\n📥 <b>Send accounts to the bot</b>\n\n\
         <b>Option 1 — session.json FILE</b> (recommended — no password needed)\n\
         1. Open Chrome signed in to ChatGPT\n\
         2. Visit: <code>https://chatgpt.com/api/auth/session</code>\n\
         3. Right-click → <i>Save As</i> → save as <code>session.json</code>\n\
         4. Drag-drop the file into the chat (📎 icon → File / Document)\n\
         5. Bot accepts <code>.json</code> or <code>.txt</code>, up to 1.5 MB\n\n\
         <b>Option 2 — Combo text</b> (login with password + 2FA)\n\
         • Format: <code>email|password|2fa_secret</code>\n\
         • One account per line, paste directly into chat\n\
         • Example:\n\
         <code>foo@gmail.com|MyPass123|JBSWY3DPEHPK3PXP\nbar@yahoo.com|Pass456|MFRGGZDFMZTWQ2LK</code>\n\n\
         ⚠️ <b>DO NOT paste session.json directly into chat</b> — Telegram splits messages over 4096 chars, the JSON breaks and the bot cannot handle it.\n"
    });

    help.push_str(if vi {
        "\n<b>Định dạng proxy hỗ trợ</b>\n\
         • host:port\n\
         • host:port:user:pass\n\
         • scheme://user:pass@host:port (http, https, socks5)\n\
         • {SID} cho sticky session\n"
    } else {
        "\n<b>Supported proxy formats</b>\n\
         • host:port\n\
         • host:port:user:pass\n\
         • scheme://user:pass@host:port (http, https, socks5)\n\
         • {SID} placeholder for sticky sessions\n"
    });

    match store.get_user_proxy(user_id) {
        Ok(Some(raw)) => {
            help.push_str(&format!(
                "\n🌐 {}: <code>{}</code>\n",
                if vi { "Proxy của bạn" } else { "Your proxy" },
                bot::board::html_escape(&proxy_format::mask_proxy(&raw))
            ));
        }
        Ok(None) => {
            help.push_str(if vi {
                "\n🌐 Proxy: chưa đặt (DIRECT hoặc dùng pool chung của admin)\n"
            } else {
                "\n🌐 Proxy: not set (DIRECT or admin's global pool)\n"
            });
        }
        Err(_) => {}
    }

    let _ = reply_to; // kept for backward compat — HTML send dùng send_message_kb_html (không reply_to).
    if let Err(e) = tg
        .send_message_kb_html(chat_id, &help, Value::Array(vec![]))
        .await
    {
        tracing::warn!("send_help fail: {}", e);
    }
}
