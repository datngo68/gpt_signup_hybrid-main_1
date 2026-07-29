//! UPI flow orchestrator — port từ `web/upi_runner.py::run_upi_qr_probe`.
//!
//! Giữ nguyên 100% logic:
//!   - 4 confirm variants thử lần lượt
//!   - approve loop với `restart_threshold` + `max_restarts`
//!   - network outage detection + recovery polling
//!   - proxy advance per-batch
//!   - aggregate matches qua mọi response → tìm UPI URI / QR image URL.

use crate::http::HttpClient;
use crate::random_profile::random_india_profile;
use crate::stripe::bundles::{extract_config_live, BundleCache};
use crate::stripe_token::StripeTokenConfig;
use crate::upi::endpoints::{
    chatgpt_approve_checkout, create_chatgpt_checkout, extract_amount, stripe_confirm_upi_qr,
    stripe_elements_session, stripe_init, ApproveAttempt,
    ConfirmAttempt, RefreshAttempt,
};
use crate::upi::matchers::{
    find_matches, find_hosted_instructions_url, find_qr_expires_at, find_qr_image_url, find_upi_uri, Match,
};
use crate::upi::qr::{download_qr_image, render_qr_png};
use crate::upi::types::{
    ApproveAttemptSummary, ConfirmAttemptSummary, RefreshAttemptSummary, UpiAuth, UpiQrResult,
};
use std::collections::{HashMap, HashSet};
use serde_json::Value;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

/// Hardcoded knobs — đồng bộ Python upi_runner.py.
pub const PROMO: bool = true;
pub const APPROVE_DELAY_MS: u64 = 3000;
pub const APPROVE_PROXY_BATCH: u32 = 3;
pub const APPROVE_BACKEND_EXCEPTION_CONSECUTIVE: u32 = 0; // disabled
pub const NETWORK_FAIL_DETECT: u32 = 3;
pub const NETWORK_RECOVERY_POLL_MS: u64 = 5000;
pub const NETWORK_RECOVERY_MAX_WAIT_S: u64 = 600;
pub const CONFIRM_VARIANTS: &[&str] = &["qr_code", "empty", "flow_qr", "intent"];

/// Nguồn auth cho job: session.json có sẵn HOẶC login HTTP từ email|pass|2fa.
#[derive(Debug, Clone)]
pub enum AuthSource {
    /// Session đã có sẵn (upload session.json) — Bearer token + cookie header.
    Session {
        access_token: String,
        cookie_header: String,
    },
    /// Combo email|pass|2fa — login HTTP để lấy session ở step [1/6].
    Login {
        password: String,
        totp_secret: String,
    },
}

#[derive(Debug, Clone)]
pub struct UpiJobConfig {
    pub email: String,
    pub auth: AuthSource,
    pub proxy_pool: Vec<String>,
    /// Login proxy (admin-set) đã materialize cho client. Áp cho mọi step
    /// TRƯỚC `proxy_from_step` (gồm login HTTP). None → các step đó DIRECT
    /// (giữ hành vi cũ khi admin chưa cấu hình login proxy).
    pub login_proxy: Option<String>,
    pub approve_retries: u32,
    /// Delay giữa các approve attempt (ms). Default `APPROVE_DELAY_MS=3000`
    /// nếu None — đồng bộ Python `APPROVE_DELAY=3.0`.
    pub approve_delay_ms: Option<u64>,
    pub restart_threshold: u32,
    pub max_restarts: u32,
    pub proxy_from_step: u32,
    pub qr_out_path: PathBuf,
    pub bundles_cache_dir: PathBuf,
    pub qr_watermark: String,
}

pub type LogFn = Arc<dyn Fn(&str) + Send + Sync>;

/// Mask email cho log (giữ 3 ký tự đầu + 2 cuối local part).
pub fn mask_email(email: &str) -> String {
    if let Some(at_idx) = email.find('@') {
        let local = &email[..at_idx]; // at_idx tại '@' (ASCII) → boundary an toàn
        let domain = &email[at_idx + 1..];
        let chars: Vec<char> = local.chars().collect();
        if chars.len() <= 3 {
            let head: String = chars.iter().take(1).collect();
            return format!("{}***@{}", head, domain);
        }
        let head: String = chars.iter().take(3).collect();
        let tail: String = chars.iter().skip(chars.len() - 2).collect();
        return format!("{}***{}@{}", head, tail, domain);
    }
    "***".into()
}

pub fn mask_proxy(proxy: &str) -> String {
    crate::proxy_format::mask_proxy(proxy)
}

/// Proxy cho step không-retry (2,3,4,5 + QR).
///   - step `>= from_step`        → proxy đầu pool (user proxy, hoặc login
///                                   proxy ở TH user không có proxy riêng).
///   - step `<  from_step`        → login proxy (None = DIRECT, giữ hành vi cũ).
fn proxy_for_step<'a>(
    proxy_pool: &'a [String],
    login_proxy: Option<&'a str>,
    from_step: u32,
    step: u32,
) -> Option<&'a str> {
    if step >= from_step {
        proxy_pool.first().map(|s| s.as_str())
    } else {
        login_proxy
    }
}

/// Pick proxy cho approve retry, SKIP các index trong `dead_proxies`.
/// Trả `(url, idx)` để caller track per-proxy stats. Khi tất cả pool dead
/// (hoặc step dùng login segment / pool rỗng) → trả `(None, None)` ⇒ caller
/// chạy DIRECT thay vì block flow.
fn proxy_for_retry_alive<'a>(
    proxy_pool: &'a [String],
    dead_proxies: &std::collections::HashSet<usize>,
    from_step: u32,
    step: u32,
    attempt: u32,
    per_proxy: u32,
) -> (Option<&'a str>, Option<usize>) {
    if step < from_step || proxy_pool.is_empty() {
        return (None, None);
    }
    let len = proxy_pool.len();
    if dead_proxies.len() >= len {
        return (None, None); // ALL dead → DIRECT (không block)
    }
    let start = ((attempt.saturating_sub(1)) / per_proxy) as usize % len;
    for offset in 0..len {
        let idx = (start + offset) % len;
        if !dead_proxies.contains(&idx) {
            return (Some(proxy_pool[idx].as_str()), Some(idx));
        }
    }
    (None, None)
}

fn is_backend_exception(att: &ApproveAttempt) -> bool {
    att.http_status == Some(200) && att.result.as_deref() == Some("exception")
}

fn is_network_error(att: &ApproveAttempt) -> bool {
    att.http_status.is_none()
}

async fn probe_connectivity(client: &HttpClient) -> bool {
    matches!(client.head("https://chatgpt.com/", 5).await, Ok(_))
}

async fn wait_network_recovery(client: &HttpClient, log: &LogFn) -> bool {
    let started = Instant::now();
    let mut poll_idx: u32 = 0;
    loop {
        if started.elapsed() > Duration::from_secs(NETWORK_RECOVERY_MAX_WAIT_S) {
            log(&format!(
                "[net]  outage      FAIL not recovered in {}s",
                NETWORK_RECOVERY_MAX_WAIT_S
            ));
            return false;
        }
        if probe_connectivity(client).await {
            log(&format!(
                "[net]  recovered   OK   after {:.0}s ({} probes)",
                started.elapsed().as_secs_f64(),
                poll_idx + 1
            ));
            return true;
        }
        poll_idx += 1;
        if poll_idx == 1 || poll_idx % 6 == 0 {
            log(&format!(
                "[net]  waiting     ...  poll={} elapsed={:.0}s max={}s",
                poll_idx,
                started.elapsed().as_secs_f64(),
                NETWORK_RECOVERY_MAX_WAIT_S
            ));
        }
        tokio::time::sleep(Duration::from_millis(NETWORK_RECOVERY_POLL_MS)).await;
    }
}

fn confirm_to_summary(a: &ConfirmAttempt, phase: u32) -> ConfirmAttemptSummary {
    ConfirmAttemptSummary {
        variant: a.variant.clone(),
        phase,
        http_status: a.http_status,
        ok: a.ok,
        keys: a.keys.clone(),
        error: a.error.clone(),
    }
}

fn approve_to_summary(
    a: &ApproveAttempt,
    variant: Option<&str>,
    attempt: u32,
    phase: u32,
    proxy: &str,
) -> ApproveAttemptSummary {
    ApproveAttemptSummary {
        variant: variant.map(|s| s.to_string()),
        attempt,
        phase,
        proxy: proxy.to_string(),
        http_status: a.http_status,
        ok: a.ok,
        result: a.result.clone(),
        error_type: a.error_type.clone(),
        error: a.error.clone(),
        keys: a.keys.clone(),
    }
}

fn refresh_to_summary(a: &RefreshAttempt, attempt: u32, proxy: &str) -> RefreshAttemptSummary {
    RefreshAttemptSummary {
        attempt,
        proxy: proxy.to_string(),
        http_status: a.http_status,
        ok: a.ok,
        error_type: a.error_type.clone(),
        error: a.error_msg.clone(),
        keys: a.keys.clone(),
    }
}

/// Login errors fatal — KHÔNG retry để tránh login spam → lockout. Port 1:1
/// pattern từ Python `session_phase.NON_RETRYABLE_LOGIN_PATTERNS` (lowercase
/// substring match). Lỗi không match → coi là transient (CF flaky / cookie
/// chậm / network) → retry.
pub fn is_fatal_login_error(msg: &str) -> bool {
    let lower = msg.to_lowercase();
    const FATAL: &[&str] = &[
        "password verify fail",
        "mfa verify fail",
        "no mail_provider available",
        "no secret provided",
        "yêu cầu 2fa nhưng không có",
        "otp polling returned empty",
        "passwordless otp",
        "không xác định được login flow",
    ];
    FATAL.iter().any(|p| lower.contains(p))
}

/// Refresh payment page với pool retry — port từ Python
/// `_stripe_payment_page_refresh_retry`. Thử LẦN LƯỢT từng proxy trong pool
/// (rotate cùng cách với approve loop) cho đến khi 1 proxy trả OK. Trả attempt
/// cuối + lý do nếu tất cả fail. Pool rỗng → thử 1 lần direct.
async fn payment_page_refresh_with_pool(
    client: &HttpClient,
    session_id: &str,
    publishable_key: &str,
    stripe_js_id: &str,
    elements_data: &Value,
    proxy_pool: &[String],
    login_proxy: Option<&str>,
    proxy_from_step: u32,
    // Index proxy đã được approve loop đánh dấu chết — skip ở đây để không
    // tốn thời gian retry proxy đã chắc chắn dead. Empty set ⇒ giữ hành vi
    // cũ (thử full pool).
    dead_proxies: &HashSet<usize>,
) -> crate::upi::endpoints::RefreshAttempt {
    use crate::upi::endpoints::{stripe_payment_page_refresh, RefreshAttempt};

    // Khi step 5 < proxy_from_step → segment login (login_proxy / direct).
    // Khi step 5 >= proxy_from_step → segment work (pool). Pool rỗng hoặc
    // tất cả dead = thử DIRECT 1 lần (không block — đỡ hơn skip refresh hoàn toàn).
    let candidates: Vec<Option<&str>> = if 5 < proxy_from_step {
        vec![login_proxy]
    } else {
        let alive: Vec<&str> = proxy_pool
            .iter()
            .enumerate()
            .filter(|(i, _)| !dead_proxies.contains(i))
            .map(|(_, s)| s.as_str())
            .collect();
        if alive.is_empty() {
            vec![None]
        } else {
            alive.into_iter().map(Some).collect()
        }
    };

    let mut last: Option<RefreshAttempt> = None;
    for proxy in candidates {
        match stripe_payment_page_refresh(
            client,
            session_id,
            publishable_key,
            stripe_js_id,
            elements_data,
            proxy,
        )
        .await
        {
            Ok(r) => {
                if r.ok {
                    return r;
                }
                last = Some(r);
            }
            Err(e) => {
                last = Some(RefreshAttempt {
                    http_status: None,
                    ok: false,
                    keys: vec![],
                    error: None,
                    error_type: Some("NetworkError".into()),
                    error_msg: Some(format!("{}", e)),
                    data: None,
                });
            }
        }
    }
    last.unwrap_or(RefreshAttempt {
        http_status: None,
        ok: false,
        keys: vec![],
        error: None,
        error_type: Some("NoCandidate".into()),
        error_msg: Some("no proxy candidates available".into()),
        data: None,
    })
}

pub async fn run_upi_qr(
    client: Arc<HttpClient>,
    cfg: UpiJobConfig,
    log: LogFn,
) -> UpiQrResult {
    let started = Instant::now();
    let masked_email = mask_email(&cfg.email);

    let restart_enabled = cfg.restart_threshold > 0 && cfg.max_restarts > 0;
    let proxy_advance_enabled = cfg.proxy_from_step <= 6
        && APPROVE_PROXY_BATCH > 1
        && cfg.proxy_pool.len() > 1;
    // Validate delay [2000, 60000] ms — đồng bộ Python `[2.0, 60.0]s`. Quá thấp
    // → Stripe rate-limit (`blocked` spam). Fallback default khi None hoặc invalid.
    let approve_delay_ms: u64 = cfg
        .approve_delay_ms
        .filter(|ms| (2_000..=60_000).contains(ms))
        .unwrap_or(APPROVE_DELAY_MS);

    log(&format!("Account: {}", masked_email));

    // Step 1 — resolve auth: session có sẵn HOẶC login HTTP (email|pass|2fa).
    let (access_token, cookie_header) = match &cfg.auth {
        AuthSource::Session {
            access_token,
            cookie_header,
        } => {
            log("[1/6] login   OK   session supplied");
            (access_token.clone(), cookie_header.clone())
        }
        AuthSource::Login {
            password,
            totp_secret,
        } => {
            // Login dùng login proxy (admin-set) nếu có. Nếu chưa cấu hình →
            // fallback proxy đầu pool (hành vi cũ: residential IP qua Cloudflare
            // tốt hơn router IP). KHÔNG dùng user proxy cho login khi đã có
            // login proxy riêng (tách IP login vs IP flow).
            let login_proxy = cfg
                .login_proxy
                .as_deref()
                .or_else(|| cfg.proxy_pool.first().map(|s| s.as_str()));
            log(&format!(
                "[1/6] login   →    HTTP login (email|pass|2fa) proxy={}",
                login_proxy.map(mask_proxy).unwrap_or_else(|| "direct".into())
            ));
            // Login retry — port từ Python `LOGIN_MAX_ATTEMPTS=3, RETRY_DELAY=3s`.
            // Lỗi transient (CF flaky / cookie chậm / WARNING_BANNER) retry tới
            // 3 lần; lỗi fatal (sai password/MFA/passwordless) bỏ ngay.
            const LOGIN_MAX_ATTEMPTS: u32 = 3;
            const LOGIN_RETRY_DELAY_MS: u64 = 3000;
            let mut session_opt: Option<crate::auth::LoginSession> = None;
            let mut last_err: Option<String> = None;
            for attempt in 1..=LOGIN_MAX_ATTEMPTS {
                match crate::auth::login_pure_request(
                    &cfg.email,
                    password,
                    totp_secret,
                    login_proxy,
                    &log,
                )
                .await
                {
                    Ok(sess) => {
                        if attempt > 1 {
                            log(&format!(
                                "[1/6] login   OK   attempt {}/{} (cookies={})",
                                attempt, LOGIN_MAX_ATTEMPTS, sess.cookie_count
                            ));
                        } else {
                            log(&format!(
                                "[1/6] login   OK   session acquired (cookies={})",
                                sess.cookie_count
                            ));
                        }
                        session_opt = Some(sess);
                        break;
                    }
                    Err(e) => {
                        let msg = format!("{}", e);
                        last_err = Some(msg.clone());
                        if is_fatal_login_error(&msg) {
                            log(&format!(
                                "[1/6] login   FAIL fatal: {}",
                                short_msg(&msg, 200)
                            ));
                            break;
                        }
                        if attempt >= LOGIN_MAX_ATTEMPTS {
                            log(&format!(
                                "[1/6] login   FAIL after {} attempts: {}",
                                LOGIN_MAX_ATTEMPTS,
                                short_msg(&msg, 200)
                            ));
                            break;
                        }
                        log(&format!(
                            "[1/6] login   WARN transient (attempt {}/{}): {} → retry sau {}s",
                            attempt,
                            LOGIN_MAX_ATTEMPTS,
                            short_msg(&msg, 140),
                            LOGIN_RETRY_DELAY_MS / 1000
                        ));
                        tokio::time::sleep(Duration::from_millis(LOGIN_RETRY_DELAY_MS)).await;
                    }
                }
            }
            // Tất cả attempts qua login proxy đều fail (non-fatal) → thử 1
            // attempt cuối cùng DIRECT trước khi declare failure. Áp dụng khi:
            //   - Đã có dùng proxy (login_proxy.is_some())
            //   - Không phải lỗi fatal (sai password/MFA/passwordless/...)
            //   - Vẫn chưa có session
            // ⇒ Đảm bảo "all proxy die không block process" mở rộng đến login
            // segment, không chỉ approve loop.
            if session_opt.is_none() && login_proxy.is_some() {
                let last_msg = last_err.clone().unwrap_or_default();
                if !is_fatal_login_error(&last_msg) {
                    log("[1/6] login   WARN proxy attempts exhausted (non-fatal) → fallback DIRECT 1 attempt");
                    match crate::auth::login_pure_request(
                        &cfg.email, password, totp_secret, None, &log,
                    )
                    .await
                    {
                        Ok(sess) => {
                            log(&format!(
                                "[1/6] login   OK   via DIRECT fallback (cookies={})",
                                sess.cookie_count
                            ));
                            session_opt = Some(sess);
                        }
                        Err(e) => {
                            let combined = format!(
                                "{} | direct fallback also failed: {}",
                                short_msg(&last_msg, 200),
                                short_msg(&format!("{}", e), 200)
                            );
                            log(&format!("[1/6] login   FAIL {}", combined));
                            last_err = Some(combined);
                        }
                    }
                }
            }
            match session_opt {
                Some(sess) => (sess.access_token, sess.cookie_header),
                None => {
                    return finalize_error(
                        masked_email,
                        started,
                        format!(
                            "login fail: {}",
                            short_msg(&last_err.unwrap_or_else(|| "unknown".into()), 300)
                        ),
                    );
                }
            }
        }
    };
    let auth = UpiAuth {
        email: cfg.email.clone(),
        access_token,
        cookie_header,
    };

    // Login proxy ref cho các step TRƯỚC proxy_from_step (segment login).
    let login_proxy_ref = cfg.login_proxy.as_deref();

    let stripe_js_id = uuid::Uuid::new_v4().to_string();
    let profile = random_india_profile();
    let bundle_cache = BundleCache::new(cfg.bundles_cache_dir.clone());

    let mut confirm_attempts: Vec<ConfirmAttemptSummary> = Vec::new();
    let mut approve_attempts: Vec<ApproveAttemptSummary> = Vec::new();
    let mut refresh_attempts: Vec<RefreshAttemptSummary> = Vec::new();

    let mut backend_exception_count: u32 = 0;
    let mut fatal_approve_error: Option<String> = None;
    let mut amount: i64 = 0;
    let mut return_url = String::new();
    let mut session_id = String::new();
    #[allow(unused_assignments)]
    let mut publishable_key = String::new();
    let mut approved = false;
    let mut final_confirmed = false;
    let mut approve_index_total: u32 = 0;
    let mut proxy_virtual_attempt: u32 = 0;
    let mut restart_count: u32 = 0;
    let mut token_config: Option<StripeTokenConfig> = None;

    // Per-phase context — overwrite mỗi phase, dùng cho aggregate match.
    let mut checkout_value = Value::Null;
    let mut init_data = Value::Null;
    let mut elements_data = Value::Null;
    let mut last_confirm_data: Value = Value::Null;
    let mut last_refresh_data: Value = Value::Null;
    let mut all_confirm_data: Vec<(String, Value)> = Vec::new();
    let mut all_approve_data: Vec<(String, Value)> = Vec::new();
    let mut all_refresh_data: Vec<Value> = Vec::new();

    'phase_loop: loop {
        let phase_idx = restart_count + 1;
        let phase_tag = if restart_enabled {
            format!(" [p{}]", phase_idx)
        } else {
            String::new()
        };
        let mut triggered_restart = false;

        if restart_count > 0 {
            log(&format!(
                "[restart] phase {}/{}  approve_idx kept at {}/{}",
                phase_idx,
                cfg.max_restarts + 1,
                approve_index_total,
                cfg.approve_retries
            ));
        }

        // Step 2 — checkout (retry network errors)
        log(&format!(
            "[2/6{}] checkout   →    requesting...",
            phase_tag
        ));
        let mut checkout_result = None;
        for attempt in 1..=3u32 {
            match create_chatgpt_checkout(
                &client,
                &auth,
                proxy_for_step(&cfg.proxy_pool, login_proxy_ref, cfg.proxy_from_step, 2),
            )
            .await
            {
                Ok(co) => {
                    checkout_result = Some(Ok(co));
                    break;
                }
                Err(e) => {
                    let msg = format!("{}", e);
                    if msg.contains("checkout HTTP") {
                        // Server reject (HTTP 4xx/5xx) — log nguyên message + fail-fast.
                        log(&format!(
                            "[2/6{}] checkout   FAIL {}",
                            phase_tag,
                            short_msg(&msg, 200)
                        ));
                        checkout_result = Some(Err(e));
                        break;
                    }
                    if attempt < 3 {
                        log(&format!(
                            "[2/6{}] checkout   WARN attempt {}/3: {} → retry in 2s",
                            phase_tag,
                            attempt,
                            short_msg(&msg, 160)
                        ));
                        tokio::time::sleep(Duration::from_secs(2)).await;
                        continue;
                    }
                    log(&format!(
                        "[2/6{}] checkout   FAIL all 3 attempts failed: {}",
                        phase_tag,
                        short_msg(&msg, 200)
                    ));
                    checkout_result = Some(Err(e));
                }
            }
        }
        match checkout_result.unwrap() {
            Ok(co) => {
                session_id = co.session_id.clone();
                return_url = format!("https://checkout.stripe.com/c/pay/{}", session_id);
                publishable_key = co.publishable_key;
                checkout_value = co.raw;
                log(&format!(
                    "[2/6{}] checkout   OK   cs={} ui={}",
                    phase_tag,
                    short(&session_id, 14),
                    co.checkout_ui_mode.unwrap_or_else(|| "-".into())
                ));
            }
            Err(e) => {
                let msg = format!("phase {} checkout fail: {}", phase_idx, e);
                if restart_count == 0 {
                    return finalize_error(masked_email, started, msg);
                }
                fatal_approve_error = Some(msg.clone());
                break 'phase_loop;
            }
        }

        // Step 3 — Stripe init
        log(&format!("[3/6{}] init       →    requesting...", phase_tag));
        match stripe_init(
            &client,
            &session_id,
            &publishable_key,
            &stripe_js_id,
            proxy_for_step(&cfg.proxy_pool, login_proxy_ref, cfg.proxy_from_step, 3),
        )
        .await
        {
            Ok(d) => {
                amount = extract_amount(&d);
                let id = d.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string();
                init_data = d;
                log(&format!(
                    "[3/6{}] init       OK   amount={} ppage={}",
                    phase_tag,
                    amount,
                    short(&id, 12)
                ));
                if PROMO && amount > 0 {
                    log(&format!(
                        "[upi] no free offer   FAIL amount={} (promo enabled but > 0)",
                        amount
                    ));
                    if restart_count == 0 {
                        let mut r = UpiQrResult {
                            ok: false,
                            email: masked_email.clone(),
                            amount,
                            return_url: return_url.clone(),
                            checkout_session: short(&session_id, 18),
                            error: Some("no free offer (promo enabled but amount > 0)".into()),
                            elapsed_seconds: started.elapsed().as_secs_f64(),
                            ..Default::default()
                        };
                        r.ok = false;
                        return r;
                    }
                    fatal_approve_error =
                        Some(format!("phase {} no free offer (amount={})", phase_idx, amount));
                    break 'phase_loop;
                }
            }
            Err(e) => {
                let msg = format!("phase {} init fail: {}", phase_idx, e);
                if restart_count == 0 {
                    return finalize_error(masked_email, started, msg);
                }
                fatal_approve_error = Some(msg.clone());
                log(&format!("[3/6{}] init       FAIL {}", phase_tag, &msg));
                break 'phase_loop;
            }
        }

        // Step 4 — elements
        log(&format!("[4/6{}] elements   →    requesting...", phase_tag));
        match stripe_elements_session(
            &client,
            &session_id,
            &publishable_key,
            &stripe_js_id,
            amount,
            proxy_for_step(&cfg.proxy_pool, login_proxy_ref, cfg.proxy_from_step, 4),
        )
        .await
        {
            Ok(d) => {
                let sid = d
                    .get("session_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                elements_data = d;
                log(&format!(
                    "[4/6{}] elements   OK   session={}",
                    phase_tag,
                    short(&sid, 14)
                ));
            }
            Err(e) => {
                let msg = format!("phase {} elements fail: {}", phase_idx, e);
                if restart_count == 0 {
                    return finalize_error(masked_email, started, msg);
                }
                fatal_approve_error = Some(msg.clone());
                log(&format!("[4/6{}] elements   FAIL {}", phase_tag, &msg));
                break 'phase_loop;
            }
        }

        // Step 5a — token config (chỉ phase 1)
        if restart_count == 0 {
            log("[5a]   token-cfg  →    fetching Stripe bundle...");
            match extract_config_live(&client, &bundle_cache).await {
                Ok(cfg2) => {
                    log(&format!(
                        "[5a]   token-cfg  OK   shift={} rv={}",
                        cfg2.shift,
                        short(&cfg2.rv, 8)
                    ));
                    token_config = Some(cfg2);
                }
                Err(e) => {
                    log(&format!("[5a]   token-cfg  WARN extract fail: {}", e));
                }
            }
        }

        // Step 5b — confirm variants
        let mut phase_confirmed = false;
        let mut confirm_variant_used: Option<String> = None;
        for variant in CONFIRM_VARIANTS {
            let proxy = proxy_for_step(&cfg.proxy_pool, login_proxy_ref, cfg.proxy_from_step, 5);
            let attempt = match stripe_confirm_upi_qr(
                &client,
                &session_id,
                &publishable_key,
                &stripe_js_id,
                &init_data,
                &elements_data,
                &profile,
                &cfg.email,
                amount,
                variant,
                token_config.as_ref(),
                proxy,
            )
            .await
            {
                Ok(a) => a,
                Err(e) => {
            log(&format!(
                "[5b{}] confirm    FAIL network: {}",
                phase_tag, e
            ));
                    ConfirmAttempt {
                        variant: variant.to_string(),
                        http_status: None,
                        ok: false,
                        keys: vec![],
                        error: Some(Value::String(e.to_string())),
                        data: None,
                    }
                }
            };
            confirm_attempts.push(confirm_to_summary(&attempt, phase_idx));
            if let Some(ref d) = attempt.data {
                last_confirm_data = d.clone();
                all_confirm_data.push((variant.to_string(), d.clone()));
            }
            log(&format!(
                "[5b{}] confirm    {}    variant={} http={}",
                phase_tag,
                if attempt.ok { "OK  " } else { "FAIL" },
                variant,
                attempt
                    .http_status
                    .map(|s| s.to_string())
                    .unwrap_or_else(|| "—".into())
            ));
            if attempt.ok {
                phase_confirmed = true;
                confirm_variant_used = Some(variant.to_string());
                break;
            }
        }

        if !phase_confirmed {
            if restart_count == 0 {
                break 'phase_loop;
            }
            fatal_approve_error =
                Some(format!("phase {} confirm failed (all variants)", phase_idx));
            log(&format!(
                "[5b{}] confirm    FAIL all variants failed in restart phase",
                phase_tag
            ));
            break 'phase_loop;
        }
        final_confirmed = true;

        // Step 5c — page refresh trước approve. Dùng pool retry: thử lần lượt
        // từng proxy live → giảm khả năng fail bước 5c (best-effort cho aggregate
        // matches QR). Pre-approve nên chưa có dead_proxies → pass empty set
        // (thử full pool).
        let empty_dead: HashSet<usize> = HashSet::new();
        let r = payment_page_refresh_with_pool(
            &client,
            &session_id,
            &publishable_key,
            &stripe_js_id,
            &elements_data,
            &cfg.proxy_pool,
            login_proxy_ref,
            cfg.proxy_from_step,
            &empty_dead,
        )
        .await;
        if let Some(ref d) = r.data {
            last_refresh_data = d.clone();
            all_refresh_data.push(d.clone());
        }
        log(&format!(
            "[5c{}] refresh    {}    http={}",
            phase_tag,
            if r.ok { "OK  " } else { "FAIL" },
            r.http_status
                .map(|s| s.to_string())
                .unwrap_or_else(|| "—".into())
        ));
        refresh_attempts.push(refresh_to_summary(&r, 1, "direct"));

        // Step 6 — approve loop
        if restart_count == 0 {
            log(&format!(
                "[6/6] approve loop start  retries={} delay={:.1}s batch={}",
                cfg.approve_retries,
                approve_delay_ms as f64 / 1000.0,
                APPROVE_PROXY_BATCH
            ));
        } else {
            log(&format!(
                "[6/6{}] approve resume  from {}/{}",
                phase_tag, approve_index_total, cfg.approve_retries
            ));
        }
        let mut consec_be: u32 = 0;
        let mut consec_net: u32 = 0;
        // Log body chi tiết cho mỗi http status mới gặp trong approve loop —
        // giúp phân biệt Cloudflare edge block (HTML, cf-ray) vs OpenAI app
        // error (JSON detail) để chẩn đoán proxy reputation vs auth/session.
        let mut seen_error_statuses: HashSet<u16> = HashSet::new();
        // Track batch đã warm CF cookie — port từ Python `_warm_cf_cookie` khi
        // sang batch proxy mới. Mỗi batch chỉ warm 1 lần (cookie đủ cho cả batch).
        let mut warmed_batches: HashSet<u32> = HashSet::new();
        // Runtime proxy health: proxy_pool[idx] gặp ≥ DEAD_THRESHOLD network
        // error LIÊN TIẾP → đánh dấu dead, skip ở mọi lần pick sau. Khi tất
        // cả pool dead → next pick trả None ⇒ chạy DIRECT (không block job).
        // Per-job (không update PROXY_STATUS toàn cục) vì lỗi có thể cục bộ
        // theo IP/account, các job khác vẫn dùng được proxy đó.
        const DEAD_THRESHOLD: u32 = 2;
        let mut dead_proxies: HashSet<usize> = HashSet::new();
        let mut proxy_consec_errors: HashMap<usize, u32> = HashMap::new();
        let mut all_dead_logged = false;

        let approve_started = Instant::now();
        while approve_index_total < cfg.approve_retries {
            approve_index_total += 1;
            proxy_virtual_attempt += 1;
            let (proxy_url, current_proxy_idx) = proxy_for_retry_alive(
                &cfg.proxy_pool,
                &dead_proxies,
                cfg.proxy_from_step,
                6,
                proxy_virtual_attempt,
                APPROVE_PROXY_BATCH,
            );
            // Pool có proxy nhưng tất cả đã dead → từ bây giờ chạy DIRECT.
            // Log thông báo 1 lần để dễ đọc log (không spam mỗi attempt).
            if proxy_url.is_none()
                && !cfg.proxy_pool.is_empty()
                && cfg.proxy_from_step <= 6
                && !all_dead_logged
            {
                log(&format!(
                    "[6/6{}] proxy       WARN all {} proxy dead → fallback DIRECT (job tiếp tục)",
                    phase_tag,
                    cfg.proxy_pool.len()
                ));
                all_dead_logged = true;
            }
            // Sang batch mới → warm CF cookie cho proxy (giảm 403 hit đầu).
            // Best-effort, không block flow nếu fail.
            if proxy_advance_enabled && proxy_url.is_some() {
                let current_batch = (proxy_virtual_attempt - 1) / APPROVE_PROXY_BATCH;
                if warmed_batches.insert(current_batch) {
                    crate::upi::endpoints::warm_cf_cookie(&client, proxy_url, &log).await;
                }
            }
            let attempt = match chatgpt_approve_checkout(&client, &auth, &session_id, proxy_url)
                .await
            {
                Ok(a) => a,
                Err(e) => ApproveAttempt {
                    http_status: None,
                    ok: false,
                    result: None,
                    keys: vec![],
                    error_type: Some("NetworkError".into()),
                    error: Some(format!("{}", e)),
                    data: None,
                },
            };
            let proxy_mask = proxy_url.map(mask_proxy).unwrap_or_else(|| "direct".into());
            // Update per-proxy health từ kết quả attempt:
            //   - network error (HTTP None) → tăng consec, đạt threshold → mark dead
            //   - server có reply (2xx/4xx/5xx) → proxy còn forward được → reset
            if let Some(idx) = current_proxy_idx {
                if is_network_error(&attempt) {
                    let entry = proxy_consec_errors.entry(idx).or_insert(0);
                    *entry += 1;
                    if *entry >= DEAD_THRESHOLD && !dead_proxies.contains(&idx) {
                        dead_proxies.insert(idx);
                        let alive_left = cfg.proxy_pool.len().saturating_sub(dead_proxies.len());
                        log(&format!(
                            "[6/6{}] proxy       DEAD {} ({} consec network err) → out of pool (alive {}/{})",
                            phase_tag,
                            mask_proxy(&cfg.proxy_pool[idx]),
                            *entry,
                            alive_left,
                            cfg.proxy_pool.len()
                        ));
                        proxy_consec_errors.remove(&idx);
                    }
                } else {
                    proxy_consec_errors.remove(&idx);
                }
            }
            approve_attempts.push(approve_to_summary(
                &attempt,
                confirm_variant_used.as_deref(),
                approve_index_total,
                phase_idx,
                &proxy_mask,
            ));
            if let Some(ref d) = attempt.data {
                let v = confirm_variant_used.clone().unwrap_or_default();
                all_approve_data.push((v, d.clone()));
            }
            log(&format!(
                "      try {:03}/{:03}  {}  http={:>3}  {:<10} proxy={}",
                approve_index_total,
                cfg.approve_retries,
                if attempt.ok { "OK  " } else { "FAIL" },
                attempt
                    .http_status
                    .map(|s| s.to_string())
                    .unwrap_or_else(|| "---".into()),
                attempt
                    .result
                    .clone()
                    .or_else(|| attempt.error_type.clone())
                    .unwrap_or_else(|| "—".into()),
                proxy_mask
            ));
            // Lần đầu gặp 1 status code lỗi (>=400) → log thêm body short để
            // chẩn đoán Cloudflare vs OpenAI. De-dup theo status để tránh spam
            // khi 100 attempt đều cùng 403.
            if !attempt.ok {
                if let Some(status) = attempt.http_status {
                    if status >= 400 && seen_error_statuses.insert(status) {
                        if let Some(body) = attempt.error.as_deref() {
                            log(&format!(
                                "            first-seen http={} type={} body={}",
                                status,
                                attempt.error_type.as_deref().unwrap_or("-"),
                                body
                            ));
                        }
                    }
                }
            }
            if attempt.ok {
                approved = true;
                break;
            }
            if is_network_error(&attempt) {
                consec_net += 1;
                if consec_net >= NETWORK_FAIL_DETECT {
                    log(&format!(
                        "[net]  outage      WARN {} timeouts → pause loop, polling connectivity",
                        consec_net
                    ));
                    if wait_network_recovery(&client, &log).await {
                        consec_net = 0;
                        continue;
                    }
                    fatal_approve_error = Some(format!(
                        "network outage not recovered in {}s (consec={})",
                        NETWORK_RECOVERY_MAX_WAIT_S, consec_net
                    ));
                    break;
                }
            } else if is_backend_exception(&attempt) {
                consec_net = 0;
                backend_exception_count += 1;
                consec_be += 1;
                if restart_enabled
                    && consec_be >= cfg.restart_threshold
                    && restart_count < cfg.max_restarts
                {
                    triggered_restart = true;
                    log(&format!(
                        "[6/6{}] approve     WARN consec exceptions {}/{} → restart ({}/{})",
                        phase_tag,
                        consec_be,
                        cfg.restart_threshold,
                        restart_count + 1,
                        cfg.max_restarts
                    ));
                    break;
                }
                if APPROVE_BACKEND_EXCEPTION_CONSECUTIVE > 0
                    && consec_be >= APPROVE_BACKEND_EXCEPTION_CONSECUTIVE
                {
                    fatal_approve_error = Some(format!(
                        "approve consec exception threshold ({}/{}) total={}",
                        consec_be, APPROVE_BACKEND_EXCEPTION_CONSECUTIVE, backend_exception_count
                    ));
                    log(&format!(
                        "[6/6] approve     FAIL consec exception {}/{}",
                        consec_be, APPROVE_BACKEND_EXCEPTION_CONSECUTIVE
                    ));
                    break;
                }
                if proxy_advance_enabled {
                    let current_batch = (proxy_virtual_attempt - 1) / APPROVE_PROXY_BATCH;
                    let pos_in_batch =
                        proxy_virtual_attempt - current_batch * APPROVE_PROXY_BATCH;
                    if pos_in_batch < APPROVE_PROXY_BATCH {
                        proxy_virtual_attempt = (current_batch + 1) * APPROVE_PROXY_BATCH;
                    }
                }
            } else {
                let http = attempt.http_status;
                let res = attempt.result.clone();
                if http == Some(200) && res.as_deref().map_or(false, |s| s != "exception") {
                    consec_net = 0;
                    if consec_be > 0 {
                        log(&format!(
                            "[6/6] approve     INFO reset consec exception ({} → 0) result={}",
                            consec_be,
                            res.as_deref().unwrap_or("—")
                        ));
                        consec_be = 0;
                    }
                } else {
                    consec_net = 0;
                }
            }
            if approve_index_total < cfg.approve_retries {
                tokio::time::sleep(Duration::from_millis(approve_delay_ms)).await;
            }
        }
        let approve_elapsed = approve_started.elapsed().as_secs_f64();
        if approved {
            log(&format!(
                "[6/6] approve     OK   approved at {}/{} ({:.1}s, restarts={})",
                approve_index_total, cfg.approve_retries, approve_elapsed, restart_count
            ));
        }

        // Refresh post-approve (best-effort) — pool retry để tăng cơ hội lấy
        // hosted_instructions_url cho QR. Skip các proxy approve loop đã đánh
        // dấu chết — đỡ tốn thời gian + đỡ pollute log.
        if !triggered_restart && fatal_approve_error.is_none() && (approved || !approve_attempts.is_empty()) {
            let r = payment_page_refresh_with_pool(
                &client,
                &session_id,
                &publishable_key,
                &stripe_js_id,
                &elements_data,
                &cfg.proxy_pool,
                login_proxy_ref,
                cfg.proxy_from_step,
                &dead_proxies,
            )
            .await;
            if let Some(ref d) = r.data {
                last_refresh_data = d.clone();
                all_refresh_data.push(d.clone());
            }
            log(&format!(
                "[5c{}] refresh    {}    http={}",
                phase_tag,
                if r.ok { "OK  " } else { "FAIL" },
                r.http_status
                    .map(|s| s.to_string())
                    .unwrap_or_else(|| "—".into())
            ));
            refresh_attempts.push(refresh_to_summary(&r, 2, "direct"));
        }

        if approved || fatal_approve_error.is_some() {
            break 'phase_loop;
        }
        if approve_index_total >= cfg.approve_retries {
            log(&format!(
                "[6/6] approve     FAIL not approved after {} attempts ({:.1}s, restarts={})",
                cfg.approve_retries, approve_elapsed, restart_count
            ));
            break 'phase_loop;
        }
        if triggered_restart {
            restart_count += 1;
            continue 'phase_loop;
        }
        break 'phase_loop;
    }

    // Avoid unused_variable warnings on temp vars used only for cumulative match below.
    let _ = (&last_confirm_data, &last_refresh_data);

    // Aggregate matches
    let mut matches: Vec<Match> = Vec::new();
    matches.extend(find_matches(&checkout_value, "chatgpt_checkout"));
    matches.extend(find_matches(&init_data, "stripe_init"));
    matches.extend(find_matches(&elements_data, "stripe_elements"));
    for (variant, d) in &all_confirm_data {
        matches.extend(find_matches(d, &format!("confirm:{}", variant)));
    }
    for (variant, d) in &all_approve_data {
        matches.extend(find_matches(d, &format!("approve:{}", variant)));
    }
    for (i, d) in all_refresh_data.iter().enumerate() {
        matches.extend(find_matches(d, &format!("payment_page_refresh:{}", i + 1)));
    }
    let upi_uri = find_upi_uri(&matches);
    let qr_image_url = find_qr_image_url(&matches);
    let qr_expires_at = find_qr_expires_at(&matches);
    let payment_link = find_hosted_instructions_url(&matches);

    let mut qr_path: Option<String> = None;
    let mut qr_source: Option<String> = None;
    let mut qr_reason: Option<String> = None;

    if let Some(url) = &qr_image_url {
        let ext = if url.to_lowercase().ends_with(".svg") {
            "svg"
        } else {
            "png"
        };
        let target = cfg.qr_out_path.with_extension(ext);
        let watermark = if cfg.qr_watermark.is_empty() {
            None
        } else {
            Some(cfg.qr_watermark.as_str())
        };
        match download_qr_image(
            &client,
            url,
            &target,
            proxy_for_step(&cfg.proxy_pool, login_proxy_ref, cfg.proxy_from_step, 5),
            watermark,
        )
        .await
        {
            Ok(d) if d.rendered => {
                qr_path = d.path.map(|p| p.to_string_lossy().to_string());
                qr_source = Some(d.source.unwrap_or_else(|| "stripe_image".into()));
            }
            Ok(d) => {
                qr_reason = d.reason.or_else(|| Some("stripe image download fail".into()));
            }
            Err(e) => {
                qr_reason = Some(format!("download fail: {}", e));
            }
        }
    } else if let Some(uri) = &upi_uri {
        let watermark = if cfg.qr_watermark.is_empty() {
            None
        } else {
            Some(cfg.qr_watermark.as_str())
        };
        match render_qr_png(uri, &cfg.qr_out_path, watermark) {
            Ok(()) => {
                qr_path = Some(cfg.qr_out_path.to_string_lossy().to_string());
                qr_source = Some("upi_uri".into());
            }
            Err(e) => qr_reason = Some(format!("qrcode render fail: {}", e)),
        }
    } else {
        qr_reason = Some("no upi:// URI or QR image URL found in any response".into());
    }

    if qr_path.is_some() {
        log(&format!(
            "[QR]  ready       OK   expires_at={}",
            qr_expires_at
                .map(|n| n.to_string())
                .unwrap_or_else(|| "—".into())
        ));
    } else {
        log(&format!(
            "[QR]  ready       FAIL {}",
            qr_reason.clone().unwrap_or_else(|| "unknown".into())
        ));
    }

    let elapsed = started.elapsed().as_secs_f64();
    let error_msg = if let Some(ref e) = fatal_approve_error {
        Some(e.clone())
    } else if !final_confirmed {
        Some("confirm thất bại với mọi variant".into())
    } else if !approved {
        Some(format!(
            "approve failed after {} attempts (retries={})",
            approve_attempts.len(),
            cfg.approve_retries
        ))
    } else if qr_path.is_none() {
        qr_reason.clone().or_else(|| Some("no QR generated".into()))
    } else {
        None
    };
    let ok = error_msg.is_none();

    log(&format!(
        "[done] {}  qr={} approved={} restarts={} total={:.1}s{}",
        if ok { "OK  " } else { "FAIL" },
        if qr_path.is_some() { "yes" } else { "no" },
        if approved { "yes" } else { "no" },
        restart_count,
        elapsed,
        error_msg
            .as_deref()
            .map(|e| format!("  error={}", e))
            .unwrap_or_default()
    ));

    UpiQrResult {
        ok,
        email: masked_email,
        amount,
        return_url,
        checkout_session: short(&session_id, 18),
        qr_path,
        qr_source,
        qr_source_url: qr_image_url,
        qr_reason,
        qr_expires_at,
        payment_link,
        has_upi_uri: upi_uri.is_some(),
        has_qr_image_url: false, // re-set below
        confirm_attempts,
        approve_attempts,
        page_refresh_attempts: refresh_attempts,
        backend_exception_count,
        restart_count,
        error: error_msg,
        elapsed_seconds: elapsed,
    }
}

fn finalize_error(masked: String, started: Instant, msg: String) -> UpiQrResult {
    UpiQrResult {
        ok: false,
        email: masked,
        error: Some(msg),
        elapsed_seconds: started.elapsed().as_secs_f64(),
        ..Default::default()
    }
}

fn short(s: &str, head: usize) -> String {
    if s.chars().count() <= head {
        s.to_string()
    } else {
        let h: String = s.chars().take(head).collect();
        format!("{}…", h)
    }
}

/// Cắt error message dài (vd: full reqwest error chain) — giữ head + tail.
fn short_msg(s: &str, max: usize) -> String {
    let s = s.replace('\n', " ");
    if s.chars().count() <= max {
        s
    } else {
        let take_head = max.saturating_sub(20);
        let head: String = s.chars().take(take_head).collect();
        format!("{}…", head)
    }
}
