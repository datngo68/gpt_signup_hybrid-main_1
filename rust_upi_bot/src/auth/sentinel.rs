//! OpenAI Sentinel token — port từ `sentinel_pow.py` (FNV-1a PoW fallback).
//!
//! Đây là path PURE-RUST (không cần Node/QuickJS). Đủ cho flow password+TOTP:
//! token pass surface validation (200 OK) ở `password/verify` + `mfa/verify`.
//! (Cảnh báo "OTP silent-drop" trong Python chỉ áp dụng cho email-OTP flow —
//! không liên quan combo email|pass|2fa.)

use crate::auth::client::{LoginBody, LoginClient};
use crate::auth::LogFn;
use base64::Engine;
use chrono::Utc;
use rand::seq::SliceRandom;
use rand::Rng;
use reqwest::Method;
use serde_json::{json, Value};
use std::time::Instant;
use uuid::Uuid;

const SENTINEL_REQ_URL: &str = "https://sentinel.openai.com/backend-api/sentinel/req";
const SENTINEL_REFERER: &str = "https://sentinel.openai.com/backend-api/sentinel/frame.html";
const SENTINEL_SDK_URL: &str = "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js";
const MAX_ATTEMPTS: u64 = 500_000;
const ERROR_PREFIX: &str = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D";
/// UA khớp wreq Emulation::Chrome137 (Windows) — embedded vào sentinel PoW
/// payload để đồng bộ với JA3/UA thực tế trên wire.
const SENTINEL_UA: &str =
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36";

const NAV_PROPS: &[&str] = &[
    "vendorSub", "productSub", "vendor", "maxTouchPoints", "scheduling",
    "userActivation", "doNotTrack", "geolocation", "connection", "plugins",
    "mimeTypes", "pdfViewerEnabled", "webkitTemporaryStorage",
    "webkitPersistentStorage", "hardwareConcurrency", "cookieEnabled",
    "credentials", "mediaDevices", "permissions", "locks", "ink",
];
const CHOICE_12: &[&str] = &["location", "implementation", "URL", "documentURI", "compatMode"];
const CHOICE_13: &[&str] = &["Object", "Function", "Array", "Number", "parseFloat", "undefined"];
const CHOICE_17: &[i64] = &[4, 8, 12, 16];

fn b64_encode(arr: &[Value]) -> String {
    // serde_json compact = separators=(",",":"); giữ unicode (ensure_ascii=False).
    let raw = serde_json::to_string(&Value::Array(arr.to_vec())).unwrap_or_default();
    base64::engine::general_purpose::STANDARD.encode(raw.as_bytes())
}

fn fnv1a_32(text: &str) -> String {
    let mut h: u32 = 2166136261;
    for ch in text.chars() {
        h ^= ch as u32;
        h = h.wrapping_mul(16777619);
    }
    h ^= h >> 16;
    h = h.wrapping_mul(2246822507);
    h ^= h >> 13;
    h = h.wrapping_mul(3266489909);
    h ^= h >> 16;
    format!("{:08x}", h)
}

fn get_config(user_agent: &str) -> Vec<Value> {
    let mut rng = rand::thread_rng();
    let now = Utc::now();
    let date_str = format!(
        "{} GMT+0000 (Coordinated Universal Time)",
        now.format("%a %b %d %Y %H:%M:%S")
    );
    let perf_now: f64 = rng.gen_range(1000.0..50000.0);
    let time_origin: f64 = (now.timestamp_millis() as f64) - perf_now;
    let nav_prop = NAV_PROPS.choose(&mut rng).copied().unwrap_or("vendor");
    let sid = Uuid::new_v4().to_string();

    vec![
        json!("1920x1080"),
        json!(date_str),
        json!(4_294_705_152i64),
        json!(rng.gen::<f64>()), // [3] nonce placeholder
        json!(user_agent),
        json!(SENTINEL_SDK_URL),
        Value::Null,
        Value::Null,
        json!("en-US"),
        json!("en-US,en"), // [9] elapsed-ms placeholder
        json!(rng.gen::<f64>()),
        json!(format!("{}\u{2212}undefined", nav_prop)),
        json!(CHOICE_12.choose(&mut rng).copied().unwrap_or("location")),
        json!(CHOICE_13.choose(&mut rng).copied().unwrap_or("Object")),
        json!(perf_now),
        json!(sid),
        json!(""),
        json!(CHOICE_17.choose(&mut rng).copied().unwrap_or(8)),
        json!(time_origin),
    ]
}

fn solve_pow(seed: &str, difficulty: &str, user_agent: &str) -> String {
    let mut config = get_config(user_agent);
    let start = Instant::now();
    let dlen = difficulty.len();
    for nonce in 0..MAX_ATTEMPTS {
        config[3] = json!(nonce);
        config[9] = json!(start.elapsed().as_millis() as i64);
        let encoded = b64_encode(&config);
        let digest = fnv1a_32(&format!("{}{}", seed, encoded));
        if dlen <= digest.len() && digest[..dlen] <= *difficulty {
            return format!("gAAAAAB{}~S", encoded);
        }
    }
    // Fallback error token (mirror Python): "gAAAAAB" + ERROR_PREFIX + b64('"None"').
    let none_b64 = base64::engine::general_purpose::STANDARD.encode("\"None\"".as_bytes());
    format!("gAAAAAB{}{}", ERROR_PREFIX, none_b64)
}

fn generate_requirements_token(user_agent: &str) -> String {
    let mut config = get_config(user_agent);
    let mut rng = rand::thread_rng();
    config[3] = json!(1);
    config[9] = json!(rng.gen_range(5.0f64..50.0).round() as i64);
    format!("gAAAAAC{}", b64_encode(&config))
}

async fn fetch_challenge(
    client: &LoginClient,
    device_id: &str,
    flow: &str,
    request_p: &str,
    log: &LogFn,
) -> Option<Value> {
    let body = json!({"p": request_p, "id": device_id, "flow": flow});
    let payload = serde_json::to_string(&body).ok()?;
    let headers = [
        ("Accept", "*/*"),
        ("Referer", SENTINEL_REFERER),
        ("Origin", "https://sentinel.openai.com"),
        ("Sec-Fetch-Dest", "empty"),
        ("Sec-Fetch-Mode", "cors"),
        ("Sec-Fetch-Site", "same-origin"),
    ];
    match client
        .send(
            Method::POST,
            SENTINEL_REQ_URL,
            &headers,
            LoginBody::Text {
                body: payload,
                content_type: "text/plain;charset=UTF-8",
            },
        )
        .await
    {
        Ok(resp) if resp.status == 200 => resp.json().ok(),
        Ok(resp) => {
            log(&format!("[sentinel] /req HTTP {}", resp.status));
            None
        }
        Err(e) => {
            log(&format!("[sentinel] /req error: {}", e));
            None
        }
    }
}

/// Build sentinel token via pure-Rust PoW. Luôn trả String (không raise).
pub async fn get_sentinel_token(
    client: &LoginClient,
    device_id: &str,
    flow: &str,
    log: &LogFn,
) -> String {
    let did = if device_id.is_empty() {
        Uuid::new_v4().to_string()
    } else {
        device_id.to_string()
    };
    let req_p = generate_requirements_token(SENTINEL_UA);

    let challenge = fetch_challenge(client, &did, flow, &req_p, log).await;
    let Some(challenge) = challenge else {
        log("[sentinel] challenge fetch failed → fallback token");
        return serde_json::to_string(
            &json!({"p": req_p, "t": "", "c": "", "id": did, "flow": flow}),
        )
        .unwrap_or_default();
    };

    let c_value = challenge
        .get("token")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    let pow = challenge.get("proofofwork").cloned().unwrap_or(Value::Null);
    let required = pow.get("required").and_then(|v| v.as_bool()).unwrap_or(false);
    let seed = pow.get("seed").and_then(|v| v.as_str()).unwrap_or("");

    let p_value = if required && !seed.is_empty() {
        let difficulty = pow.get("difficulty").and_then(|v| v.as_str()).unwrap_or("0");
        solve_pow(seed, difficulty, SENTINEL_UA)
    } else {
        req_p
    };

    let token = serde_json::to_string(
        &json!({"p": p_value, "t": "", "c": c_value, "id": did, "flow": flow}),
    )
    .unwrap_or_default();
    log(&format!("[sentinel] token built (PoW, len={})", token.len()));
    token
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fnv1a_known() {
        // FNV-1a 32-bit của "" với post-mixing (như Python _fnv1a_32).
        // Chỉ kiểm tra deterministic + 8 hex digits.
        let d = fnv1a_32("abc");
        assert_eq!(d.len(), 8);
        assert!(d.chars().all(|c| c.is_ascii_hexdigit()));
        assert_eq!(fnv1a_32("abc"), fnv1a_32("abc"));
    }

    #[test]
    fn requirements_token_prefix() {
        let t = generate_requirements_token(SENTINEL_UA);
        assert!(t.starts_with("gAAAAAC"));
    }
}
