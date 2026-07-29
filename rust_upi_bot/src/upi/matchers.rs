//! QR/UPI URI matchers — port từ `upi_runner.py::_find_matches` family.
//!
//! Quét recursive Stripe responses (init/elements/confirm/approve/refresh)
//! tìm:
//!   - `upi://...` URI (text in any value)
//!   - QR image URL (Stripe `image_url_png`/`image_url_svg`)
//!   - `expires_at` từ `qr_code` object trong `next_action`.

use serde_json::Value;

pub const MATCH_TERMS: &[&str] = &[
    "qr", "upi", "intent", "collect", "vpa", "next_action",
    "hosted_instructions", "image_url", "display_qr",
];

pub const SENSITIVE_TERMS: &[&str] = &[
    "access", "authorization", "client_secret", "cookie", "key",
    "password", "secret", "token",
];

#[derive(Debug, Clone)]
pub struct Match {
    #[allow(dead_code)]
    pub source: String,
    pub path: String,
    #[allow(dead_code)]
    pub kind: String, // "key" | "value"
    pub value: Value,
}

pub fn find_matches(value: &Value, source: &str) -> Vec<Match> {
    let mut out = Vec::new();
    walk(value, source, "$", &mut out);
    out
}

fn walk(value: &Value, source: &str, path: &str, out: &mut Vec<Match>) {
    match value {
        Value::Object(map) => {
            for (key, child) in map {
                let child_path = format!("{}.{}", path, key);
                let key_lower = key.to_lowercase();
                if MATCH_TERMS.iter().any(|t| key_lower.contains(t)) {
                    out.push(Match {
                        source: source.to_string(),
                        path: child_path.clone(),
                        kind: "key".into(),
                        value: redact_if_sensitive(child, &child_path),
                    });
                }
                walk(child, source, &child_path, out);
            }
        }
        Value::Array(arr) => {
            for (i, child) in arr.iter().enumerate() {
                let child_path = format!("{}[{}]", path, i);
                walk(child, source, &child_path, out);
            }
        }
        Value::String(s) => {
            let lower = s.to_lowercase();
            if MATCH_TERMS.iter().any(|t| lower.contains(t)) {
                out.push(Match {
                    source: source.to_string(),
                    path: path.to_string(),
                    kind: "value".into(),
                    value: redact_if_sensitive(value, path),
                });
            }
        }
        _ => {}
    }
}

fn redact_if_sensitive(value: &Value, path: &str) -> Value {
    let lower = path.to_lowercase();
    if SENSITIVE_TERMS.iter().any(|t| lower.contains(t)) {
        return Value::String("[redacted]".into());
    }
    value.clone()
}

pub fn find_upi_uri(matches: &[Match]) -> Option<String> {
    for m in matches {
        if let Value::String(s) = &m.value {
            if s.to_lowercase().starts_with("upi://") {
                return Some(s.clone());
            }
        }
    }
    None
}

pub fn find_qr_image_url(matches: &[Match]) -> Option<String> {
    for m in matches {
        if let Value::String(s) = &m.value {
            let path_lower = m.path.to_lowercase();
            if s.starts_with("https://")
                && path_lower.contains("qr")
                && (s.ends_with(".png")
                    || s.ends_with(".svg")
                    || s.to_lowercase().contains("qr"))
            {
                return Some(s.clone());
            }
        }
    }
    None
}

/// Tìm `hosted_instructions_url` (`https://payments.stripe.com/upi/instructions/...`)
/// — port từ Python `_find_hosted_instructions_url`. Đây là link thanh toán
/// UPI hosted (trang QR + nút mở app). Bắt 2 cách:
///   1. key `hosted_instructions_url` → value là URL string trực tiếp.
///   2. key `next_action` → value là dict, đào nested
///      `upi_handle_redirect_or_display_qr_code.hosted_instructions_url`.
pub fn find_hosted_instructions_url(matches: &[Match]) -> Option<String> {
    const PREFIX: &str = "https://payments.stripe.com/upi/instructions/";
    // (1) direct string value.
    for m in matches {
        if let Value::String(s) = &m.value {
            if s.starts_with(PREFIX) {
                return Some(s.clone());
            }
        }
    }
    // (2) nested dict.
    for m in matches {
        if let Value::Object(obj) = &m.value {
            if let Some(block) = obj
                .get("upi_handle_redirect_or_display_qr_code")
                .and_then(|v| v.as_object())
            {
                if let Some(url) = block.get("hosted_instructions_url").and_then(|v| v.as_str()) {
                    if url.starts_with(PREFIX) {
                        return Some(url.to_string());
                    }
                }
            }
            if let Some(url) = obj.get("hosted_instructions_url").and_then(|v| v.as_str()) {
                if url.starts_with(PREFIX) {
                    return Some(url.to_string());
                }
            }
        }
    }
    None
}

pub fn find_qr_expires_at(matches: &[Match]) -> Option<i64> {
    for m in matches {
        if let Value::Object(obj) = &m.value {
            if let Some(exp) = obj.get("expires_at").and_then(|v| v.as_i64()) {
                if exp > 0
                    && (obj.contains_key("image_url_png") || obj.contains_key("image_url_svg"))
                {
                    return Some(exp);
                }
            }
        }
    }
    None
}

/// Parse Stripe hosted-instructions HTML — extract base64 payload từ
/// `<meta id="payload" data-message="...">` → JSON với key `mobile_auth_url`.
pub fn extract_hosted_upi_uri(html: &str) -> Option<String> {
    use regex::Regex;
    let re = Regex::new(
        r#"(?i)<meta\s+[^>]*id\s*=\s*["']payload["'][^>]*data-message\s*=\s*["']([^"']+)["']"#,
    )
    .ok()?;
    let caps = re.captures(html)?;
    let raw = &caps[1];
    let pad = (4 - (raw.len() % 4)) % 4;
    let padded = format!("{}{}", raw, "=".repeat(pad));
    use base64::{engine::general_purpose::URL_SAFE, Engine};
    let bytes = URL_SAFE.decode(padded.as_bytes()).ok()?;
    let txt = String::from_utf8(bytes).ok()?;
    let v: Value = serde_json::from_str(&txt).ok()?;
    let uri = v.get("mobile_auth_url")?.as_str()?;
    if uri.starts_with("upi:") {
        Some(uri.to_string())
    } else {
        None
    }
}
