//! Login HTTP thuần (email|password|2fa) — port từ
//! `session_phase.get_session_pure_request` + `request_phase.py`.
//!
//! Flow (password + TOTP, không browser):
//!   0. prime chatgpt.com → CSRF
//!   1. signin/openai → authorize URL (auth.openai.com)
//!   2. GET authorize → device_id (cookie oai-did), landing → detect flow
//!   3. password/verify (kèm sentinel token)
//!   4. MFA issue + verify (TOTP secret)
//!   5. follow redirect chain → callback `code=`
//!   6. consume callback → set `__Secure-next-auth.session-token`
//!   7. GET /api/auth/session → accessToken + export cookies
//!
//! KHÁC Python: không có TLS-impersonate rotation (reqwest+rustls không giả
//! JA3). Nếu Cloudflare chặn fingerprint, login sẽ fail-fast với message rõ —
//! KHÔNG fallback che lỗi. Dùng proxy residential để tăng tỉ lệ pass.

pub mod client;
pub mod sentinel;
pub mod totp;

use crate::auth::client::{absolutize, LoginBody, LoginClient, LoginResponse};
use crate::auth::sentinel::get_sentinel_token;
use crate::upi::runner::mask_email;
use anyhow::{anyhow, Result};
use once_cell::sync::Lazy;
use regex::Regex;
use reqwest::Method;
use serde_json::{json, Value};
use std::sync::Arc;
use std::time::Duration;

/// Log callback — đồng nhất với `upi::runner::LogFn` (Arc<dyn Fn(&str)>).
pub type LogFn = Arc<dyn Fn(&str) + Send + Sync>;

/// Session lấy được sau login, sẵn sàng cho UPI flow.
pub struct LoginSession {
    pub access_token: String,
    pub cookie_header: String,
    pub cookie_count: usize,
}

#[derive(Debug, Clone, Copy, PartialEq)]
enum Flow {
    Password,
    Otp,
}

static MFA_CHALLENGE_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"/mfa-challenge/([a-f0-9]+)").unwrap());

fn short(s: &str, n: usize) -> String {
    if s.chars().count() <= n {
        s.to_string()
    } else {
        let h: String = s.chars().take(n).collect();
        format!("{}…", h)
    }
}

async fn sleep_secs(n: u64) {
    tokio::time::sleep(Duration::from_secs(n)).await;
}

fn common_json_headers<'a>(referer: &'a str, origin: &'a str) -> [(&'a str, &'a str); 4] {
    // UA + sec-ch-ua + Accept-Encoding do wreq Emulation::Chrome137 tự set
    // (khớp JA3) — KHÔNG override ở đây.
    [
        ("Accept", "application/json"),
        ("Accept-Language", "en-US,en;q=0.9"),
        ("Referer", referer),
        ("Origin", origin),
    ]
}

fn nav_headers_html<'a>(referer: &'a str, fetch_site: &'a str) -> [(&'a str, &'a str); 8] {
    [
        ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
        ("Accept-Language", "en-US,en;q=0.9"),
        ("Referer", referer),
        ("Sec-Fetch-Dest", "document"),
        ("Sec-Fetch-Mode", "navigate"),
        ("Sec-Fetch-Site", fetch_site),
        ("Sec-Fetch-User", "?1"),
        ("Upgrade-Insecure-Requests", "1"),
    ]
}

// ─── Steps ────────────────────────────────────────────────────────────

async fn prime(client: &LoginClient, log: &LogFn) -> Result<()> {
    if client.get_cookie("__cf_bm").is_some() {
        return Ok(());
    }
    log("[login] [0/9] prime chatgpt.com (GET /auth/login)");
    let headers = nav_headers_html("https://chatgpt.com/", "same-origin");
    for attempt in 0..3u32 {
        let resp = client
            .get_follow("https://chatgpt.com/auth/login", &headers, 12)
            .await?;
        if resp.status == 403 && attempt < 2 {
            let wait = (attempt + 1) as u64 * 5;
            log(&format!("[login] prime 403 → retry in {}s", wait));
            sleep_secs(wait).await;
            continue;
        }
        if resp.status >= 400 {
            return Err(anyhow!("prime chatgpt fail: HTTP {}", resp.status));
        }
        return Ok(());
    }
    Ok(())
}

async fn step_csrf(client: &LoginClient, log: &LogFn) -> Result<String> {
    prime(client, log).await?;
    log("[login] [1/9] CSRF token");
    let headers = common_json_headers("https://chatgpt.com/auth/login", "https://chatgpt.com");
    for attempt in 0..3u32 {
        let resp = client
            .send(Method::GET, "https://chatgpt.com/api/auth/csrf", &headers, LoginBody::None)
            .await?;
        if resp.status == 403 && attempt < 2 {
            let wait = (attempt + 1) as u64 * 5;
            log(&format!("[login] CSRF 403 → retry in {}s", wait));
            sleep_secs(wait).await;
            continue;
        }
        if resp.status != 200 {
            return Err(anyhow!("CSRF fetch fail: HTTP {}", resp.status));
        }
        let v = resp.json()?;
        let csrf = v.get("csrfToken").and_then(|x| x.as_str()).unwrap_or("");
        if csrf.is_empty() {
            return Err(anyhow!("CSRF token missing from response"));
        }
        return Ok(csrf.to_string());
    }
    Err(anyhow!("CSRF fetch fail after retries"))
}

async fn step_auth_url(
    client: &LoginClient,
    csrf: &str,
    device_id: &str,
    login_hint: &str,
    log: &LogFn,
) -> Result<String> {
    log("[login] [2/9] authorize URL");
    let mut q: Vec<(String, String)> = vec![
        ("prompt".into(), "login".into()),
        ("ext-passkey-client-capabilities".into(), "01001".into()),
        ("screen_hint".into(), "login_or_signup".into()),
    ];
    if !device_id.is_empty() {
        q.push(("ext-oai-did".into(), device_id.into()));
    }
    if !login_hint.is_empty() {
        q.push(("login_hint".into(), login_hint.into()));
    }
    let qs = serde_urlencoded::to_string(&q)?;
    let url = format!("https://chatgpt.com/api/auth/signin/openai?{}", qs);

    let headers = common_json_headers("https://chatgpt.com/auth/login", "https://chatgpt.com");
    let form = vec![
        ("csrfToken".to_string(), csrf.to_string()),
        ("callbackUrl".to_string(), "https://chatgpt.com/".to_string()),
        ("json".to_string(), "true".to_string()),
    ];
    let resp = client
        .send(Method::POST, &url, &headers, LoginBody::Form(&form))
        .await?;
    if resp.status != 200 {
        return Err(anyhow!("signin/openai fail: HTTP {}", resp.status));
    }
    let v = resp.json()?;
    let auth_url = v.get("url").and_then(|x| x.as_str()).unwrap_or("");
    if auth_url.is_empty() {
        return Err(anyhow!("signin/openai: no url in response — {}", short(&resp.body, 300)));
    }
    if !auth_url.contains("auth.openai.com") {
        return Err(anyhow!(
            "signin/openai trả URL không phải auth.openai.com (CSRF/anti-bot reject): {}",
            short(auth_url, 200)
        ));
    }
    Ok(auth_url.to_string())
}

/// Bootstrap: CSRF + signin + GET authorize. Trả (device_id, landing_url).
async fn bootstrap(
    client: &LoginClient,
    email: &str,
    use_login_hint: bool,
    log: &LogFn,
) -> Result<(String, String)> {
    let did = uuid::Uuid::new_v4().to_string();
    let csrf = step_csrf(client, log).await?;
    let login_hint = if use_login_hint { email } else { "" };
    let auth_url = step_auth_url(client, &csrf, &did, login_hint, log).await?;

    log("[login] [3/9] OAuth init (GET authorize)");
    let headers = nav_headers_html("https://chatgpt.com/", "cross-site");
    let resp = client.get_follow(&auth_url, &headers, 12).await?;
    let landing = resp.final_url;
    let device_id = client.get_cookie("oai-did").unwrap_or(did);
    Ok((device_id, landing))
}

fn detect_flow(landing: &str) -> Option<Flow> {
    if landing.contains("/log-in/password") {
        Some(Flow::Password)
    } else if landing.contains("/email-verification") {
        Some(Flow::Otp)
    } else {
        None
    }
}

async fn authorize_continue(
    client: &LoginClient,
    email: &str,
    sentinel: &str,
    device_id: &str,
    _log: &LogFn,
) -> Result<Value> {
    let base = common_json_headers("https://auth.openai.com/log-in", "https://auth.openai.com");
    let mut h = base.to_vec();
    if !sentinel.is_empty() {
        h.push(("openai-sentinel-token", sentinel));
    }
    if !device_id.is_empty() {
        h.push(("oai-device-id", device_id));
    }
    let payload = json!({"username": {"value": email, "kind": "email"}, "screen_hint": "login"});
    let resp = client
        .send(
            Method::POST,
            "https://auth.openai.com/api/accounts/authorize/continue",
            &h,
            LoginBody::Json(&payload),
        )
        .await?;
    if resp.status != 200 {
        return Err(anyhow!(
            "authorize/continue fail: HTTP {} - {}",
            resp.status,
            short(&resp.body, 300)
        ));
    }
    resp.json()
}

async fn password_verify(
    client: &LoginClient,
    password: &str,
    device_id: &str,
    sentinel: &str,
    log: &LogFn,
) -> Result<Value> {
    log("[login] [4/9] password/verify");
    let base = common_json_headers(
        "https://auth.openai.com/log-in/password",
        "https://auth.openai.com",
    );
    let mut h = base.to_vec();
    if !device_id.is_empty() {
        h.push(("oai-device-id", device_id));
    }
    if !sentinel.is_empty() {
        h.push(("openai-sentinel-token", sentinel));
    }
    let payload = json!({ "password": password });
    let resp = client
        .send(
            Method::POST,
            "https://auth.openai.com/api/accounts/password/verify",
            &h,
            LoginBody::Json(&payload),
        )
        .await?;
    if resp.status != 200 {
        return Err(anyhow!(
            "password verify fail: HTTP {} - {}",
            resp.status,
            short(&resp.body, 300)
        ));
    }
    resp.json()
}

async fn mfa_issue(client: &LoginClient, challenge_id: &str, device_id: &str, log: &LogFn) {
    let base = common_json_headers("https://auth.openai.com/mfa-challenge", "https://auth.openai.com");
    let mut h = base.to_vec();
    if !device_id.is_empty() {
        h.push(("oai-device-id", device_id));
    }
    let payload = json!({"id": challenge_id, "type": "totp", "force_fresh_challenge": false});
    match client
        .send(
            Method::POST,
            "https://auth.openai.com/api/accounts/mfa/issue_challenge",
            &h,
            LoginBody::Json(&payload),
        )
        .await
    {
        Ok(r) if r.status != 200 => log(&format!("[login] issue_challenge HTTP {} (non-fatal)", r.status)),
        Ok(_) => {}
        Err(e) => log(&format!("[login] issue_challenge err {} (non-fatal)", e)),
    }
}

async fn mfa_verify(
    client: &LoginClient,
    challenge_id: &str,
    code: &str,
    device_id: &str,
    log: &LogFn,
) -> Result<Value> {
    log("[login] [5/9] MFA verify (TOTP)");
    let base = common_json_headers("https://auth.openai.com/mfa-challenge", "https://auth.openai.com");
    let mut h = base.to_vec();
    if !device_id.is_empty() {
        h.push(("oai-device-id", device_id));
    }
    let payload = json!({"id": challenge_id, "type": "totp", "code": code});
    let resp = client
        .send(
            Method::POST,
            "https://auth.openai.com/api/accounts/mfa/verify",
            &h,
            LoginBody::Json(&payload),
        )
        .await?;
    if resp.status != 200 {
        return Err(anyhow!(
            "MFA verify fail: HTTP {} - {}",
            resp.status,
            short(&resp.body, 300)
        ));
    }
    resp.json()
}

async fn follow_redirects(
    client: &LoginClient,
    start_url: &str,
    _log: &LogFn,
) -> Result<Option<String>> {
    let mut current = start_url.to_string();
    let headers = nav_headers_html("https://chatgpt.com/", "cross-site");
    for _ in 0..12 {
        if current.contains("/api/auth/callback/openai") && current.contains("code=") {
            return Ok(Some(current));
        }
        let resp = client.send(Method::GET, &current, &headers, LoginBody::None).await?;
        if resp.is_redirect() {
            let Some(loc) = resp.location else { break };
            let loc = absolutize(&current, &loc)?;
            if loc.contains("/api/auth/callback/openai") && loc.contains("code=") {
                return Ok(Some(loc));
            }
            current = loc;
        } else {
            break;
        }
    }
    Ok(None)
}

/// Consume callback hop-by-hop để NextAuth set session-token cookie vào jar.
async fn consume_callback(client: &LoginClient, callback_url: &str, log: &LogFn) -> bool {
    if !callback_url.contains("code=") {
        return false;
    }
    let headers = nav_headers_html("https://auth.openai.com/", "cross-site");
    let mut current = callback_url.to_string();
    for _ in 0..12 {
        let resp = match client.send(Method::GET, &current, &headers, LoginBody::None).await {
            Ok(r) => r,
            Err(e) => {
                log(&format!("[login] consume_callback err: {}", e));
                return client.has_session_token();
            }
        };
        if client.has_session_token() {
            return true;
        }
        if resp.is_redirect() {
            let Some(loc) = resp.location else { break };
            current = match absolutize(&current, &loc) {
                Ok(u) => u,
                Err(_) => break,
            };
            continue;
        }
        break;
    }
    client.has_session_token()
}

async fn consume_callback_verified(client: &LoginClient, callback_url: &str, log: &LogFn) -> bool {
    if !callback_url.contains("code=") {
        return false;
    }
    for attempt in 1..=3u32 {
        let ok = consume_callback(client, callback_url, log).await;
        if client.has_session_token() {
            log(&format!(
                "[login] [6/9] callback verified (attempt {}/3, consumed={})",
                attempt, ok
            ));
            return true;
        }
        if attempt < 3 {
            log(&format!(
                "[login] callback chưa set session cookie (attempt {}/3) → retry 1s",
                attempt
            ));
            sleep_secs(1).await;
        }
    }
    false
}

async fn get_session(client: &LoginClient, log: &LogFn) -> Result<Value> {
    log("[login] [9/9] GET /api/auth/session");
    let headers = common_json_headers("https://chatgpt.com/", "https://chatgpt.com");
    let resp: LoginResponse = client
        .send(Method::GET, "https://chatgpt.com/api/auth/session", &headers, LoginBody::None)
        .await?;
    if resp.status != 200 {
        return Err(anyhow!(
            "/api/auth/session fail: HTTP {} - {}",
            resp.status,
            short(&resp.body, 200)
        ));
    }
    resp.json()
}

/// Build cookie header (chatgpt.com/openai.com) từ jar export — mirror
/// `main::build_cookie_header`.
fn cookie_header_for_chatgpt(cookies: &[Value]) -> String {
    let mut pairs: Vec<String> = Vec::new();
    for c in cookies {
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
    pairs.join("; ")
}

// ─── Public orchestrator ────────────────────────────────────────────────

/// Login HTTP thuần. Trả `LoginSession` (accessToken + cookie_header) hoặc lỗi.
pub async fn login_pure_request(
    email: &str,
    password: &str,
    totp_secret: &str,
    proxy: Option<&str>,
    log: &LogFn,
) -> Result<LoginSession> {
    log(&format!(
        "[login] start — {} proxy={}",
        mask_email(email),
        proxy.map(crate::proxy_format::mask_proxy).unwrap_or_else(|| "direct".into())
    ));

    let mut client = LoginClient::new(proxy)?;

    // Fast path: bootstrap WITH login_hint → server thường redirect thẳng
    // /log-in/password (skip authorize/continue).
    let (mut device_id, landing) = bootstrap(&client, email, true, log).await?;
    log(&format!("[login] landing: {}", short(&landing, 90)));
    let mut flow = detect_flow(&landing);
    #[allow(unused_assignments)]
    let mut page_type = String::new();
    #[allow(unused_assignments)]
    let mut continue_url = String::new();

    // Fallback: landing không xác định → re-bootstrap KHÔNG login_hint (fresh
    // client để state machine clean), rồi authorize/continue.
    if flow.is_none() {
        log("[login] landing không xác định — re-bootstrap không login_hint");
        client = LoginClient::new(proxy)?;
        let (did2, landing2) = bootstrap(&client, email, false, log).await?;
        device_id = did2;
        log(&format!("[login] retry landing: {}", short(&landing2, 90)));
        flow = detect_flow(&landing2);

        if flow.is_none() {
            log("[login] resolve qua authorize/continue (no login_hint)");
            let sentinel = get_sentinel_token(&client, &device_id, "login", log).await;
            let ac = authorize_continue(&client, email, &sentinel, &device_id, log).await?;
            page_type = ac
                .get("page")
                .and_then(|p| p.get("type"))
                .and_then(|t| t.as_str())
                .unwrap_or("")
                .trim()
                .to_string();
            continue_url = ac
                .get("continue_url")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim()
                .to_string();
            if page_type == "login_password" || continue_url.contains("/log-in/password") {
                flow = Some(Flow::Password);
            } else if matches!(page_type.as_str(), "email_otp_verification" | "email_verification")
                || continue_url.contains("/email-verification")
            {
                flow = Some(Flow::Otp);
            }
        }
    }

    let flow = flow.ok_or_else(|| {
        anyhow!("không xác định được login flow (landing + authorize/continue đều không rõ)")
    })?;

    match flow {
        Flow::Password => {
            let sentinel = get_sentinel_token(&client, &device_id, "login", log).await;
            let data = password_verify(&client, password, &device_id, &sentinel, log).await?;
            page_type = data
                .get("page")
                .and_then(|p| p.get("type"))
                .and_then(|t| t.as_str())
                .unwrap_or("")
                .trim()
                .to_string();
            continue_url = data
                .get("continue_url")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim()
                .to_string();
            log(&format!(
                "[login] post-password → page_type={:?} continue={}",
                page_type,
                short(&continue_url, 80)
            ));
        }
        Flow::Otp => {
            return Err(anyhow!(
                "account dùng passwordless OTP (không password) — combo email|pass|2fa \
                 không hỗ trợ flow này vì cần đọc mailbox. Hãy dùng session.json."
            ));
        }
    }

    // MFA (TOTP)
    if page_type.contains("mfa") || continue_url.contains("mfa") {
        let challenge_id = MFA_CHALLENGE_RE
            .captures(&continue_url)
            .and_then(|c| c.get(1))
            .map(|m| m.as_str().to_string())
            .ok_or_else(|| {
                anyhow!("MFA challenge id không thấy trong continue_url: {}", short(&continue_url, 100))
            })?;
        mfa_issue(&client, &challenge_id, &device_id, log).await;
        let code = totp::now_code(totp_secret)
            .map_err(|e| anyhow!("sinh TOTP code fail: {}", e))?;
        let data = mfa_verify(&client, &challenge_id, &code, &device_id, log).await?;
        continue_url = data
            .get("continue_url")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_string();
    }

    // Normalize continue_url
    if continue_url.starts_with('/') {
        continue_url = absolutize("https://auth.openai.com", &continue_url)?;
    }

    // Step 7-8: follow redirects + consume callback
    if !continue_url.is_empty()
        && continue_url.contains("auth.openai.com")
        && !continue_url.contains("code=")
    {
        log("[login] continue_url là auth page → reauthorize lấy callback");
        let csrf2 = step_csrf(&client, log).await?;
        let auth_url2 = step_auth_url(&client, &csrf2, "", "", log).await?;
        if let Some(cb) = follow_redirects(&client, &auth_url2, log).await? {
            consume_callback_verified(&client, &cb, log).await;
        } else {
            log("[login] reauthorize: callback URL không thấy trong redirect chain");
        }
    } else if !continue_url.is_empty() {
        let cb = follow_redirects(&client, &continue_url, log)
            .await?
            .ok_or_else(|| anyhow!("login xong nhưng không tìm thấy callback URL trong redirect chain"))?;
        if !consume_callback_verified(&client, &cb, log).await {
            return Err(anyhow!(
                "callback consumed nhưng session-token cookie KHÔNG set \
                 (code expire / Cloudflare reject set-cookie / proxy strip cookie)"
            ));
        }
    } else {
        log("[login] không có continue_url → thử reauthorize");
        let csrf2 = step_csrf(&client, log).await?;
        let auth_url2 = step_auth_url(&client, &csrf2, "", "", log).await?;
        let headers = nav_headers_html("https://chatgpt.com/", "cross-site");
        let resp = client.send(Method::GET, &auth_url2, &headers, LoginBody::None).await?;
        if let Some(loc) = resp.location {
            let redir = absolutize(&auth_url2, &loc)?;
            if let Some(cb) = follow_redirects(&client, &redir, log).await? {
                consume_callback_verified(&client, &cb, log).await;
            }
        }
    }

    if !client.has_session_token() {
        return Err(anyhow!(
            "login flow xong nhưng cookie __Secure-next-auth.session-token KHÔNG set → \
             /api/auth/session sẽ trả WARNING_BANNER (callback code expire / Cloudflare \
             strip cookie / proxy). Thử proxy khác."
        ));
    }

    let session = get_session(&client, log).await?;
    let access_token = session
        .get("accessToken")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    if access_token.is_empty() {
        return Err(anyhow!(
            "/api/auth/session không có accessToken: {}",
            short(&session.to_string(), 200)
        ));
    }

    let cookies = client.export_cookies();
    let cookie_header = cookie_header_for_chatgpt(&cookies);
    let cookie_count = client.cookie_count();

    let user_email = session
        .get("user")
        .and_then(|u| u.get("email"))
        .and_then(|e| e.as_str())
        .unwrap_or(email);
    log(&format!(
        "[login] ✓ session OK — user={} accessToken_len={} cookies={}",
        mask_email(user_email),
        access_token.len(),
        cookie_count
    ));

    Ok(LoginSession {
        access_token,
        cookie_header,
        cookie_count,
    })
}
