//! Proxy live probe — port từ `web/proxy_health.py::probe_proxy`.
//!
//! Probe nhẹ L4 connectivity qua proxy bằng GET `https://api64.ipify.org`
//! (echo IP dạng plain text) → verify proxy thực sự reachable. KHÔNG đại diện
//! TLS path login thật của ChatGPT, chỉ confirm proxy còn sống và auth ok.
//!
//! Phân loại reason giống Python:
//!   - `Ok` — 2xx, body chứa IP (text plain)
//!   - `Auth` — 407 / DNS resolve fail / proxy auth fail (line hỏng cả)
//!   - `Ip` — timeout/reset/refused/tunnel (IP-level, có thể rotate SID)
//!   - `BadFormat` — line không parse được

use crate::proxy_format::materialize_for_client;
use anyhow::Result;
use once_cell::sync::Lazy;
use regex::Regex;
use reqwest::{Client, Proxy};
use std::time::{Duration, Instant};

/// Default endpoint + timeout — đồng bộ Python `_DEFAULT_KNOBS`.
pub const PROBE_ENDPOINT: &str = "https://api64.ipify.org";
pub const PROBE_TIMEOUT_SECS: u64 = 6;

#[derive(Debug, Clone)]
pub enum ProbeReason {
    Ok,
    Auth,
    Ip,
    BadFormat,
}

#[derive(Debug, Clone)]
pub struct ProbeResult {
    pub ok: bool,
    pub reason: ProbeReason,
    /// IP echo từ endpoint khi `ok=true`, error message khi `ok=false`.
    pub detail: String,
    pub latency_ms: u64,
    /// Concrete URL đã probe (đã materialize SID nếu có) — để log mask.
    #[allow(dead_code)]
    pub probed_url: Option<String>,
}

impl ProbeResult {
    fn err(reason: ProbeReason, detail: impl Into<String>) -> Self {
        Self {
            ok: false,
            reason,
            detail: detail.into(),
            latency_ms: 0,
            probed_url: None,
        }
    }
}

/// Phân loại reqwest error → reason. Conservative:
///   - DNS fail → `Auth` (giết line)
///   - Status 401/403/407 hoặc keyword `proxy authentication` / `auth fail` /
///     `unauthorized` / `forbidden` → `Auth` (provider chối credential).
///     `auth fail` cụ thể bắt provider non-standard như bestproxy.com trả
///     `HTTP 612 OK` body `auth fail bp-... ...`.
///   - Còn lại (timeout/reset/refused/tunnel với mã không phổ biến) → `Ip`
///     (rotate SID có thể giúp).
///
/// Match `\b(401|403|407)\b` qua regex để tránh false-positive với số trùng
/// trong domain/UUID (vd `port=4011`).
static AUTH_STATUS_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"\b(401|403|407)\b").unwrap());

fn classify(err: &reqwest::Error) -> ProbeReason {
    let s = err.to_string();
    let lower = s.to_lowercase();
    if lower.contains("could not resolve")
        || lower.contains("name or service not known")
        || lower.contains("dns error")
    {
        return ProbeReason::Auth;
    }
    if AUTH_STATUS_RE.is_match(&s)
        || lower.contains("proxy authentication")
        || lower.contains("auth fail")
        || lower.contains("unauthorized")
        || lower.contains("forbidden")
    {
        return ProbeReason::Auth;
    }
    // Log full error string → cho phép decode pattern lạ về sau (provider mới
    // dùng status hoặc keyword chưa cover ở đây).
    tracing::debug!(error = %s, "proxy probe error classified as IP");
    ProbeReason::Ip
}

/// Probe 1 raw proxy line. Materialize SID nội bộ (nếu line có template) →
/// build short-lived reqwest client với proxy → GET endpoint.
pub async fn probe_proxy_line(raw_line: &str) -> ProbeResult {
    let url = match materialize_for_client(raw_line, 8) {
        Ok(u) => u,
        Err(e) => return ProbeResult::err(ProbeReason::BadFormat, e.to_string()),
    };

    let proxy = match Proxy::all(&url) {
        Ok(p) => p,
        Err(e) => return ProbeResult::err(ProbeReason::BadFormat, format!("proxy build: {}", e)),
    };

    let client_res = Client::builder()
        .proxy(proxy)
        .timeout(Duration::from_secs(PROBE_TIMEOUT_SECS))
        // Force HTTP/1.1 — đồng bộ HttpClient main; tránh H2 stream stall.
        .http1_only()
        .build();
    let client = match client_res {
        Ok(c) => c,
        Err(e) => return ProbeResult::err(ProbeReason::BadFormat, format!("client build: {}", e)),
    };

    let started = Instant::now();
    let result: Result<reqwest::Response, reqwest::Error> = client.get(PROBE_ENDPOINT).send().await;
    let latency_ms = started.elapsed().as_millis() as u64;

    match result {
        Ok(resp) => {
            let status = resp.status();
            if status.is_success() {
                let body = resp.text().await.unwrap_or_default();
                let ip = body.trim().to_string();
                ProbeResult {
                    ok: true,
                    reason: ProbeReason::Ok,
                    detail: ip,
                    latency_ms,
                    probed_url: Some(url),
                }
            } else if matches!(status.as_u16(), 401 | 403 | 407) {
                // Auth fail chuẩn HTTP — provider trả 401/403/407 trực tiếp.
                ProbeResult {
                    ok: false,
                    reason: ProbeReason::Auth,
                    detail: format!("HTTP {}", status),
                    latency_ms,
                    probed_url: Some(url),
                }
            } else {
                ProbeResult {
                    ok: false,
                    reason: ProbeReason::Ip,
                    detail: format!("HTTP {}", status),
                    latency_ms,
                    probed_url: Some(url),
                }
            }
        }
        Err(e) => ProbeResult {
            ok: false,
            reason: classify(&e),
            detail: e.to_string(),
            latency_ms,
            probed_url: Some(url),
        },
    }
}
